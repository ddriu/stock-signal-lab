from streamlit.testing.v1 import AppTest

from src.conviction import (
    STATUS_NOT_APPLICABLE,
    evaluate_conviction,
    summarize_conviction,
)


def strong_technology_company() -> dict[str, object]:
    return {
        "symbol": "GOOD",
        "sector": "Technology",
        "marketCap": 10_000_000_000,
        "totalCash": 2_000_000_000,
        "forwardPE": 80,
        "priceToBook": 12,
        "operatingMargins": 0.20,
        "grossMargins": 0.60,
        "returnOnInvestedCapital": 0.25,
        "operatingCashflow": 1_000_000_000,
        "capitalExpenditures": 100_000_000,
        "debtToEquity": 20,
        "revenueGrowth": 0.15,
        "earningsGrowth": 0.18,
    }


def test_conviction_keeps_valuation_and_entry_out_of_company_score() -> None:
    expensive_bad_entry = evaluate_conviction(
        strong_technology_company(),
        "GOOD",
        entry_score=20,
    )
    cheaper_good_entry = evaluate_conviction(
        {
            **strong_technology_company(),
            "forwardPE": 15,
            "priceToBook": 2,
        },
        "GOOD",
        entry_score=90,
    )

    assert len(expensive_bad_entry.checks) == 22
    assert expensive_bad_entry.automatic_score == 100
    assert cheaper_good_entry.automatic_score == 100
    context = {
        check.key: check.counts_for_score for check in expensive_bad_entry.checks
    }
    assert context["pe_context"] is False
    assert context["book_support"] is False
    assert context["entry_now"] is False
    assert context["institutional_capacity"] is False


def test_missing_metrics_reduce_coverage_instead_of_becoming_zero() -> None:
    result = evaluate_conviction({"sector": "Industrials"}, "EMPTY")

    assert result.automatic_score is None
    assert result.automatic_coverage_pct == 0
    assert any(check.status == "Sin datos" for check in result.checks)


def test_financial_profile_marks_non_comparable_ratios_without_penalty() -> None:
    result = evaluate_conviction(
        {
            "sector": "Financial Services",
            "marketCap": 50_000_000_000,
            "totalCash": 15_000_000_000,
            "operatingMargins": 0.30,
            "grossMargins": 0.70,
            "returnOnInvestedCapital": 0.30,
            "debtToEquity": 350,
        },
        "BANK",
    )
    statuses = {check.key: check.status for check in result.checks}

    assert statuses["cash_buffer"] == STATUS_NOT_APPLICABLE
    assert statuses["double_digit_margin"] == STATUS_NOT_APPLICABLE
    assert statuses["high_gross_margin"] == STATUS_NOT_APPLICABLE
    assert statuses["roic"] == STATUS_NOT_APPLICABLE
    assert statuses["long_term_debt"] == STATUS_NOT_APPLICABLE


def test_manual_evidence_produces_combined_score_only_with_enough_answers() -> None:
    result = evaluate_conviction(strong_technology_company(), "GOOD", entry_score=70)
    empty = summarize_conviction(result, {})
    answers = {
        check.key: "Sí"
        for check in result.checks
        if not check.automatic and check.counts_for_score
    }
    complete = summarize_conviction(result, answers)

    assert empty.manual_score is None
    assert empty.combined_score is None
    assert complete.manual_score == 100
    assert complete.manual_coverage_pct == 100
    assert complete.combined_score == 100


def test_conviction_ui_exposes_full_checklist_and_manual_form() -> None:
    script = '''
from app import render_conviction_analysis

render_conviction_analysis(
    "GOOD",
    {
        "sector": "Technology", "marketCap": 10_000_000_000,
        "totalCash": 2_000_000_000, "operatingMargins": .20,
        "grossMargins": .60, "returnOnInvestedCapital": .25,
        "operatingCashflow": 1_000_000_000, "capitalExpenditures": 100_000_000,
        "debtToEquity": 20, "revenueGrowth": .15, "earningsGrowth": .18,
    },
    entry_score=70,
)
'''
    app = AppTest.from_string(script, default_timeout=20).run()

    assert not app.exception
    assert not app.error
    assert any("Checklist completo: 22 preguntas" in item.value for item in app.caption)
    assert any(button.label == "Guardar mi revisión" for button in app.button)
