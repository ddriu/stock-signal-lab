"""Private archived report reads and a compact, mutation-free Streamlit view."""
from copy import deepcopy
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from src.daily_digest import build_daily_score_report
from src.daily_digest_ui import load_daily_score_report


def test_archived_report_is_read_exactly_without_other_reads_or_recalculation():
    report = build_daily_score_report(pd.DataFrame(), as_of="2026-10-23")
    class Journal:
        def get_daily_score_report(self, report_id):
            assert report_id == report["report_id"]
            return deepcopy(report)
        def __getattr__(self, name):
            raise AssertionError("Unexpected read or mutation " + name)
    assert load_daily_score_report(Journal(), report_id=report["report_id"]) == report


def test_archived_report_link_never_falls_back_to_another_owner_or_live_report():
    class Journal:
        def get_daily_score_report(self, report_id):
            return None
    assert load_daily_score_report(Journal(), report_id="a" * 64) is None
    with pytest.raises(ValueError):
        load_daily_score_report(Journal(), report_id="bad")


def test_archived_report_rejects_mutated_payload():
    report = build_daily_score_report(pd.DataFrame(), as_of="2026-10-23")
    report["as_of"] = "2026-10-22"
    class Journal:
        def get_daily_score_report(self, report_id):
            return report
    with pytest.raises(ValueError):
        load_daily_score_report(Journal(), report_id=report["report_id"])


SCRIPT = '''
import pandas as pd
import streamlit as st
from src.daily_digest_ui import render_daily_score_page
class Journal:
    def list_analysis_snapshots(self):
        return pd.DataFrame([dict(ticker="TEST", analyzed_at="2026-10-23", entry_score=100, note="legacy")])
    def list_favorites(self):
        return pd.DataFrame([dict(ticker="TEST", name="Compañía de prueba")])
    def open_positions(self): return pd.DataFrame()
    def list_portfolio_snapshot_positions(self): return pd.DataFrame()
    def __getattr__(self, name):
        raise AssertionError("Forbidden mutation or download: " + name)
render_daily_score_page(Journal())
'''


def test_streamlit_page_keeps_missing_values_and_names_without_network_or_writes():
    page = AppTest.from_string(SCRIPT, default_timeout=30).run()
    assert not page.exception and not page.error and len(page.dataframe) == 1
    table = page.dataframe[0].value
    assert table.iloc[0]["Empresa"] == "Compañía de prueba (TEST)"
    assert "—" in table.iloc[0]["Última"] and table.iloc[0]["Última"].startswith("23/10")
    assert table.iloc[0]["Lectura"] == "Revisar datos" and "deterioro" not in str(table).lower()


MAIN_SCRIPT = '''
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import app as application
from src.daily_digest import build_daily_score_report
report = build_daily_score_report(pd.DataFrame(), as_of="2026-10-23")
class Journal:
    def list_analysis_snapshots(self): return pd.DataFrame()
    def list_favorites(self): return pd.DataFrame()
    def open_positions(self): return pd.DataFrame()
    def list_portfolio_snapshot_positions(self): return pd.DataFrame()
    def get_daily_score_report(self, report_id):
        assert report_id == report["report_id"]
        return report
    def __getattr__(self, name): raise AssertionError("Unexpected mutation or download " + name)
def forbidden(*args, **kwargs):
    raise AssertionError("The saved-history route entered the live refresh path")
st.session_state["main_navigation"] = "Inicio" if ARCHIVE else "Analizar"
st.session_state["analysis_navigation"] = "Resumen diario"
if ARCHIVE:
    st.query_params["digest"] = report["report_id"]
with ExitStack() as stack:
    for name in ("apply_visual_theme", "render_app_header", "render_analysis_view_guide", "render_quick_company_search"):
        stack.enter_context(patch.object(application, name, return_value=None))
    stack.enter_context(patch.object(application, "require_login", return_value=SimpleNamespace(username="test", display_name="Test", is_admin=False)))
    stack.enter_context(patch.object(application, "load_auth_accounts", return_value={}))
    stack.enter_context(patch.object(application, "cached_journal", return_value=Journal()))
    for name in ("build_sidebar", "prepare_data", "persist_analysis_batch_snapshots"):
        stack.enter_context(patch.object(application, name, side_effect=forbidden))
    application.main()
'''


@pytest.mark.parametrize("archived", [False, True])
def test_app_history_route_returns_before_live_refresh_calculation_or_persistence(archived):
    page = AppTest.from_string(MAIN_SCRIPT.replace("ARCHIVE", str(archived)), default_timeout=30).run()
    assert not page.exception and not page.error
    assert any("Resumen diario" in str(item.value) for item in page.subheader)
