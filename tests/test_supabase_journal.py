from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
import pytest

from src.supabase_journal import SupabaseTradingJournal
from src.alerts import AlertState, normalize_alert_preferences


@dataclass
class FakeResponse:
    payload: Any
    status_code: int = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


def test_supabase_journal_filters_every_request_by_owner(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        FakeResponse([]),
        FakeResponse([{"id": 7}]),
        FakeResponse(
            [
                {
                    "id": 7,
                    "ticker": "AAPL",
                    "side": "Compra",
                    "quantity": 2.0,
                    "price": 100.0,
                    "fees": 1.0,
                    "executed_at": "2025-01-01T00:00:00",
                    "notes": "",
                    "currency": "USD",
                    "created_at": "2025-01-01T12:00:00",
                }
            ]
        ),
        FakeResponse(None, status_code=204),
    ]

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr("src.supabase_journal.requests.request", fake_request)
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "stocklab",
    )

    journal.healthcheck()
    operation_id = journal.add_operation(
        "aapl",
        "Compra",
        2,
        100,
        1,
        "2025-01-01",
        currency="usd",
        recorded_by="Luci",
    )
    operations = journal.list_operations()
    journal.delete_operation(operation_id)

    assert operation_id == 7
    assert operations.iloc[0]["ticker"] == "AAPL"
    assert calls[0]["params"]["owner"] == "eq.stocklab"
    assert calls[1]["json"]["owner"] == "stocklab"
    assert calls[1]["json"]["recorded_by"] == "luci"
    assert calls[2]["params"]["owner"] == "eq.stocklab"
    assert calls[3]["params"] == {"id": "eq.7", "owner": "eq.stocklab"}
    assert "Authorization" not in calls[0]["headers"]


def test_supabase_journal_rejects_sale_larger_than_remote_position(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.supabase_journal.requests.request",
        lambda *args, **kwargs: FakeResponse([]),
    )
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "stocklab",
    )
    with pytest.raises(ValueError, match="supera"):
        journal.add_operation("AAPL", "Venta", 1, 100, 1, "2025-01-01")


def test_legacy_service_role_key_uses_bearer_header() -> None:
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "legacy-jwt",
        "stocklab",
    )
    assert journal.headers["Authorization"] == "Bearer legacy-jwt"


def test_supabase_favorites_use_separate_table_and_owner(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        FakeResponse([]),
        FakeResponse([{"id": 9}]),
        FakeResponse(
            [
                {
                    "id": 9,
                    "ticker": "TSM",
                    "name": "Taiwan Semiconductor",
                    "exchange": "NYSE",
                    "tags": "Tecnología",
                    "recorded_by": "xavi",
                    "created_at": "2026-07-28T12:00:00",
                }
            ]
        ),
        FakeResponse(None, status_code=204),
    ]

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr("src.supabase_journal.requests.request", fake_request)
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "grupo_compartido",
    )

    favorite_id = journal.add_favorite(
        "tsm",
        "Taiwan Semiconductor",
        "NYSE",
        tags=["Tecnología"],
        recorded_by="Xavi",
    )
    favorites = journal.list_favorites()
    journal.delete_favorite("TSM")

    assert favorite_id == 9
    assert favorites.iloc[0]["recorded_by"] == "xavi"
    assert favorites.iloc[0]["tags"] == "Tecnología"
    assert all(call["url"].endswith("/rest/v1/favorites") for call in calls)
    assert calls[0]["params"]["owner"] == "eq.grupo_compartido"
    assert calls[1]["json"]["owner"] == "grupo_compartido"
    assert calls[1]["json"]["tags"] == "Tecnología"
    assert calls[3]["params"]["owner"] == "eq.grupo_compartido"


def test_supabase_favorite_tags_are_updated_by_owner(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(None, status_code=204)

    monkeypatch.setattr("src.supabase_journal.requests.request", fake_request)
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "ddriu",
    )

    journal.update_favorite_tags("ypf", ["Energía", "Dividendos"])

    assert calls[0]["method"] == "PATCH"
    assert calls[0]["params"] == {"ticker": "eq.YPF", "owner": "eq.ddriu"}
    assert calls[0]["json"] == {"tags": "Energía, Dividendos"}


def test_supabase_private_investments_are_always_filtered_by_owner(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        FakeResponse([{"id": 21}]),
        FakeResponse(
            [
                {
                    "id": 21,
                    "platform": "Segofactoring",
                    "project_name": "Factura 44",
                    "invested_amount": 800,
                    "current_value": 820,
                    "expected_return_pct": 8,
                    "start_date": "2026-03-01T00:00:00Z",
                    "maturity_date": "2026-09-01T00:00:00Z",
                    "status": "Activa",
                    "notes": "",
                    "recorded_by": "ddriu",
                    "created_at": "2026-03-01T10:00:00Z",
                }
            ]
        ),
        FakeResponse(None, status_code=204),
        FakeResponse(None, status_code=204),
    ]

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr("src.supabase_journal.requests.request", fake_request)
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "ddriu",
    )
    investment_id = journal.add_private_investment(
        platform="Segofactoring",
        project_name="Factura 44",
        invested_amount=800,
        current_value=820,
        expected_return_pct=8,
        start_date="2026-03-01",
        maturity_date="2026-09-01",
        recorded_by="ddriu",
    )
    stored = journal.list_private_investments()
    journal.update_private_investment(
        investment_id,
        current_value=825,
        status="Finalizada",
        notes="Cobrada",
    )
    journal.delete_private_investment(investment_id)

    assert stored.iloc[0]["project_name"] == "Factura 44"
    assert all(call["url"].endswith("/rest/v1/private_investments") for call in calls)
    assert calls[0]["json"]["owner"] == "ddriu"
    assert calls[1]["params"]["owner"] == "eq.ddriu"
    assert calls[2]["params"] == {"id": "eq.21", "owner": "eq.ddriu"}
    assert calls[3]["params"] == {"id": "eq.21", "owner": "eq.ddriu"}


def test_supabase_portfolio_accounts_are_upserted_and_filtered_by_owner(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        FakeResponse([{"id": 31}]),
        FakeResponse(
            [
                {
                    "id": 31,
                    "account_name": "Trade Republic",
                    "account_type": "Bróker",
                    "investments_value": 3_000,
                    "cash_balance": 400,
                    "currency": "EUR",
                    "status": "Actualizada",
                    "notes": "Resumen provisional",
                    "updated_at": "2026-08-01T10:00:00Z",
                    "created_at": "2026-08-01T09:00:00Z",
                }
            ]
        ),
    ]

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr("src.supabase_journal.requests.request", fake_request)
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "ddriu",
    )

    account_id = journal.upsert_portfolio_account(
        account_name="Trade Republic",
        account_type="Bróker",
        investments_value=3_000,
        cash_balance=400,
        status="Actualizada",
        notes="Resumen provisional",
    )
    accounts = journal.list_portfolio_accounts()

    assert account_id == 31
    assert accounts.iloc[0]["account_name"] == "Trade Republic"
    assert all(call["url"].endswith("/rest/v1/portfolio_accounts") for call in calls)
    assert calls[0]["params"] == {"on_conflict": "owner,account_name"}
    assert calls[0]["json"]["owner"] == "ddriu"
    assert calls[1]["params"]["owner"] == "eq.ddriu"


def test_supabase_complete_snapshot_replacement_deletes_same_date_first(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(None, status_code=204)

    monkeypatch.setattr("src.supabase_journal.requests.request", fake_request)
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "ddriu",
    )

    count = journal.replace_portfolio_snapshot_positions(
        pd.DataFrame(
            [
                {
                    "snapshot_date": "2026-08-13",
                    "platform": "Revolut",
                    "asset_name": "Oracle",
                    "value_eur": 270.0,
                }
            ]
        ),
        snapshot_date="2026-08-13",
        recorded_by="ddriu",
    )

    assert count == 1
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["params"] == {
        "owner": "eq.ddriu",
        "snapshot_date": "eq.2026-08-13",
    }
    assert calls[1]["method"] == "POST"
    assert calls[1]["json"][0]["asset_name"] == "Oracle"


def test_supabase_analysis_history_uses_owner_and_separate_table(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        FakeResponse([{"id": 12}]),
        FakeResponse(
            [
                {
                    "id": 12,
                    "ticker": "TSM",
                    "analyzed_at": "2026-07-29T00:00:00Z",
                    "price": 151.5,
                    "opportunity_score": 78,
                    "company_score": 90,
                    "entry_score": 64,
                    "valuation_score": 55,
                    "relative_score": 82,
                    "risk_score": 61,
                    "opportunity_label": "Candidata",
                    "entry_label": "Vigilancia",
                    "position_label": "Mantener",
                    "expected_return_pct": 6.5,
                    "positive_rate_pct": 62.0,
                    "expected_price": 161.35,
                    "horizon_days": 20,
                    "sector": "Technology",
                    "explanation": "Seguimiento",
                    "note": "",
                    "created_at": "2026-07-29T12:00:00Z",
                }
            ]
        ),
        FakeResponse(None, status_code=204),
    ]

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr("src.supabase_journal.requests.request", fake_request)
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "ddriu",
    )

    snapshot_id = journal.add_analysis_snapshot(
        ticker="TSM",
        analyzed_at="2026-07-29",
        price=151.5,
        opportunity_score=78,
        company_score=90,
        entry_score=64,
        valuation_score=55,
        relative_score=82,
        risk_score=61,
        opportunity_label="Candidata",
        entry_label="Vigilancia",
        position_label="Mantener",
    )
    history = journal.list_analysis_snapshots("tsm")
    journal.delete_analysis_snapshot(snapshot_id)

    assert history.iloc[0]["ticker"] == "TSM"
    assert all(call["url"].endswith("/rest/v1/analysis_snapshots") for call in calls)
    assert calls[0]["json"]["owner"] == "ddriu"
    assert calls[1]["params"]["owner"] == "eq.ddriu"
    assert calls[1]["params"]["ticker"] == "eq.TSM"
    assert calls[2]["params"] == {"id": "eq.12", "owner": "eq.ddriu"}


def test_supabase_email_alerts_are_filtered_and_upserted_by_owner(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        FakeResponse(
            [
                {
                    "owner": "ddriu",
                    "email": "ddriu@example.com",
                    "enabled": True,
                    "alert_buy": True,
                    "alert_reduce": True,
                    "alert_sell": True,
                    "include_group": True,
                    "minimum_buy_score": 65,
                    "only_changes": True,
                    "updated_at": "2026-07-29T08:00:00Z",
                }
            ]
        ),
        FakeResponse(None, status_code=204),
        FakeResponse([]),
        FakeResponse(None, status_code=204),
    ]

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr("src.supabase_journal.requests.request", fake_request)
    journal = SupabaseTradingJournal(
        "https://example.supabase.co",
        "sb_secret_test",
        "ddriu",
    )

    preferences = journal.get_alert_preferences()
    journal.save_alert_preferences(
        normalize_alert_preferences(
            owner="ddriu",
            email="ddriu@example.com",
            enabled=True,
        )
    )
    states = journal.list_alert_states()
    journal.upsert_alert_states(
        [
            AlertState(
                owner="ddriu",
                ticker="TSM",
                signature="neutral",
                entry_score=50,
                entry_label="Esperar",
                position_label="Mantener",
                price=150,
                evaluated_at="2026-07-29T08:00:00Z",
            )
        ]
    )

    assert preferences.enabled is True
    assert states.empty
    assert calls[0]["params"]["owner"] == "eq.ddriu"
    assert calls[1]["json"]["owner"] == "ddriu"
    assert calls[2]["params"]["owner"] == "eq.ddriu"
    assert calls[3]["json"][0]["owner"] == "ddriu"
    assert all(
        call["url"].endswith(
            "/rest/v1/email_alert_preferences"
            if index < 2
            else "/rest/v1/email_alert_states"
        )
        for index, call in enumerate(calls)
    )
