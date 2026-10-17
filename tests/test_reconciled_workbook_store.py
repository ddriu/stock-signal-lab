"""Synthetic SQLite and mocked REST checks; never opens a private database."""

from copy import deepcopy
from dataclasses import dataclass
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from src.journal import TradingJournal, calculate_position_states, normalize_operation
from src.reconciled_workbook_store import (
    normalize_reconciled_bundle,
    prepare_reconciled_workbook_bundle,
)
from src.supabase_journal import JournalStorageError, SupabaseTradingJournal


def trade(ticker="AAA", side="Compra", quantity=2, price=10, day="2026-10-01", event="event-1", **changes):
    values = dict(ticker=ticker, account_name="Broker", side=side, quantity=quantity,
                  price=price, fees=1, executed_at=day, currency="EUR",
                  source_event_id=event, settlement_amount_local=quantity*price + (1 if side == "Compra" else -1),
                  fee_quality="documented", fx_quality="exact", phase="historical")
    values.update(changes)
    return values


def bundle(**changes):
    values = dict(source_sha256="a" * 64, snapshot_date="2026-10-13",
                  positions=[dict(snapshot_date="2026-10-13", platform="Broker", asset_name="Synthetic asset",
                                  analysis_ticker="AAA", asset_type="Acción", quantity=2,
                                  current_price=20, currency="EUR", value_eur=40,
                                  cost_estimate_eur=21, gain_loss_eur=19)],
                  operations=[trade(), trade(quantity=1, price=20, day="2026-10-15", event="event-post", phase="post_snapshot")],
                  metadata=dict(events=[], issues=[], inventory=[], coverage={"capital_flows_complete": False},
                                provenance={"schema": "synthetic"}))
    values.update(changes)
    return values


def test_import_is_atomic_additive_and_source_hash_idempotent(tmp_path):
    journal = TradingJournal(tmp_path / "test.db", owner="test")
    journal.add_operation("MANUAL", "Compra", 1, 5, 0, "2026-01-01", account_name="Manual")
    first = journal.import_reconciled_portfolio(bundle(), recorded_by="TEST")
    second = journal.import_reconciled_portfolio(bundle())

    assert first["inserted_operations"] == 2
    assert first["inserted_positions"] == 1
    assert not first["already_imported"]
    assert second["already_imported"]
    assert second["batch_id"] == first["batch_id"]
    assert len(journal.list_operations()) == 3
    assert len(journal.list_portfolio_snapshot_positions()) == 1
    assert "backup" not in journal.list_portfolio_imports().columns
    imports = journal.list_portfolio_imports(include_backup=True)
    assert len(imports) == 1
    backup = json.loads(imports.iloc[0]["backup"])
    assert backup["operations"][0]["ticker"] == "MANUAL"
    assert backup["portfolio_snapshots"] == []
    assert set(backup) == {"operations", "portfolio_snapshots", "portfolio_accounts", "private_investments"}
    assert journal.open_positions().set_index("ticker").loc["AAA", "quantity"] == 3


def test_different_workbook_with_same_events_does_not_duplicate_transactions(tmp_path):
    journal = TradingJournal(tmp_path / "test.db")
    journal.import_reconciled_portfolio(bundle())
    result = journal.import_reconciled_portfolio(bundle(source_sha256="b" * 64))

    assert result["inserted_operations"] == 0
    assert result["skipped_operations"] == 2
    assert result["skipped_positions"] == 1
    assert len(journal.list_operations()) == 2
    assert len(journal.list_portfolio_imports()) == 2


def test_unambiguous_manual_event_is_reused_without_modifying_it(tmp_path):
    journal = TradingJournal(tmp_path / "test.db")
    journal.add_operation("AAA", "Compra", 2, 10, 1, "2026-10-01", account_name="Broker", notes="Manual preserved")
    before = journal.list_operations().iloc[0].to_dict()
    result = journal.import_reconciled_portfolio(bundle())

    assert result["skipped_operations"] == 1
    assert result["inserted_operations"] == 1
    manual = journal.list_operations().set_index("id").loc[before["id"]]
    assert manual["notes"] == "Manual preserved"
    assert pd.isna(manual["source_event_id"])


def test_conflicting_manual_figures_and_snapshot_are_backed_up_not_overwritten(tmp_path):
    journal = TradingJournal(tmp_path / "test.db")
    journal.add_operation("AAA", "Compra", 2, 10, 4, "2026-10-01", account_name="Broker", notes="Manual cost")
    journal.add_private_investment(platform="Civislend", project_name="Synthetic project", invested_amount=10,
                                   current_value=11, expected_return_pct=10, start_date="2026-01-01")
    existing_snapshot = pd.DataFrame(bundle()["positions"])
    existing_snapshot["value_eur"] = 99
    journal.replace_portfolio_snapshot_positions(existing_snapshot, snapshot_date="2026-10-13")
    before_private = journal.list_private_investments().to_dict("records")
    before_accounts = journal.list_portfolio_accounts().to_dict("records")
    result = journal.import_reconciled_portfolio(bundle())

    assert result["pending_conflicts"] == 2
    assert journal.list_operations().loc[lambda frame: frame.notes == "Manual cost", "fees"].iloc[0] == 4
    assert journal.list_portfolio_snapshot_positions().iloc[0]["value_eur"] == 99
    assert journal.list_private_investments().to_dict("records") == before_private
    assert journal.list_portfolio_accounts().to_dict("records") == before_accounts
    batch = journal.list_portfolio_imports(include_backup=True).iloc[0]
    assert len(json.loads(batch["metadata"])["pending_conflicts"]) == 2
    assert json.loads(batch["backup"])["private_investments"] == before_private


def test_failed_insert_rolls_back_backup_batch_operations_and_snapshot(tmp_path):
    journal = TradingJournal(tmp_path / "test.db")
    with journal._connect() as connection:
        connection.execute("CREATE TRIGGER synthetic_fail BEFORE INSERT ON operations WHEN NEW.ticker='FAIL' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    data = bundle()
    data["operations"].append(trade(ticker="FAIL", event="fail"))
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        journal.import_reconciled_portfolio(data)

    assert journal.list_operations().empty
    assert journal.list_portfolio_snapshot_positions().empty
    assert journal.list_portfolio_imports().empty


def test_recorded_native_cash_owns_fifo_without_fictitious_fee_or_fx(tmp_path):
    journal = TradingJournal(tmp_path / "test.db")
    data = bundle(operations=[
        trade(ticker="USD", quantity=10, price=10, currency="USD", settlement_amount_local=101,
              fees=0, fee_quality="unknown", fx_quality="reference"),
        trade(ticker="USD", side="Venta", quantity=2, price=15, currency="USD", settlement_amount_local=29,
              fees=0, fee_quality="unknown", fx_quality="reference", day="2026-10-02", event="usd-sale"),
    ])
    journal.import_reconciled_portfolio(data)
    operations = journal.list_operations()
    state = calculate_position_states(operations, rates_per_eur={"EUR": 1, "USD": 1.2}).iloc[0]

    assert state["cost_basis"] == pytest.approx(80.8)
    assert state["realized_pnl"] == pytest.approx(8.8)
    assert pd.isna(state["paid_fees"])
    assert pd.isna(state["paid_fees_eur"])
    assert pd.isna(state["cost_basis_eur"])
    assert pd.isna(state["realized_pnl_eur"])
    assert operations.settlement_amount_local.tolist() == [29, 101]
    assert operations.fee_eur.isna().all()


def test_unknown_eur_fee_stays_unknown_despite_exact_settlement(tmp_path):
    journal = TradingJournal(tmp_path / "test.db")
    journal.import_reconciled_portfolio(bundle(operations=[trade(fees=0, fee_quality="unknown", settlement_amount_local=21)]))
    saved = journal.list_operations().iloc[0]
    assert saved.settlement_amount_eur == 21
    assert pd.isna(saved.fee_eur)
    assert pd.isna(calculate_position_states(journal.list_operations()).iloc[0].paid_fees_eur)


def test_bundle_preview_strips_source_paths_and_preserves_income_without_deposits():
    data = bundle()
    plan = SimpleNamespace(source_sha256=data["source_sha256"], snapshot_date=data["snapshot_date"],
        snapshot=SimpleNamespace(positions=pd.DataFrame(data["positions"])),
        operations=pd.DataFrame(data["operations"]),
        events=pd.DataFrame([dict(event_type="dividend", amount_eur=5, source_path="/Users/private/source.xlsx")]),
        issues=pd.DataFrame(), inventory=pd.DataFrame(), coverage={"capital_flows_complete": False})
    prepared = prepare_reconciled_workbook_bundle(plan)

    assert "/Users/" not in json.dumps(prepared)
    assert prepared["metadata"]["events"][0]["event_type"] == "dividend"
    assert prepared["metadata"]["events"][0]["amount_eur"] == 5
    assert not prepared["metadata"]["coverage"]["capital_flows_complete"]
    assert prepared["metadata"]["provenance"]["operations"][0]["source_event_id"] == "event-1"
    assert prepared["metadata"]["provenance"]["operations"][0]["fee_quality"] == "documented"


def test_source_identity_cannot_hide_changed_execution_details(tmp_path):
    journal = TradingJournal(tmp_path / "test.db")
    journal.import_reconciled_portfolio(bundle())
    data = bundle(source_sha256="c" * 64)
    data["operations"][0]["quantity"] = 3
    result = journal.import_reconciled_portfolio(data)
    assert result["pending_conflicts"] == 1
    assert len(journal.list_operations()) == 2


def test_manual_defaults_and_recorded_local_validation_are_unchanged():
    manual = normalize_operation("AAA", "Compra", 1, 10, 1, "2026-01-01")
    assert manual["settlement_amount_eur"] == 11
    assert manual["settlement_amount_local"] is None
    assert manual["source_event_id"] is None
    for value in (0, -1, float("inf")):
        with pytest.raises(ValueError):
            normalize_operation("AAA", "Compra", 1, 10, 1, "2026-01-01", settlement_amount_local=value)


@dataclass
class Response:
    payload: object
    status_code: int = 200
    text: str = ""
    headers: object = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


def test_remote_import_is_one_owner_scoped_rpc_and_history_is_owner_filtered(monkeypatch):
    calls = []
    outputs = [Response({"source_sha256": "a" * 64, "batch_id": 7}), Response([dict(id=7,
        owner="test", source_sha256="a" * 64, metadata={}, backup={}, summary={})], headers={})]
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return outputs.pop(0)
    monkeypatch.setattr("src.supabase_journal.requests.request", request)
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "test")
    result = journal.import_reconciled_portfolio(bundle(), recorded_by="TEST")
    history = journal.list_portfolio_imports(include_backup=True)

    assert result["batch_id"] == 7
    assert calls[0][0] == "POST" and calls[0][1].endswith("/rpc/import_reconciled_portfolio")
    assert calls[0][2]["json"]["p_owner"] == "test"
    assert calls[0][2]["json"]["p_recorded_by"] == "test"
    assert calls[1][2]["params"]["owner"] == "eq.test"
    assert json.loads(history.iloc[0]["backup"]) == {}


def test_missing_remote_migration_fails_once_without_unsafe_fallback(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url))
        return Response({}, 404, "Could not find import_reconciled_portfolio in schema cache")
    monkeypatch.setattr("src.supabase_journal.requests.request", request)
    journal = SupabaseTradingJournal("https://example.supabase.co", "sb_secret_test", "test")
    with pytest.raises(JournalStorageError, match="No se ha importado"):
        journal.import_reconciled_portfolio(bundle())
    assert len(calls) == 1
