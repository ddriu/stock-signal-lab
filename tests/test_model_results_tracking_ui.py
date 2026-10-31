"""Prospective-page actions with synthetic data; no database, network or SMTP."""

from streamlit.testing.v1 import AppTest


SCRIPT = '''
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import src.model_results_tracking_ui as ui
from src.model_results_ui import _signature

scenario = SCENARIO
private_owner = "private-owner@example.invalid"

def count(name):
    st.session_state[name] = st.session_state.get(name, 0) + 1

class Journal:
    owner = private_owner
    def list_analysis_snapshots(self):
        count("history_reads")
        return pd.DataFrame([dict(id=1, ticker="HIST", analyzed_at="2026-10-19",
            created_at="2026-10-19T20:01:00Z", entry_label="Entrada fuerte",
            entry_score=85, note="")])
    def __getattr__(self, name):
        raise AssertionError("No portfolio, mail or preference access: " + name)

class Store:
    def list_seasons(self):
        count("season_reads")
        if scenario == "initial":
            return [dict(season_id=mode+"-"+model, manifest=dict(engine_version=ui.TRACKING_VERSION,
                source="technical", cohort="raw", strategy_key=model, mode=mode,
                engine_identity=ui._tracking_module.engine_identity(),
                activation_at="2026-10-20T21:00:00Z", experiment_id="experiment-a"))
                for mode in ("theoretical", "budget") for model in "ABCD"]
        return [dict(season_id="season-a", manifest=dict(engine_version=ui.TRACKING_VERSION,
            strategy_key="A", mode="theoretical", activation_at="2026-10-20T21:00:00Z",
            experiment_id="experiment-a", owner=private_owner))]
    def select(self, kind):
        count(kind + "_reads")
        if kind == "snapshots":
            if scenario == "initial":
                return [dict(season_id=mode+"-"+model, snapshot=dict(snapshot_kind="activation",
                    available_at="2026-10-20T21:00:00Z", activation_at="2026-10-20T21:00:00Z",
                    coverage_complete=False, metrics=dict(initial_cash_usd=6000 if mode=="budget" else 0,
                        final_value_usd=6000 if mode=="budget" else 0, net_return_pct=None), daily=[],lots=[],trades=[]))
                    for mode in ("theoretical", "budget") for model in "ABCD"]
            return [dict(season_id="season-a", snapshot=dict(available_at="2026-10-21T21:00:00Z",
                coverage_complete=False, metrics=None))]
        return [dict(season_id="season-a", event_type="email_delivery", owner=private_owner,
            payload=dict(status="attempted", detail="Legacy sender: " + private_owner))]

def factory(journal):
    count("store_factories")
    return Store()

def activate(journal, manifest, **kwargs):
    count("activations")
    st.session_state["activation_config"] = dict(source=kwargs["source"],
        cohort=kwargs.get("cohort", "raw"), budget_usd=kwargs["budget_usd"], costs=asdict(kwargs["costs"]))
    return "new-experiment"

def collect(tickers, **kwargs):
    count("market_reads")
    st.session_state["requested_tickers"] = tickers
    return SimpleNamespace(bars={"HIST":pd.DataFrame()})

def advance(journal, experiment, snapshots, bars):
    count("advances")
    assert experiment == "new-experiment"
    assert snapshots.ticker.tolist() == ["HIST"]
    assert list(bars) == ["HIST"]

if scenario in {"prepared", "verified", "pending-other", "stale-controls"}:
    costs = dict(commission_fixed_usd=2.0, spread_bps=3.0, fx_bps=4.0, verified=True)
    manifest = dict(source="growth", report_hash="frozen-report")
    if scenario in {"verified", "pending-other"}:
        manifest.update(cohort="verified", cohort_version="recorded-quality-first-observation-v1")
    report = SimpleNamespace(manifest=manifest,
        budget={"A":SimpleNamespace(manifest=dict(costs=costs, budget_usd=7000))})
    st.session_state["model_results_" + _signature(private_owner)[:16] + "_state"] = {"report":report}
    if scenario == "stale-controls":
        st.session_state["model_results_" + _signature(private_owner)[:16] + "_state"].update(
            run_key="original-raw-controls", current_run_key="new-verified-controls")
if scenario == "pending-other":
    tracking_key="model_results_"+ui.hashlib.sha256(private_owner.encode()).hexdigest()[:16]+"_tracking"
    if tracking_key not in st.session_state:
        st.session_state[tracking_key] = {"pending_activation":dict(audit_manifest={"source":"growth"},
            source="growth",cohort="raw")}

with (patch.object(ui,"create_model_results_store",side_effect=factory),
      patch.object(ui,"activate_model_experiment",side_effect=activate),
      patch.object(ui,"collect_model_market_bundle",side_effect=collect),
      patch.object(ui,"advance_model_experiment",side_effect=advance)):
    ui.render_model_tracking_page(Journal())
'''


def page(scenario="blank"):
    return AppTest.from_string(SCRIPT.replace("SCENARIO", repr(scenario)), default_timeout=30).run()


def button(app, label):
    return next(item for item in app.button if item.label == label)


def visible(app):
    return " ".join(str(item.value) for kind in
        ("markdown", "caption", "warning", "info", "error", "success", "json", "dataframe")
        for item in getattr(app, kind))


def test_opening_tracking_page_is_inert_and_activation_needs_prepared_audit(monkeypatch):
    monkeypatch.delenv("MODEL_RESULTS_ENABLED", raising=False)
    app = page()
    assert not app.exception
    assert button(app, "Activar las cuatro estrategias virtuales").disabled
    app.checkbox[0].check().run()
    assert button(app, "Activar las cuatro estrategias virtuales").disabled
    for counter in ("store_factories", "history_reads", "market_reads", "activations", "advances"):
        assert counter not in app.session_state
    assert "private-owner@example.invalid" not in visible(app)
    assert "no acredita que el seguimiento de GitHub esté habilitado" in visible(app)


def test_consulting_reads_only_saved_tracking_without_advancing_or_exposing_owner():
    app = page()
    button(app, "Consultar seguimiento guardado").click().run()
    assert not app.exception
    assert app.session_state["store_factories"] == 1
    assert app.session_state["season_reads"] == 1
    assert app.session_state["snapshots_reads"] == app.session_state["events_reads"] == 1
    assert "history_reads" not in app.session_state and "market_reads" not in app.session_state
    assert "activations" not in app.session_state and "advances" not in app.session_state
    assert "Resultado todavía no concluyente" in visible(app)
    assert "private-owner@example.invalid" not in visible(app)
    app.run()
    assert app.session_state["store_factories"] == 1  # No read on ordinary reruns.


def test_explicit_activation_freezes_report_settings_and_initializes_without_sending_mail():
    app = page("prepared")
    assert button(app, "Activar las cuatro estrategias virtuales").disabled
    app.checkbox[0].check().run()
    assert not button(app, "Activar las cuatro estrategias virtuales").disabled
    assert "activations" not in app.session_state
    button(app, "Activar las cuatro estrategias virtuales").click().run()
    assert not app.exception and not app.error
    assert app.session_state["activations"] == app.session_state["advances"] == 1
    assert app.session_state["history_reads"] == app.session_state["market_reads"] == 1
    assert app.session_state["activation_config"] == dict(source="growth", cohort="raw", budget_usd=7000,
        costs=dict(commission_fixed_usd=2.0, spread_bps=3.0, fx_bps=4.0, verified=True))
    assert set(app.session_state["requested_tickers"]) == {"HIST", "SPY", "QQQ", "VT"}
    assert "Temporadas virtuales creadas" in visible(app)
    assert "private-owner@example.invalid" not in visible(app)
    app.run()
    assert app.session_state["activations"] == app.session_state["advances"] == 1


def test_verified_report_activation_preserves_recorded_cohort_instead_of_falling_back_to_raw():
    app = page("verified")
    app.checkbox[0].check().run()
    button(app, "Activar las cuatro estrategias virtuales").click().run()
    assert not app.exception and not app.error
    assert app.session_state["activation_config"]["cohort"] == "verified"
    assert app.session_state["activations"] == app.session_state["advances"] == 1


def test_initial_cash_is_not_displayed_as_measured_performance_and_reading_stays_inert():
    app = page("initial")
    button(app, "Consultar seguimiento guardado").click().run()
    assert not app.exception and not app.error
    assert "Recogidas de mercado: 0" in visible(app)
    assert "Rentabilidad: N/D" in visible(app)
    assert "cabeceras y el efectivo inicial no demuestran rentabilidad" in visible(app)
    assert "market_reads" not in app.session_state and "advances" not in app.session_state
    assert not any("net_return_pct" in frame.value for frame in app.dataframe)


def test_a_pending_raw_activation_cannot_silently_use_a_new_verified_report():
    app = page("pending-other")
    app.checkbox[0].check().run()
    assert not app.exception and button(app, "Activar las cuatro estrategias virtuales").disabled
    assert "otra auditoría o cohorte" in visible(app)
    assert "activations" not in app.session_state and "market_reads" not in app.session_state
    button(app, "Liberar sólo el intento local y conservar el registro anterior").click().run()
    assert not app.exception and not button(app, "Activar las cuatro estrategias virtuales").disabled
    for name in ("activations", "market_reads", "store_factories", "advances"):
        assert name not in app.session_state


def test_controls_changed_after_report_cannot_activate_the_previous_selection():
    app = page("stale-controls")
    app.checkbox[0].check().run()
    assert not app.exception and button(app, "Activar las cuatro estrategias virtuales").disabled
    assert "Recalcula antes de activar" in visible(app)
    assert "motor bruto original" in visible(app)
    assert "activations" not in app.session_state and "market_reads" not in app.session_state
