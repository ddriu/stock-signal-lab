from datetime import datetime, timezone

import pandas as pd
import pytest

from src.external_analysts import historical_model_reading, normalize_call, public_source_url, revision_id, utc_timestamp


def call(**extra):
    return normalize_call({**dict(provider="perito_inversor", source_id="synthetic-forecast",
        company_name="Empresa sintética", ticker="DEMO", currency="EUR", analyst="Analista de prueba",
        received_at="2026-11-09T09:00:00Z", observed_at="2026-11-09T09:01:00Z",
        action="buy", direction="long", entry_rule="limit", entry=10, stop=9, target=12,
        source_url="https://www.peritoinversor.com/#/radar/synthetic"), **extra})


def test_only_supplied_facts_and_later_detection_no_false_return():
    c = call()
    assert c["published_at"] is c["exchange"] is None
    assert c["executable"] and c["state"] == "pending"
    assert c["reward_risk"] == 2 and c["risk_pct"] == pytest.approx(10)
    assert c["potential_return_pct"] == pytest.approx(20)
    unknown = normalize_call({**c, "ticker": None, "currency": None})
    assert not unknown["executable"] and unknown["state"] == "not_verifiable"
    assert unknown["ticker"] is None  # Never map a company name to an instrument.
    assert not call(source_status="target_reached")["executable"]


def test_revision_identity_changes_levels_not_repeated_receipts():
    a = call()
    b = normalize_call({**a, "received_at": "2026-11-10T09:00:00Z",
                        "observed_at": "2026-11-10T09:01:00Z"})
    assert a["id"] == b["id"] and revision_id(a) == revision_id(b)
    c = normalize_call({**b, "target": 13})
    assert c["id"] == a["id"] and revision_id(c) != revision_id(a)


def test_unresolved_expiry_future_publication_or_terminal_reference_never_executable():
    assert not call(evidence={"expires_date": "2026-11-10", "expires_precision": "day"})["executable"]
    assert not call(evidence={"published_date": "2026-11-11", "published_precision": "day"})["executable"]
    assert call(evidence={"published_date": "2026-11-09", "published_precision": "day"})["published_at"] is None
    assert not call(reference_price=12)["executable"]
    assert not call(reference_price=9)["executable"]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "1+1", "1e10000", "1e-10000"])
def test_bad_levels_never_become_trade(value):
    with pytest.raises(ValueError):
        call(entry=value)


@pytest.mark.parametrize("currency", ["GBp", "GBX"])
def test_quote_subunits_are_not_normalized_into_an_iso_currency(currency):
    with pytest.raises(ValueError, match="peniques"):
        call(currency=currency)


@pytest.mark.parametrize("value", ["2026-11-09", "2026-11-09T09:00:00", None])
def test_no_invented_timezone(value):
    with pytest.raises(ValueError):
        utc_timestamp(value)


@pytest.mark.parametrize("url", ["http://www.peritoinversor.com/", "https://evil.test/",
                               "https://peritoinversor.com@evil.test/", "https://127.0.0.1/",
                               "https://www.peritoinversor.com:81/"])
def test_links_never_call_private_or_non_provider_urls(url):
    with pytest.raises(ValueError):
        public_source_url(url, provider="perito_inversor")


def test_cross_check_reads_only_a_score_known_at_receipt_not_later_winner():
    c = call()
    snapshots = pd.DataFrame([
        dict(id=1, ticker="DEMO", analyzed_at="2026-11-06", created_at="2026-11-07T01:00:00Z",
             entry_score=42, opportunity_score=45, note=""),
        dict(id=2, ticker="DEMO", analyzed_at="2026-11-09", created_at="2026-11-09T20:00:00Z",
             entry_score=99, opportunity_score=99, note=""),
    ])
    result = historical_model_reading(c, snapshots)
    assert result["technical"] == 42 and result["radar"] == 45 and result["growth"] is None
    assert result["as_of"] == "2026-11-06"
    assert historical_model_reading({**c, "ticker": "UNKNOWN"}, snapshots)["technical"] is None
