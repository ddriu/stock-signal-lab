from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize("username", ["ddriu", "luci", "fer", "xavi", "alberite"])
def test_authenticated_user_can_open_home_without_requested_ticker(
    username: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """El primer render tras el login no debe exigir una empresa seleccionada."""

    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=30)
    app.session_state["_authenticated_user"] = username

    app.run()

    assert not app.exception
    assert not app.error
    assert app.session_state["main_navigation"] == "Inicio"


def test_analysis_company_page_exposes_explicit_new_company_search(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=30)
    app.session_state["_authenticated_user"] = "ddriu"
    app.session_state["main_navigation"] = "Analizar"
    app.session_state["analysis_navigation"] = "Empresa"

    app.run()

    assert not app.exception
    assert any(
        widget.label == "Nombre o ticker nuevo" for widget in app.text_input
    )
    assert any(button.label == "Buscar" for button in app.button)


def test_each_favorite_list_has_a_visible_add_company_action(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=30)
    app.session_state["_authenticated_user"] = "ddriu"
    app.session_state["main_navigation"] = "Favoritos"
    app.session_state["favorite_view"] = "Mis listas"

    app.run()

    assert not app.exception
    assert sum(button.label == "Añadir empresa" for button in app.button) == 2


def test_favorite_add_page_searches_by_name_or_symbol(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=30)
    app.session_state["_authenticated_user"] = "ddriu"
    app.session_state["main_navigation"] = "Favoritos"
    app.session_state["favorite_view"] = "Añadir empresa"

    app.run()

    assert not app.exception
    assert any(widget.label == "Nombre o símbolo" for widget in app.text_input)
    assert any(widget.label == "Dónde guardarla" for widget in app.radio)


@pytest.mark.parametrize("analysis_page", ["Radar", "Oportunidades"])
def test_analysis_overview_pages_open_without_market_data(
    analysis_page: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=30)
    app.session_state["_authenticated_user"] = "ddriu"
    app.session_state["main_navigation"] = "Analizar"
    app.session_state["analysis_navigation"] = analysis_page

    app.run()

    assert not app.exception
    assert app.session_state["analysis_navigation"] == analysis_page
