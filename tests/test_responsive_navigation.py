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
