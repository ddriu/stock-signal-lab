"""Persistencia local del diario de operaciones en SQLite."""

from __future__ import annotations

import sqlite3
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from src.alerts import (
    ALERT_STATE_COLUMNS,
    AlertPreferences,
    AlertState,
    normalize_alert_preferences,
    preferences_from_mapping,
)
from src.favorite_tags import serialize_favorite_tags


OPERATION_COLUMNS = [
    "id",
    "ticker",
    "side",
    "quantity",
    "price",
    "fees",
    "executed_at",
    "notes",
    "currency",
    "recorded_by",
    "created_at",
]

FAVORITE_COLUMNS = [
    "id",
    "ticker",
    "name",
    "exchange",
    "tags",
    "recorded_by",
    "created_at",
]
ANALYSIS_SNAPSHOT_COLUMNS = [
    "id",
    "ticker",
    "analyzed_at",
    "price",
    "opportunity_score",
    "company_score",
    "entry_score",
    "valuation_score",
    "relative_score",
    "risk_score",
    "opportunity_label",
    "entry_label",
    "position_label",
    "expected_return_pct",
    "positive_rate_pct",
    "expected_price",
    "horizon_days",
    "sector",
    "explanation",
    "note",
    "created_at",
]
PRIVATE_INVESTMENT_COLUMNS = [
    "id",
    "platform",
    "project_name",
    "invested_amount",
    "current_value",
    "expected_return_pct",
    "start_date",
    "maturity_date",
    "status",
    "notes",
    "recorded_by",
    "created_at",
]
PRIVATE_INVESTMENT_PLATFORMS = ("Civislend", "Segofactoring")
PRIVATE_INVESTMENT_STATUSES = ("Activa", "Finalizada", "Retrasada", "Impagada")
MAX_FAVORITES = 300


def default_database_path() -> Path:
    """Usa el proyecto en desarrollo y una carpeta privada al estar instalado."""

    configured = os.environ.get("STOCK_SIGNAL_LAB_DATA_DIR")
    if configured:
        return Path(configured).expanduser() / "trading_journal.db"
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
            return base / "StockSignalLab" / "trading_journal.db"
        return Path.home() / ".stock_signal_lab" / "trading_journal.db"
    return Path(__file__).resolve().parents[1] / "data" / "trading_journal.db"


DEFAULT_DATABASE = default_database_path()


def normalize_operation(
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    fees: float,
    executed_at: date | datetime | str,
    notes: str = "",
    currency: str = "EUR",
) -> dict[str, object]:
    """Valida una operación y devuelve valores normalizados para cualquier backend."""

    if side not in {"Compra", "Venta"}:
        raise ValueError("El tipo debe ser Compra o Venta.")
    if quantity <= 0 or price <= 0 or fees < 0:
        raise ValueError("Cantidad/precio deben ser positivos y las comisiones no negativas.")
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3:
        raise ValueError("La moneda debe tener tres letras, por ejemplo EUR o USD.")
    return {
        "ticker": ticker.strip().upper(),
        "side": side,
        "quantity": float(quantity),
        "price": float(price),
        "fees": float(fees),
        "executed_at": pd.Timestamp(executed_at).isoformat(),
        "notes": notes.strip(),
        "currency": normalized_currency,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_favorite(
    ticker: str,
    name: str = "",
    exchange: str = "",
    tags: object = "",
) -> dict[str, str]:
    """Valida una empresa favorita para cualquier backend de almacenamiento."""

    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("La empresa favorita necesita un ticker.")
    return {
        "ticker": normalized_ticker,
        "name": name.strip() or normalized_ticker,
        "exchange": exchange.strip(),
        "tags": serialize_favorite_tags(tags),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_analysis_snapshot(
    *,
    ticker: str,
    analyzed_at: date | datetime | str,
    price: float,
    opportunity_score: int,
    company_score: int | None,
    entry_score: int,
    valuation_score: int | None,
    relative_score: int | None,
    risk_score: int | None,
    opportunity_label: str,
    entry_label: str,
    position_label: str,
    expected_return_pct: float | None = None,
    positive_rate_pct: float | None = None,
    expected_price: float | None = None,
    horizon_days: int | None = None,
    sector: str = "",
    explanation: str = "",
    note: str = "",
) -> dict[str, object]:
    """Valida una fotografía resumida del análisis para cualquier backend."""

    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("El análisis necesita un ticker.")
    if price <= 0:
        raise ValueError("El precio analizado debe ser positivo.")

    def checked_score(value: int | None, name: str) -> int | None:
        if value is None:
            return None
        normalized = int(value)
        if not 0 <= normalized <= 100:
            raise ValueError(f"La nota de {name} debe estar entre 0 y 100.")
        return normalized

    normalized_expected_price = (
        float(expected_price) if expected_price is not None else None
    )
    if normalized_expected_price is not None and normalized_expected_price <= 0:
        raise ValueError("El precio esperado debe ser positivo.")
    normalized_horizon = int(horizon_days) if horizon_days is not None else None
    if normalized_horizon is not None and normalized_horizon <= 0:
        raise ValueError("El horizonte del análisis debe ser positivo.")
    normalized_positive_rate = (
        float(positive_rate_pct) if positive_rate_pct is not None else None
    )
    if normalized_positive_rate is not None and not 0 <= normalized_positive_rate <= 100:
        raise ValueError("El porcentaje de casos positivos debe estar entre 0 y 100.")

    return {
        "ticker": normalized_ticker,
        "analyzed_at": pd.Timestamp(analyzed_at).isoformat(),
        "price": float(price),
        "opportunity_score": checked_score(opportunity_score, "oportunidad"),
        "company_score": checked_score(company_score, "empresa"),
        "entry_score": checked_score(entry_score, "entrada"),
        "valuation_score": checked_score(valuation_score, "valoración"),
        "relative_score": checked_score(relative_score, "fortaleza relativa"),
        "risk_score": checked_score(risk_score, "riesgo"),
        "opportunity_label": opportunity_label.strip(),
        "entry_label": entry_label.strip(),
        "position_label": position_label.strip(),
        "expected_return_pct": (
            float(expected_return_pct) if expected_return_pct is not None else None
        ),
        "positive_rate_pct": normalized_positive_rate,
        "expected_price": normalized_expected_price,
        "horizon_days": normalized_horizon,
        "sector": sector.strip(),
        "explanation": explanation.strip(),
        "note": note.strip()[:1_000],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_private_investment(
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
) -> dict[str, object]:
    """Valida una inversión manual de Civislend o Segofactoring."""

    normalized_platform = platform.strip()
    if normalized_platform not in PRIVATE_INVESTMENT_PLATFORMS:
        raise ValueError("La plataforma debe ser Civislend o Segofactoring.")
    normalized_project = project_name.strip()
    if not normalized_project:
        raise ValueError("Indica el nombre o referencia del proyecto.")
    if invested_amount <= 0:
        raise ValueError("El importe invertido debe ser positivo.")
    if current_value < 0:
        raise ValueError("El valor actual no puede ser negativo.")
    if not -100 <= expected_return_pct <= 1_000:
        raise ValueError("La rentabilidad esperada debe estar entre -100% y 1.000%.")
    if status not in PRIVATE_INVESTMENT_STATUSES:
        raise ValueError("El estado de la inversión no es válido.")

    normalized_start = pd.Timestamp(start_date)
    normalized_maturity = (
        pd.Timestamp(maturity_date) if maturity_date not in (None, "") else None
    )
    if normalized_maturity is not None and normalized_maturity < normalized_start:
        raise ValueError("El vencimiento no puede ser anterior a la inversión.")
    return {
        "platform": normalized_platform,
        "project_name": normalized_project[:200],
        "invested_amount": float(invested_amount),
        "current_value": float(current_value),
        "expected_return_pct": float(expected_return_pct),
        "start_date": normalized_start.isoformat(),
        "maturity_date": (
            normalized_maturity.isoformat() if normalized_maturity is not None else None
        ),
        "status": status,
        "notes": notes.strip()[:1_000],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def calculate_position_states(
    operations: pd.DataFrame,
    *,
    include_closed: bool = True,
) -> pd.DataFrame:
    """Reconstruye el estado de cada posición, incluidas las ya cerradas."""

    columns = [
        "ticker",
        "currency",
        "quantity",
        "average_cost",
        "cost_basis",
        "realized_pnl",
        "paid_fees",
    ]
    if operations.empty:
        return pd.DataFrame(columns=columns)

    operations = operations.sort_values(["executed_at", "id"])
    states: dict[tuple[str, str], dict[str, float | str]] = {}
    for operation in operations.itertuples(index=False):
        currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
        key = (str(operation.ticker), currency)
        state = states.setdefault(
            key,
            {
                "ticker": key[0],
                "currency": key[1],
                "quantity": 0.0,
                "cost_basis": 0.0,
                "realized_pnl": 0.0,
                "paid_fees": 0.0,
            },
        )
        quantity = float(operation.quantity)
        price = float(operation.price)
        fee = float(operation.fees)
        state["paid_fees"] = float(state["paid_fees"]) + fee
        if operation.side == "Compra":
            state["quantity"] = float(state["quantity"]) + quantity
            state["cost_basis"] = float(state["cost_basis"]) + quantity * price + fee
            continue

        available = float(state["quantity"])
        sold = min(quantity, available)
        if sold <= 0:
            continue
        average_cost = float(state["cost_basis"]) / available
        allocated_fee = fee * (sold / quantity)
        proceeds = sold * price - allocated_fee
        removed_cost = average_cost * sold
        state["realized_pnl"] = float(state["realized_pnl"]) + proceeds - removed_cost
        state["quantity"] = available - sold
        state["cost_basis"] = max(0.0, float(state["cost_basis"]) - removed_cost)

    rows: list[dict[str, float | str]] = []
    for state in states.values():
        quantity = float(state["quantity"])
        if not include_closed and quantity <= 1e-9:
            continue
        cost_basis = float(state["cost_basis"])
        rows.append(
            {
                **state,
                "average_cost": cost_basis / quantity if quantity > 1e-9 else 0.0,
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["ticker", "currency"], ignore_index=True
    )


def calculate_open_positions(operations: pd.DataFrame) -> pd.DataFrame:
    """Reconstruye únicamente las posiciones todavía abiertas."""

    return calculate_position_states(operations, include_closed=False)


class TradingJournal:
    backend_name = "SQLite local"

    def __init__(
        self,
        database_path: str | Path = DEFAULT_DATABASE,
        *,
        owner: str = "",
    ) -> None:
        self.database_path = Path(database_path)
        self.owner = owner.strip().lower() or self.database_path.stem
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('Compra', 'Venta')),
                    quantity REAL NOT NULL CHECK (quantity > 0),
                    price REAL NOT NULL CHECK (price > 0),
                    fees REAL NOT NULL DEFAULT 0 CHECK (fees >= 0),
                    executed_at TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(operations)").fetchall()
            }
            if "currency" not in columns:
                connection.execute(
                    "ALTER TABLE operations ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'"
                )
            if "recorded_by" not in columns:
                connection.execute(
                    "ALTER TABLE operations ADD COLUMN recorded_by TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    exchange TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            favorite_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(favorites)").fetchall()
            }
            if "tags" not in favorite_columns:
                connection.execute(
                    "ALTER TABLE favorites ADD COLUMN tags TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    analyzed_at TEXT NOT NULL,
                    price REAL NOT NULL CHECK (price > 0),
                    opportunity_score INTEGER NOT NULL
                        CHECK (opportunity_score BETWEEN 0 AND 100),
                    company_score INTEGER,
                    entry_score INTEGER NOT NULL
                        CHECK (entry_score BETWEEN 0 AND 100),
                    valuation_score INTEGER,
                    relative_score INTEGER,
                    risk_score INTEGER,
                    opportunity_label TEXT NOT NULL DEFAULT '',
                    entry_label TEXT NOT NULL DEFAULT '',
                    position_label TEXT NOT NULL DEFAULT '',
                    expected_return_pct REAL,
                    positive_rate_pct REAL,
                    expected_price REAL,
                    horizon_days INTEGER,
                    sector TEXT NOT NULL DEFAULT '',
                    explanation TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS analysis_snapshots_ticker_date_idx
                ON analysis_snapshots (ticker, analyzed_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS email_alert_preferences (
                    owner TEXT PRIMARY KEY,
                    email TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    alert_buy INTEGER NOT NULL DEFAULT 1,
                    alert_reduce INTEGER NOT NULL DEFAULT 1,
                    alert_sell INTEGER NOT NULL DEFAULT 1,
                    include_group INTEGER NOT NULL DEFAULT 1,
                    minimum_buy_score INTEGER NOT NULL DEFAULT 65
                        CHECK (minimum_buy_score BETWEEN 55 AND 100),
                    only_changes INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS email_alert_states (
                    owner TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    entry_score INTEGER NOT NULL,
                    entry_label TEXT NOT NULL DEFAULT '',
                    position_label TEXT NOT NULL DEFAULT '',
                    price REAL NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    notified_at TEXT,
                    PRIMARY KEY (owner, ticker)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS private_investments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL
                        CHECK (platform IN ('Civislend', 'Segofactoring')),
                    project_name TEXT NOT NULL,
                    invested_amount REAL NOT NULL CHECK (invested_amount > 0),
                    current_value REAL NOT NULL CHECK (current_value >= 0),
                    expected_return_pct REAL NOT NULL DEFAULT 0,
                    start_date TEXT NOT NULL,
                    maturity_date TEXT,
                    status TEXT NOT NULL DEFAULT 'Activa'
                        CHECK (status IN ('Activa', 'Finalizada', 'Retrasada', 'Impagada')),
                    notes TEXT NOT NULL DEFAULT '',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
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
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operations
                    (
                        ticker, side, quantity, price, fees, executed_at, notes,
                        currency, recorded_by, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation["ticker"],
                    operation["side"],
                    operation["quantity"],
                    operation["price"],
                    operation["fees"],
                    operation["executed_at"],
                    operation["notes"],
                    operation["currency"],
                    recorded_by.strip().lower(),
                    operation["created_at"],
                ),
            )
            return int(cursor.lastrowid)

    def list_operations(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                "SELECT * FROM operations ORDER BY executed_at DESC, id DESC", connection
            )

    def delete_operation(self, operation_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM operations WHERE id = ?", (int(operation_id),))

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
        columns = [column for column in PRIVATE_INVESTMENT_COLUMNS if column != "id"]
        values = {**investment, "recorded_by": recorded_by.strip().lower()}
        with self._connect() as connection:
            cursor = connection.execute(
                f"INSERT INTO private_investments ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            return int(cursor.lastrowid)

    def list_private_investments(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT * FROM private_investments
                ORDER BY start_date DESC, id DESC
                """,
                connection,
            )

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
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE private_investments
                SET current_value = ?, status = ?, notes = ?
                WHERE id = ?
                """,
                (float(current_value), status, notes.strip()[:1_000], int(investment_id)),
            )
            if cursor.rowcount == 0:
                raise ValueError("La inversión privada no existe.")

    def delete_private_investment(self, investment_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM private_investments WHERE id = ?",
                (int(investment_id),),
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
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM favorites WHERE ticker = ?",
                (favorite["ticker"],),
            ).fetchone()
            if existing:
                if favorite["tags"]:
                    connection.execute(
                        "UPDATE favorites SET tags = ? WHERE ticker = ?",
                        (favorite["tags"], favorite["ticker"]),
                    )
                return int(existing["id"])
            total = int(
                connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
            )
            if total >= MAX_FAVORITES:
                raise ValueError(f"Cada lista admite hasta {MAX_FAVORITES} favoritos.")
            cursor = connection.execute(
                """
                INSERT INTO favorites
                    (ticker, name, exchange, tags, recorded_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    favorite["ticker"],
                    favorite["name"],
                    favorite["exchange"],
                    favorite["tags"],
                    recorded_by.strip().lower(),
                    favorite["created_at"],
                ),
            )
            return int(cursor.lastrowid)

    def list_favorites(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT id, ticker, name, exchange, tags, recorded_by, created_at
                FROM favorites
                ORDER BY name COLLATE NOCASE, ticker
                """,
                connection,
            )

    def update_favorite_tags(self, ticker: str, tags: object) -> None:
        normalized_ticker = ticker.strip().upper()
        normalized_tags = serialize_favorite_tags(tags)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE favorites SET tags = ? WHERE ticker = ?",
                (normalized_tags, normalized_ticker),
            )
            if cursor.rowcount == 0:
                raise ValueError("La empresa favorita no existe.")

    def delete_favorite(self, ticker: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM favorites WHERE ticker = ?",
                (ticker.strip().upper(),),
            )

    def add_analysis_snapshot(self, **values: object) -> int:
        """Guarda una fotografía ligera de una señal y sus notas."""

        snapshot = normalize_analysis_snapshot(**values)  # type: ignore[arg-type]
        columns = [column for column in ANALYSIS_SNAPSHOT_COLUMNS if column != "id"]
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO analysis_snapshots ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                tuple(snapshot[column] for column in columns),
            )
            return int(cursor.lastrowid)

    def list_analysis_snapshots(self, ticker: str | None = None) -> pd.DataFrame:
        """Devuelve el seguimiento más reciente, opcionalmente de una empresa."""

        query = "SELECT * FROM analysis_snapshots"
        parameters: tuple[object, ...] = ()
        if ticker:
            query += " WHERE ticker = ?"
            parameters = (ticker.strip().upper(),)
        query += " ORDER BY analyzed_at DESC, id DESC"
        with self._connect() as connection:
            return pd.read_sql_query(query, connection, params=parameters)

    def delete_analysis_snapshot(self, snapshot_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM analysis_snapshots WHERE id = ?",
                (int(snapshot_id),),
            )

    def get_alert_preferences(self) -> AlertPreferences:
        """Devuelve valores seguros aunque el usuario aún no los haya guardado."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM email_alert_preferences WHERE owner = ?",
                (self.owner,),
            ).fetchone()
        if row is None:
            return normalize_alert_preferences(owner=self.owner)
        return preferences_from_mapping(dict(row), owner=self.owner)

    def save_alert_preferences(
        self,
        preferences: AlertPreferences,
    ) -> None:
        values = normalize_alert_preferences(**preferences.__dict__)
        if values.owner != self.owner:
            raise ValueError("No se pueden modificar las alertas de otro usuario.")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO email_alert_preferences (
                    owner, email, enabled, alert_buy, alert_reduce, alert_sell,
                    include_group, minimum_buy_score, only_changes, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner) DO UPDATE SET
                    email = excluded.email,
                    enabled = excluded.enabled,
                    alert_buy = excluded.alert_buy,
                    alert_reduce = excluded.alert_reduce,
                    alert_sell = excluded.alert_sell,
                    include_group = excluded.include_group,
                    minimum_buy_score = excluded.minimum_buy_score,
                    only_changes = excluded.only_changes,
                    updated_at = excluded.updated_at
                """,
                (
                    values.owner,
                    values.email,
                    int(values.enabled),
                    int(values.alert_buy),
                    int(values.alert_reduce),
                    int(values.alert_sell),
                    int(values.include_group),
                    values.minimum_buy_score,
                    int(values.only_changes),
                    values.updated_at,
                ),
            )

    def list_alert_states(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT owner, ticker, signature, entry_score, entry_label,
                       position_label, price, evaluated_at, notified_at
                FROM email_alert_states
                ORDER BY ticker
                """,
                connection,
            )

    def upsert_alert_states(self, states: list[AlertState]) -> None:
        if not states:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO email_alert_states (
                    owner, ticker, signature, entry_score, entry_label,
                    position_label, price, evaluated_at, notified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner, ticker) DO UPDATE SET
                    signature = excluded.signature,
                    entry_score = excluded.entry_score,
                    entry_label = excluded.entry_label,
                    position_label = excluded.position_label,
                    price = excluded.price,
                    evaluated_at = excluded.evaluated_at,
                    notified_at = COALESCE(
                        excluded.notified_at,
                        email_alert_states.notified_at
                    )
                """,
                [
                    tuple(getattr(state, column) for column in ALERT_STATE_COLUMNS)
                    for state in states
                ],
            )

    def open_positions(self) -> pd.DataFrame:
        """Reconstruye posiciones mediante coste medio, incluidas comisiones pagadas."""

        return calculate_open_positions(self.list_operations())

    def portfolio_summary(self) -> pd.DataFrame:
        """Compatibilidad: devuelve ahora las posiciones abiertas calculadas."""

        return self.open_positions()
