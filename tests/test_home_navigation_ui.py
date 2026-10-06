"""Home navigation must not execute hidden simulation or decision panels."""

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
