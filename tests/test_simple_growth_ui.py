"""Offline UI regression checks for the compact primary-analysis screen."""

import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app as application
from src.storage import JournalStorageError


SETUP = '''
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import app
from src.data_sources import FxSnapshot

today = pd.Timestamp.now(tz="Europe/Madrid").date()
recent = pd.Timestamp(today) - pd.offsets.BDay(1)
def result(ticker, **changes):
    values = dict(ticker=ticker, as_of=recent, label="Entrada fuerte", score=85,
        growth_score=84, momentum_score=90, context_score=75, confidence_pct=100,
        sector_key="technology", sector_label="Tecnología", price=100.0,
        atr_stop_pct=10.0, suggested_risk_pct=0.5, max_position_pct=7.0,
        is_small_cap=False, positive_factors=("Ingresos en crecimiento",),
        risk_factors=("Vigilar el riesgo",), manual_checks=("Revisar guía",))
    values.update(changes)
    return SimpleNamespace(**values)
def frame(end=recent):
    return pd.DataFrame(dict(close=[100.0]*220, sma_medium=[90.0]*220,
        sma_long=[80.0]*220), index=pd.bdate_range(end=end, periods=220))
'''


def _overview(*, unverified: bool = False, empty: bool = False) -> AppTest:
    script = SETUP + f'''
results = {{}} if {empty!r} else {{"NEW": result("NEW"), "HELD": result("HELD"),
    "WAIT": result("WAIT", label="Esperar mejor precio"),
    "OLD": result("OLD", as_of=recent-pd.Timedelta(days=30))}}
app.render_growth_candidate_overview(results, held_tickers=["HELD"] if not {empty!r} else [],
    universe=[] if {empty!r} else ["NEW", "HELD", "WAIT", "OLD", "IPO"],
    names={{"NEW": "Nueva empresa", "HELD": "Empresa actual"}}, currencies={{"NEW": "EUR"}},
    portfolio_snapshot=pd.DataFrame([dict(snapshot_date="2026-09-13",
        analysis_ticker="HELD", value_eur=100, asset_type="Acción")]),
    reference_date=today, holdings_verified={not unverified!r})
'''
    return AppTest.from_string(script, default_timeout=30).run()


def _page(*, scenario: str = "fresh", manual: bool = False) -> AppTest:
    script = SETUP + f'''
scenario = {scenario!r}
held = ["TEST"] if scenario == "invalidated" else []
snapshot = pd.DataFrame([dict(snapshot_date=str(today), platform="Test",
    asset_name="Test company", asset_type="Acción", analysis_ticker="TEST",
    value_eur=100.0, currency="EUR",
    comments="[[SSL_TESIS_INVALIDADA]] Fraude declarado")]) if held else pd.DataFrame()
reading = result("TEST")
if scenario == "stale":
    reading = result("TEST", as_of=recent-pd.Timedelta(days=30))
if scenario == "wait":
    reading = result("TEST", label="Esperar mejor precio")
results = {{"TEST": reading}}
prepared = {{"TEST": frame(reading.as_of)}}
if {manual!r}:
    held = ["TEST"]
    results["MANUAL"] = result("MANUAL")
    prepared["MANUAL"] = frame()
class Journal:
    def open_positions(self):
        if scenario == "unverified":
            raise app.JournalStorageError("Unavailable")
        return pd.DataFrame()
    def list_operations(self):
        return pd.DataFrame()
    def list_portfolio_snapshot_positions(self):
        return snapshot
    def add_analysis_snapshot(self, **kwargs):
        st.session_state["saved_observation"] = kwargs
fundamentals = {{key: dict(currency="EUR", revenueGrowth=.2, earningsGrowth=.3)
    for key in prepared}}
favorites = pd.DataFrame() if {manual!r} else pd.DataFrame([dict(ticker="TEST", name="Test company")])
with patch.object(app, "evaluate_growth_momentum", side_effect=lambda **kw: results[kw["ticker"]]):
    app.render_growth_momentum_page(prepared, fundamentals, {{}}, {{}}, {{}},
        FxSnapshot(today, {{"EUR": 1.0}}), Journal(), favorites, pd.DataFrame(), "test",
        portfolio_snapshot=snapshot, held_tickers=None if scenario == "unverified" else held)
'''
    return AppTest.from_string(script, default_timeout=30).run()


def test_one_table_has_three_filters_and_six_visible_columns() -> None:
    page = _overview()
    assert not page.exception
    assert page.radio[0].options == ["Nuevas entradas · 1", "Mi cartera · 1", "Esperar · 3"]
    assert page.dataframe[0].value["Empresa"].tolist() == ["Nueva empresa · NEW"]
    assert list(page.dataframe[0].value.columns) == [
        "Ticker", "Empresa", "En cartera / peso", "Qué revisar",
        "Entrada / stop orientativos", "Motivo", "Actualizado",
    ]
    assert json.loads(page.dataframe[0].proto.columns)["Ticker"]["hidden"]
    assert not page.toggle(key="growth_show_detail").value


def test_filters_separate_existing_positions_and_keep_missing_favorites() -> None:
    page = _overview()
    page.radio(key="growth_action_group").set_value("Mi cartera").run()
    assert not page.exception
    held = page.dataframe[0].value.iloc[0]
    assert held["En cartera / peso"] == "Sí · 100.0%"
    assert "Revisar ampliación" in held["Qué revisar"]
    assert any("2026-09-13" in item.value for item in page.caption)
    page.radio(key="growth_action_group").set_value("Esperar").run()
    assert not page.exception
    waiting = page.dataframe[0].value.set_index("Ticker")
    assert set(waiting.index) == {"WAIT", "OLD", "IPO"}
    assert waiting.loc["OLD", "Entrada / stop orientativos"] == "Pendiente de datos"
    assert waiting.loc["IPO", "Actualizado"] == "N/D"


def test_filter_tables_have_distinct_widget_identities() -> None:
    page = _overview()
    first_id = page.dataframe[0].proto.id
    page.radio(key="growth_action_group").set_value("Mi cartera").run()
    assert not page.exception
    assert first_id != page.dataframe[0].proto.id
    page.radio(key="growth_action_group").set_value("Nuevas entradas").run()
    assert not page.exception
    assert page.dataframe[0].proto.id == first_id


def test_unverified_holdings_are_not_reported_as_empty_or_new_entries() -> None:
    page = _overview(unverified=True)
    assert not page.exception
    assert page.radio[0].options == ["Nuevas entradas · 0", "Mi cartera · 1", "Esperar · 4"]
    page.radio(key="growth_action_group").set_value("Esperar").run()
    rows = page.dataframe[0].value
    assert set(rows["En cartera / peso"]) == {"N/D"}
    assert all("verificar cartera" in value.lower() for value in rows["Entrada / stop orientativos"])
    assert page.warning


def test_empty_overview_is_explanatory_and_safe() -> None:
    page = _overview(empty=True)
    assert not page.exception
    assert len(page.dataframe) == 0
    assert any("no significa" in item.value for item in page.info)


def test_main_page_keeps_configuration_collapsed_and_detail_opt_in() -> None:
    page = _page()
    assert not page.exception
    assert len(page.dataframe) == 1
    assert [(item.label, item.proto.expanded) for item in page.expander] == [
        ("Listas y cobertura de datos", False), ("Capital y riesgo (opcional)", False),
    ]
    assert not any(widget.label == "Empresa para preparar el plan" for widget in page.selectbox)
    assert not any(widget.label == "Guardar evaluación dinámica" for widget in page.button)
    assert "saved_observation" not in page.session_state
    page.toggle(key="growth_show_detail").set_value(True).run()
    assert not page.exception
    assert any(metric.label == "Entrada máxima este mes" for metric in page.metric)
    assert any(widget.label == "Guardar evaluación dinámica" for widget in page.button)
    assert "saved_observation" not in page.session_state


@pytest.mark.parametrize("scenario", ["stale", "invalidated", "wait", "unverified"])
def test_pending_or_invalidated_details_cannot_reenable_an_entry_plan(scenario: str) -> None:
    page = _page(scenario=scenario)
    assert not page.exception
    page.toggle(key="growth_show_detail").set_value(True).run()
    assert not page.exception
    assert not any(metric.label == "Entrada máxima este mes" for metric in page.metric)
    assert not any("Entrada fuerte" in item.value for item in page.success)
    assert any("No se propone tamaño" in item.value for item in page.info)
    assert any(widget.label == "Guardar evaluación dinámica" for widget in page.button)
    assert any(widget.label == "Revisar guía" for widget in page.checkbox)
    assert "saved_observation" not in page.session_state
    page.button(key="growth_save_TEST").click().run()
    assert not page.exception
    saved = page.session_state["saved_observation"]
    assert saved["position_label"] == "Sin plan de entrada"
    assert "observación sin plan" in saved["explanation"]


def test_sidebar_companies_remain_visible_without_favorites_when_holdings_exist() -> None:
    page = _page(manual=True)
    assert not page.exception
    assert page.radio(key="growth_action_group").options == [
        "Nuevas entradas · 1", "Mi cartera · 1", "Esperar · 0",
    ]
    assert page.dataframe[0].value["Ticker"].tolist() == ["MANUAL"]


def test_strict_holdings_lookup_exposes_storage_failure_without_breaking_legacy_default() -> None:
    class BrokenJournal:
        def open_positions(self):
            raise JournalStorageError("Unavailable")

    assert application._portfolio_tracking_tickers(BrokenJournal()) == []
    with pytest.raises(JournalStorageError):
        application._portfolio_tracking_tickers(BrokenJournal(), strict=True)
