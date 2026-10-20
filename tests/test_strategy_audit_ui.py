"""Synthetic Streamlit audit workflow: no network, credentials, or account writes."""

from datetime import date

from streamlit.testing.v1 import AppTest


SCRIPT = '''
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import src.strategy_audit_ui as ui
import src.strategy_audit_service as service
import src.strategy_audit_engine as engine
import src.strategy_audit_diagnostics as diagnostics

private_email = "private-owner@example.invalid"
scenario = SCENARIO
account = st.radio("Cuenta sintética", ["A", "B"], key="test_account")
class Journal:
    owner = private_email if account == "A" else "other-owner@example.invalid"
    def list_analysis_snapshots(self):
        st.session_state["reads"] = st.session_state.get("reads", 0) + 1
        if scenario == "load_error":
            raise RuntimeError(private_email)
        return pd.DataFrame([
            dict(id=1, ticker="OLD", analyzed_at="2025-01-02", created_at="2025-01-02T21:01:00Z",
                 entry_label="Vigilancia", entry_score=60, owner=private_email, note=""),
            dict(id=2, ticker="OLD", analyzed_at="2025-01-03", created_at="2025-01-03T21:01:00Z",
                 entry_label="Entrada fuerte", entry_score=80, owner=private_email, note=""),
            dict(id=3, ticker="NEW", analyzed_at="2025-01-03", created_at="2025-01-03T21:02:00Z",
                 entry_label="Entrada fuerte", entry_score=85, owner=private_email, note=""),
        ])
    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            raise AssertionError("Unexpected journal mutation/account access: " + name)
        return forbidden

def fake_fetch(tickers, start, end, **kwargs):
    st.session_state["downloads"] = st.session_state.get("downloads", 0) + 1
    st.session_state["requested_tickers"] = list(tickers)
    if scenario == "download_error":
        raise RuntimeError(private_email)
    return SimpleNamespace(bars={ticker: pd.DataFrame({"close":[100.]}) for ticker in tickers},
        manifest={"period":[str(start),str(end)], "owner":private_email, "version":"synthetic"},
        issues=pd.DataFrame([dict(code="missing_quote", detail=private_email)]) if scenario == "incomplete" else pd.DataFrame())

def fake_run(signals, bars, config):
    calls = list(st.session_state.get("calculations", []))
    calls.append(dict(config=asdict(config), ids=signals.event_id.tolist()))
    st.session_state["calculations"] = calls
    eligible = signals.loc[signals.entry_label.eq("Entrada fuerte")].copy()
    if scenario == "incomplete" and config.exit_policy != "hold":
        eligible = eligible.iloc[:1]
    rows = [dict(lot_id="lot-"+row["event_id"], event_id=row["event_id"], ticker=row["ticker"],
        entry_at=pd.Timestamp("2025-01-06T14:30:00Z"), contribution_usd=config.amount_usd,
        quantity=.01, sector="Synthetic", status="open", value_usd=config.amount_usd*1.05,
        proceeds_usd=None, realized_pnl_usd=None, owner=private_email) for row in eligible.to_dict("records")]
    lots = pd.DataFrame(rows)
    if lots.empty:
        lots = pd.DataFrame(columns=["lot_id","event_id","ticker","entry_at","contribution_usd",
                                    "quantity","sector","status","value_usd","proceeds_usd","realized_pnl_usd"])
    amount = len(lots) * config.amount_usd
    complete = scenario != "incomplete"
    metrics = dict(contributions_usd=amount, final_value_usd=amount*1.05, profit_usd=amount*.05,
                   roi_pct=5., coverage_complete=complete)
    if scenario == "incomplete":
        metrics.update(last_complete_valuation_date="2025-01-06", last_complete_value_usd=amount*1.05,
                       last_complete_contributions_usd=amount, last_complete_roi_pct=5., valuation_complete=False)
    daily = pd.DataFrame(dict(date=["2025-01-06"],value_usd=[amount*1.05],
                             contributions_usd=[amount],contribution_usd=[amount])).set_index("date")
    return SimpleNamespace(lots=lots, trades=pd.DataFrame(), issues=pd.DataFrame(), daily=daily,
                           metrics=metrics, manifest={"owner":private_email,"input_hash":"synthetic"})

def fake_benchmark(lots, bars, end):
    called = list(st.session_state.get("benchmark_ids", []))
    called.append(lots.event_id.tolist())
    st.session_state["benchmark_ids"] = called
    return dict(contributions_usd=len(lots),value_usd=len(lots)*1.02,roi_pct=2.,
                data_complete=scenario != "incomplete",pending_contributions=0)

def fake_excel(report, **kwargs):
    assert private_email not in repr(vars(report)) + repr(kwargs)
    assert "date" in report.daily.columns
    st.session_state["exports"] = st.session_state.get("exports", 0) + 1
    return b"synthetic-excel-bytes"

with (patch.object(service,"fetch_audit_market_bundle",side_effect=fake_fetch),
      patch.object(engine,"run_strategy_audit",side_effect=fake_run),
      patch.object(service,"matched_benchmark",side_effect=fake_benchmark),
      patch.object(service,"audit_excel",side_effect=fake_excel),
      patch.object(service,"prospective_results",return_value=pd.DataFrame()),
      patch.object(diagnostics,"sector_results",return_value=pd.DataFrame()),
      patch.object(diagnostics,"exit_counterfactuals",return_value=pd.DataFrame())):
    ui.render_strategy_audit(Journal())
'''


def _page(scenario: str = "complete") -> AppTest:
    return AppTest.from_string(SCRIPT.replace("SCENARIO", repr(scenario)), default_timeout=30).run()


def _button(page: AppTest, label: str):
    return next(button for button in page.button if button.label == label)


def _text(page: AppTest) -> str:
    result = []
    for kind in ("markdown", "caption", "warning", "info", "error", "success", "json"):
        result.extend(str(item.value) for item in getattr(page, kind, []))
    result.extend(str(item.value) for item in page.dataframe)
    return " ".join(result)


def _load(page: AppTest) -> AppTest:
    _button(page, "Cargar historial guardado").click().run()
    assert not page.exception
    return page


def _calculate(scenario: str = "complete") -> AppTest:
    page = _load(_page(scenario))
    _button(page, "Descargar precios para esta auditoría").click().run()
    assert not page.exception
    _button(page, "Calcular auditoría con los datos descargados").click().run()
    assert not page.exception
    return page


def test_opening_page_does_not_read_history_download_or_calculate() -> None:
    page = _page()
    assert not page.exception
    assert "reads" not in page.session_state
    assert "downloads" not in page.session_state
    assert "calculations" not in page.session_state
    assert "señal guardada no demuestra" in _text(page)
    assert "private-owner@example.invalid" not in _text(page)


def test_history_and_prices_require_separate_explicit_actions() -> None:
    page = _load(_page())
    assert page.session_state["reads"] == 1
    assert "downloads" not in page.session_state
    assert _button(page, "Calcular auditoría con los datos descargados").disabled
    _button(page, "Descargar precios para esta auditoría").click().run()
    assert not page.exception
    assert page.session_state["downloads"] == 1
    assert set(page.session_state["requested_tickers"]) == {"OLD", "NEW", "SPY", "QQQ", "VT"}
    assert "calculations" not in page.session_state
    page.run()
    assert page.session_state["reads"] == 1
    assert page.session_state["downloads"] == 1


def test_growth_without_policy_does_not_substitute_technical_entries() -> None:
    page = _load(_page())
    next(item for item in page.selectbox if item.label == "Fuente de las señales").set_value("growth").run()
    assert not page.exception
    assert "No se rellenan con señales de otro motor" in _text(page)
    assert _button(page, "Descargar precios para esta auditoría").disabled
    assert "downloads" not in page.session_state


def test_selected_result_hold_and_indices_have_matching_executed_contributions() -> None:
    page = _calculate("incomplete")
    calls = page.session_state["calculations"]
    assert len(calls) == 2
    assert calls[0]["config"]["exit_policy"] == "reduce"
    assert calls[1]["config"]["exit_policy"] == "hold"
    assert calls[1]["config"]["entry_mode"] == "daily"
    assert len(calls[0]["ids"]) == 3  # Includes non-entry activation context.
    assert len(calls[1]["ids"]) == 1  # Exactly the actually filled event.
    assert page.session_state["benchmark_ids"] == [calls[1]["ids"]] * 3
    assert "Cobertura insuficiente" in _text(page)
    assert next(metric for metric in page.metric if metric.label == "Rentabilidad total validada").value == "N/D"
    assert "2025-01-06" in _text(page)
    assert next(metric for metric in page.metric if metric.label == "Rentabilidad del corte parcial").value == "5.00%"
    assert "Resultado parcial, no rentabilidad de la estrategia completa" in _text(page)
    comparison = next(item.value for item in page.dataframe if "Modelo" in item.value)
    assert comparison["Rentabilidad %"].isna().all()
    assert not page.success
    assert "private-owner@example.invalid" not in _text(page)


def test_results_persist_without_repeated_downloads_or_calculations() -> None:
    page = _calculate()
    calls = len(page.session_state["calculations"])
    assert "Último cálculo guardado" in _text(page)
    page.run()
    assert not page.exception
    assert page.session_state["downloads"] == 1
    assert len(page.session_state["calculations"]) == calls
    assert "Último cálculo guardado" in _text(page)


def test_period_change_disables_calculation_and_labels_saved_result_as_previous() -> None:
    page = _calculate()
    next(item for item in page.date_input if item.label == "Hasta (día UTC completo)").set_value(date(2025, 1, 7)).run()
    assert not page.exception
    assert _button(page, "Calcular auditoría con los datos descargados").disabled
    assert "resultados son del cálculo anterior" in _text(page)
    assert page.session_state["downloads"] == 1


def test_controls_pass_activation_sizing_score_and_optional_exit_variant() -> None:
    page = _load(_page())
    next(item for item in page.selectbox if item.label == "Cuándo aportar").set_value("activation").run()
    next(item for item in page.selectbox if item.label == "Tamaño de cada aportación").set_value("tiered").run()
    next(item for item in page.number_input if item.label == "Nota mínima (0 = sin filtro)").set_value(85).run()
    page.multiselect[0].set_value(["fixed8"]).run()
    _button(page, "Descargar precios para esta auditoría").click().run()
    _button(page, "Calcular auditoría con los datos descargados").click().run()
    assert not page.exception
    calls = page.session_state["calculations"]
    assert calls[0]["config"]["entry_mode"] == "activation"
    assert calls[0]["config"]["sizing"] == "tiered"
    assert calls[0]["config"]["min_score"] == 85
    assert calls[-1]["config"]["exit_policy"] == "fixed8"


def test_excel_preparation_is_explicit_private_and_does_not_recalculate() -> None:
    page = _calculate()
    assert "exports" not in page.session_state
    calls = len(page.session_state["calculations"])
    _button(page, "Preparar Excel privado").click().run()
    assert not page.exception
    assert not page.error
    assert page.session_state["exports"] == 1
    assert page.get("download_button")
    page.run()
    assert page.session_state["exports"] == 1
    assert len(page.session_state["calculations"]) == calls


def test_different_owner_does_not_see_the_previous_owners_history_or_results() -> None:
    page = _calculate()
    page.radio[0].set_value("B").run()
    assert not page.exception
    assert "Último cálculo guardado" not in _text(page)
    assert "Primero carga el historial" in _text(page)
    assert "other-owner@example.invalid" not in _text(page)


def test_load_errors_do_not_expose_owner_email() -> None:
    page = _page("load_error")
    _button(page, "Cargar historial guardado").click().run()
    assert not page.exception
    assert page.error
    assert "private-owner@example.invalid" not in _text(page)


def test_download_errors_do_not_invent_prices_or_expose_owner_email() -> None:
    page = _load(_page("download_error"))
    _button(page, "Descargar precios para esta auditoría").click().run()
    assert not page.exception
    assert page.error
    assert _button(page, "Calcular auditoría con los datos descargados").disabled
    assert "private-owner@example.invalid" not in _text(page)
