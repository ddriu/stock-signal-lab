"""Synthetic integration regression: editing a plan must preserve its opening balance."""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from src.journal import PORTFOLIO_SNAPSHOT_COLUMNS, normalize_portfolio_snapshot_position
from src.portfolio_reconciliation import build_reconciled_portfolio
from src.portfolio_snapshot import latest_portfolio_snapshot
from src.portfolio_thesis import build_position_plan_update_rows, build_thesis_update_rows


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _home_comment_source(raw_snapshot: pd.DataFrame, reconciled_snapshot: pd.DataFrame) -> pd.DataFrame:
    """Execute only render_home's baseline-capture assignment, without starting the app."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    home = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_home")
    editor = next(node for node in ast.walk(home) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == "_render_thesis_override")
    assert isinstance(editor.args[1], ast.Name)
    source_name = editor.args[1].id
    # A derived view must never be the persistence input of a comments-only form.
    assert source_name not in {"latest_snapshot", "market_snapshot"}
    captures = [node for node in home.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == source_name for target in node.targets)]
    assert len(captures) == 1
    capture = captures[0]
    report_read = next(node for node in ast.walk(home) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name) and node.func.id == "load_reconciled_portfolio")
    assert capture.lineno < report_read.lineno
    scope = {"latest_snapshot": raw_snapshot.copy(deep=True)}
    exec(compile(ast.Module(body=[capture], type_ignores=[]), str(APP_PATH), "exec"), scope)
    scope["latest_snapshot"] = reconciled_snapshot.copy(deep=True)
    return scope[source_name]


@pytest.mark.parametrize("edit", ["plan", "invalidation"])
def test_home_comments_never_persist_current_inventory_into_historical_baseline(edit: str) -> None:
    security = normalize_portfolio_snapshot_position(
        snapshot_date="2026-10-13", platform="Synthetic broker", asset_name="Synthetic holding",
        analysis_ticker="SYN", asset_type="Acción", quantity=10, currency="EUR",
        value_eur=120, cost_estimate_eur=100,
    )
    cash = normalize_portfolio_snapshot_position(
        snapshot_date="2026-10-13", platform="Synthetic broker", asset_name="Efectivo",
        asset_type="Efectivo", currency="EUR", value_eur=500,
    )
    original = pd.DataFrame([security, cash])
    operations = pd.DataFrame([{
        "ticker": "SYN", "account_name": "Synthetic broker", "side": "Compra",
        "quantity": 2, "price": 10, "fees": 0, "currency": "EUR", "executed_at": "2026-10-14",
    }])
    first = build_reconciled_portfolio(original, operations)
    raw_latest, _ = latest_portfolio_snapshot(original)
    comment_source = _home_comment_source(raw_latest, first.positions)
    if edit == "plan":
        rows = build_position_plan_update_rows(
            comment_source, "SYN", strategy="growth_momentum", horizon="12 meses",
            thesis="Una tesis sintética expresamente documentada",
        )
    else:
        rows = build_thesis_update_rows(
            comment_source, "SYN", invalidated=True,
            reason="Hecho sintético que invalida la tesis documentada",
        )
    # Simulate only payload normalization and the unique-key UPSERT, entirely in memory.
    saved = normalize_portfolio_snapshot_position(**{
        column: rows.iloc[0][column] for column in PORTFOLIO_SNAPSHOT_COLUMNS
        if column in rows and column not in {"id", "recorded_by", "created_at", "updated_at"}
    })
    assert saved["quantity"] == 10
    assert saved["value_eur"] == 120
    assert saved["cost_estimate_eur"] == 100
    assert saved["snapshot_date"] == "2026-10-13"
    reread = build_reconciled_portfolio(pd.DataFrame([saved, cash]), operations)
    holding = reread.positions.loc[reread.positions.ticker == "SYN"].iloc[0]
    assert holding.quantity == 12
    assert holding.cost_basis_eur == 120
    assert reread.metrics_global["cash_value_eur"] == 480

