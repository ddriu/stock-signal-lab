from streamlit.testing.v1 import AppTest


def test_entry_opportunities_is_independent_from_existing_radar() -> None:
    from app import ANALYSIS_OPTIONS, LEGACY_ANALYSIS_ROUTES

    assert "Radar" in ANALYSIS_OPTIONS
    assert "Oportunidades" in ANALYSIS_OPTIONS
    assert "Oportunidades" not in LEGACY_ANALYSIS_ROUTES


def test_entry_opportunities_explains_when_prices_are_not_loaded() -> None:
    script = '''
from app import render_entry_opportunities_page
from config import StrategyConfig

render_entry_opportunities_page(
    {}, StrategyConfig(), {}, {}, {}, {}, {}, object(), [], {}
)
'''
    app = AppTest.from_string(script, default_timeout=15).run()

    assert not app.exception
    assert not app.error
    assert "no hay precios actualizados" in app.info[0].value.lower()
    assert any("small caps" in button.label.lower() for button in app.button)


def test_complete_review_requests_favorites_and_external_discovery() -> None:
    script = '''
import streamlit as st
from app import _request_complete_review

st.button("Revisar", on_click=_request_complete_review)
st.write(str(bool(st.session_state.get("_force_all_favorite_refresh"))))
st.write(str(bool(st.session_state.get("_pending_speculative_discovery"))))
'''
    app = AppTest.from_string(script, default_timeout=15).run()
    app.button[0].click().run()

    assert not app.exception
    assert [item.value for item in app.markdown[-2:]] == ["True", "True"]


def test_entry_opportunities_renders_scores_zones_and_table() -> None:
    script = '''
import numpy as np
import pandas as pd
from app import prepare_data, render_entry_opportunities_page
from config import StrategyConfig

index = pd.date_range("2025-01-02", periods=280, freq="B")
close = np.linspace(80, 120, len(index)) + np.sin(np.linspace(0, 12, len(index)))
raw = pd.DataFrame({
    "open": close * 0.998,
    "high": close * 1.01,
    "low": close * 0.99,
    "close": close,
    "volume": np.linspace(900_000, 1_300_000, len(index)),
}, index=index)
info = {
    "symbol": "TEST", "longName": "Test Company", "sector": "Technology",
    "country": "United States", "currency": "USD", "returnOnEquity": .18,
    "profitMargins": .15, "operatingMargins": .17, "revenueGrowth": .12,
    "earningsGrowth": .15, "debtToEquity": 45, "currentRatio": 1.6,
    "freeCashflow": 100, "marketCap": 1_000, "forwardPE": 18,
    "pegRatio": 1.2,
}
config = StrategyConfig()
(prepared, _, fundamentals, valuations, relatives, risks, _) = prepare_data(
    {"TEST": raw}, {"TEST": info}, {}, config
)

class Journal:
    def open_positions(self):
        return pd.DataFrame(columns=["ticker"])

render_entry_opportunities_page(
    prepared, config, fundamentals, valuations, relatives, risks,
    {"TEST": info}, Journal(), ["TEST"], {"TEST": "Test Company (TEST)"}
)
'''
    app = AppTest.from_string(script, default_timeout=20).run()

    assert not app.exception
    assert not app.error
    assert app.dataframe
    assert any("Tabla de decisión" in heading.value for heading in app.markdown)


def test_opportunities_lists_favorites_without_live_download() -> None:
    script = '''
import pandas as pd
from app import render_opportunities_page
from config import StrategyConfig, BacktestConfig

class Journal:
    def list_analysis_snapshots(self):
        return pd.DataFrame([{
            "ticker": "MA", "analyzed_at": "2026-08-01", "price": 575.0,
            "opportunity_score": 72, "company_score": 88, "entry_score": 64,
            "valuation_score": 60, "relative_score": 70, "risk_score": 55,
            "opportunity_label": "Candidata", "entry_label": "Vigilancia",
            "position_label": "Mantener",
        }])

render_opportunities_page(
    {}, {}, [], StrategyConfig(), BacktestConfig(), {}, {}, {}, {}, {}, {}, {},
    Journal(), ["MA", "RTX"],
)
'''
    app = AppTest.from_string(script, default_timeout=15).run()

    assert not app.exception
    assert not app.error
    assert len(app.dataframe) == 2
    assert "Tus favoritas ya están en el radar" in app.info[0].value


def test_favorite_picker_requests_analysis_without_mutating_selectbox_key() -> None:
    script = '''
import pandas as pd
import streamlit as st
from app import render_analysis_company_picker

class Journal:
    def list_analysis_snapshots(self):
        return pd.DataFrame()

st.session_state.setdefault("main_navigation", "Analizar")
st.session_state.setdefault("analysis_navigation", "Empresa")
st.segmented_control(
    "Navegación principal",
    ["Inicio", "Analizar"],
    key="main_navigation",
    required=True,
)
st.segmented_control(
    "Tipo de análisis",
    ["Radar", "Empresa"],
    key="analysis_navigation",
    required=True,
)
render_analysis_company_picker(
    ["MA", "RTX"],
    {"MA": "Mastercard (MA)", "RTX": "RTX (RTX)"},
    Journal(),
    {},
)
'''
    app = AppTest.from_string(script, default_timeout=15).run()
    app.selectbox[0].set_value("Mastercard (MA)").run()
    next(button for button in app.button if button.label == "Abrir").click().run()

    state = app.session_state.filtered_state
    assert not app.exception
    assert state.get("analysis_ticker") is None
    assert state["main_navigation"] == "Analizar"
    assert state["analysis_navigation"] == "Empresa"
    assert state["_requested_main_navigation"] == "Analizar"
    assert state["_requested_analysis_navigation"] == "Empresa"
    assert state["_requested_analysis_ticker"] == "MA"
    assert state["_pending_analysis_ticker"] == "MA"
