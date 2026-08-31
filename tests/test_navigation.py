from datetime import date

import pandas as pd

from src.navigation import (
    analysis_refresh_tickers,
    direct_ticker_from_query,
    growth_radar_ticker_groups,
    market_data_freshness_rows,
    merge_analysis_ticker_sources,
    next_daily_review_batch,
    sanitize_favorite_selection,
)


def test_market_data_freshness_separates_market_date_from_download_time() -> None:
    frame = pd.DataFrame(
        {"close": [62.96, 174.38]},
        index=pd.to_datetime(["2026-08-18", "2026-08-19"]),
    )
    frame.attrs["provider"] = "Yahoo Finance"
    frame.attrs["fetched_at_utc"] = "2026-08-20T00:15:00+00:00"

    rows = market_data_freshness_rows({"mrna": frame}, today=date(2026, 8, 20))

    assert rows == [
        {
            "Ticker": "MRNA",
            "Última vela": date(2026, 8, 19),
            "Descargado": "20/08/2026 00:15 UTC",
            "Proveedor": "Yahoo Finance",
            "Estado": "Reciente",
        }
    ]


def test_direct_ticker_query_accepts_international_symbols() -> None:
    assert direct_ticker_from_query(" anet ") == "ANET"
    assert direct_ticker_from_query("san.mc") == "SAN.MC"
    assert direct_ticker_from_query("025560.ks") == "025560.KS"
    assert direct_ticker_from_query("BRK-B") == "BRK-B"


def test_direct_ticker_query_does_not_treat_company_name_as_symbol() -> None:
    assert direct_ticker_from_query("BAE Systems") is None
    assert direct_ticker_from_query("") is None
    assert direct_ticker_from_query("AAPL / MSFT") is None


def test_temporary_ticker_cannot_break_favorite_multiselect() -> None:
    assert sanitize_favorite_selection(
        ["aapl", "XE", "AAPL"],
        ["AAPL", "MSFT"],
    ) == ["AAPL"]


def test_direct_ticker_remains_in_analysis_refresh() -> None:
    assert analysis_refresh_tickers(
        ["AAPL"],
        ["NKE"],
        pending_ticker=" xe ",
        active_ticker="XE",
    ) == ["XE", "AAPL", "NKE"]


def test_analysis_picker_merges_favorites_history_and_recent_views() -> None:
    assert merge_analysis_ticker_sources(
        ["AAPL", " ma "],
        ["MA", "RTX", None],
        ["aapl", "HALO"],
    ) == ["AAPL", "MA", "RTX", "HALO"]


def test_daily_review_batches_cover_every_favorite_without_repeating() -> None:
    universe = [f"TICKER{index:03d}" for index in range(107)]
    attempted: list[str] = []
    batches: list[list[str]] = []

    while batch := next_daily_review_batch(universe, attempted, limit=25):
        batches.append(batch)
        attempted.extend(batch)

    assert [len(batch) for batch in batches] == [25, 25, 25, 25, 7]
    assert attempted == universe
    assert len(set(attempted)) == len(universe)


def test_growth_radar_groups_keep_order_and_classify_readings() -> None:
    groups = growth_radar_ticker_groups(
        [
            ("ma", "Entrada fuerte"),
            ("RTX", "Vigilancia activa"),
            ("halo", "Pendiente de fundamentales"),
            ("MA", "Entrada candidata"),
            ("ANET", "Entrada candidata"),
            ("NKE", "Sin entrada"),
        ]
    )

    assert groups == {
        "all": ["MA", "RTX", "HALO", "ANET", "NKE"],
        "strong": ["MA"],
        "candidates": ["ANET"],
        "watch": ["RTX"],
        "pending": ["HALO"],
    }
