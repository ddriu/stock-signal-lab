from src.navigation import analysis_refresh_tickers, sanitize_favorite_selection


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
