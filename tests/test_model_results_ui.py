from streamlit.testing.v1 import AppTest


SCRIPT = '''
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
from test_model_results_service import inputs, _growth_cohort_inputs
from src.model_results_ui import render_model_results_page, _execution_runtime
import src.strategy_audit_service as market
experiment = _execution_runtime()

account = st.radio("Cuenta sintética", ["A", "B"], key="account")
scenario = SCENARIO
class Journal:
    owner = account + "@example.invalid"
    def list_analysis_snapshots(self):
        st.session_state["reads"] = st.session_state.get("reads", 0) + 1
        if scenario == "load_error":
            raise RuntimeError("private@example.invalid")
        return _growth_cohort_inputs()[0] if scenario == "growth" else inputs()[0]
    def __getattr__(self, name):
        raise AssertionError("Unexpected journal/account mutation: " + name)

def fetch(tickers, start, end, **kwargs):
    st.session_state["downloads"] = st.session_state.get("downloads", 0) + 1
    st.session_state["tickers"] = list(tickers)
    if scenario == "download_error":
        raise RuntimeError("private@example.invalid")
    return SimpleNamespace(bars=inputs()[1], raw_prices=inputs()[1], metadata={},
        issues=pd.DataFrame(), manifest={"version":"synthetic"})

def compare(observed, bars, **kwargs):
    st.session_state["comparison_calls"] = st.session_state.get("comparison_calls", 0) + 1
    st.session_state["comparison_context"] = dict(source=observed.manifest["source"],
        cohort=observed.manifest.get("cohort", "raw"), start=kwargs["start"], end=kwargs["end"],
        budget=kwargs["budget_usd"], git=kwargs["git_sha"], fx=kwargs["fx_records"],
        matched=kwargs["include_matched_hold"])
    return SimpleNamespace(summary=pd.DataFrame([
        dict(variant=variant, model=model, executed_entries=1, final_value_usd=5990 if variant=="baseline" else 6000,
            profit_usd=-10 if variant=="baseline" else 0, net_return_pct=-.167 if variant=="baseline" else 0,
            coverage_complete=variant=="baseline")
        for variant in ("baseline", "liquidity_eur", "execution_setup") for model in "ABCD"]),
        decisions=pd.DataFrame([dict(status="blocked_data", ticker="SYNTHETIC", adv_eur=None)]),
        benchmarks=pd.DataFrame(),manifest=dict(limitations=["SYNTHETIC_EXPERIMENT_NOT_A_WINNER"]))

with (patch.object(market, "fetch_audit_market_bundle", side_effect=fetch),
      patch.object(experiment, "build_execution_comparison", side_effect=compare)):
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
    assert "comparison_calls" not in app.session_state
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


def test_technical_has_no_verified_selector_and_growth_has_explicit_separate_cohorts():
    app = page("growth")
    button(app, "Cargar historial de modelos").click().run()
    assert "Cohorte histórica" not in [item.label for item in app.selectbox]
    next(item for item in app.selectbox if item.label == "Fuente independiente de señales").set_value("growth").run()
    picker = next(item for item in app.selectbox if item.label == "Cohorte histórica")
    assert picker.value == "raw"
    assert picker.options == ["Motor bruto · referencia original", "Verificación guardada · cohorte separada"]
    picker.set_value("verified").run()
    assert not app.exception and "downloads" not in app.session_state
    next(item for item in app.selectbox if item.label == "Fuente independiente de señales").set_value("technical").run()
    assert not app.exception
    assert "Cohorte histórica" not in [item.label for item in app.selectbox]


def test_cohort_change_never_reuses_previous_results_as_current_or_triggers_downloads():
    app = page("growth")
    button(app, "Cargar historial de modelos").click().run()
    next(item for item in app.selectbox if item.label == "Fuente independiente de señales").set_value("growth").run()
    next(item for item in app.date_input if item.label == "Hasta (día UTC completo)").set_value("2025-01-13").run()
    button(app, "Descargar precios de modelos e índices").click().run()
    button(app, "Calcular los cuatro modelos en ambas modalidades").click().run()
    assert not app.exception
    from src.model_results_ui import _signature
    state_key = "model_results_" + _signature("A@example.invalid")[:16] + "_state"
    original = app.session_state[state_key]["report"].manifest["report_hash"]
    assert app.session_state[state_key]["run_key"] == app.session_state[state_key]["current_run_key"]
    next(item for item in app.selectbox if item.label == "Cohorte histórica").set_value("verified").run()
    assert not app.exception and app.session_state["downloads"] == 1
    assert app.session_state[state_key]["report"].manifest["report_hash"] == original
    assert app.session_state[state_key]["run_key"] != app.session_state[state_key]["current_run_key"]
    assert "resultados del cálculo anterior" in text(app)
    button(app, "Calcular los cuatro modelos en ambas modalidades").click().run()
    assert not app.exception
    manifest = app.session_state[state_key]["report"].manifest
    assert app.session_state[state_key]["run_key"] == app.session_state[state_key]["current_run_key"]
    assert manifest["cohort"] == "verified" and manifest["report_hash"] != original
    assert "primera observación" in text(app) and "no acredita un método ganador" in text(app)
    assert app.session_state["reads"] == app.session_state["downloads"] == 1


def test_switch_to_source_without_observations_invalidates_downstream_activation_readiness():
    from src.model_results_ui import _signature
    app = page()
    button(app, "Cargar historial de modelos").click().run()
    next(item for item in app.date_input if item.label == "Hasta (día UTC completo)").set_value("2025-01-13").run()
    button(app, "Descargar precios de modelos e índices").click().run()
    button(app, "Calcular los cuatro modelos en ambas modalidades").click().run()
    key = "model_results_" + _signature("A@example.invalid")[:16] + "_state"
    assert app.session_state[key]["report"].manifest["source"] == "technical"
    next(item for item in app.selectbox if item.label == "Fuente independiente de señales").set_value("growth").run()
    assert not app.exception
    assert app.session_state[key]["current_run_key"] is None
    assert app.session_state[key]["report"].manifest["source"] == "technical"
    assert app.session_state["reads"] == app.session_state["downloads"] == 1


def test_execution_comparison_requires_explicit_click_keeps_baseline_and_uses_current_cohort():
    app = page("growth")
    button(app, "Cargar historial de modelos").click().run()
    next(item for item in app.selectbox if item.label == "Fuente independiente de señales").set_value("growth").run()
    next(item for item in app.selectbox if item.label == "Cohorte histórica").set_value("verified").run()
    assert button(app, "Comparar filtros experimentales de ejecución").disabled
    next(item for item in app.date_input if item.label == "Hasta (día UTC completo)").set_value("2025-01-13").run()
    button(app, "Descargar precios de modelos e índices").click().run()
    assert "comparison_calls" not in app.session_state
    button(app, "Comparar filtros experimentales de ejecución").click().run()
    assert not app.exception and not app.error
    assert app.session_state["comparison_calls"] == app.session_state["downloads"] == app.session_state["reads"] == 1
    context = app.session_state["comparison_context"]
    assert context["source"] == "growth" and context["cohort"] == "verified"
    assert context["end"] == "2025-01-13" and context["budget"] == 6000
    assert context["matched"] is False and context["git"] == "synthetic" and context["fx"] == {}
    table = next(table.value for table in app.dataframe if "Hipótesis" in table.value)
    assert len(table) == 12
    assert table.loc[table["Hipótesis"].eq("Referencia actual · sin filtro nuevo"), "Patrimonio USD"].tolist() == [5990]*4
    assert "no declara un ganador" in text(app) and "EUR N/D no equivale a liquidez baja" in text(app)
    from src.model_results_ui import _signature
    key="model_results_"+_signature("A@example.invalid")[:16]+"_state"
    assert app.session_state[key].get("report") is None  # No experimental activation report.
    app.run()
    assert app.session_state["comparison_calls"] == app.session_state["downloads"] == 1
    next(item for item in app.number_input if item.label == "Presupuesto virtual inicial USD").set_value(7000).run()
    assert app.session_state["comparison_calls"] == 1
    assert "execution_comparison" not in app.session_state[key]
    assert "SYNTHETIC_EXPERIMENT_NOT_A_WINNER" not in text(app)


def test_execution_comparison_cannot_be_reused_by_another_owner():
    app = page()
    button(app, "Cargar historial de modelos").click().run()
    next(item for item in app.date_input if item.label == "Hasta (día UTC completo)").set_value("2025-01-13").run()
    button(app, "Descargar precios de modelos e índices").click().run()
    button(app, "Comparar filtros experimentales de ejecución").click().run()
    assert "SYNTHETIC_EXPERIMENT_NOT_A_WINNER" in text(app)
    next(item for item in app.radio if item.label == "Cuenta sintética").set_value("B").run()
    assert not app.exception
    assert "Carga el historial" in text(app) and "SYNTHETIC_EXPERIMENT_NOT_A_WINNER" not in text(app)
    assert app.session_state["reads"] == app.session_state["downloads"] == app.session_state["comparison_calls"] == 1
