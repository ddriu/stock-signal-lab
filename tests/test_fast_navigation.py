"""Main routing: passive navigation must not run market engines or cross owners.

Real navigation widgets are exercised. Only authentication/storage/panels and
the expensive market functions are substituted; no live requests or writes.
"""
import pytest
from streamlit.testing.v1 import AppTest


SCRIPT = '''
from contextlib import ExitStack
from datetime import date
from unittest.mock import patch
import pandas as pd
import streamlit as st
import app as application
from config import StrategyConfig, BacktestConfig
from src.auth import AuthConfig
from src.storage import GROUP_PORTFOLIO_OWNER

def symbol(owner): return "ALPHA" if owner == "qa_a" else "BETA"

class Journal:
    def __init__(self, owner): self.owner = owner
    def get_latest_daily_score_report(self):
        st.session_state["qa_reads"][self.owner]["latest"] += 1
        return {"owner_marker": self.owner, "report_id": ("a" if self.owner == "qa_a" else "b") * 64,
                "rows": [{"ticker": symbol(self.owner), "session": date.today().isoformat()}]}
    def list_analysis_snapshots_for_sessions(self, sessions):
        assert sessions == [date.today().isoformat()], "Only the saved report's sessions may be read"
        st.session_state["qa_reads"][self.owner]["bounded"] += 1
        return pd.DataFrame([{"ticker": symbol(self.owner), "owner_marker": self.owner}])
    def list_favorites(self):
        return pd.DataFrame() if self.owner == GROUP_PORTFOLIO_OWNER else pd.DataFrame([
            {"ticker": symbol(self.owner), "name": "Company for " + self.owner}])
    def list_portfolio_snapshot_positions(self): return pd.DataFrame()
    def list_analysis_snapshots(self):
        st.session_state["qa_reads"][self.owner]["snapshots"] += 1
        return pd.DataFrame()
    def list_operations(self): return pd.DataFrame()
    def open_positions(self): return pd.DataFrame()
    def __getattr__(self, name):
        if name.startswith(("add_", "save_", "delete_", "upsert_", "set_")):
            raise AssertionError("Unexpected journal write: " + name)
        raise AttributeError(name)

def raw(price=100.):
    index = pd.bdate_range(end=date.today(), periods=260)
    return pd.DataFrame({"open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 1_000_000.}, index=index)

if "qa_initialized" not in st.session_state:
    st.session_state["qa_initialized"] = True
    st.session_state["qa_owner"] = "qa_a"
    st.session_state["qa_counts"] = {"prepare": 0, "enrich": 0, "load": 0}
    st.session_state["qa_reads"] = {owner: {"latest": 0, "bounded": 0, "snapshots": 0}
        for owner in ("qa_a", "qa_b", GROUP_PORTFOLIO_OWNER)}
    st.session_state["qa_compute_budget"] = INITIAL_BUDGET
    st.session_state["qa_load_budget"] = 0
    st.session_state["_navigation_view_owner"] = "qa_a"
    st.session_state["market_data"] = {"ALPHA": raw()}
    st.session_state["reference_data"] = {"SPY": raw()}
    st.session_state["fundamental_data"] = {"ALPHA": {"symbol": "ALPHA", "currency": "USD"}}
    st.session_state["main_navigation"] = INITIAL_ROUTE
    st.session_state["analysis_navigation"] = INITIAL_ANALYSIS
    st.session_state["more_navigation"] = INITIAL_MORE
    st.session_state["portfolio_navigation"] = INITIAL_PORTFOLIO
    if INITIAL_DIGEST:
        st.query_params["digest"] = INITIAL_DIGEST
    st.session_state["qa_journals"] = {owner: Journal(owner) for owner in ("qa_a", "qa_b", GROUP_PORTFOLIO_OWNER)}
    for owner in ("qa_a", "qa_b"):
        st.session_state["_portfolio_auto_refresh_done_" + owner] = date.today().isoformat()
    if PENDING_FLAGS:
        st.session_state["_pending_analysis_ticker"] = "ALPHA"
        st.session_state["_growth_scan_tickers"] = ["ALPHA"]
        st.session_state["_portfolio_market_refresh_requested"] = True

def refresh():
    st.session_state["qa_compute_budget"] += 1
    st.session_state["qa_load_budget"] += 1
    application._request_portfolio_market_refresh()

def change_owner(): st.session_state["qa_owner"] = "qa_b"

st.button("QA refresh explicitly", on_click=refresh, key="qa_refresh")
st.button("QA switch account", on_click=change_owner, key="qa_switch")
owner = st.session_state["qa_owner"]
user = AuthConfig(username=owner, password_hash="synthetic-only", display_name=owner)

def forbidden(*args, **kwargs):
    raise AssertionError("Passive routing attempted a download, model, persistence or SMTP")

def prepare(prices, fundamentals, references, strategy):
    counts = st.session_state["qa_counts"]
    assert counts["prepare"] < st.session_state["qa_compute_budget"], "prepare_data reran on passive navigation"
    assert set(prices) == {symbol(owner)}, "Market data leaked across authenticated owners"
    counts["prepare"] += 1
    rows = [{"Ticker": ticker, "OwnerMarker": owner, "Precio": float(frame.close.iloc[-1])}
            for ticker, frame in prices.items()]
    return prices, rows, {}, {}, {}, {}, {}

def enrich(rows, *args, **kwargs):
    counts = st.session_state["qa_counts"]
    assert counts["enrich"] < st.session_state["qa_compute_budget"], "Policy/motor reran on passive navigation"
    counts["enrich"] += 1
    return [{**row, "Política de análisis": {"ticker": row["Ticker"], "owner_marker": row["OwnerMarker"]}}
            for row in rows]

def load(tickers, *args, **kwargs):
    counts = st.session_state["qa_counts"]
    assert counts["load"] < st.session_state["qa_load_budget"], "load_market_data ran without explicit refresh"
    assert set(tickers) == {symbol(owner)}, "Refresh selected another owner's tickers"
    counts["load"] += 1
    st.session_state["market_data"] = {symbol(owner): raw(100. + counts["load"])}
    st.session_state["fundamental_data"] = {symbol(owner): {"symbol": symbol(owner), "currency": "USD"}}
    st.session_state["reference_data"] = {"SPY": raw()}
    st.session_state["_market_view_revision"] = int(st.session_state.get("_market_view_revision", 0)) + 1
    return set(tickers)

def home(current, journal, group, prepared, references, summary, *args, **kwargs):
    assert current.username == owner and journal.owner == owner
    assert not prepared or set(prepared) == {symbol(owner)}, "Prepared data leaked across owners"
    assert all(row["OwnerMarker"] == owner for row in summary), "Cached policy leaked across owners"
    st.write("HOME " + owner)
    st.write("TICKERS " + ",".join(row["Ticker"] for row in summary))

def saved_workspace(journal, *, report, snapshots=None, on_action=None, view):
    assert journal.owner == owner and report["owner_marker"] == owner, "Saved report leaked across owners"
    assert report["rows"][0]["ticker"] == symbol(owner)
    if view == "Inicio":
        assert snapshots is None, "Inicio must not read historical analysis"
    else:
        assert snapshots.owner_marker.tolist() == [owner], "Snapshot context leaked across owners"
    st.write("SAVED " + view + " " + owner + " " + symbol(owner))
    if view == "Inicio":
        for horizon in ("days", "weeks", "long_term"):
            st.button("QA open " + horizon, key="qa_horizon_" + horizon,
                on_click=on_action, args=("horizon", horizon))
        st.button("QA review plan", key="qa_plan", on_click=on_action, args=("plan", symbol(owner)))
        st.button("QA results", key="qa_results", on_click=on_action, args=("results", None))

def saved_results(journal, *, on_action=None):
    assert journal.owner == owner, "Results leaked across owners"
    st.write("SAVED Resultados " + owner)

def plan(journal, *, actor_username):
    assert journal.owner == actor_username == owner
    assert st.session_state.get("_requested_plan_ticker") == symbol(owner)
    st.write("PLAN " + owner)

def digest(journal, *, report_id=None):
    assert journal.owner == owner and report_id == INITIAL_DIGEST
    st.write("DIGEST " + owner + " " + report_id)

def favorites(journal, group, private, shared, **kwargs):
    assert journal.owner == owner and private.ticker.tolist() == [symbol(owner)]
    st.write("FAVORITES " + owner)

def sidebar(*args, **kwargs):
    stop = st.sidebar.slider('QA stop', 1.0, 30.0, 7.0, key='cfg_stop_loss')
    return [symbol(owner)], date(2026, 1, 1), date.today(), True, "", StrategyConfig(stop_loss_pct=stop), BacktestConfig(), False

with ExitStack() as patches:
    patches.enter_context(patch("requests.sessions.Session.request", side_effect=forbidden))
    patches.enter_context(patch("smtplib.SMTP", side_effect=forbidden))
    patches.enter_context(patch("smtplib.SMTP_SSL", side_effect=forbidden))
    for name in ("apply_visual_theme", "apply_section_layout", "render_app_header"):
        patches.enter_context(patch.object(application, name, return_value=None))
    patches.enter_context(patch.object(application, "require_login", return_value=user))
    patches.enter_context(patch.object(application, "load_auth_accounts", return_value={}))
    patches.enter_context(patch.object(application, "cached_journal", side_effect=lambda selected: st.session_state["qa_journals"][selected]))
    patches.enter_context(patch.object(application, "persistent_journal_enabled", return_value=True))
    patches.enter_context(patch.object(application, "build_sidebar", side_effect=sidebar))
    patches.enter_context(patch.object(application, "cached_download", side_effect=forbidden))
    patches.enter_context(patch.object(application, "cached_fundamentals", side_effect=forbidden))
    patches.enter_context(patch.object(application, "load_market_data", side_effect=load))
    patches.enter_context(patch.object(application, "prepare_data", side_effect=prepare))
    patches.enter_context(patch.object(application, "_enrich_current_analysis_policy", side_effect=enrich))
    patches.enter_context(patch.object(application, "_portfolio_tracking_tickers", side_effect=lambda journal, *, strict=False: [] if journal.owner == GROUP_PORTFOLIO_OWNER else [symbol(journal.owner)]))
    patches.enter_context(patch.object(application, "_paper_tracking_tickers", return_value=[]))
    patches.enter_context(patch.object(application, "render_home", side_effect=home))
    patches.enter_context(patch.object(application, "render_investment_home",
        side_effect=lambda *args, **kwargs: saved_workspace(*args, view="Inicio", **kwargs)))
    patches.enter_context(patch.object(application, "render_investment_workspace",
        side_effect=lambda *args, **kwargs: saved_workspace(*args, view="Invertir", **kwargs)))
    patches.enter_context(patch.object(application, "render_investment_results", side_effect=saved_results))
    patches.enter_context(patch.object(application, "_render_investment_position_plan", side_effect=plan))
    patches.enter_context(patch("src.daily_digest_ui.render_daily_score_page", side_effect=digest))
    patches.enter_context(patch.object(application, "render_favorites_manager", side_effect=favorites))
    patches.enter_context(patch.object(application, "render_methodology", side_effect=lambda: st.write("GUIDE")))
    patches.enter_context(patch.object(application, "render_email_alert_settings", side_effect=lambda journal: st.write("MAIL_SETTINGS " + journal.owner)))
    patches.enter_context(patch.object(application, "render_capital_projection_page", side_effect=lambda selected: st.write("CAPITAL " + selected)))
    for name in ("persist_analysis_batch_snapshots", "run_daily_alerts", "evaluate_growth_momentum",
                 "evaluate_latest_signal", "run_backtest"):
        patches.enter_context(patch.object(application, name, side_effect=forbidden))
    application.main()
'''


def page(route="Inicio", *, analysis="Panel anterior", more="Guía y riesgos",
         portfolio="Plan de capital", pending=False, digest=None):
    script = (SCRIPT.replace("INITIAL_BUDGET", "1" if route == "Analizar" else "0")
        .replace("INITIAL_ROUTE", repr(route)).replace("INITIAL_MORE", repr(more))
        .replace("INITIAL_ANALYSIS", repr(analysis)).replace("INITIAL_DIGEST", repr(digest))
        .replace("INITIAL_PORTFOLIO", repr(portfolio)).replace("PENDING_FLAGS", repr(pending)))
    return AppTest.from_string(script, default_timeout=30).run()


def assert_ok(current):
    assert not current.exception, [item.message for item in current.exception]
    assert not current.error


def open_compact_destination(current, destination):
    """Use the real compact menu instead of assuming every route is a top tab."""

    current.selectbox(key="compact_destination").set_value(destination).run()
    assert_ok(current)
    current.button(key="compact_open").click().run()
    assert_ok(current)
    assert current.session_state["main_navigation"] == destination
    return current


@pytest.mark.parametrize("route,more", [("Inicio", "Guía y riesgos"), ("Invertir", "Guía y riesgos"),
    ("Resultados", "Guía y riesgos"), ("Favoritos", "Guía y riesgos"), ("Más", "Guía y riesgos"),
    ("Más", "Alertas por correo"), ("Carteras", "Guía y riesgos")])
def test_light_routes_never_prepare_enrich_download_or_run_models(route, more):
    current = page(route, more=more)
    assert_ok(current)
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}
    current.run()
    assert_ok(current)
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}


@pytest.mark.parametrize("route", ["Inicio", "Invertir", "Resultados", "Favoritos", "Más"])
def test_light_routes_ignore_pending_market_work_instead_of_running_it(route):
    current = page(route, pending=True)
    assert_ok(current)
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}
    assert current.session_state["_pending_analysis_ticker"] == "ALPHA"
    assert current.session_state["_growth_scan_tickers"] == ["ALPHA"]
    assert current.session_state["_portfolio_market_refresh_requested"] is True
    current.run()
    assert_ok(current)
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}


@pytest.mark.parametrize("route,bounded", [("Inicio", 0), ("Invertir", 1), ("Resultados", 0)])
def test_saved_screens_read_only_owner_report_and_bounded_context(route, bounded):
    current = page(route)
    assert_ok(current)
    current.run()
    assert_ok(current)
    assert current.session_state["qa_reads"]["qa_a"] == {
        "latest": int(route != "Resultados"), "bounded": bounded, "snapshots": 0}
    assert current.session_state["qa_reads"]["qa_b"] == {"latest": 0, "bounded": 0, "snapshots": 0}
    assert any("SAVED " + route + " qa_a" in str(item.value) for item in current.markdown)


@pytest.mark.parametrize("horizon", ["days", "weeks", "long_term"])
def test_home_horizon_action_opens_investing_without_market_work(horizon):
    current = page()
    current.button(key="qa_horizon_" + horizon).click().run()
    assert_ok(current)
    assert current.session_state["main_navigation"] == "Invertir"
    assert current.session_state["investment_horizon"] == horizon
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}
    assert current.session_state["qa_reads"]["qa_a"] == {"latest": 1, "bounded": 1, "snapshots": 0}


def test_home_plan_action_targets_own_position_without_saving_or_market_work():
    current = page()
    current.button(key="qa_plan").click().run()
    assert_ok(current)
    assert current.session_state["main_navigation"] == "Carteras"
    assert current.session_state["portfolio_navigation"] == "Plan por posición"
    assert current.session_state["_requested_plan_ticker"] == "ALPHA"
    assert any("PLAN qa_a" in str(item.value) for item in current.markdown)
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}


def test_home_results_action_opens_saved_results_without_market_work():
    current = page()
    current.button(key="qa_results").click().run()
    assert_ok(current)
    assert current.session_state["main_navigation"] == "Resultados"
    assert any("SAVED Resultados qa_a" in str(item.value) for item in current.markdown)
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}


def test_archived_digest_link_remains_read_only_in_home():
    current = page(digest="d" * 64, pending=True)
    assert_ok(current)
    current.run()
    assert_ok(current)
    assert any("DIGEST qa_a " + "d" * 64 in str(item.value) for item in current.markdown)
    assert current.session_state["qa_reads"]["qa_a"] == {"latest": 0, "bounded": 0, "snapshots": 0}
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}


@pytest.mark.parametrize("route", ["Inicio", "Invertir", "Resultados"])
def test_owner_switch_clears_new_workspace_state_and_reads_only_new_owners_report(route):
    current = page(route, pending=True)
    assert_ok(current)
    current.session_state["investment_private_context"] = {"ticker": "ALPHA"}
    current.session_state["_investment_old_report"] = {"owner_marker": "qa_a"}
    current.session_state["_requested_plan_ticker"] = "ALPHA"
    current.button(key="qa_switch").click().run()
    assert_ok(current)
    assert current.session_state["qa_owner"] == "qa_b"
    for key in ("investment_private_context", "_investment_old_report", "_requested_plan_ticker",
                "_pending_analysis_ticker", "_growth_scan_tickers", "_portfolio_market_refresh_requested"):
        assert key not in current.session_state
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}
    assert current.session_state["qa_reads"]["qa_b"] == {
        "latest": int(route != "Resultados"), "bounded": int(route == "Invertir"), "snapshots": 0}
    assert any("SAVED " + route + " qa_b" in str(item.value) for item in current.markdown)
    assert not any("SAVED " + route + " qa_a" in str(item.value) for item in current.markdown)


def test_all_main_destinations_roundtrip_without_repeated_analysis_or_smtp():
    current = page("Analizar")
    assert_ok(current)
    for destination in ("Inicio", "Invertir", "Carteras", "Favoritos", "Resultados", "Más", "Analizar"):
        open_compact_destination(current, destination)
        assert current.session_state["qa_counts"] == {"prepare": 1, "enrich": 1, "load": 0}
    assert current.session_state["qa_reads"]["qa_a"]["latest"] == 1
    assert current.session_state["qa_reads"]["qa_a"]["bounded"] == 1


def test_home_has_three_primary_tabs_and_advanced_route_roundtrips_without_market_work():
    current = page("Inicio")
    assert_ok(current)
    primary_options = current.segmented_control(key="main_navigation").options
    assert len(primary_options) == 3
    assert all(
        any(label in option for option in primary_options)
        for label in ("Hoy", "Cartera", "Gestionar y herramientas")
    )
    assert "Favoritos" in current.selectbox(key="compact_destination").options

    open_compact_destination(current, "Favoritos")
    advanced_options = current.segmented_control(key="main_navigation").options
    assert len(advanced_options) == 4
    assert any("Favoritos" in option for option in advanced_options)
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}

    open_compact_destination(current, "Inicio")
    returned_options = current.segmented_control(key="main_navigation").options
    assert len(returned_options) == 3
    assert not any("Favoritos" in option for option in returned_options)
    assert current.session_state["qa_counts"] == {"prepare": 0, "enrich": 0, "load": 0}


def test_repeated_legacy_panel_and_roundtrip_tabs_reuse_prepared_analysis_without_recalculation():
    current = page("Analizar")
    assert_ok(current)
    assert current.session_state["qa_counts"] == {"prepare": 1, "enrich": 1, "load": 0}
    current.run()
    assert_ok(current)
    open_compact_destination(current, "Favoritos")
    open_compact_destination(current, "Analizar")
    assert current.session_state["qa_counts"] == {"prepare": 1, "enrich": 1, "load": 0}


def test_explicit_refresh_invalidates_once_and_later_navigation_remains_fast():
    current = page("Analizar")
    assert_ok(current)
    current.button(key="qa_refresh").click().run()
    assert_ok(current)
    assert current.session_state["qa_counts"] == {"prepare": 2, "enrich": 2, "load": 1}
    current.run()
    assert_ok(current)
    open_compact_destination(current, "Más")
    open_compact_destination(current, "Analizar")
    assert current.session_state["qa_counts"] == {"prepare": 2, "enrich": 2, "load": 1}


def test_account_switch_cannot_reuse_previous_owners_market_or_policy_cache():
    current = page("Analizar")
    assert_ok(current)
    current.button(key="qa_switch").click().run()
    assert_ok(current)
    assert current.session_state["qa_owner"] == "qa_b"
    assert "market_data" not in current.session_state or "ALPHA" not in current.session_state["market_data"]
    # Account rebinding deliberately drops the previous owner's analysis view.
    current.segmented_control(key="analysis_navigation").set_value("Panel anterior").run()
    assert_ok(current)
    current.button(key="qa_refresh").click().run()
    assert_ok(current)
    assert current.session_state["qa_counts"] == {"prepare": 2, "enrich": 2, "load": 1}
    assert set(current.session_state["market_data"]) == {"BETA"}
    assert any("TICKERS BETA" in str(item.value) for item in current.markdown)


def test_analysis_controls_survive_a_page_without_the_sidebar():
    script = SCRIPT.replace("INITIAL_BUDGET", "1").replace("INITIAL_ROUTE", repr("Analizar"))
    script = script.replace("INITIAL_MORE", repr("Guía y riesgos")).replace("INITIAL_PORTFOLIO", repr("Privada"))
    script = script.replace("INITIAL_ANALYSIS", repr("Panel anterior")).replace("INITIAL_DIGEST", "None")
    script = script.replace("PENDING_FLAGS", "False")
    current = AppTest.from_string(script, default_timeout=30)
    current.session_state['cfg_stop_loss'] = 13.0
    current.run()
    assert_ok(current)
    assert current.slider(key='cfg_stop_loss').value == 13.0
    open_compact_destination(current, 'Favoritos')
    open_compact_destination(current, 'Analizar')
    assert current.slider(key='cfg_stop_loss').value == 13.0
    assert current.session_state['qa_counts'] == {'prepare': 1, 'enrich': 1, 'load': 0}
