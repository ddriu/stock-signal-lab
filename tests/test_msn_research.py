from urllib.parse import parse_qs, urlparse

import pytest

from src.msn_research import build_msn_research_links


def test_msn_links_use_a_scoped_search_without_internal_ids() -> None:
    links = build_msn_research_links(" msft ")
    query = parse_qs(urlparse(links.search_url).query)["q"][0]

    assert links.ticker == "MSFT"
    assert links.money_url == "https://www.msn.com/es-es/dinero"
    assert "site:microsoftstart.msn.com/es-es/dinero" in query
    assert '"MSFT"' in query
    assert links.disclaimer_url.startswith("https://assets.msn.com/")


def test_msn_links_reject_an_empty_ticker() -> None:
    with pytest.raises(ValueError, match="ticker"):
        build_msn_research_links("  ")
