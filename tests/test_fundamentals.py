from src.fundamentals import evaluate_fundamentals


def test_strong_complete_company_scores_100() -> None:
    result = evaluate_fundamentals(
        {
            "returnOnEquity": 0.25,
            "profitMargins": 0.22,
            "operatingMargins": 0.24,
            "revenueGrowth": 0.25,
            "earningsGrowth": 0.30,
            "debtToEquity": 40,
            "currentRatio": 1.8,
            "freeCashflow": 1_000_000,
            "country": "Example",
            "sector": "Technology",
        },
        "TEST",
    )
    assert result.score == 100
    assert result.coverage_pct == 100
    assert result.country == "Example"


def test_incomplete_fundamentals_return_no_score() -> None:
    result = evaluate_fundamentals({"returnOnEquity": 0.25}, "TEST")
    assert result.score is None
    assert result.coverage_pct == 15
