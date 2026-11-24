"""Archive an explicitly requested saved-data report, never quotes or deliveries.

The email schedule is not a data-availability clock. A late job can archive the
latest completed US close without widening SMTP/Arena's execution windows.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import exchange_calendars
import pandas as pd

from src.daily_digest import (
    _calendar_name, _sessions as _daily_sessions,
    build_daily_score_report, seal_daily_score_report,
)
from src.instruments import resolve_analysis_ticker
from src.journal import validate_radar_checkpoint_owner
from src.portfolio_snapshot import latest_portfolio_snapshot
from src.storage import GROUP_PORTFOLIO_OWNER
from src.supabase_journal import JournalStorageError

SAVED_CLOSE_REPORT_VERSION = "saved-close-report-v1"
_SNAPSHOT_TICKER_BATCH_SIZE = 25


@dataclass(frozen=True)
class SavedCloseReportResult:
    status: str
    session: str
    report_id: str = ""
    authorized: bool = False
    error_count: int = 0
    eligible_count: int = 0
    valid_count: int = 0
    row_count: int = 0
    unchanged: bool = False


def _clock(reference_time=None) -> pd.Timestamp:
    clock = pd.Timestamp.now(tz="UTC") if reference_time is None else pd.Timestamp(reference_time)
    if pd.isna(clock) or clock.tzinfo is None:
        raise ValueError("El archivo de cierre necesita una hora con zona horaria.")
    return clock.tz_convert("UTC")


def latest_completed_us_session(*, reference_time=None) -> str:
    """Use official exchange closes, including early closes and holidays."""
    clock = _clock(reference_time)
    calendar = exchange_calendars.get_calendar("XNYS")
    session = calendar.date_to_session(pd.Timestamp(clock.date()), direction="previous")
    if calendar.session_close(session) > clock:
        session = calendar.previous_session(session)
    return session.date().isoformat()


def _sessions(session: str, universe: Iterable[str], clock: pd.Timestamp) -> tuple[str, ...]:
    calendar = exchange_calendars.get_calendar("XNYS")
    day = pd.Timestamp(session)
    result = set()
    for _ in range(5):
        result.add(day.date().isoformat())
        day = calendar.previous_session(day)
    # A US holiday is not necessarily a holiday for the foreign favorites.
    # Reuse the report's own calendar contract, then page the union in <=5 dates.
    seen_calendars = {"XNYS"}
    for ticker in universe:
        name = _calendar_name(ticker)
        if name not in seen_calendars:
            days, _ = _daily_sessions(ticker, session, clock)
            result.update(days)
            seen_calendars.add(name)
    return tuple(sorted(result, reverse=True))


def _symbols(values: Iterable[object]) -> set[str]:
    return {resolve_analysis_ticker(str(value).strip()) for value in values
            if isinstance(value, str) and value.strip()}


def build_saved_close_report(
    snapshots: pd.DataFrame, *, favorite_tickers: Iterable[str],
    held_tickers: Iterable[str] = (), company_names: Mapping[str, object] | None = None,
    reference_time=None,
) -> dict[str, object]:
    """Pure saved-input reconstruction: missing inputs stay N/D and blocked."""
    clock = _clock(reference_time)
    session = latest_completed_us_session(reference_time=clock)
    favorites, held = _symbols(favorite_tickers), _symbols(held_tickers)
    if not favorites | held:
        raise ValueError("No hay favoritas ni posiciones para archivar una revisión.")
    report = build_daily_score_report(
        snapshots, tickers=sorted(favorites | held), held_tickers=held,
        company_names=company_names, favorite_tickers=sorted(favorites),
        as_of=session, reference_time=clock,
    )
    # This archive's target is explicitly the completed US close. Foreign and
    # unknown-only universes may have an older/no display anchor; keep every row
    # and its real session unchanged, not a fabricated US observation.
    if report.get("as_of") is not None and report["as_of"] > session:
        raise ValueError("El informe contiene un cierre posterior al objetivo.")
    status = "verified" if report["arena_quality"].get("authorized") is True else "blocked"
    return seal_daily_score_report({**report, "as_of": session, "review_mode": "saved_close_archive",
        "saved_close_report": {"version": SAVED_CLOSE_REPORT_VERSION,
            "source": "persisted_analysis_snapshots", "target_calendar": "XNYS",
            "session": session, "status": status,
            "mail_attempted": False, "arena_activated": False, "operations_validated": False}})


def _owner_frame(frame, owner: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise JournalStorageError("El archivo necesita filas guardadas con formato verificable.")
    if "owner" in frame and not frame["owner"].eq(owner).fillna(False).all():
        raise JournalStorageError("El archivo contiene datos de otro propietario.")
    return frame


def archive_saved_close_report(journal, *, reference_time=None) -> SavedCloseReportResult:
    """Read only this journal's universe; append its exact immutable report.

    No preference enumeration, provider download, alert claim, SMTP, simulation,
    portfolio write or Arena request is reachable from this service.
    """
    owner = validate_radar_checkpoint_owner(getattr(journal, "owner", None))
    if owner.lower() in {"anon", "anonymous", "public", GROUP_PORTFOLIO_OWNER}:
        raise ValueError("El archivo necesita un propietario privado explícito.")
    clock = _clock(reference_time)
    session = latest_completed_us_session(reference_time=clock)
    favorites_frame = _owner_frame(journal.list_favorites(), owner)
    if not favorites_frame.empty and ("ticker" not in favorites_frame
            or any(not isinstance(value, str) or not value.strip() for value in favorites_frame["ticker"])):
        raise JournalStorageError("Las favoritas contienen un símbolo no verificable.")
    favorites = _symbols(favorites_frame.get("ticker", []))
    names = {str(row["ticker"]): row.get("name") or row["ticker"]
             for row in favorites_frame.to_dict("records") if isinstance(row.get("ticker"), str)}
    positions = _owner_frame(journal.open_positions(), owner)
    held = _symbols(positions.get("ticker", []))
    snapshots = _owner_frame(journal.list_portfolio_snapshot_positions(), owner)
    if not snapshots.empty:
        latest, _ = latest_portfolio_snapshot(snapshots)
        held |= _symbols(latest.get("analysis_ticker", []))
        for row in latest.to_dict("records"):
            if isinstance(row.get("analysis_ticker"), str):
                names.setdefault(row["analysis_ticker"], row.get("asset_name") or row["analysis_ticker"])
    universe = sorted(favorites | held)
    if not universe:
        return SavedCloseReportResult(status="empty", session=session)
    pages = []
    sessions = _sessions(session, universe, clock)
    for date_offset in range(0, len(sessions), 5):
        for offset in range(0, len(universe), _SNAPSHOT_TICKER_BATCH_SIZE):
            page = _owner_frame(journal.list_analysis_snapshots_for_sessions(
                sessions[date_offset:date_offset + 5],
                tickers=tuple(universe[offset:offset + _SNAPSHOT_TICKER_BATCH_SIZE])), owner)
            if page.attrs.get("possibly_truncated"):
                raise JournalStorageError("El archivo de cierre no pudo verificar todo el historial acotado.")
            pages.append(page)
    report = build_saved_close_report(pd.concat(pages, ignore_index=True),
        favorite_tickers=favorites, held_tickers=held, company_names=names, reference_time=clock)
    identifier = report["report_id"]
    previous = journal.get_daily_score_report(identifier)
    if previous is not None and previous != report:
        raise JournalStorageError("El informe archivado no conserva su versión exacta.")
    if previous is None and journal.save_daily_score_report(report) != identifier:
        raise JournalStorageError("La identidad del informe archivado no coincide.")
    quality = report["arena_quality"]
    return SavedCloseReportResult(status=report["saved_close_report"]["status"],
        session=session, report_id=identifier, authorized=quality.get("authorized") is True,
        error_count=len(quality.get("errors", [])), eligible_count=quality.get("eligible_count", 0),
        valid_count=quality.get("valid_count", 0), row_count=len(report["rows"]),
        unchanged=previous is not None)
