"""Home navigation must not execute hidden simulation or decision panels."""

import json

from streamlit.testing.v1 import AppTest


HOME_SCRIPT = '''
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import streamlit as st
import app as application

class EmptyJournal:
    def list_operations(self):
        return pd.DataFrame()
    def list_portfolio_snapshot_positions(self):
        return pd.DataFrame()

def forbidden(*args, **kwargs):
    raise AssertionError("A hidden decision calculation was executed")

def lab(*args, **kwargs):
    st.write("VISIBLE_LAB_ONLY")

def render_decisions(*args, **kwargs):
    st.write("VISIBLE_DECISIONS_ONLY")

with ExitStack() as patches:
    patches.enter_context(patch.object(application, "_portfolio_snapshot", return_value=(pd.DataFrame(), None)))
    patches.enter_context(patch.object(application, "_paper_tracking_tickers", return_value=[]))
    patches.enter_context(patch.object(application, "_merge_saved_analysis_summary", side_effect=lambda summary, *args: summary))
    patches.enter_context(patch.object(application, "_render_paper_simulation_lab", side_effect=lab))
    patches.enter_context(patch.object(application, "_render_thesis_override", return_value=None))
    patches.enter_context(patch.object(application, "_render_rotation_dashboard", side_effect=render_decisions))
    view = str(st.session_state.get("home_view_navigation") or "Resumen")
    if view == "Decisiones":
        patches.enter_context(patch.object(application, "build_approximate_return_report", return_value=SimpleNamespace(open_positions=pd.DataFrame())))
        patches.enter_context(patch.object(application, "build_recovery_hurdles", return_value={}))
        patches.enter_context(patch.object(application, "build_pair_correlations", return_value={}))
        patches.enter_context(patch.object(application, "build_rotation_dashboard", return_value=SimpleNamespace()))
        patches.enter_context(patch.object(application, "summarize_candidate_eligibility", return_value={}))
    else:
        for name in ("build_approximate_return_report", "build_recovery_hurdles", "build_pair_correlations", "build_rotation_dashboard"):
            patches.enter_context(patch.object(application, name, side_effect=forbidden))

    application.render_home(
        SimpleNamespace(display_name="Test", username="test"),
        EmptyJournal(), EmptyJournal(), {}, {}, [],
        SimpleNamespace(rates_per_eur={"EUR": 1.0}),
        pd.DataFrame(), pd.DataFrame(),
    )
'''


def _text(app: AppTest) -> str:
    return " ".join(str(item.value) for item in app.markdown)


def test_summary_does_not_execute_hidden_lab_or_rotation_calculations() -> None:
    app = AppTest.from_string(HOME_SCRIPT, default_timeout=30).run()

    assert not app.exception
    assert not app.error
    assert "VISIBLE_LAB_ONLY" not in _text(app)
    assert "VISIBLE_DECISIONS_ONLY" not in _text(app)
    assert any(button.label == "Actualizar precios ahora" for button in app.button)
    assert not any(button.label == "Revisar toda mi cartera y buscar oportunidades" for button in app.button)


def test_lab_is_rendered_only_when_selected_and_does_not_run_real_rotations() -> None:
    app = AppTest.from_string(HOME_SCRIPT, default_timeout=30).run()
    app.segmented_control(key="home_view_navigation").set_value("Laboratorio").run()

    assert not app.exception
    assert not app.error
    assert "VISIBLE_LAB_ONLY" in _text(app)
    assert "VISIBLE_DECISIONS_ONLY" not in _text(app)

    app.segmented_control(key="home_view_navigation").set_value("Resumen").run()
    assert not app.exception
    assert "VISIBLE_LAB_ONLY" not in _text(app)


def test_decisions_are_rendered_only_when_selected_without_running_lab() -> None:
    app = AppTest.from_string(HOME_SCRIPT, default_timeout=30).run()
    app.segmented_control(key="home_view_navigation").set_value("Decisiones").run()

    assert not app.exception
    assert not app.error
    assert "VISIBLE_DECISIONS_ONLY" in _text(app)
    assert "VISIBLE_LAB_ONLY" not in _text(app)
    assert any(button.label == "Revisar toda mi cartera y buscar oportunidades" for button in app.button)


def test_access_panel_does_not_compute_hidden_decisions_or_lab() -> None:
    app = AppTest.from_string(HOME_SCRIPT, default_timeout=30).run()
    app.segmented_control(key="home_view_navigation").set_value("Accesos").run()

    assert not app.exception
    assert not app.error
    assert "VISIBLE_LAB_ONLY" not in _text(app)
    assert "VISIBLE_DECISIONS_ONLY" not in _text(app)
    assert any(button.label == "Abrir análisis" for button in app.button)


def test_rotation_tables_show_company_names_and_preserve_separate_symbols() -> None:
    script = '''
from types import SimpleNamespace
import app
from src.portfolio_rotation import HORIZON_POLICIES

dashboard = SimpleNamespace(
    policy=HORIZON_POLICIES["Mensual"], switches=(),
    positions=({"Ticker": "HELD", "Empresa": "Held Company, Inc.", "Color": "Naranja",
        "Peso cotizado": 100.0, "Acción": "Revisar exposición", "Motivo": "Peso alto"},),
    candidates=({"Ticker": "NEW", "Empresa": "New Company, Inc.", "Color": "Azul",
        "Score horizonte": 85, "Acción": "Estudiar entrada"},),
)
app._render_rotation_dashboard(dashboard)
'''
    page = AppTest.from_string(script, default_timeout=30).run()
    assert not page.exception
    priorities, positions, candidates = (item.value for item in page.dataframe)
    assert priorities["Empresa"].tolist() == ["Held Company, Inc.", "New Company, Inc."]
    assert priorities["Ticker"].tolist() == ["HELD", "NEW"]
    assert json.loads(page.dataframe[0].proto.columns)["Ticker"]["hidden"]
    assert positions["Empresa"].tolist() == ["Held Company, Inc."]
    assert candidates["Empresa"].tolist() == ["New Company, Inc."]


def test_company_row_selection_opens_ticker_not_full_company_name() -> None:
    script = '''
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import app

with patch.object(st, "dataframe", return_value=SimpleNamespace(selection=SimpleNamespace(rows=[0]))):
    selected = app.render_ticker_dataframe(pd.DataFrame([{
        "Ticker": "NKE", "Empresa": "NIKE, Inc.", "Posición": "NKE",
    }]), key="named_row", column_config={"Ticker": None})
st.write("SELECTED=" + ",".join(selected))
'''
    page = AppTest.from_string(script, default_timeout=30).run()
    assert not page.exception
    assert any(item.label == "Abrir NKE" for item in page.button)
    assert "SELECTED=NKE" in _text(page)


def test_reconciled_overview_replaces_legacy_totals_without_hidden_calculations() -> None:
    report = '''
    report = SimpleNamespace(
        positions=pd.DataFrame(columns=['platform','asset_name','analysis_ticker','value_eur','liquidity_class']),
        metrics_global={'value_eur':1000.,'value_complete':True},
        metrics_by_platform=pd.DataFrame(),metrics_by_asset=pd.DataFrame(),
        realized_trades=pd.DataFrame(),cash_flows=pd.DataFrame(),
        monthly=pd.DataFrame(),annual=pd.DataFrame(),issues=(),
    )
    patches.enter_context(patch.object(application,'load_reconciled_portfolio',return_value=report))
'''
    script = HOME_SCRIPT.replace('    application.render_home(', report + '\n    application.render_home(')
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception
    assert not app.error
    assert any(metric.label == 'Patrimonio financiero' for metric in app.metric)
    assert 'Valor de mi cartera' not in _text(app)
    assert 'VISIBLE_LAB_ONLY' not in _text(app)
    assert 'VISIBLE_DECISIONS_ONLY' not in _text(app)
