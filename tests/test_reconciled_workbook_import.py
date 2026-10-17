"""Synthetic in-memory workbook fixtures only; no private source material."""

from io import BytesIO
import json

from openpyxl import Workbook
import pandas as pd
import pytest

from src.portfolio_snapshot_import import REQUIRED_COLUMNS
from src.reconciled_workbook_import import (
    IMPORT_MARKER,
    parse_reconciled_workbook_excel,
    plan_reconciled_operation_reimport,
)


TR_HEADERS = ["Fecha", "Tipo", "Activo", "ISIN", "Participaciones (+/-)",
              "Precio", "Flujo bróker €", "Comisión €"]
REV_HEADERS = ["Fecha", "Tipo", "Ticker", "Cantidad", "Precio unitario",
               "Importe original", "Moneda", "FX referencia"]
POST_HEADERS = ["Fecha", "Plataforma", "Activo", "Ticker / ISIN", "Cantidad",
                "Precio ejecución (€)", "Desembolso (€)", "Comisión (€)", "Nota"]


def synthetic_tables():
    return {
        "Cartera": [list(REQUIRED_COLUMNS),
            ["2026-10-13", "Trade Republic", "Synthetic asset", "AAA", "Acción",
             "Crecimiento", 2, 50, "EUR", 100, 0.1, 20.5, 79.5, "", "Synthetic"]],
        "Referencias de tickers": [["Activo", "Ticker en importación", "ISIN fuente"],
            ["Synthetic asset", "AAA", "ZZ0000000001"]],
        "Operaciones TR": [TR_HEADERS,
            ["2026-09-01", "BUY", "Synthetic asset", "ZZ0000000001", 4, 10, -40, 1],
            ["2026-10-01", "SELL", "Synthetic asset", "ZZ0000000001", -2, 15, 30, 1],
            ["2026-10-15", "BUY", "Synthetic asset", "ZZ0000000001", 1, 20, -20, 0]],
        "Compras TR 15-10": [POST_HEADERS,
            ["2026-10-15", "Trade Republic", "Synthetic asset", "AAA", 1, 20, 20, 0, ""],
            ["Importe total de compras posteriores", None, None, None, None, None, 20],
            ["No sumar estas compras otra vez al importar el extracto completo en el diario."]],
        "Operaciones Revolut": [REV_HEADERS,
            ["2026-09-01", "BUY - MARKET", "BBB", 10, 10, 101, "USD", 1.2],
            ["2026-10-01", "SELL - MARKET", "BBB", 2, 15, 29, "USD", 1.18],
            ["2026-10-01", "DIVIDEND", "BBB", None, None, 0.5, "USD", 1.18]],
        "Ventas TR": [["Fecha", "Empresa", "ISIN", "Acciones vendidas", "Coste FIFO €",
                       "Venta bruta €", "Comisión venta €", "Beneficio neto €"],
            ["2026-10-01", "Synthetic asset", "ZZ0000000001", 2, 20.5, 30, 1, 8.5]],
        "Posiciones TR": [["Activo", "ISIN", "Participaciones al 15/10", "Coste FIFO €", "Fecha cálculo"],
            ["Synthetic asset", "ZZ0000000001", 3, 40.5, "2026-10-15"]],
        "LEER ANTES DE IMPORTAR": [["USO DEL ARCHIVO", "Nota"], ["Not an operation", "Synthetic only"]],
    }


def workbook_bytes(tables=None):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in (tables or synthetic_tables()).items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_historical_photo_is_not_a_purchase_and_post_snapshot_is_separate():
    plan = parse_reconciled_workbook_excel(workbook_bytes())
    assert plan.snapshot_date == "2026-10-13"
    assert plan.snapshot.positions["value_eur"].sum() == 100
    assert len(plan.operations) == 5
    assert len(plan.historical_operations) == 4
    assert len(plan.post_snapshot_operations) == 1
    assert plan.post_snapshot_operations["settlement_amount_local"].sum() == 20
    assert not plan.operations["executed_at"].str.startswith("2026-10-13").any()
    assert "created_at" not in plan.operations.columns
    assert not plan.coverage["capital_flows_complete"]


def test_supplementary_purchases_merge_provenance_and_are_not_counted_twice():
    plan = parse_reconciled_workbook_excel(workbook_bytes())
    operation = plan.post_snapshot_operations.iloc[0]
    refs = json.loads(operation.provenance_json)
    assert {ref["sheet"] for ref in refs} == {"Operaciones TR", "Compras TR 15-10"}
    assert len(refs) == 2
    assert (plan.issues["code"] == "supplementary_duplicate_not_reimported").sum() == 1
    assert plan.pending_rows.empty
    assert set(plan.events["event_type"]) == {"dividend"}
    assert not set(plan.events["event_type"]) & {"contribution", "deposit"}


def test_recorded_net_settlements_own_fifo_without_inventing_fees_or_fx():
    plan = parse_reconciled_workbook_excel(workbook_bytes())
    tr = plan.operations.loc[plan.operations.platform == "Trade Republic"]
    assert tr.iloc[0].settlement_amount_local == 41
    assert tr.iloc[1].settlement_amount_local == 29
    realized_tr = plan.fifo_sales.loc[plan.fifo_sales.platform == "Trade Republic"].iloc[0]
    assert realized_tr.realized_pnl_eur == pytest.approx(8.5)
    rev = plan.operations.loc[plan.operations.platform == "Revolut"]
    assert rev.iloc[0].settlement_amount_local == 101
    assert rev.iloc[0].residual_cost_local == 1
    assert rev.iloc[0].fees == 0  # Compatibility only; explicitly unknown.
    assert set(rev.fee_quality) == {"unknown"}
    assert rev.settlement_amount_eur.isna().all()
    assert rev.fee_eur.isna().all()
    assert rev.fx_rate_to_eur.isna().all()
    assert set(rev.fx_quality) == {"reference"}
    realized_rev = plan.fifo_sales.loc[plan.fifo_sales.platform == "Revolut"].iloc[0]
    assert realized_rev.realized_pnl_local == pytest.approx(8.8)
    assert pd.isna(realized_rev.realized_pnl_eur)
    assert plan.events.amount_eur.isna().all()


def test_identities_survive_file_changes_and_reimports_reuse_manual_operations():
    first = parse_reconciled_workbook_excel(workbook_bytes())
    tables = synthetic_tables()
    tables["LEER ANTES DE IMPORTAR"].append(["Different note", "Still not an operation"])
    second = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert first.source_sha256 != second.source_sha256
    assert first.operations.source_event_id.tolist() == second.operations.source_event_id.tolist()
    existing = first.operations.copy()
    existing["id"] = range(1, len(existing) + 1)
    assert set(plan_reconciled_operation_reimport(second.operations, existing).action) == {"skip"}
    manual = existing.drop(columns=["source_event_id"])
    manual["notes"] = "A synthetic manual operation"
    assert set(plan_reconciled_operation_reimport(second.operations, manual).action) == {"skip"}


def test_old_notes_metadata_can_prove_idempotence_without_source_column():
    plan = parse_reconciled_workbook_excel(workbook_bytes())
    assert plan.operations.notes.str.startswith(IMPORT_MARKER).all()
    legacy = plan.operations.drop(columns=["source_event_id"])
    assert set(plan_reconciled_operation_reimport(plan.operations, legacy).action) == {"skip"}


def test_ambiguous_or_contradictory_existing_matches_require_review():
    plan = parse_reconciled_workbook_excel(workbook_bytes())
    one = plan.operations.head(1)
    duplicate = pd.concat([one, one], ignore_index=True)
    assert plan_reconciled_operation_reimport(one, duplicate).iloc[0].action == "pending"
    wrong_fee = one.copy()
    wrong_fee["fees"] = 4
    assert plan_reconciled_operation_reimport(one, wrong_fee).iloc[0].reason == "contradictory_existing_amount"
    missing_account = one.copy()
    missing_account["account_name"] = ""
    missing_account["source_event_id"] = ""
    missing_account["notes"] = ""
    assert plan_reconciled_operation_reimport(one, missing_account).iloc[0].action == "pending"


def test_same_source_identical_rows_are_ambiguous_not_silently_deduplicated():
    tables = synthetic_tables()
    tables["Operaciones TR"].append(tables["Operaciones TR"][1].copy())
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert (plan.pending_rows.code == "ambiguous_same_source_duplicate").sum() == 2
    assert len(plan.operations.loc[(plan.operations.platform == "Trade Republic") & (plan.operations.side == "Compra")]) == 1
    assert "sale_without_documented_opening_lot" in set(plan.pending_rows.code)


def test_conflicting_supplementary_totals_are_quarantined():
    tables = synthetic_tables()
    tables["Compras TR 15-10"][1][6] = 21
    tables["Compras TR 15-10"][1][7] = 1
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert plan.post_snapshot_operations.empty
    assert (plan.pending_rows.code == "contradictory_duplicate").sum() == 2


def test_missing_quantity_and_sign_conflicts_are_pending_without_raw_values():
    tables = synthetic_tables()
    tables["Operaciones TR"].append(["2026-10-02", "BUY", "Synthetic asset", "ZZ0000000001", None, 10, -10, 0])
    tables["Operaciones TR"].append(["2026-10-03", "BUY", "Synthetic asset", "ZZ0000000001", -1, 10, -10, 0])
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert {"incomplete_operation", "contradictory_quantity_sign"}.issubset(set(plan.pending_rows.code))
    assert "Synthetic asset" not in plan.issues.to_json()


def test_snapshot_difference_is_explicit_and_never_patched_with_an_operation():
    plan = parse_reconciled_workbook_excel(workbook_bytes(), expected_snapshot_value_eur=101.23)
    assert plan.coverage["snapshot_reference_difference_eur"] == -1.23
    assert "snapshot_wealth_reference_mismatch" in set(plan.pending_rows.code)
    assert plan.snapshot.positions["value_eur"].sum() == 100
    assert len(plan.operations) == 5


def test_unknown_isin_is_preserved_without_a_guessed_analysis_ticker():
    tables = synthetic_tables()
    tables["Referencias de tickers"] = tables["Referencias de tickers"][:1]
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    unknown = plan.operations.loc[plan.operations.ticker == "ZZ0000000001"]
    assert len(unknown) == 3
    assert set(unknown.analysis_ticker) == {""}
    assert "analysis_ticker_unmapped" in set(plan.issues.code)


def test_source_identity_does_not_change_when_an_isin_gets_a_quote_symbol():
    tables = synthetic_tables()
    tables["Referencias de tickers"] = tables["Referencias de tickers"][:1]
    tables.pop("Compras TR 15-10")
    unknown = parse_reconciled_workbook_excel(workbook_bytes(tables))
    known = parse_reconciled_workbook_excel(workbook_bytes())
    before = unknown.operations.loc[unknown.operations.platform == "Trade Republic"]
    after = known.operations.loc[known.operations.platform == "Trade Republic"]
    assert before.source_event_id.tolist() == after.source_event_id.tolist()


def test_sales_control_ticker_alias_uses_the_same_canonical_instrument():
    tables = synthetic_tables()
    for row in tables["Operaciones Revolut"][1:]:
        row[2] = "CEBS"
    tables["Ventas Revolut"] = [["Fecha compra", "Fecha venta", "Ticker", "Activo", "Moneda",
                                "Cantidad", "Coste bruto", "Venta bruta", "PnL bruto"],
        ["2026-09-01", "2026-10-01", "CEBS", "Synthetic ETF", "USD", 2, 20, 30, 10]]
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert "reported_fifo_sales_mismatch" not in set(plan.pending_rows.code)
    assert "CEBS.DE" in set(plan.sales_references.ticker)


def test_no_private_cell_values_appear_in_validation_errors():
    tables = synthetic_tables()
    tables["Cartera"][1][9] = "SYNTHETIC_SECRET_VALUE"
    with pytest.raises(ValueError) as error:
        parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert "SYNTHETIC_SECRET_VALUE" not in str(error.value)
    with pytest.raises(ValueError, match="leer"):
        parse_reconciled_workbook_excel(b"not an Excel file")


def test_only_snapshot_is_supported_as_an_incomplete_plan_without_invented_history():
    tables = {"Cartera": synthetic_tables()["Cartera"]}
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert plan.operations.empty
    assert plan.events.empty
    assert (plan.pending_rows.code == "missing_operation_sheet").sum() == 3
    assert plan.coverage["operations_complete"] is False


def test_fifo_control_mismatch_is_pending_and_aggregate_rows_are_not_operations():
    tables = synthetic_tables()
    tables["Ventas TR"][1][-1] = 99
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert "reported_fifo_sales_mismatch" in set(plan.pending_rows.code)
    assert len(plan.operations) == 5
    assert plan.inventory.loc[plan.inventory.sheet == "Compras TR 15-10", "nonempty_data_rows"].iloc[0] == 3


def test_supplementary_platform_contradiction_is_not_reassigned_to_trade_republic():
    tables = synthetic_tables()
    tables["Compras TR 15-10"][1][1] = "Different synthetic broker"
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert "contradictory_supplementary_platform" in set(plan.pending_rows.code)
    assert len(plan.post_snapshot_operations) == 1  # Only documented main extract.


def test_missing_fee_is_not_zero_for_documented_trade_republic_rows():
    tables = synthetic_tables()
    tables["Operaciones TR"][1][-1] = None
    plan = parse_reconciled_workbook_excel(workbook_bytes(tables))
    assert "incomplete_settlement_or_fee" in set(plan.pending_rows.code)
    assert not (plan.operations["executed_at"].str.startswith("2026-09-01")
                & (plan.operations.platform == "Trade Republic")).any()
