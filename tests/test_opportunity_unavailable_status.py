"""Offline regressions: unavailable/partial analysis must not mean zero buys."""
from copy import deepcopy
from datetime import date

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.opportunity_status import measured_count_label, opportunity_coverage


DAY = date(2026, 11, 4)


def row(ticker="TEST", **updates):
    return {"Ticker": ticker, "Comprobación": "Actualizado en esta sesión",
            "Momento entrada": 80, "Lectura entrada": "Entrada fuerte",
            "Fecha": "2026-11-03", **updates}


@pytest.mark.parametrize("bad_score", [None, float("nan"), float("inf"), True, -1, 101])
def test_failed_rows_do_not_become_measured_zero(bad_score):
    status = opportunity_coverage([row(**{"Momento entrada": bad_score})], reference_date=DAY)
    assert status["status"] == "unavailable"
    assert measured_count_label(status["strong"], available=status["available"], total=status["total"]) == "N/D"


@pytest.mark.parametrize("stamp", [None, "not-a-date", "2026-11-05", "2026-10-15"])
def test_undated_future_or_stale_recalculation_is_not_current(stamp):
    status = opportunity_coverage([row(Fecha=stamp)], reference_date=DAY)
    assert status["available"] == 0
    assert status["pending"] == 1


def test_saved_strong_entry_is_preserved_but_not_counted_as_current():
    rows = [row(**{"Comprobación": "Pendiente de actualizar"})]
    original = deepcopy(rows)
    status = opportunity_coverage(rows, reference_date=DAY)
    assert status["status"] == "unavailable"
    assert rows == original
    assert rows[0]["Lectura entrada"] == "Entrada fuerte"


def test_partial_positive_is_lower_bound_and_partial_zero_unknown():
    status = opportunity_coverage([row(), row("OTHER", **{"Momento entrada": None})], reference_date=DAY)
    assert status["status"] == "partial"
    assert status["available"] == 1
    assert measured_count_label(status["strong"], available=1, total=2) == "≥ 1"
    assert measured_count_label(status["candidate"], available=1, total=2) == "N/D"


def test_complete_review_can_honestly_measure_zero_and_valid_zero_score():
    status = opportunity_coverage([row(**{"Momento entrada": 0, "Lectura entrada": "Vigilancia"})], reference_date=DAY)
    assert status["status"] == "complete"
    assert status["available"] == status["waiting"] == 1
    assert measured_count_label(status["strong"], available=1, total=1) == "0"
    assert status["newest_date"] == date(2026, 11, 3)


def test_empty_catalog_is_not_a_measured_zero():
    assert opportunity_coverage([], reference_date=DAY)["status"] == "unavailable"
    assert measured_count_label(0, available=0, total=0) == "N/D"


def radar_page(scenario):
    script = f'''
import pandas as pd
import streamlit as st
from app import render_opportunities_page
from config import StrategyConfig, BacktestConfig

scenario = {scenario!r}
recent = (pd.Timestamp.now(tz="Europe/Madrid") - pd.offsets.BDay(1)).date()
class Journal:
    def list_analysis_snapshots(self):
        return pd.DataFrame([dict(ticker="OLD", analyzed_at="2026-07-01", price=50,
            opportunity_score=85, company_score=80, entry_score=90,
            opportunity_label="Candidata", entry_label="Entrada fuerte",
            position_label="Mantener")])
    def open_positions(self):
        return pd.DataFrame()
summary = []
favorites = ["OLD", "NEW"]
if scenario in {{"partial", "complete", "stale", "error"}}:
    summary = [{{"Ticker": "NEW", "Oportunidad": 85, "Momento entrada": 85,
        "Calidad empresa": 80, "Lectura entrada": "Entrada fuerte",
        "Lectura conjunta": "Candidata", "Si ya la tienes": "Mantener",
        "Fecha": recent}}]
if scenario == "complete":
    favorites = ["NEW"]
    summary[0]["Lectura entrada"] = "Vigilancia"
if scenario == "stale":
    summary[0]["Fecha"] = "2026-07-01"
if scenario == "error":
    summary = [{{"Ticker": "NEW", "Estado": "Sin señal: histórico insuficiente"}}]
render_opportunities_page({{}}, {{}}, summary, StrategyConfig(), BacktestConfig(),
    {{}}, {{}}, {{}}, {{}}, {{}}, {{}}, {{}}, Journal(), favorites, include_company_detail=False)
'''
    return AppTest.from_string(script, default_timeout=20).run()


@pytest.mark.parametrize("scenario", ["unavailable", "stale", "error"])
def test_radar_ui_keeps_saved_rows_and_does_not_report_zero(scenario):
    page = radar_page(scenario)
    assert not page.exception
    metrics = {metric.label: metric.value for metric in page.metric}
    assert metrics["Entradas fuertes"] == metrics["Candidatas"] == "N/D"
    assert metrics["Con lectura reciente"] == "0"
    assert any("no significa cero" in warning.value for warning in page.warning)
    table = page.dataframe[0].value
    assert "OLD" in table["Ticker"].values
    assert "Pendiente de actualizar" in table["Comprobación"].values
    if scenario == "stale":
        historical = table.loc[table["Ticker"] == "NEW"].iloc[0]
        assert historical["Comprobación"] == "Datos históricos; pendiente de actualizar"
        assert historical["Fecha"] == "2026-07-01"
    if scenario == "error":
        assert table.loc[table["Ticker"] == "NEW", "Comprobación"].iloc[0] == "Lectura no disponible"
    assert any("Entradas fuertes · N/D" in option for option in page.segmented_control[0].options)


def test_radar_partial_ui_states_coverage_price_date_and_lower_bound():
    page = radar_page("partial")
    assert not page.exception
    metrics = {metric.label: metric.value for metric in page.metric}
    assert metrics["Entradas fuertes"] == "≥ 1"
    assert metrics["Candidatas"] == "N/D"
    assert metrics["Pendientes de revisar"] == "1"
    assert any("Revisión parcial: 1/2" in warning.value for warning in page.warning)
    assert any("Fechas de precio utilizadas" in caption.value for caption in page.caption)


def test_radar_complete_ui_can_display_true_zero():
    page = radar_page("complete")
    assert not page.exception
    metrics = {metric.label: metric.value for metric in page.metric}
    assert metrics["Entradas fuertes"] == "0"
    assert metrics["Candidatas"] == "0"
    assert not page.warning


def test_empty_growth_ui_marks_entry_counts_as_unavailable():
    script = '''
from app import render_growth_candidate_overview
render_growth_candidate_overview({}, universe=["NEW"], held_tickers=[])
'''
    page = AppTest.from_string(script, default_timeout=20).run()
    assert not page.exception
    assert "Entradas fuertes · N/D" in page.radio[0].options
    assert "Candidatas · N/D" in page.radio[0].options
    assert any("no cero oportunidades" in warning.value for warning in page.warning)
