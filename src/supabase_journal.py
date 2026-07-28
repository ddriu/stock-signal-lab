"""Diario persistente alojado en Supabase mediante su API REST."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd
import requests

from src.journal import (
    FAVORITE_COLUMNS,
    MAX_FAVORITES,
    OPERATION_COLUMNS,
    calculate_open_positions,
    normalize_favorite,
    normalize_operation,
)


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
        favorites_table: str = "favorites",
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
        if not favorites_table.replace("_", "").isalnum():
            raise JournalStorageError("El nombre de tabla de favoritos no es válido.")
        self.url = normalized_url
        self.secret_key = secret_key.strip()
        self.owner = owner.strip()
        self.table = table
        self.favorites_table = favorites_table
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.table}"

    @property
    def favorites_endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.favorites_table}"

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

    def _request(
        self,
        method: str,
        *,
        endpoint: str | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        headers = kwargs.pop("headers", self.headers)
        try:
            response = requests.request(
                method,
                endpoint or self.endpoint,
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

    def add_favorite(
        self,
        ticker: str,
        name: str = "",
        exchange: str = "",
        tags: object = "",
        recorded_by: str = "",
    ) -> int:
        favorite = normalize_favorite(ticker, name, exchange, tags)
        current = self.list_favorites()
        match = current.loc[current["ticker"] == favorite["ticker"]]
        if not match.empty:
            if favorite["tags"]:
                self.update_favorite_tags(favorite["ticker"], favorite["tags"])
            return int(match.iloc[0]["id"])
        if len(current) >= MAX_FAVORITES:
            raise ValueError(f"Cada lista admite hasta {MAX_FAVORITES} favoritos.")
        response = self._request(
            "POST",
            endpoint=self.favorites_endpoint,
            params={"on_conflict": "owner,ticker"},
            json={
                "owner": self.owner,
                **favorite,
                "recorded_by": recorded_by.strip().lower(),
            },
            headers={
                **self.headers,
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows or "id" not in rows[0]:
            raise JournalStorageError("Supabase guardó un favorito con respuesta inesperada.")
        return int(rows[0]["id"])

    def list_favorites(self) -> pd.DataFrame:
        response = self._request(
            "GET",
            endpoint=self.favorites_endpoint,
            params={
                "owner": f"eq.{self.owner}",
                "select": ",".join(FAVORITE_COLUMNS),
                "order": "name.asc,ticker.asc",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió una lista de favoritos inválida.")
        if not rows:
            return pd.DataFrame(columns=FAVORITE_COLUMNS)
        frame = pd.DataFrame(rows)
        for column in FAVORITE_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        return frame.loc[:, FAVORITE_COLUMNS]

    def update_favorite_tags(self, ticker: str, tags: object) -> None:
        favorite = normalize_favorite(ticker, tags=tags)
        self._request(
            "PATCH",
            endpoint=self.favorites_endpoint,
            params={
                "ticker": f"eq.{favorite['ticker']}",
                "owner": f"eq.{self.owner}",
            },
            json={"tags": favorite["tags"]},
        )

    def delete_favorite(self, ticker: str) -> None:
        self._request(
            "DELETE",
            endpoint=self.favorites_endpoint,
            params={
                "ticker": f"eq.{ticker.strip().upper()}",
                "owner": f"eq.{self.owner}",
            },
        )

    def open_positions(self) -> pd.DataFrame:
        return calculate_open_positions(self.list_operations())

    def portfolio_summary(self) -> pd.DataFrame:
        return self.open_positions()
