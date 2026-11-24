"""ISIN identifiers are not executable US symbols or calendar observations."""

import pandas as pd
import pytest

from src.analysis_policy import encode_policy_note
from src.arena_data_quality import build_arena_quality_manifest
from src.daily_digest import _calendar_name, build_daily_score_report
from src.instruments import resolve_analysis_ticker


ISIN = "IE00BM95B621"
DAY = "2026-11-23"
CLOCK = "2026-11-24T16:13:00Z"


def current_row(ticker="SYNTH"):
    return {"ticker": ticker, "session": DAY, "calendar": "XNYS",
        "score": 80, "growth_score": 70, "momentum_score": 80, "context_score": 90,
        "status": "entry_strong", "primary_data_status": "complete",
        "quality": {"block_entry": False, "status": "ok"},
        "snapshot_id": "a" * 64, "recorded_at": DAY + "T22:00:00Z",
        "price": 100.0, "price_session": DAY, "price_currency": "USD",
        "price_source": "analysis_snapshot", "price_identity_verified": True}


def manifest(favorites, rows):
    return build_arena_quality_manifest(rows, favorite_tickers=favorites,
        expected_session=DAY, reference_time=CLOCK)


@pytest.mark.parametrize("identifier", [ISIN, "US0378331005", "IE00BK5BQT80"])
def test_unmapped_isin_remains_an_identifier_without_a_fabricated_us_calendar(identifier):
    assert resolve_analysis_ticker(identifier) == identifier
    assert _calendar_name(identifier) == "no_verificado"


def test_isin_is_annotated_outside_scope_without_removing_it_from_favorites():
    favorites = [ISIN, "SYNTH"]
    checked = manifest(favorites, [current_row()])

    assert favorites == [ISIN, "SYNTH"]
    assert checked["favorite_tickers"] == sorted(favorites)
    assert checked["required_count"] == checked["eligible_count"] == checked["valid_count"] == 1
    assert checked["eligible_tickers"] == ["SYNTH"]
    assert checked["explicit_exclusions"] == []
    assert checked["excluded"] == [{"ticker": ISIN, "code": "outside_us_market_scope",
                                    "reason": "outside_us_market_scope"}]
    assert checked["errors"] == []
    assert checked["authorized"] is True


def test_isin_scope_fix_never_authorizes_an_incomplete_us_subset():
    checked = manifest([ISIN, "SYNTH", "MISSING"], [current_row()])

    assert checked["favorite_tickers"] == sorted([ISIN, "SYNTH", "MISSING"])
    assert checked["required_count"] == checked["eligible_count"] == 2
    assert checked["valid_count"] == 1
    assert checked["eligible_tickers"] == ["MISSING", "SYNTH"]
    assert checked["authorized"] is False
    assert checked["errors"] == [{"ticker": "MISSING", "code": "missing_analysis"}]


def test_even_a_purported_us_isin_row_cannot_supply_an_arena_instrument():
    checked = manifest([ISIN], [current_row(ISIN)])

    assert checked["favorite_tickers"] == [ISIN]
    assert checked["required_count"] == checked["eligible_count"] == checked["valid_count"] == 0
    assert checked["eligible_tickers"] == checked["evidence"] == []
    assert checked["authorized"] is False
    assert checked["errors"] == [{"ticker": None, "code": "no_eligible_instruments"}]


def test_report_does_not_reuse_legacy_us_score_or_price_for_an_unmapped_isin():
    policy = {"policy_version": "dual_strategy_v1", "snapshot_method": "dual",
        "ticker": ISIN, "as_of": DAY, "source": "synthetic", "price": 100.0,
        "currency": "USD", "config": {},
        "primary": {"id": "growth_momentum", "strategy_id": "growth_momentum", "score": 80,
            "growth_score": 70, "momentum_score": 80, "context_score": 90,
            "status": "Entrada fuerte", "data_status": "complete"},
        "comparative": {}, "position": {"invalidated": False},
        "data_quality": {"status": "ok", "block_entry": False, "reasons": []}}
    snapshots = pd.DataFrame([{"id": 1, "ticker": ISIN, "analyzed_at": DAY,
        "created_at": DAY + "T22:00:00Z", "price": 100.0,
        "note": encode_policy_note("synthetic legacy observation", policy)}])
    report = build_daily_score_report(snapshots, tickers=[ISIN], favorite_tickers=[ISIN],
        as_of=DAY, reference_time=CLOCK)

    assert report["rows"][0]["ticker"] == ISIN
    assert report["rows"][0]["calendar"] == "no_verificado"
    assert report["rows"][0]["score"] is None
    assert report["rows"][0]["price"] is None
    assert report["rows"][0]["history"] == []
    assert report["arena_quality"]["authorized"] is False


@pytest.mark.parametrize("ticker", ["AAPL", "XE", "SYNTH", "BRK-B", "BRK.B"])
def test_existing_us_symbols_keep_their_known_digest_calendar(ticker):
    assert _calendar_name(ticker) == "XNYS"


@pytest.mark.parametrize("identifier", [ISIN, "US0378331005", "  ie00bm95b621  "])
def test_central_isin_shape_check_does_not_guess_listing(identifier):
    from src.instruments import is_isin_identifier

    assert is_isin_identifier(identifier) is True


@pytest.mark.parametrize("ticker", ["AAPL", "XE", "BRK-B", "BRK.B", "IE00BM95B621.L", "", None])
def test_central_isin_shape_check_keeps_listing_symbols_distinct(ticker):
    from src.instruments import is_isin_identifier

    assert is_isin_identifier(ticker) is False
