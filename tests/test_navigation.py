from src.navigation import (
    analysis_refresh_tickers,
    growth_radar_ticker_groups,
    merge_analysis_ticker_sources,
    sanitize_favorite_selection,
)


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
