from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.supabase_journal import SupabaseTradingJournal


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
