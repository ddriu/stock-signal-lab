"""Synthetic Streamlit state tests; no real users, providers or email."""
from datetime import date, datetime, timezone
import csv
from io import StringIO
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from src.external_analyst_store import ExternalAnalystStore
from src.external_analyst_ui import _csv_cell, _publication, build_call_rows, filtered_calls_csv
from src.external_analysts import normalize_call


def call(source="one", **changes):
    data = {"source_id": source, "provider": "perito_inversor", "company_name": "Empresa sintética",
            "ticker": "SYN", "analyst": "Ana", "currency": "EUR", "entry": 10, "stop": 9, "target": 12,
            "received_at": "2026-11-12T09:00:00Z", "observed_at": "2026-11-12T09:01:00Z",
            "source_url": "https://peritoinversor.com/synthetic", "action": "buy", "direction": "long",
            "entry_rule": "limit", "source_status": "pending"}
    data.update(changes)
    return normalize_call(data)


def test_compact_projection_prioritizes_holdings_without_inventing_missing_levels():
    rows = build_call_rows([call("other", ticker="OTHER"), call("held", ticker="HELD"),
                            call("favorite", ticker="FAV"), call("unknown", ticker=None, entry=None)],
                           favorites={"FAV"}, held={"HELD"})
    assert [row["Ticker"] for row in rows] == ["HELD", "FAV", "OTHER", "N/D"]
    assert rows[-1]["Entrada"] == "N/D"
    assert rows[-1]["Estado"] == "No verificable"
    assert not any("score" in key for row in rows for key in row)


def test_filters_are_explicit_and_missing_dates_not_projected_as_today():
    calls = [call(), call("two", ticker="TWO", analyst="Beatriz", observed_at="2026-11-13T10:00:00Z")]
    assert len(build_call_rows(calls, analyst="Ana", since=date(2026,11,12), until=date(2026,11,12))) == 1
    assert len(build_call_rows(calls, favorites={"TWO"}, scope="Mis favoritas")) == 1
    assert build_call_rows(calls, held=set(), scope="Mi cartera") == []
    assert build_call_rows(calls, status="historical") == []


def test_company_filter_includes_recommendations_without_a_documented_ticker():
    calls = [call(), call("company-only", company_name="Empresa sin ticker", ticker=None)]
    rows = build_call_rows(calls, company=("Empresa sin ticker", ""))
    assert len(rows) == 1 and rows[0]["Empresa"] == "Empresa sin ticker"
    assert rows[0]["Ticker"] == "N/D"


@pytest.mark.parametrize("text", ["=HYPERLINK(\"https://example.invalid\")", "+SUM(1,2)", "-SUM(1,2)",
                                   "@SUM(1,2)", "\t=SUM(1,2)", "\r=SUM(1,2)", "\ufeff=SUM(1,2)",
                                   "\x00=SUM(1,2)", "\x80=SUM(1,2)"])
def test_csv_cells_neutralize_spreadsheet_formulas(text):
    assert _csv_cell(text) == "'" + text
    assert _csv_cell(None) == ""
    assert _csv_cell(10) == "10"


def test_csv_exports_only_filtered_facts_with_safe_formulas_and_no_letter_data():
    selected = call("selected", company_name='=HYPERLINK("https://example.invalid")', analyst="@SUM(1,2)")
    selected["raw_body"] = "private letter must never be exported"
    calls = [selected, call("excluded", ticker="OTHER")]
    rows = build_call_rows(calls, ticker="SYN")
    exported = list(csv.DictReader(StringIO(filtered_calls_csv(calls, rows).decode("utf-8-sig"))))
    assert len(exported) == 1 and exported[0]["Ticker"] == "SYN"
    assert exported[0]["Empresa"].startswith("'=HYPERLINK")
    assert exported[0]["Analista"] == "'@SUM(1,2)"
    assert exported[0]["Fuente"] == selected["source_url"]
    assert exported[0]["Entrada"] == "10"
    assert exported[0]["Técnica histórica"] == ""
    assert "raw_body" not in exported[0]
    assert "private letter" not in str(exported)


def test_publication_with_day_precision_never_invents_a_midnight_time():
    assert _publication(call(evidence={"published_date": "2026-11-10"})) == "2026-11-10 (sin hora)"
    assert _publication(call()) == "N/D"


def seed(tmp_path, owner="synthetic-ui"):
    path = tmp_path / "ui.db"
    journal = SimpleNamespace(database_path=path, owner=owner)
    store = ExternalAnalystStore(journal)
    store.import_message({"message_id": "synthetic-only", "provider": "perito_inversor",
                          "received_at": "2026-11-12T09:00:00Z", "observed_at": "2026-11-12T09:01:00Z",
                          "announced_count": 4, "calls": [call()], "issues": []})
    return store


def app_for(path, owner="synthetic-ui"):
    return AppTest.from_string(f'''
from types import SimpleNamespace
from src.external_analyst_ui import render_external_analysts
journal = SimpleNamespace(database_path={str(path)!r}, owner={owner!r},
    list_analysis_snapshots=lambda: [], list_favorites=lambda: [{{"ticker":"SYN"}}], open_positions=lambda: [])
render_external_analysts(journal)
''')


def test_screen_rerender_is_private_read_only_without_mail_or_market_calls(tmp_path, monkeypatch):
    import imaplib
    import smtplib
    import socket
    def fail(*args, **kwargs):
        raise AssertionError("Navigation cannot access market, SMTP or IMAP")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fail)
    monkeypatch.setattr(smtplib, "SMTP", fail)
    monkeypatch.setattr(smtplib, "SMTP_SSL", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    store = seed(tmp_path)
    before = (store.list_messages(), store.list_revisions(), store.list_events(), store.list_simulation_runs(), store.list_deliveries())
    app = app_for(store.path).run(timeout=20)
    assert not app.exception
    assert app.metric[0].value == "1"
    assert any("anunciadas 4" in item.value for item in app.caption)
    app.radio(key="external_filter_scope").set_value("Mis favoritas").run()
    assert not app.exception
    assert (store.list_messages(), store.list_revisions(), store.list_events(), store.list_simulation_runs(), store.list_deliveries()) == before
    assert store.get_preferences() == {"enabled": False}


def test_second_owner_and_empty_list_never_render_first_owner_calls(tmp_path):
    store = seed(tmp_path)
    app = app_for(store.path, owner="synthetic-other").run(timeout=20)
    assert not app.exception
    assert app.metric[0].value == "0"
    assert any("Todavía no hay" in item.value for item in app.info)
    assert not app.dataframe
    assert len(store.list_calls()) == 1


def test_company_control_filters_by_documented_name_even_without_ticker(tmp_path):
    store = seed(tmp_path)
    store.import_message({"message_id": "company-only", "provider": "perito_inversor",
        "received_at": "2026-11-12T09:00:00Z", "observed_at": "2026-11-12T09:01:00Z",
        "calls": [call("company-only", company_name="Empresa sin ticker", ticker=None)], "issues": []})
    app = app_for(store.path).run(timeout=20)
    assert not app.exception
    select = app.selectbox(key="external_filter_company")
    assert "Empresa sin ticker (Ticker N/D)" in select.options
    select.set_value(("Empresa sin ticker", "")).run()
    assert not app.exception
    first = app.dataframe[0].value
    assert first["Empresa"].tolist() == ["Empresa sin ticker (N/D)"]


def test_expired_recommendations_project_current_state_without_writing_history(tmp_path, monkeypatch):
    import src.external_analyst_ui as ui
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls(2026, 11, 13, 12, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr(ui, "datetime", FrozenDatetime)
    store = seed(tmp_path)
    store.import_message({"message_id": "expires", "provider": "perito_inversor",
        "received_at": "2026-11-12T09:00:00Z", "observed_at": "2026-11-12T09:02:00Z",
        "calls": [call(expires_at="2026-11-12T10:00:00Z")], "issues": []})
    before = store.list_revisions()
    app = app_for(store.path).run(timeout=20)
    assert not app.exception
    assert app.dataframe[0].value["Estado"].tolist() == ["Caducada"]
    assert app.metric[1].value == "0"
    assert store.list_revisions() == before


def test_opt_in_explicit_and_comparison_activation_do_not_send_or_modify_calls(tmp_path):
    store = seed(tmp_path)
    app = app_for(store.path).run(timeout=20)
    app.checkbox(key="external_summary_enabled").check().run()
    assert store.get_preferences() == {"enabled": False}
    app.button(key="external_save_preferences").click().run()
    assert not app.exception
    assert store.get_preferences()["enabled"] is True
    assert store.list_deliveries() == []
    app.button(key="external_activate").click().run()
    assert not app.exception
    activations = [row["payload"] for row in store.list_simulation_runs() if row["payload"]["kind"] == "activation"]
    assert len(activations) == 1 and activations[0]["initial_cash_per_account_eur"] == 100
    app.run()
    assert not app.exception
    assert any("aún no hay una sesión" in item.value for item in app.info)
    assert len(store.list_calls()) == 1


def test_unavailable_schema_is_visible_not_empty_opportunities():
    app = AppTest.from_string('''
from types import SimpleNamespace
from src.external_analyst_ui import render_external_analysts
def broken(*args,**kwargs):
    raise ValueError("synthetic storage unavailable")
journal=SimpleNamespace(owner="synthetic-ui",url="https://synthetic.invalid",_request=broken)
render_external_analysts(journal)
''').run(timeout=20)
    assert not app.exception
    assert any("migración" in error.value for error in app.error)
    assert not app.metric


def test_preview_from_another_owner_is_discarded(tmp_path):
    store = seed(tmp_path)
    app = app_for(store.path)
    app.session_state["external_ui_owner"] = "synthetic-other"
    app.session_state["external_preview_data"] = {"owner": "synthetic-other", "parsed": {"raw": "must-not-render"}}
    app.run(timeout=20)
    assert not app.exception
    with pytest.raises(KeyError):
        _ = app.session_state["external_preview_data"]


def test_import_requires_rights_confirmation_then_refreshes_saved_table(tmp_path, monkeypatch):
    import streamlit as st
    raw = ("source_id,ticker,company_name,currency,received_at,entry,stop,target,source_url,action,direction,entry_rule\n"
           "manual-second,NEW,Empresa nueva,EUR,2026-11-10T09:00:00Z,20,19,24,https://peritoinversor.com/new,buy,long,limit\n").encode()
    upload = SimpleNamespace(name="synthetic.csv", getvalue=lambda: raw)
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    store = seed(tmp_path)
    app = app_for(store.path).run(timeout=20)
    assert app.button(key="external_preview").disabled
    app.checkbox(key="external_lawful_import").check().run()
    app.button(key="external_preview").click().run()
    assert not app.exception
    assert len(store.list_calls()) == 1
    app.button(key="external_confirm_import").click().run()
    assert not app.exception
    assert len(store.list_calls()) == 2
    assert app.metric[0].value == "2"
    assert any("Guardadas 1" in item.value for item in app.success)
    assert store.list_calls()[1]["received_at"].startswith("2026-11-10")
    assert store.list_deliveries() == []


def test_email_upload_larger_than_2mb_never_reaches_the_parser(tmp_path, monkeypatch):
    import streamlit as st
    import src.external_analyst_ingest as ingest
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: SimpleNamespace(name="synthetic.eml", getvalue=lambda: b"x" * 2_000_001))
    def forbidden(*args, **kwargs):
        raise AssertionError("Oversize upload must not be parsed")
    monkeypatch.setattr(ingest, "parse_email_bytes", forbidden)
    store = seed(tmp_path)
    app = app_for(store.path).run(timeout=20)
    app.checkbox(key="external_lawful_import").check().run()
    app.button(key="external_preview").click().run()
    assert not app.exception
    assert any("No se pudo preparar" in item.value for item in app.error)
    assert len(store.list_messages()) == 1


def test_partial_ingestion_and_next_report_are_visible_without_sending(tmp_path):
    store = seed(tmp_path)
    store.save_simulation_run("mail-check", {"kind": "mail_ingestion", "observed_at": "2026-11-12T09:10:00Z",
        "status": "partial", "scanned": 3, "remaining": 2, "failures": [{"message_id": "a"*64, "code": "ValueError"}],
        "issues": ["imap_logout_unconfirmed"]})
    app = app_for(store.path).run(timeout=20)
    assert not app.exception
    assert any("pendientes 2" in item.value for item in app.caption)
    assert any("no se pudieron verificar" in item.value for item in app.warning)
    assert any("Analistas externos" in item.value for item in app.text)
    assert store.list_deliveries() == []


def test_individual_analyst_metrics_are_displayed_without_predictive_claim(tmp_path):
    from src.external_analyst_service import activate_comparison, advance_comparison
    store = seed(tmp_path)
    activate_comparison(store, now="2026-11-12T09:01:00Z")
    advance_comparison(store, [{"ticker": "SYN", "currency": "EUR", "open_at": "2026-11-13T09:00:00Z",
        "close_at": "2026-11-13T17:00:00Z", "available_at": "2026-11-13T17:20:00Z",
        "open": 10, "high": 12.5, "low": 9.5, "close": 12.2}], now="2026-11-13T18:00:00Z")
    app = app_for(store.path).run(timeout=20)
    app.selectbox(key="external_comparison_account").set_value("D").run()
    assert not app.exception
    frames = [item.value for item in app.dataframe]
    assert any("Cobertura %" in frame.columns and "Ganancia media €" in frame.columns for frame in frames)
    assert all("Conclusión fiable" not in frame.columns for frame in frames)
    assert any("no demuestra capacidad predictiva" in item.value for item in app.caption)
