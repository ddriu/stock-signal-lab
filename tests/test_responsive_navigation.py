from pathlib import Path

from src.ui import APP_CSS


ROOT = Path(__file__).resolve().parents[1]


def test_main_navigation_has_a_stable_scoped_container() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert 'st.container(key="main_navigation_container")' in source


def test_mobile_navigation_uses_horizontal_touch_rails() -> None:
    mobile_rules = APP_CSS[APP_CSS.index("@media (max-width: 700px)") :]

    assert '[class*="st-key-main_navigation_container"]' in APP_CSS
    assert '[class*="st-key-section_subnavigation"]' in APP_CSS
    # Streamlit 1.5x renders segmented controls as stButtonGroup. Keeping this
    # explicit prevents a visually plausible CSS rule that never reaches the DOM.
    assert '[data-testid="stButtonGroup"]' in mobile_rules
    assert "overflow-x: auto !important" in mobile_rules
    assert "flex-wrap: nowrap !important" in mobile_rules
    assert "-webkit-overflow-scrolling: touch" in mobile_rules
    assert "touch-action: pan-x" in mobile_rules


def test_mobile_tabs_scroll_instead_of_wrapping() -> None:
    mobile_rules = APP_CSS[APP_CSS.index("@media (max-width: 700px)") :]

    assert '[data-testid="stTabs"] [data-baseweb="tab-list"]' in mobile_rules
    assert "flex-wrap: nowrap" in mobile_rules
    assert "overflow-x: auto !important" in mobile_rules
def _css_rule(selector: str) -> str:
    start = APP_CSS.index(selector)
    opening_brace = APP_CSS.index("{", start)
    closing_brace = APP_CSS.index("}", opening_brace)
    return APP_CSS[opening_brace + 1 : closing_brace]


def test_app_container_keeps_streamlits_viewport_positioning() -> None:
    app_container_rule = _css_rule('[data-testid="stAppViewContainer"]')

    # Overriding this to relative makes the sidebar grow to its full content
    # height while the fixed Streamlit root clips it at the viewport edge.
    assert "position:" not in app_container_rule


def test_sidebar_is_the_vertical_touch_scroll_container() -> None:
    sidebar_rule = _css_rule('[data-testid="stSidebarContent"]')

    assert "max-height: 100dvh" in sidebar_rule
    assert "overflow-y: auto !important" in sidebar_rule
    assert "overscroll-behavior-y: contain" in sidebar_rule
    assert "-webkit-overflow-scrolling: touch" in sidebar_rule
    assert "touch-action: pan-y" in sidebar_rule


def test_search_popovers_remain_scrollable_on_small_screens() -> None:
    popover_rule = _css_rule('[data-testid="stPopoverBody"]')

    assert "max-height: min(74dvh, 42rem) !important" in popover_rule
    assert "overflow-y: auto !important" in popover_rule
    assert "overscroll-behavior: contain" in popover_rule
    assert "touch-action: pan-y" in popover_rule


def test_paper_seed_keeps_unpriced_capital_and_offers_day_one_rebuild() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    # La semilla ya no pide aceptar que desaparezcan posiciones: conserva el NAV
    # completo y deja sin operar las líneas que sólo tienen valor declarado.
    assert 'valuation_mode = "frozen"' in source
    assert 'key="paper_rebuild_complete_portfolio"' in source
    assert "Corregir Día 1 con las dos cuentas" in source
    assert "paper_partial_seed_start_form" not in source


def test_day_one_rebuild_does_not_depend_on_a_fresh_cached_journal() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    rebuild_guard = source[
        source.index("        can_rebuild = (") : source.index(
            "        rebuild = st.button(",
            source.index("        can_rebuild = ("),
        )
    ]

    # Streamlit puede conservar durante un despliegue una instancia de journal
    # creada con la clase anterior. Eso no debe impedir crear primero la nueva
    # temporada completa; el archivado de la parcial ya tiene un fallback seguro.
    assert 'hasattr(journal, "set_paper_simulation_status")' not in rebuild_guard
    assert "initial_state is not None" in rebuild_guard
    assert "market_date is not None" in rebuild_guard
    assert "benchmark_price is not None" in rebuild_guard
    assert "journal.set_paper_simulation_status(" in source
    assert "except (JournalStorageError, ValueError, AttributeError)" in source


def test_home_separates_real_portfolio_decisions_lab_and_navigation() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert '["Resumen", "Decisiones", "Laboratorio", "Accesos"]' in source
    assert "with summary_tab:" in source
    assert "with decisions_tab:" in source
    assert "with laboratory_tab:" in source
    assert "with access_tab:" in source
    assert "Este es un ensayo con una copia virtual de tus dos cuentas" in source
    assert "Patrimonio fuera del mapa de decisiones" in source


def test_hot_deploy_reloads_the_paper_model_before_using_the_new_schema() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert '"valuation_mode" not in getattr(' in source
    assert 'getattr(_paper_simulation_module, "PAPER_ENGINE_VERSION", "") != "paper-v2"' in source
    assert 'hasattr(_paper_simulation_module, "_position_valuation_mode")' in source
    assert "importlib.reload(_paper_simulation_module)" in source
    assert 'st.error(f"No se pudo crear la temporada completa: {exc}")' not in source
