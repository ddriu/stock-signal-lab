"""A partially collected report is never presented as a global zero result."""
from copy import deepcopy

import pandas as pd
from streamlit.testing.v1 import AppTest

from src.daily_digest import (
    build_daily_score_report, daily_score_overview, daily_score_reading,
    render_daily_score_email, seal_daily_score_report,
)


def row(ticker="TEST", *, score=60, label="Esperar", blocked=False):
    return {"ticker": ticker, "company_name": "Empresa " + ticker,
            "score": score, "label": label, "held": False,
            "status": "review_data" if blocked else "entry_strong" if label == "Entrada fuerte" else "wait",
            "session": "2026-11-03", "quality": {"block_entry": blocked, "reasons": []}}


def test_partial_7_of_155_qualifies_zero_as_only_among_observed_scores():
    rows = [row(str(i)) for i in range(7)] + [row(str(i), score=None, label="Sin dato de esta sesión", blocked=True) for i in range(7, 155)]
    before = deepcopy(rows)
    overview = daily_score_overview(rows)
    assert overview["status"] == "partial"
    assert "Revisión parcial: 7/155" in overview["text"]
    assert "no es un recuento completo del universo" in overview["text"]
    assert "Entre las 7 empresas con nota: 0 entradas fuertes" in overview["counts_text"]
    assert overview["coverage"]["missing_score"] == 148
    assert rows == before


def test_no_scores_means_unavailable_not_zero_entries():
    overview = daily_score_overview([row(score=None, label="Sin dato de esta sesión", blocked=True)])
    assert overview["status"] == "unavailable"
    assert "Entradas fuertes y candidatas: N/D" in overview["text"]
    assert "0 entradas fuertes" not in overview["text"]


def test_empty_report_is_unavailable_without_division_or_zero_candidates():
    overview = daily_score_overview([])
    assert overview["status"] == "unavailable"
    assert "no hay empresas incluidas" in overview["text"]
    assert "N/D" in overview["counts_text"]


def test_fully_observed_zero_is_allowed_without_converting_it_to_missing():
    overview = daily_score_overview([row(score=0)])
    assert overview["status"] == "complete"
    assert overview["coverage"]["with_score"] == 1
    assert "0 entradas fuertes con datos suficientes" in overview["counts_text"]


def test_verified_and_pending_strong_counts_keep_their_existing_meanings():
    overview = daily_score_overview([row("GOOD", score=85, label="Entrada fuerte"),
        row("PENDING", score=99, label="Entrada fuerte", blocked=True),
        row("MISSING", score=None, label="Sin dato de esta sesión", blocked=True)])
    assert overview["status"] == "partial"
    assert overview["coverage"]["strong_verified"] == 1
    assert overview["coverage"]["strong_pending"] == 1
    assert "1 entradas fuertes con datos suficientes" in overview["counts_text"]
    assert "1 señales fuertes del motor pendientes de verificar" in overview["counts_text"]


def test_legacy_missing_analysis_reason_does_not_blame_unassessed_fundamentals():
    missing = row(score=None, label="Sin dato de esta sesión", blocked=True)
    missing["quality"]["reasons"] = ["El análisis guardado no acredita la fecha de los fundamentales; revisar los datos antes de comprar."]
    report = seal_daily_score_report({"as_of": "2026-11-03", "rows": [missing]})
    before = deepcopy(report)
    reading = daily_score_reading(report["rows"][0])
    assert reading["blocked"]
    assert reading["reasons"][0].startswith("No hay análisis guardado para esta sesión")
    assert "no acredita la fecha" not in " ".join(reading["reasons"])
    assert report == before
    assert seal_daily_score_report(report)["report_id"] == report["report_id"]


def test_new_missing_session_report_uses_the_missing_analysis_reason():
    report = build_daily_score_report(pd.DataFrame(), tickers=["TEST"], as_of="2026-11-03", reference_time="2026-11-03T23:00:00Z")
    assert report["rows"][0]["quality"]["reasons"][0].startswith("No hay análisis guardado para esta sesión")


def test_email_uses_exact_shared_overview_and_never_changes_a_report_seal():
    rows = [row("CURRENT"), row("MISSING", score=None, label="Sin dato de esta sesión", blocked=True)]
    report = seal_daily_score_report({"as_of": "2026-11-03", "rows": rows})
    before = deepcopy(report)
    _, plain, html = render_daily_score_email(report)
    overview = daily_score_overview(rows)
    assert overview["text"] in plain and overview["text"] in html
    assert "Revisión parcial: 1/2" in plain
    assert "Entre las 1 empresas con nota" in plain
    assert report == before
    assert seal_daily_score_report(report)["report_id"] == report["report_id"]


def test_archived_partial_ui_and_email_share_coverage_without_network_or_writes():
    rows = [row("CURRENT"), row("MISSING", score=None, label="Sin dato de esta sesión", blocked=True)]
    script = '''
from copy import deepcopy
import streamlit as st
from src.daily_digest import seal_daily_score_report, render_daily_score_email
from src.daily_digest_ui import render_daily_score_page
rows = REPLACE_ROWS
report = seal_daily_score_report({"as_of": "2026-11-03", "calendar_status": "complete", "rows": rows})
before = deepcopy(report)
class Journal:
    def get_daily_score_report(self, identifier):
        assert identifier == report["report_id"]
        return report
    def __getattr__(self, name):
        raise AssertionError("Forbidden collection, account write or email: " + name)
render_daily_score_page(Journal(), report_id=report["report_id"])
assert report == before
assert seal_daily_score_report(report)["report_id"] == report["report_id"]
st.session_state["rendered_email"] = render_daily_score_email(report)[1]
'''.replace("REPLACE_ROWS", repr(rows))
    page = AppTest.from_string(script, default_timeout=30).run()
    assert not page.exception
    overview = daily_score_overview(rows)
    assert any(overview["coverage_text"] == item.value for item in page.warning)
    assert any(overview["counts_text"] == item.value for item in page.caption)
    assert overview["text"] in page.session_state["rendered_email"]
