"""Pruebas unitarias de las helpers de rotación definidas en ``app.py``.

Se extraen sólo las funciones puras para evitar arrancar Streamlit durante estos
tests y mantener los casos centrados en el contrato de reconciliación.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd

from src.portfolio_snapshot import (
    latest_portfolio_snapshot,
    reconcile_current_portfolio,
)


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
HELPER_NAMES = {
    "_rotation_excluded_asset_mask",
    "_rotation_eligible_snapshot",
    "_current_rotation_tickers",
    "_portfolio_tracking_tickers",
    "_portfolio_allocations",
}


class JournalStorageError(Exception):
    pass


class _TrackingJournal:
    def __init__(
        self,
        *,
        operations: pd.DataFrame,
        positions: pd.DataFrame,
        snapshots: pd.DataFrame,
    ) -> None:
        self._operations = operations
        self._positions = positions
        self._snapshots = snapshots

    def list_operations(self) -> pd.DataFrame:
        return self._operations.copy()

    def open_positions(self) -> pd.DataFrame:
        return self._positions.copy()

    def list_portfolio_snapshot_positions(self) -> pd.DataFrame:
        return self._snapshots.copy()


def _load_portfolio_helpers() -> dict[str, object]:
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_ROTATION_EXCLUDED_ASSET_TERMS"
                for target in node.targets
            )
        )
        or (isinstance(node, ast.FunctionDef) and node.name in HELPER_NAMES)
    ]
    namespace: dict[str, object] = {
        "pd": pd,
        "resolve_analysis_ticker": lambda ticker: str(ticker).strip().upper(),
        "JournalStorageError": JournalStorageError,
        "latest_portfolio_snapshot": latest_portfolio_snapshot,
        "reconcile_current_portfolio": reconcile_current_portfolio,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), APP_PATH, "exec"), namespace)
    return namespace


HELPERS = _load_portfolio_helpers()


def test_rotation_excluded_mask_recognizes_alternatives_even_with_ticker() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "platform": "Civislend",
                "asset_name": "Proyecto residencial",
                "analysis_ticker": "CIVI",
            },
            {
                "platform": "Segofactoring",
                "asset_name": "Factura comercial",
                "analysis_ticker": "SEGO",
            },
            {
                "platform": "Broker",
                "asset_name": "Apple",
                "analysis_ticker": "AAPL",
            },
        ]
    )

    mask = HELPERS["_rotation_excluded_asset_mask"](snapshot)

    assert mask.tolist() == [True, True, False]


def test_current_rotation_tickers_does_not_restore_a_sold_dashboard_ticker() -> None:
    stale_dashboard = pd.DataFrame([{"ticker": "SOLD", "quantity": 4.0}])
    reconciled_snapshot = pd.DataFrame(
        columns=["platform", "asset_name", "analysis_ticker", "value_eur"]
    )

    tickers = HELPERS["_current_rotation_tickers"](
        stale_dashboard,
        reconciled_snapshot,
    )

    assert tickers == []


def test_tracking_tickers_reconciles_sale_and_excludes_alternatives() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-01",
                "platform": "Broker",
                "asset_name": "Apple",
                "asset_type": "Acción",
                "analysis_ticker": "AAPL",
                "value_eur": 500.0,
            },
            {
                "snapshot_date": "2026-08-01",
                "platform": "Civislend",
                "asset_name": "Proyecto residencial",
                "asset_type": "Crowdlending",
                "analysis_ticker": "CIVI",
                "value_eur": 300.0,
            },
            {
                "snapshot_date": "2026-08-01",
                "platform": "Segofactoring",
                "asset_name": "Factura comercial",
                "asset_type": "Factoring",
                "analysis_ticker": "SEGO",
                "value_eur": 200.0,
            },
        ]
    )
    operations = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "side": "Venta",
                "executed_at": "2026-08-02T12:00:00+00:00",
            }
        ]
    )
    journal = _TrackingJournal(
        operations=operations,
        positions=pd.DataFrame(columns=["ticker", "quantity"]),
        snapshots=snapshots,
    )

    assert HELPERS["_portfolio_tracking_tickers"](journal) == []


def test_tracking_tickers_keeps_recent_snapshot_authoritative_over_old_diary() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-13",
                "platform": "Broker",
                "asset_name": "Oracle",
                "asset_type": "Acción",
                "analysis_ticker": "ORCL",
                "value_eur": 270.0,
            }
        ]
    )
    operations = pd.DataFrame(
        [
            {
                "ticker": "MRNA",
                "side": "Compra",
                "executed_at": "2026-08-01T12:00:00+00:00",
            }
        ]
    )
    journal = _TrackingJournal(
        operations=operations,
        positions=pd.DataFrame(
            [{"ticker": "MRNA", "quantity": 1.0, "currency": "USD"}]
        ),
        snapshots=snapshots,
    )

    assert HELPERS["_portfolio_tracking_tickers"](journal) == ["ORCL"]


def test_portfolio_allocations_excludes_explicit_alternatives_with_tickers() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "platform": "Broker",
                "asset_name": "Apple",
                "analysis_ticker": "AAPL",
                "value_eur": 600.0,
            },
            {
                "platform": "Civislend",
                "asset_name": "Proyecto residencial",
                "analysis_ticker": "CIVI",
                "value_eur": 300.0,
            },
            {
                "platform": "Segofactoring",
                "asset_name": "Factura comercial",
                "analysis_ticker": "SEGO",
                "value_eur": 100.0,
            },
        ]
    )

    allocations = HELPERS["_portfolio_allocations"](pd.DataFrame(), snapshot)

    assert allocations == {"AAPL": 100.0}
