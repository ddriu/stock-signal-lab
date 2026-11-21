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


def test_conflicting_quote_currency_is_not_silently_overridden():
    raw = _frame()
    raw.attrs["quote_currency"] = "USD"
    with pytest.raises(ValueError, match="moneda") as failed:
        normalize_price_frame_units(raw, "GBp")
    assert failed.value.code == "currency_mismatch"
    assert raw.close.iloc[-1] == 1792.


def test_source_pence_are_not_mistaken_for_a_display_pound_hint():
    raw = _frame()
    raw.attrs["quote_currency"] = "GBp"
    result = normalize_price_frame_units(raw, "GBP")
    assert result.close.iloc[-1] == pytest.approx(17.92)
    assert result.attrs["source_currency"] == "GBp"


def test_unknown_currency_is_not_certified_or_scaled():
    result = normalize_price_frame_units(_frame(), None)
    assert result.close.iloc[-1] == 1792.
    assert result.attrs["quote_units_normalized"] is False
    assert result.attrs["display_currency"] == ""
    assert result.attrs["price_scale"] is None
    with pytest.raises(ValueError) as failed:
        normalize_price_frame_units(_frame(), None, require_verified=True)
    assert failed.value.code == "currency_unverified"


def test_normalized_history_cannot_be_relabelled_in_another_currency():
    normalized = normalize_price_frame_units(_frame(), "GBP")
    with pytest.raises(ValueError):
        normalize_price_frame_units(normalized, "USD")


def test_old_normalized_marker_without_currency_is_not_proof():
    raw = _frame()
    raw.attrs["quote_units_normalized"] = True
    with pytest.raises(ValueError) as failed:
        normalize_price_frame_units(raw, None, require_verified=True)
    assert failed.value.code == "currency_unverified"

