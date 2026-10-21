from streamlit.testing.v1 import AppTest


SCRIPT = '''
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
from test_model_results_service import inputs
from src.model_results_ui import render_model_results_page
import src.strategy_audit_service as market

account = st.radio("Cuenta sintética", ["A", "B"], key="account")
scenario = SCENARIO
class Journal:
    owner = account + "@example.invalid"
    def list_analysis_snapshots(self):
        st.session_state["reads"] = st.session_state.get("reads", 0) + 1
        if scenario == "load_error":
            raise RuntimeError("private@example.invalid")
        return inputs()[0]
    def __getattr__(self, name):
        raise AssertionError("Unexpected journal/account mutation: " + name)

def fetch(tickers, start, end, **kwargs):
    st.session_state["downloads"] = st.session_state.get("downloads", 0) + 1
    st.session_state["tickers"] = list(tickers)
    if scenario == "download_error":
        raise RuntimeError("private@example.invalid")
    return SimpleNamespace(bars=inputs()[1], issues=pd.DataFrame(), manifest={"version":"synthetic"})

with patch.object(market, "fetch_audit_market_bundle", side_effect=fetch):
    render_model_results_page(Journal(), git_sha="synthetic")
'''


def page(scenario="complete"):
    result = AppTest.from_string(SCRIPT.replace("SCENARIO", repr(scenario)), default_timeout=30).run()
    assert not result.exception
    return result


def button(app, label):
    return next(item for item in app.button if item.label == label)


def text(app):
    return " ".join(str(item.value) for kind in ("markdown", "caption", "info", "warning", "error", "json", "dataframe")
                    for item in getattr(app, kind, []))


def test_open_is_inert_and_no_prospective_or_real_execution_claims():
    app = page()
    assert "reads" not in app.session_state
    assert "downloads" not in app.session_state
    assert "prospectivo no activado" in text(app)
    assert "private@example.invalid" not in text(app)


def test_load_fetch_calculate_are_explicit_and_all_sections_render():
    app = page()
    button(app, "Cargar historial de modelos").click().run()
    assert not app.exception
    assert app.session_state["reads"] == 1
    assert "downloads" not in app.session_state
    assert button(app, "Calcular los cuatro modelos en ambas modalidades").disabled
    next(item for item in app.date_input if item.label == "Hasta (día UTC completo)").set_value("2025-01-13").run()
    button(app, "Descargar precios de modelos e índices").click().run()
    assert not app.exception
    assert app.session_state["downloads"] == 1
    assert set(app.session_state["tickers"]) == {"SYNTHETIC", "SPY", "QQQ", "VT"}
    button(app, "Calcular los cuatro modelos en ambas modalidades").click().run()
    assert not app.exception
    assert any("Patrimonio USD" in table.value.columns for table in app.dataframe)
    assert "Referencia de salidas originales" in text(app)
    assert "private@example.invalid" not in text(app)
    assert button(app, "Preparar Excel privado de modelos")
    app.run()
    assert app.session_state["reads"] == 1
    assert app.session_state["downloads"] == 1


def test_owner_switch_cannot_reuse_previous_history_or_results():
    app = page()
    button(app, "Cargar historial de modelos").click().run()
    next(item for item in app.radio if item.label == "Cuenta sintética").set_value("B").run()
    assert not app.exception
    assert "Carga el historial para empezar" in text(app)
    assert "Calcular los cuatro modelos en ambas modalidades" not in [item.label for item in app.button]


def test_growth_without_policy_does_not_substitute_technical_source():
    app = page()
    button(app, "Cargar historial de modelos").click().run()
    next(item for item in app.selectbox if item.label == "Fuente independiente de señales").set_value("growth").run()
    assert not app.exception
    assert "no se sustituye por la otra" in text(app)
    assert "downloads" not in app.session_state


def test_errors_hide_private_details_and_do_not_mutate_journal():
    app = page("load_error")
    button(app, "Cargar historial de modelos").click().run()
    assert not app.exception
    assert "No se pudo cargar" in text(app)
    assert "private@example.invalid" not in text(app)
