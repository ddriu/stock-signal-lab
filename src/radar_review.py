"""Durable, owner-scoped state machine for an explicitly requested Radar review.

The journal supplies atomic compare-and-swap. Page reruns only schedule work;
neither a browser nor its session state owns the queue or the outcomes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
import re
from typing import Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.navigation import merge_analysis_ticker_sources


_MADRID = ZoneInfo("Europe/Madrid")
_LEASE = timedelta(minutes=5)
_TICKER = re.compile(r"[A-Z0-9_^.=:/-]{1,64}\Z")
_TOKEN = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
OUTCOMES = frozenset({"complete", "partial", "no_data", "stale", "not_saved"})
FUNDAMENTALS = frozenset({"complete", "partial", "error", "pending"})
BATCH_SIZE = 6


def _utc(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("La revisión necesita una hora con zona horaria.")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class RadarOutcome:
    status: str
    fundamentals: str = "pending"
    price_date: str | None = None
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in OUTCOMES or self.fundamentals not in FUNDAMENTALS:
            raise ValueError("Resultado de revisión no válido.")
        if self.price_date is not None:
            if date.fromisoformat(self.price_date).isoformat() != self.price_date:
                raise ValueError("Fecha de precio no válida.")
        if self.snapshot_id is not None and not _HASH.fullmatch(self.snapshot_id):
            raise ValueError("Identificador de análisis no válido.")
        if self.status in {"complete", "partial"} and not self.price_date:
            raise ValueError("Una lectura guardada necesita la fecha del cierre.")
        if self.status == "complete" and self.fundamentals != "complete":
            raise ValueError("Una lectura completa necesita fundamentales completos.")
        if self.snapshot_id is not None and self.status not in {"complete", "partial"}:
            raise ValueError("No se puede acreditar un resultado no guardado.")


@dataclass(frozen=True)
class RadarReview:
    revision: int
    run_id: str
    started_at: datetime
    context: str
    tickers: tuple[str, ...]
    outcomes: Mapping[str, RadarOutcome]
    worker: str | None = None
    lease_until: datetime | None = None

    @classmethod
    def start(cls, tickers, context: str, now: datetime, *, revision: int = 1):
        normalized = tuple(merge_analysis_ticker_sources(tickers))
        return cls(revision, uuid4().hex, _utc(now), context, normalized, {})

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision <= 0:
            raise ValueError("Versión de revisión inválida.")
        if not _TOKEN.fullmatch(self.run_id) or not _HASH.fullmatch(self.context):
            raise ValueError("Identidad de revisión inválida.")
        _utc(self.started_at)
        if (
            not 1 <= len(self.tickers) <= 1000
            or len(set(self.tickers)) != len(self.tickers)
            or any(not _TICKER.fullmatch(ticker) for ticker in self.tickers)
            or not set(self.outcomes).issubset(self.tickers)
            or any(not isinstance(value, RadarOutcome) for value in self.outcomes.values())
        ):
            raise ValueError("Universo de revisión inválido.")
        if (self.worker is None) != (self.lease_until is None):
            raise ValueError("Reserva de revisión inválida.")
        if self.worker is not None:
            if not _TOKEN.fullmatch(self.worker):
                raise ValueError("Identidad de ejecución inválida.")
            _utc(self.lease_until)

    @classmethod
    def from_checkpoint(cls, payload: dict | None) -> RadarReview | None:
        if payload is None:
            return None
        try:
            if payload["version"] != 1 or not isinstance(payload["tickers"], list):
                raise ValueError("Formato de revisión no compatible.")
            return cls(
                payload["revision"], payload["run_id"], _utc(payload["started_at"]),
                payload["context"], tuple(payload["tickers"]),
                {ticker: RadarOutcome(**value) for ticker, value in payload["outcomes"].items()},
                payload.get("worker"), _utc(payload["lease_until"]) if payload.get("lease_until") else None,
            )
        except (TypeError, ValueError, AttributeError, KeyError, OverflowError) as exc:
            raise ValueError("No se pudo validar el progreso guardado del Radar.") from exc

    def checkpoint(self) -> dict:
        return {
            "version": 1, "revision": self.revision, "run_id": self.run_id,
            "started_at": self.started_at.isoformat(), "context": self.context,
            "tickers": list(self.tickers),
            "outcomes": {ticker: asdict(value) for ticker, value in self.outcomes.items()},
            "worker": self.worker, "lease_until": self.lease_until.isoformat() if self.lease_until else None,
        }

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(ticker for ticker in self.tickers if ticker not in self.outcomes)

    @property
    def retryable(self) -> tuple[str, ...]:
        return tuple(ticker for ticker, result in self.outcomes.items() if result.status != "complete")

    def compatible(self, context: str, now: datetime) -> bool:
        return self.context == context and (
            self.started_at.astimezone(_MADRID).date() == _utc(now).astimezone(_MADRID).date()
        )

    def busy(self, now: datetime, worker: str | None = None) -> bool:
        return bool(self.worker != worker and self.lease_until and self.lease_until > _utc(now))

    def claim(self, worker: str, now: datetime) -> RadarReview:
        if not self.pending or self.busy(now, worker):
            raise ValueError("No se puede reservar este bloque de revisión.")
        return replace(self, revision=self.revision + 1, worker=worker, lease_until=_utc(now) + _LEASE)

    def release(self, outcomes: Mapping[str, RadarOutcome] | None = None) -> RadarReview:
        if self.worker is None:
            raise ValueError("No hay ningún bloque reservado.")
        results = dict(outcomes or {})
        if results and tuple(results) != self.pending[:BATCH_SIZE]:
            raise ValueError("El resultado no corresponde al bloque reservado.")
        return replace(
            self, revision=self.revision + 1,
            outcomes={**self.outcomes, **results}, worker=None, lease_until=None,
        )

    def retry(self, now: datetime) -> RadarReview:
        """Requeue failed results only on explicit request, never in an auto-loop."""
        if self.busy(now) or not self.retryable:
            raise ValueError("No se puede reintentar esta revisión ahora.")
        return replace(
            self, revision=self.revision + 1, worker=None, lease_until=None,
            outcomes={ticker: result for ticker, result in self.outcomes.items() if result.status == "complete"},
        )

    @property
    def saved_readings(self) -> dict[str, tuple[str, str]]:
        return {
            ticker: (result.price_date, result.snapshot_id)
            for ticker, result in self.outcomes.items()
            if result.price_date and result.snapshot_id
        }
