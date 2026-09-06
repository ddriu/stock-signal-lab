from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _authenticated_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AppTest:
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_SIGNAL_LAB_USERNAME", "ddriu")
    monkeypatch.setenv("STOCK_SIGNAL_LAB_PASSWORD_HASH", "test-only-hash")
    app = AppTest.from_file(str(APP_PATH), default_timeout=30)
    app.session_state["_authenticated_user"] = "ddriu"
    return app


@pytest.mark.parametrize(
    ("section", "detail_key", "detail"),
    [
        ("Empresa", "analysis_company_navigation", "Comparar empresas"),
        ("Estrategias", "analysis_strategy_navigation", "Especulativas"),
        ("Validar", "analysis_validation_navigation", "Evolución del análisis"),
        ("Validar", "analysis_validation_navigation", "Resultado posterior"),
        ("Validar", "analysis_validation_navigation", "Backtest técnico"),
    ],
)
def test_reorganized_analysis_destinations_open_without_market_data(
    section: str,
    detail_key: str,
    detail: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _authenticated_app(monkeypatch, tmp_path)
    app.session_state["main_navigation"] = "Analizar"
    app.session_state["analysis_navigation"] = section
    app.session_state[detail_key] = detail

    app.run()

    assert not app.exception
    assert not app.error
    assert app.session_state["analysis_navigation"] == section
    assert app.session_state[detail_key] == detail


def test_capital_projection_is_part_of_portfolios(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _authenticated_app(monkeypatch, tmp_path)
    app.session_state["main_navigation"] = "Carteras"
    app.session_state["portfolio_navigation"] = "Plan de capital"

    app.run()

    assert not app.exception
    assert not app.error
    assert app.session_state["portfolio_navigation"] == "Plan de capital"
    assert any("Proyección de capital" in item.value for item in app.markdown)


def test_old_tools_route_is_migrated_to_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _authenticated_app(monkeypatch, tmp_path)
    app.session_state["main_navigation"] = "Analizar"
    app.session_state["analysis_navigation"] = "Más análisis"
    app.session_state["analysis_tool_navigation"] = "Prueba con el pasado"

    app.run()

    assert not app.exception
    assert app.session_state["analysis_navigation"] == "Validar"
    assert app.session_state["analysis_validation_navigation"] == "Backtest técnico"


def test_queued_strategy_is_applied_before_streamlit_builds_the_widget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _authenticated_app(monkeypatch, tmp_path)
    app.session_state["main_navigation"] = "Analizar"
    app.session_state["analysis_navigation"] = "Estrategias"
    app.session_state["analysis_strategy_navigation"] = "Calidad fundamental"
    app.session_state["_requested_analysis_strategy_navigation"] = "Especulativas"

    app.run()

    assert not app.exception
    assert not app.error
    assert app.session_state["analysis_strategy_navigation"] == "Especulativas"
    assert "_requested_analysis_strategy_navigation" not in app.session_state


def test_speculative_completion_never_mutates_an_instantiated_widget() -> None:
    source = APP_PATH.read_text(encoding="utf-8")
    start = source.index('and st.session_state.get("_pending_speculative_discovery")')
    end = source.index('for error in st.session_state.get("download_errors", [])', start)
    completion_block = source[start:end]

    assert (
        'st.session_state["_requested_analysis_strategy_navigation"]'
        in completion_block
    )
    assert 'st.session_state["analysis_strategy_navigation"] =' not in completion_block


def test_open_company_is_queued_after_navigation_widgets_exist() -> None:
    script = '''
import streamlit as st
from app import _open_ticker_analysis

st.segmented_control(
    "Principal", ["Inicio", "Analizar"], key="main_navigation", required=True
)
st.segmented_control(
    "Análisis", ["Radar", "Empresa"], key="analysis_navigation", required=True
)
st.segmented_control(
    "Empresa", ["Análisis individual", "Comparar empresas"],
    key="analysis_company_navigation", required=True,
)
if st.button("Abrir MA"):
    _open_ticker_analysis("MA")
    st.rerun()
'''
    app = AppTest.from_string(script, default_timeout=15).run()

    app.button[0].click().run()

    state = app.session_state.filtered_state
    assert not app.exception
    assert state["_requested_main_navigation"] == "Analizar"
    assert state["_requested_analysis_navigation"] == "Empresa"
    assert state["_requested_analysis_company_navigation"] == "Análisis individual"
    assert state["_requested_analysis_ticker"] == "MA"


def test_navigation_helper_queues_portfolio_child_instead_of_mutating_widget() -> None:
    script = '''
import streamlit as st
from app import _set_navigation

st.segmented_control(
    "Principal", ["Inicio", "Carteras"], key="main_navigation", required=True
)
st.segmented_control(
    "Cartera", ["Privada", "Grupo", "Plan de capital"],
    key="portfolio_navigation", required=True,
)
if st.button("Abrir cartera"):
    _set_navigation("Carteras", "portfolio_navigation", "Privada")
    st.rerun()
'''
    app = AppTest.from_string(script, default_timeout=15).run()

    app.button[0].click().run()

    state = app.session_state.filtered_state
    assert not app.exception
    assert state["_requested_main_navigation"] == "Carteras"
    assert state["_requested_portfolio_navigation"] == "Privada"
