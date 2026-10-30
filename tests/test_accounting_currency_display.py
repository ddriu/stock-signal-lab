"""Financial amount labels cannot borrow the ADR/listing quote currency."""
import pytest

from app import _accounting_metric_currency


@pytest.mark.parametrize("info, expected", [
    ({"currency": "USD", "financialCurrency": "JPY"}, "JPY"),
    ({"currency": "USD"}, ""),
    ({"currency": "USD", "financialCurrency": "unverified"}, ""),
    ({"currency": "USD", "financialCurrency": "JPY", "_metric_periods": {
        "freeCashflow": {"components": [{"unit": "CHF"}]}}}, "CHF"),
    ({"currency": "USD", "financialCurrency": "JPY", "_metric_periods": {
        "freeCashflow": {"components": [{"unit": "USD"}, {"unit": "EUR"}]}}}, ""),
    ({"currency": "USD", "_metric_periods": {
        "freeCashflow": {"components": [{"unit": "JPY"}, {"unit": "JPY"}]}}}, "JPY"),
])
def test_accounting_amount_unit_is_declared_not_guessed(info, expected):
    assert _accounting_metric_currency(info, "freeCashflow") == expected
