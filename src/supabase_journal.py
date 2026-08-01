"""Diario persistente alojado en Supabase mediante su API REST."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd
import requests

from src.alerts import (
    ALERT_PREFERENCE_COLUMNS,
    ALERT_STATE_COLUMNS,
    AlertPreferences,
    AlertState,
    normalize_alert_preferences,
    preferences_from_mapping,
)
from src.journal import (
    ANALYSIS_SNAPSHOT_COLUMNS,
    FAVORITE_COLUMNS,
    MAX_FAVORITES,
    OPERATION_COLUMNS,
    PRIVATE_INVESTMENT_COLUMNS,
    PRIVATE_INVESTMENT_STATUSES,
    PORTFOLIO_ACCOUNT_COLUMNS,
    PORTFOLIO_SNAPSHOT_COLUMNS,
    calculate_open_positions,
    normalize_analysis_snapshot,
    normalize_favorite,
    normalize_operation,
    normalize_private_investment,
    normalize_portfolio_account,
    normalize_portfolio_snapshot_position,
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
        analysis_table: str = "analysis_snapshots",
        private_investments_table: str = "private_investments",
        portfolio_accounts_table: str = "portfolio_accounts",
        portfolio_snapshots_table: str = "portfolio_snapshots",
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
        if not analysis_table.replace("_", "").isalnum():
            raise JournalStorageError("El nombre de tabla de análisis no es válido.")
        if not private_investments_table.replace("_", "").isalnum():
            raise JournalStorageError("El nombre de tabla de inversión privada no es válido.")
        if not portfolio_accounts_table.replace("_", "").isalnum():
            raise JournalStorageError("El nombre de tabla de cuentas no es válido.")
        if not portfolio_snapshots_table.replace("_", "").isalnum():
            raise JournalStorageError("El nombre de tabla de fotografías no es válido.")
        self.url = normalized_url
        self.secret_key = secret_key.strip()
        self.owner = owner.strip()
        self.table = table
        self.favorites_table = favorites_table
        self.analysis_table = analysis_table
        self.private_investments_table = private_investments_table
        self.portfolio_accounts_table = portfolio_accounts_table
        self.portfolio_snapshots_table = portfolio_snapshots_table
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.table}"

    @property
    def favorites_endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.favorites_table}"

    @property
    def analysis_endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.analysis_table}"

    @property
    def private_investments_endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.private_investments_table}"

    @property
    def portfolio_accounts_endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.portfolio_accounts_table}"

    @property
    def portfolio_snapshots_endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.portfolio_snapshots_table}"

    @property
    def alert_preferences_endpoint(self) -> str:
        return f"{self.url}/rest/v1/email_alert_preferences"

    @property
    def alert_states_endpoint(self) -> str:
        return f"{self.url}/rest/v1/email_alert_states"

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

    def add_private_investment(
        self,
        *,
        platform: str,
        project_name: str,
        invested_amount: float,
        current_value: float,
        expected_return_pct: float,
        start_date: date | datetime | str,
        maturity_date: date | datetime | str | None = None,
        status: str = "Activa",
        notes: str = "",
        recorded_by: str = "",
    ) -> int:
        investment = normalize_private_investment(
            platform=platform,
            project_name=project_name,
            invested_amount=invested_amount,
            current_value=current_value,
            expected_return_pct=expected_return_pct,
            start_date=start_date,
            maturity_date=maturity_date,
            status=status,
            notes=notes,
        )
        response = self._request(
            "POST",
            endpoint=self.private_investments_endpoint,
            json={
                "owner": self.owner,
                **investment,
                "recorded_by": recorded_by.strip().lower(),
            },
            headers={**self.headers, "Prefer": "return=representation"},
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows or "id" not in rows[0]:
            raise JournalStorageError(
                "Supabase guardó una inversión privada con respuesta inesperada."
            )
        return int(rows[0]["id"])

    def list_private_investments(self) -> pd.DataFrame:
        response = self._request(
            "GET",
            endpoint=self.private_investments_endpoint,
            params={
                "owner": f"eq.{self.owner}",
                "select": ",".join(PRIVATE_INVESTMENT_COLUMNS),
                "order": "start_date.desc,id.desc",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió inversiones privadas inválidas.")
        if not rows:
            return pd.DataFrame(columns=PRIVATE_INVESTMENT_COLUMNS)
        frame = pd.DataFrame(rows)
        for column in PRIVATE_INVESTMENT_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        return frame.loc[:, PRIVATE_INVESTMENT_COLUMNS]

    def update_private_investment(
        self,
        investment_id: int,
        *,
        current_value: float,
        status: str,
        notes: str,
    ) -> None:
        if current_value < 0:
            raise ValueError("El valor actual no puede ser negativo.")
        if status not in PRIVATE_INVESTMENT_STATUSES:
            raise ValueError("El estado de la inversión no es válido.")
        self._request(
            "PATCH",
            endpoint=self.private_investments_endpoint,
            params={"id": f"eq.{int(investment_id)}", "owner": f"eq.{self.owner}"},
            json={
                "current_value": float(current_value),
                "status": status,
                "notes": notes.strip()[:1_000],
            },
        )

    def delete_private_investment(self, investment_id: int) -> None:
        self._request(
            "DELETE",
            endpoint=self.private_investments_endpoint,
            params={"id": f"eq.{int(investment_id)}", "owner": f"eq.{self.owner}"},
        )

    def upsert_portfolio_account(
        self,
        *,
        account_name: str,
        account_type: str,
        investments_value: float = 0.0,
        cash_balance: float = 0.0,
        currency: str = "EUR",
        status: str = "Pendiente de actualizar",
        notes: str = "",
    ) -> int:
        account = normalize_portfolio_account(
            account_name=account_name,
            account_type=account_type,
            investments_value=investments_value,
            cash_balance=cash_balance,
            currency=currency,
            status=status,
            notes=notes,
        )
        response = self._request(
            "POST",
            endpoint=self.portfolio_accounts_endpoint,
            params={"on_conflict": "owner,account_name"},
            json={"owner": self.owner, **account},
            headers={
                **self.headers,
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows or "id" not in rows[0]:
            raise JournalStorageError("Supabase guardó una cuenta con respuesta inesperada.")
        return int(rows[0]["id"])

    def list_portfolio_accounts(self) -> pd.DataFrame:
        response = self._request(
            "GET",
            endpoint=self.portfolio_accounts_endpoint,
            params={
                "owner": f"eq.{self.owner}",
                "select": ",".join(PORTFOLIO_ACCOUNT_COLUMNS),
                "order": "account_name.asc",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió una lista de cuentas inválida.")
        if not rows:
            return pd.DataFrame(columns=PORTFOLIO_ACCOUNT_COLUMNS)
        frame = pd.DataFrame(rows)
        for column in PORTFOLIO_ACCOUNT_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        order = {
            "MyInvestor": 1,
            "Trade Republic": 2,
            "Revolut": 3,
            "Segofactoring": 4,
            "Civislend": 5,
        }
        frame["_order"] = frame["account_name"].map(order).fillna(6)
        return (
            frame.sort_values(["_order", "account_name"])
            .drop(columns="_order")
            .loc[:, PORTFOLIO_ACCOUNT_COLUMNS]
            .reset_index(drop=True)
        )

    def upsert_portfolio_snapshot_positions(
        self,
        positions: pd.DataFrame,
        *,
        recorded_by: str = "",
    ) -> int:
        payload: list[dict[str, object]] = []
        for row in positions.itertuples(index=False):
            normalized = normalize_portfolio_snapshot_position(
                **{
                    column: getattr(row, column)
                    for column in PORTFOLIO_SNAPSHOT_COLUMNS
                    if column
                    not in {"id", "recorded_by", "created_at", "updated_at"}
                }
            )
            payload.append(
                {
                    "owner": self.owner,
                    **normalized,
                    "recorded_by": recorded_by.strip().lower(),
                }
            )
        if not payload:
            return 0
        self._request(
            "POST",
            endpoint=self.portfolio_snapshots_endpoint,
            params={"on_conflict": "owner,snapshot_date,platform,asset_name"},
            json=payload,
            headers={
                **self.headers,
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
        return len(payload)

    def list_portfolio_snapshot_positions(self) -> pd.DataFrame:
        response = self._request(
            "GET",
            endpoint=self.portfolio_snapshots_endpoint,
            params={
                "owner": f"eq.{self.owner}",
                "select": ",".join(PORTFOLIO_SNAPSHOT_COLUMNS),
                "order": "snapshot_date.desc,platform.asc,value_eur.desc,asset_name.asc",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió fotografías de cartera inválidas.")
        if not rows:
            return pd.DataFrame(columns=PORTFOLIO_SNAPSHOT_COLUMNS)
        frame = pd.DataFrame(rows)
        for column in PORTFOLIO_SNAPSHOT_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        return frame.loc[:, PORTFOLIO_SNAPSHOT_COLUMNS]

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

    def add_analysis_snapshot(self, **values: object) -> int:
        snapshot = normalize_analysis_snapshot(**values)  # type: ignore[arg-type]
        response = self._request(
            "POST",
            endpoint=self.analysis_endpoint,
            json={"owner": self.owner, **snapshot},
            headers={
                **self.headers,
                "Prefer": "return=representation",
            },
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows or "id" not in rows[0]:
            raise JournalStorageError(
                "Supabase guardó un análisis con una respuesta inesperada."
            )
        return int(rows[0]["id"])

    def list_analysis_snapshots(self, ticker: str | None = None) -> pd.DataFrame:
        params = {
            "owner": f"eq.{self.owner}",
            "select": ",".join(ANALYSIS_SNAPSHOT_COLUMNS),
            "order": "analyzed_at.desc,id.desc",
        }
        if ticker:
            params["ticker"] = f"eq.{ticker.strip().upper()}"
        response = self._request(
            "GET",
            endpoint=self.analysis_endpoint,
            params=params,
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió un historial de análisis inválido.")
        if not rows:
            return pd.DataFrame(columns=ANALYSIS_SNAPSHOT_COLUMNS)
        frame = pd.DataFrame(rows)
        for column in ANALYSIS_SNAPSHOT_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        return frame.loc[:, ANALYSIS_SNAPSHOT_COLUMNS]

    def delete_analysis_snapshot(self, snapshot_id: int) -> None:
        self._request(
            "DELETE",
            endpoint=self.analysis_endpoint,
            params={
                "id": f"eq.{int(snapshot_id)}",
                "owner": f"eq.{self.owner}",
            },
        )

    def get_alert_preferences(self) -> AlertPreferences:
        response = self._request(
            "GET",
            endpoint=self.alert_preferences_endpoint,
            params={
                "owner": f"eq.{self.owner}",
                "select": ",".join(ALERT_PREFERENCE_COLUMNS),
                "limit": "1",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió preferencias de alertas inválidas.")
        return preferences_from_mapping(
            rows[0] if rows else None,
            owner=self.owner,
        )

    def save_alert_preferences(self, preferences: AlertPreferences) -> None:
        values = normalize_alert_preferences(**preferences.__dict__)
        if values.owner != self.owner.strip().lower():
            raise ValueError("No se pueden modificar las alertas de otro usuario.")
        self._request(
            "POST",
            endpoint=self.alert_preferences_endpoint,
            params={"on_conflict": "owner"},
            json=values.__dict__,
            headers={
                **self.headers,
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )

    def list_enabled_alert_preferences(self) -> list[AlertPreferences]:
        """Uso exclusivo del proceso servidor que prepara todos los resúmenes."""

        response = self._request(
            "GET",
            endpoint=self.alert_preferences_endpoint,
            params={
                "enabled": "eq.true",
                "select": ",".join(ALERT_PREFERENCE_COLUMNS),
                "order": "owner.asc",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió preferencias de alertas inválidas.")
        return [
            preferences_from_mapping(row, owner=str(row.get("owner") or ""))
            for row in rows
            if isinstance(row, dict) and row.get("owner")
        ]

    def list_alert_states(self) -> pd.DataFrame:
        response = self._request(
            "GET",
            endpoint=self.alert_states_endpoint,
            params={
                "owner": f"eq.{self.owner}",
                "select": ",".join(ALERT_STATE_COLUMNS),
                "order": "ticker.asc",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise JournalStorageError("Supabase devolvió estados de alertas inválidos.")
        if not rows:
            return pd.DataFrame(columns=ALERT_STATE_COLUMNS)
        frame = pd.DataFrame(rows)
        for column in ALERT_STATE_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        return frame.loc[:, ALERT_STATE_COLUMNS]

    def upsert_alert_states(self, states: list[AlertState]) -> None:
        if not states:
            return
        for state in states:
            if state.owner != self.owner.strip().lower():
                raise ValueError("No se pueden modificar estados de otro usuario.")
        self._request(
            "POST",
            endpoint=self.alert_states_endpoint,
            params={"on_conflict": "owner,ticker"},
            json=[state.__dict__ for state in states],
            headers={
                **self.headers,
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )

    def open_positions(self) -> pd.DataFrame:
        return calculate_open_positions(self.list_operations())

    def portfolio_summary(self) -> pd.DataFrame:
        return self.open_positions()
