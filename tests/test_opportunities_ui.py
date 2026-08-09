from streamlit.testing.v1 import AppTest


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
