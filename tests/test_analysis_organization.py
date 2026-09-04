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
