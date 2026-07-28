"""Diario persistente alojado en Supabase mediante su API REST."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd
import requests

from src.journal import OPERATION_COLUMNS, calculate_open_positions, normalize_operation


class JournalStorageError(RuntimeError):
    """Error recuperable al leer o escribir el diario remoto."""


class SupabaseTradingJournal:
    """Implementa el mismo contrato del diario SQLite sobre PostgREST."""

    backend_name = "Supabase (persistente)"

    def __init__(
        self,
        url: str,
        secret_key: str,
        owner: str,
        *,
        table: str = "operations",
        timeout: float = 20.0,
    ) -> None:
        normalized_url = url.strip().rstrip("/")
        if not normalized_url.startswith("https://"):
            raise JournalStorageError("La URL de Supabase debe comenzar por https://.")
        if not secret_key.strip():
            raise JournalStorageError("Falta la clave secreta de Supabase.")
        if not owner.strip():
            raise JournalStorageError("Falta el usuario propietario del diario.")
        if not table.replace("_", "").isalnum():
            raise JournalStorageError("El nombre de tabla de Supabase no es válido.")
        self.url = normalized_url
        self.secret_key = secret_key.strip()
        self.owner = owner.strip()
        self.table = table
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.table}"

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "apikey": self.secret_key,
            "Content-Type": "application/json",
        }
        # Las claves legacy son JWT. Las nuevas sb_secret_* se envían sólo
        # mediante apikey, tal como recomienda Supabase.
        if not self.secret_key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.secret_key}"
        return headers

    def _request(self, method: str, **kwargs: Any) -> requests.Response:
        headers = kwargs.pop("headers", self.headers)
        try:
            response = requests.request(
                method,
                self.endpoint,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise JournalStorageError(
                f"Supabase no pudo completar la operación{suffix}. "
                "Revisa la tabla y los secretos del despliegue."
            ) from exc

    def healthcheck(self) -> None:
        self._request(
            "GET",
            params={
                "owner": f"eq.{self.owner}",
                "select": "id",
                "limit": "1",
            },
        )

    def add_operation(
        self,
        ticker: str,
        side: str,
        quantity: float,
        price: float,
        fees: float,
        executed_at: date | datetime | str,
        notes: str = "",
        currency: str = "EUR",
        recorded_by: str = "",
    ) -> int:
        operation = normalize_operation(
            ticker,
            side,
            quantity,
            price,
            fees,
            executed_at,
            notes,
            currency,
        )
        if side == "Venta":
            positions = self.open_positions()
            available = positions.loc[
                (positions["ticker"] == operation["ticker"])
                & (positions["currency"] == operation["currency"]),
                "quantity",
            ]
            if available.empty or float(available.iloc[0]) + 1e-9 < quantity:
                raise ValueError("La venta supera la cantidad registrada en cartera.")
        payload = {
            "owner": self.owner,
            **operation,
            "recorded_by": recorded_by.strip().lower(),
        }
        response = self._request(
            "POST",
            json=payload,
            headers={
                **self.headers,
                "Prefer": "return=representation",
            },
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows or "id" not in rows[0]:
            raise JournalStorageError("Supabase guardó una respuesta inesperada.")
        return int(rows[0]["id"])

    def list_operations(self) -> pd.DataFrame:
        response = self._request(
            "GET",
            params={
                "owner": f"eq.{self.owner}",
                "select": ",".join(OPERATION_COLUMNS),
                "order": "executed_at.desc,id.desc",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió un histórico inválido.")
        if not rows:
            return pd.DataFrame(columns=OPERATION_COLUMNS)
        frame = pd.DataFrame(rows)
        for column in OPERATION_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        return frame.loc[:, OPERATION_COLUMNS]

    def delete_operation(self, operation_id: int) -> None:
        self._request(
            "DELETE",
            params={
                "id": f"eq.{int(operation_id)}",
                "owner": f"eq.{self.owner}",
            },
        )

    def open_positions(self) -> pd.DataFrame:
        return calculate_open_positions(self.list_operations())

    def portfolio_summary(self) -> pd.DataFrame:
        return self.open_positions()
