"""Persistencia local del diario de operaciones en SQLite."""

from __future__ import annotations

import sqlite3
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd


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


class TradingJournal:
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
    ) -> int:
        if side not in {"Compra", "Venta"}:
            raise ValueError("El tipo debe ser Compra o Venta.")
        if quantity <= 0 or price <= 0 or fees < 0:
            raise ValueError("Cantidad/precio deben ser positivos y las comisiones no negativas.")
        normalized_currency = currency.strip().upper()
        if len(normalized_currency) != 3:
            raise ValueError("La moneda debe tener tres letras, por ejemplo EUR o USD.")
        if side == "Venta":
            positions = self.open_positions()
            available = positions.loc[
                (positions["ticker"] == ticker.strip().upper())
                & (positions["currency"] == normalized_currency),
                "quantity",
            ]
            if available.empty or float(available.iloc[0]) + 1e-9 < quantity:
                raise ValueError("La venta supera la cantidad registrada en cartera.")
        timestamp = pd.Timestamp(executed_at).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operations
                    (ticker, side, quantity, price, fees, executed_at, notes, currency, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker.strip().upper(),
                    side,
                    float(quantity),
                    float(price),
                    float(fees),
                    timestamp,
                    notes.strip(),
                    normalized_currency,
                    datetime.now().isoformat(timespec="seconds"),
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

    def open_positions(self) -> pd.DataFrame:
        """Reconstruye posiciones mediante coste medio, incluidas comisiones pagadas."""

        operations = self.list_operations()
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
            if quantity <= 1e-9:
                continue
            cost_basis = float(state["cost_basis"])
            rows.append(
                {
                    **state,
                    "average_cost": cost_basis / quantity,
                }
            )
        return pd.DataFrame(rows, columns=columns).sort_values(
            ["ticker", "currency"], ignore_index=True
        )

    def portfolio_summary(self) -> pd.DataFrame:
        """Compatibilidad: devuelve ahora las posiciones abiertas calculadas."""

        return self.open_positions()
