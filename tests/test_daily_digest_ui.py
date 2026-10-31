"""Private archived report reads and a compact, mutation-free Streamlit view."""
from copy import deepcopy
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from src.daily_digest import build_daily_score_report
from src.daily_digest_ui import _display_note, _history, _table, load_daily_score_report


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


def test_archived_virtual_summary_is_displayed_without_advancing_or_reading_any_ledger():
    script = '''
import pandas as pd
from src.daily_digest import build_daily_score_report, seal_daily_score_report
from src.daily_digest_ui import render_daily_score_page
report = build_daily_score_report(pd.DataFrame(), as_of="2026-10-23")
report["virtual_tracking"] = dict(status="partial_failure", available_at="2026-10-23T22:40:00Z",
    new_proposals=2, new_fills=None, pending_proposals=None, issues=1, ledger_incidents=None,
    ready_experiments=1, preserved_experiments=1, experiments=[])
report = seal_daily_score_report(report)
class Journal:
    def get_daily_score_report(self, identifier):
        assert identifier == report["report_id"]
        return report
    def __getattr__(self, name):
        raise AssertionError("Forbidden collection, send or ledger access: " + name)
render_daily_score_page(Journal(), report_id=report["report_id"])
'''
    page = AppTest.from_string(script, default_timeout=30).run()
    assert not page.exception and not page.error
    assert any("Laboratorio virtual" in item.value for item in page.markdown)
    text = " ".join(item.value for item in page.markdown)
    assert "Actualización incompleta" in text and "N/D ejecuciones virtuales" in text
    assert page.warning


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
    assert not page.exception and not page.error and len(page.dataframe) == 2
    table = page.dataframe[0].value
    assert table.iloc[0]["Empresa"] == "Compañía de prueba (TEST)"
    assert table.iloc[0]["Nota · sesión"] == "— · 23/10"
    assert table.iloc[0]["Señal del motor"] == "Sin dato de esta sesión"
    assert table.iloc[0]["Verificación"] == "Datos pendientes"
    assert table.iloc[0]["Revisión de compra"] == "Compra no confirmada"
    assert "deterioro" not in str(table).lower()
    assert "Anterior" not in table and "-2 sesiones" not in table
    assert len(page.dataframe[1].value) == 5


def _row(ticker="TEST", *, blocked=True, score=93, held=False, label="Entrada fuerte", status=None):
    return {"ticker": ticker, "company_name": f"Empresa {ticker}", "held": held,
        "score": score, "label": label,
        "status": status or ("review_data" if blocked else "entry_strong"),
        "session": "2026-10-23", "recorded_at": "2026-10-23T22:30:00Z",
        "quality": {"block_entry": blocked, "status": "unknown" if blocked else "ok",
            "reasons": ["No consta el periodo del crecimiento de ingresos."] if blocked else []},
        "history": [{"session": "2026-10-23", "score": score},
            {"session": "2026-10-22", "score": None}],
        "change": None, "source": "Yahoo Financial Statements + SEC EDGAR"}


def test_compact_table_preserves_strong_raw_signal_without_confirming_purchase():
    row = _row()
    table = _table([row])
    assert table.iloc[0]["Señal del motor"] == "Entrada fuerte"
    assert table.iloc[0]["Nota · sesión"] == "93 · 23/10"
    assert table.iloc[0]["Verificación"] == "Datos pendientes"
    assert table.iloc[0]["Revisión de compra"] == "Compra no confirmada"
    assert row["score"] == 93 and row["label"] == "Entrada fuerte"
    assert list(table.columns) == ["Empresa", "Nota · sesión", "Cambio", "Señal del motor", "Verificación", "Revisión de compra"]


def test_history_only_displays_recorded_sessions_and_keeps_missing_scores():
    row = _row()
    history = _history(row)
    assert list(history["Sesión"]) == ["23/10", "22/10"]
    assert list(history["Nota"]) == ["93", "—"]
    assert _history({"history": []}).empty


@pytest.mark.parametrize("value,expected", [("88", "88"), ("-10", "-10"), (0, "0"),
                                           (True, "—"), (float("nan"), "—"), (None, "—")])
def test_legacy_numeric_values_remain_consistent_with_email_without_filling_gaps(value, expected):
    assert _display_note(value) == expected


@pytest.mark.parametrize("legacy_quality", [None, {}, {"status": "unknown"}])
def test_legacy_quality_never_promotes_raw_strong_signal(legacy_quality):
    row = _row(blocked=False)
    row["quality"] = legacy_quality
    table = _table([row])
    assert table.iloc[0]["Señal del motor"] == "Entrada fuerte"
    assert table.iloc[0]["Revisión de compra"] == "Compra no confirmada"


def test_ui_orders_held_then_verified_strong_then_pending_and_shows_provenance():
    rows = [_row("HELD", held=True), _row("PENDING", score=99),
        _row("VERIFIED", blocked=False, score=88),
        _row("WAIT", blocked=False, score=70, label="Esperar", status="wait")]
    script = '''
from src.daily_digest import seal_daily_score_report
from src.daily_digest_ui import render_daily_score_page
ROWS = REPLACE_ROWS
report = seal_daily_score_report({"as_of": "2026-10-23", "calendar_status": "complete", "rows": ROWS})
class Journal:
    def get_daily_score_report(self, report_id): return report
    def __getattr__(self, name): raise AssertionError("Forbidden download, send or mutation: " + name)
render_daily_score_page(Journal(), report_id=report["report_id"])
'''.replace("REPLACE_ROWS", repr(rows))
    page = AppTest.from_string(script, default_timeout=30).run()
    assert not page.exception and not page.error
    tables = [item.value for item in page.dataframe]
    assert list(tables[0]["Empresa"]) == ["Empresa HELD (HELD)"]
    assert list(tables[1]["Empresa"]) == ["Empresa VERIFIED (VERIFIED)", "Empresa PENDING (PENDING)"]
    assert tables[1].iloc[1]["Revisión de compra"] == "Compra no confirmada"
    assert list(tables[2]["Empresa"]) == ["Empresa WAIT (WAIT)"]
    captions = " ".join(str(item.value) for item in page.caption)
    assert "2 entradas fuertes del motor: 1 con datos suficientes y 1 pendientes" in captions
    assert "No consta el periodo del crecimiento de ingresos." in captions
    assert "Yahoo Financial Statements + SEC EDGAR" in captions
    assert "2026-10-23T22:30:00Z" in captions
    assert any("2 de 4" in str(item.value) for item in page.warning)
    assert any("Compra no confirmada" in str(item.value) for item in page.info)


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
