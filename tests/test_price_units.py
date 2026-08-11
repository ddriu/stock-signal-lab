import pandas as pd
import pytest

from src.price_units import format_quote_price, normalize_price_frame_units, resolve_quote_unit


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [1_750.0, 1_780.0],
            "high": [1_790.0, 1_820.0],
            "low": [1_730.0, 1_760.0],
            "close": [1_780.0, 1_792.0],
            "volume": [5_000_000, 6_000_000],
        },
        index=pd.date_range("2026-08-07", periods=2, freq="B"),
    )


def test_gbp_pence_are_normalized_once_without_changing_volume() -> None:
    raw = _frame()
    raw.attrs["provider"] = "Yahoo Finance"

    normalized = normalize_price_frame_units(raw, "GBp")
    normalized_again = normalize_price_frame_units(normalized, "GBp")

    assert normalized["close"].iloc[-1] == pytest.approx(17.92)
    assert normalized["volume"].tolist() == raw["volume"].tolist()
    assert normalized.attrs["display_currency"] == "GBP"
    assert normalized.attrs["price_scale"] == pytest.approx(0.01)
    assert normalized.attrs["provider"] == "Yahoo Finance"
    assert normalized_again["close"].iloc[-1] == pytest.approx(17.92)


def test_main_currency_is_not_scaled() -> None:
    normalized = normalize_price_frame_units(_frame(), "GBP")

    assert normalized["close"].iloc[-1] == pytest.approx(1_792.0)
    assert resolve_quote_unit("GBP").price_scale == 1.0


def test_price_format_uses_human_currency() -> None:
    assert format_quote_price(17.66, "GBP") == "£17.66"
    assert format_quote_price(17.66, "GBp") == "£17.66"

