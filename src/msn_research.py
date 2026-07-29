"""Enlaces y guía para contrastar manualmente una empresa en MSN Dinero.

MSN Dinero no publica una API gratuita y estable para reutilizar sus datos.
La aplicación abre la ficha mediante una búsqueda acotada, pero no extrae ni
redistribuye automáticamente los datos de LSEG mostrados por Microsoft.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus


MSN_MONEY_URL = "https://www.msn.com/es-es/dinero"
MSN_DATA_DISCLAIMER_URL = (
    "https://assets.msn.com/staticsb/statics/latest/finance/financedocs/es-ES/indexR.html"
)


@dataclass(frozen=True)
class MsnResearchLinks:
    ticker: str
    search_url: str
    money_url: str = MSN_MONEY_URL
    disclaimer_url: str = MSN_DATA_DISCLAIMER_URL


def build_msn_research_links(ticker: str) -> MsnResearchLinks:
    """Crea enlaces seguros sin depender de identificadores internos de MSN."""

    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("El ticker no puede estar vacío.")
    query = quote_plus(
        f'site:microsoftstart.msn.com/es-es/dinero "{symbol}" "MSN Dinero"'
    )
    return MsnResearchLinks(
        ticker=symbol,
        search_url=f"https://www.bing.com/search?q={query}",
    )

