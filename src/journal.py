"""Persistencia local del diario de operaciones en SQLite."""

from __future__ import annotations

import sqlite3
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

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

    def __init__(self, database_path: str | Path = DEFAULT_DATABASE) -> None:
        self.database_path = Path(database_path)
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

    def open_positions(self) -> pd.DataFrame:
        """Reconstruye posiciones mediante coste medio, incluidas comisiones pagadas."""

        return calculate_open_positions(self.list_operations())

    def portfolio_summary(self) -> pd.DataFrame:
        """Compatibilidad: devuelve ahora las posiciones abiertas calculadas."""

        return self.open_positions()
