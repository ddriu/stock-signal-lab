from types import SimpleNamespace

from src.entry_opportunity import STATUS_BUYABLE, STATUS_WAIT_PRICE
from src.speculative import (
    assess_speculative_candidate,
    discover_speculative_candidates,
)


def _screen_response() -> dict[str, object]:
    return {
        "quotes": [
            {
                "symbol": "GOOD",
                "shortName": "Good Small Cap",
                "exchange": "NMS",
                "quoteType": "EQUITY",
                "regularMarketPrice": 5.0,
                "marketCap": 800_000_000,
                "averageDailyVolume3Month": 1_000_000,
                "regularMarketChangePercent": 4.0,
            },
            {
                "symbol": "OTC",
                "exchange": "PNK",
                "quoteType": "EQUITY",
                "regularMarketPrice": 3.0,
                "marketCap": 500_000_000,
                "averageDailyVolume3Month": 2_000_000,
            },
            {
                "symbol": "ILLIQUID",
                "exchange": "NYQ",
                "quoteType": "EQUITY",
                "regularMarketPrice": 2.0,
                "marketCap": 500_000_000,
                "averageDailyVolume3Month": 300_000,
            },
        ]
    }


def _opportunity(status: str = STATUS_BUYABLE, daily_return: float = 2.0):
    return SimpleNamespace(
        ticker="GOOD",
        status_code=status,
        price=5.0,
        confidence_pct=80,
        opportunity_score=82,
        timing=SimpleNamespace(score=75, return_1d_pct=daily_return),
        zones=SimpleNamespace(risk_reward=2.5),
    )


def test_discovery_excludes_otc_and_insufficient_turnover() -> None:
    candidates = discover_speculative_candidates(
        screener=_screen_response,
        limit=10,
    )

    assert [candidate.ticker for candidate in candidates] == ["GOOD"]


def test_speculative_candidate_requires_full_buyable_status() -> None:
    discovery = discover_speculative_candidates(
        screener=_screen_response,
        limit=1,
    )[0]
    risk = SimpleNamespace(average_turnover_20d=8_000_000)
    info = {
        "marketCap": 800_000_000,
        "exchange": "NMS",
        "freeCashflow": 5_000_000,
    }

    accepted = assess_speculative_candidate(
        _opportunity(), risk, info, discovery
    )
    waiting = assess_speculative_candidate(
        _opportunity(STATUS_WAIT_PRICE), risk, info, discovery
    )

    assert accepted.eligible is True
    assert waiting.eligible is False
    assert "no supera todos los filtros" in " ".join(waiting.reasons)


def test_speculative_candidate_rejects_an_explosive_daily_jump() -> None:
    risk = SimpleNamespace(average_turnover_20d=8_000_000)
    result = assess_speculative_candidate(
        _opportunity(daily_return=24.0),
        risk,
        {"marketCap": 800_000_000, "exchange": "NYQ"},
    )

    assert result.eligible is False
    assert "explosiva" in " ".join(result.reasons)
