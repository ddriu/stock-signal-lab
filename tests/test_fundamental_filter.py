from src.fundamental_filter import evaluate_fundamental_filter


def test_fundamental_filter_scores_available_checks_and_reports_coverage() -> None:
    result = evaluate_fundamental_filter(
        {
            "sector": "Technology",
            "forwardPE": 24,
            "returnOnInvestedCapital": 0.16,
            "debtToEquity": 42,
            "epsGrowthCagr": 0.14,
            "returnOnEquity": 0.22,
            "operatingMargins": 0.18,
            "grossMargins": 0.55,
        },
        "GOOD",
    )

    assert result.score == 100
    assert result.coverage_pct == 100
    assert result.passed == 7
    assert result.label == "Fundamentos sólidos"


def test_fundamental_filter_does_not_treat_missing_values_as_zero() -> None:
    result = evaluate_fundamental_filter(
        {"sector": "Industrials", "forwardPE": 18, "returnOnEquity": 0.20},
        "PARTIAL",
    )

    assert result.score is None
    assert result.coverage_pct < 50
    assert any(check.status == "Sin datos" for check in result.checks)


def test_financial_sector_marks_debt_roic_and_gross_margin_not_comparable() -> None:
    result = evaluate_fundamental_filter(
        {
            "sector": "Financial Services",
            "forwardPE": 14,
            "earningsGrowth": 0.12,
            "returnOnEquity": 0.18,
            "operatingMargins": 0.20,
        },
        "BANK",
    )

    statuses = {check.key: check.status for check in result.checks}
    assert statuses["debt_to_equity"] == "No comparable"
    assert statuses["roic"] == "No comparable"
    assert statuses["gross_margin"] == "No comparable"
    assert result.score == 100

