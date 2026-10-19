"""Names use already-loaded metadata and never replace instrument identifiers."""

import pytest

from src.instruments import resolve_company_name


def test_company_name_prefers_full_metadata_name() -> None:
    assert resolve_company_name("NKE", {"shortName": "Nike", "longName": "NIKE, Inc."}) == "NIKE, Inc."


@pytest.mark.parametrize("placeholder", [None, "", "NKE", "nke", "NaN", "None", "<NA>", "N/D", float("nan")])
def test_company_name_ignores_placeholders_and_uses_next_source(placeholder: object) -> None:
    assert resolve_company_name("NKE", placeholder, {"asset_name": "NIKE, Inc."}) == "NIKE, Inc."


def test_company_name_uses_valid_name_after_placeholder_metadata() -> None:
    assert resolve_company_name("NKE", {"longName": "NKE", "shortName": "Nike"}) == "Nike"


def test_company_name_alias_fallback_keeps_canonical_ticker_and_listing() -> None:
    assert resolve_company_name("AMZ", "AMZ", "AMZN") == "AMZN"
    assert resolve_company_name("6758.T", None) == "6758.T"
    assert resolve_company_name("ORCL", "Oracle") == "Oracle"
    assert resolve_company_name("amazon", "Amazon") == "Amazon"
