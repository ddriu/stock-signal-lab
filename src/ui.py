"""Identidad visual y perfiles sencillos de configuración."""

from __future__ import annotations


PROFILE_NAMES = ("Equilibrado", "Crecimiento", "Prudente", "Personalizado")


_BALANCED = {
    "sma_short": 20,
    "sma_medium": 50,
    "sma_long": 200,
    "rsi_period": 14,
    "rsi_buy_min": 45,
    "rsi_buy_max": 68,
    "rsi_overbought": 78,
    "max_distance": 12.0,
    "watch_score": 55,
    "buy_score": 65,
    "strong_score": 75,
    "reduce_score": 40,
    "sell_score": 25,
    "confirmation_days": 2,
    "breakout_period": 20,
    "near_high": 12.0,
    "volume_surge": 1.2,
    "volume_normal": 0.8,
    "stop_loss": 8.0,
    "trailing_stop": 10.0,
    "max_risk": 1.0,
    "forward_horizon": 20,
    "exit_on_reduce": True,
}


STRATEGY_PROFILES = {
    "Equilibrado": _BALANCED,
    "Crecimiento": {
        **_BALANCED,
        "rsi_buy_min": 48,
        "rsi_buy_max": 72,
        "rsi_overbought": 82,
        "max_distance": 15.0,
        "near_high": 15.0,
        "stop_loss": 10.0,
        "trailing_stop": 12.0,
        "forward_horizon": 40,
    },
    "Prudente": {
        **_BALANCED,
        "rsi_buy_max": 62,
        "rsi_overbought": 72,
        "max_distance": 8.0,
        "watch_score": 60,
        "buy_score": 70,
        "strong_score": 80,
        "reduce_score": 45,
        "sell_score": 30,
        "confirmation_days": 3,
        "near_high": 8.0,
        "volume_normal": 0.9,
        "volume_surge": 1.3,
        "stop_loss": 6.0,
        "trailing_stop": 8.0,
        "max_risk": 0.5,
    },
}


def strategy_profile_defaults(name: str) -> dict[str, object]:
    """Devuelve una copia para evitar que la interfaz modifique el preset."""

    profile = STRATEGY_PROFILES.get(name, _BALANCED)
    return dict(profile)


def score_tone(score: float | int | None) -> str:
    """Color semántico de una nota sin depender únicamente del color visible."""

    if score is None:
        return "neutral"
    value = float(score)
    if value >= 75:
        return "positive"
    if value >= 55:
        return "watch"
    if value >= 40:
        return "caution"
    return "negative"


def signal_tone(label: str) -> str:
    normalized = label.strip().lower()
    if normalized in {
        "oportunidad destacada",
        "candidata",
        "entrada fuerte",
        "entrada interesante",
        "mantener",
    }:
        return "positive"
    if normalized in {"vigilancia", "esperar"}:
        return "watch"
    if normalized == "reducir":
        return "caution"
    if normalized == "vender":
        return "negative"
    return "neutral"


APP_CSS = """
:root {
    --ssl-ink: #10201f;
    --ssl-muted: #667876;
    --ssl-border: #d7e2df;
    --ssl-surface: #ffffff;
    --ssl-bg: #f3f7f6;
    --ssl-primary: #087a5c;
    --ssl-primary-strong: #075f4a;
    --ssl-primary-soft: #e3f5ef;
    --ssl-aqua: #43c7a2;
    --ssl-blue: #2776d2;
    --ssl-watch: #b7791f;
    --ssl-watch-soft: #fff7df;
    --ssl-danger: #c2413a;
    --ssl-danger-soft: #fff0ef;
    --ssl-shadow: 0 18px 48px rgba(20, 58, 51, 0.09);
}

[data-testid="stAppViewContainer"] {
    position: relative;
    background:
        radial-gradient(circle at 92% 2%, rgba(67, 199, 162, 0.13), transparent 29rem),
        radial-gradient(circle at 8% 34%, rgba(39, 118, 210, 0.055), transparent 23rem),
        linear-gradient(rgba(8, 122, 92, 0.024) 1px, transparent 1px),
        linear-gradient(90deg, rgba(8, 122, 92, 0.024) 1px, transparent 1px),
        var(--ssl-bg);
    background-size: auto, auto, 36px 36px, 36px 36px, auto;
    background-attachment: fixed;
}

[data-testid="stHeader"] {
    background: rgba(243, 247, 246, 0.80);
    backdrop-filter: blur(14px) saturate(130%);
}

.block-container {
    max-width: 1380px;
    padding-top: 1.35rem;
    padding-bottom: 3rem;
}

[data-testid="stSidebar"] {
    background:
        linear-gradient(180deg, rgba(227, 245, 239, 0.78), rgba(251, 253, 252, 0.96) 15rem);
    border-right: 1px solid var(--ssl-border);
}

[data-testid="stSidebar"] [data-testid="stButton"] button {
    min-height: 2.8rem;
}

h1, h2, h3, h4 {
    color: var(--ssl-ink);
    letter-spacing: -0.025em;
}

p, label, [data-testid="stCaptionContainer"] {
    line-height: 1.55;
}

div[data-testid="stForm"],
div[data-testid="stExpander"],
div[data-testid="stDataFrame"],
div[data-testid="stPlotlyChart"],
div[data-testid="stMetric"] {
    border-radius: 16px;
}

div[data-testid="stExpander"] {
    background: rgba(255, 255, 255, 0.9);
    border: 1px solid var(--ssl-border);
    box-shadow: 0 5px 18px rgba(15, 23, 42, 0.035);
    overflow: hidden;
}

div[data-testid="stExpander"] summary {
    font-weight: 720;
    color: var(--ssl-ink);
}

div[data-testid="stVerticalBlockBorderWrapper"] {
    background: rgba(255, 255, 255, 0.90);
    backdrop-filter: blur(8px);
    border-radius: 16px;
    box-shadow: 0 5px 18px rgba(15, 23, 42, 0.035);
}

div[data-testid="stMetric"] {
    background: var(--ssl-surface);
    border: 1px solid var(--ssl-border);
    box-shadow: 0 6px 20px rgba(15, 23, 42, 0.045);
    padding: 1rem 1.05rem;
    min-height: 7.2rem;
}

div[data-testid="stMetricLabel"] {
    color: var(--ssl-muted);
}

div[data-testid="stMetricValue"] {
    color: var(--ssl-ink);
    font-weight: 750;
}

.stButton > button[kind="primary"],
.stFormSubmitButton > button[kind="primary"] {
    background: linear-gradient(135deg, var(--ssl-primary-strong), #0aa276);
    border: 0;
    box-shadow: 0 8px 20px rgba(8, 127, 91, 0.20);
}

.stButton > button,
.stFormSubmitButton > button,
button[data-baseweb="tab"] {
    min-height: 2.75rem;
    border-radius: 12px;
}

.stButton > button,
.stFormSubmitButton > button {
    font-weight: 680;
    transition: border-color 150ms ease, box-shadow 150ms ease, transform 150ms ease;
}

.stButton > button:hover,
.stFormSubmitButton > button:hover {
    border-color: rgba(8, 127, 91, 0.55);
    box-shadow: 0 7px 18px rgba(15, 23, 42, 0.08);
    transform: translateY(-1px);
}

[data-testid="stPopover"] > button {
    background: rgba(255, 255, 255, 0.92);
    border-color: var(--ssl-border);
}

[data-testid="stSegmentedControl"] {
    background: rgba(255, 255, 255, 0.86);
    border: 1px solid var(--ssl-border);
    border-radius: 15px;
    padding: 0.35rem;
    box-shadow: 0 5px 18px rgba(15, 23, 42, 0.04);
}

[data-testid="stSegmentedControl"] button {
    min-height: 2.7rem;
    font-weight: 680;
    letter-spacing: -0.01em;
    transition: color 140ms ease, background 140ms ease, transform 140ms ease;
}

[data-testid="stSegmentedControl"] button:hover {
    color: var(--ssl-primary-strong);
    transform: translateY(-1px);
}

[data-testid="stTabs"] [data-baseweb="tab-list"] {
    gap: 0.3rem;
    overflow-x: auto;
    scrollbar-width: thin;
}

.ssl-app-header {
    display: flex;
    align-items: center;
    gap: 0.9rem;
    margin: 0.2rem 0 1rem;
}

.ssl-logo {
    width: 3.1rem;
    height: 3.1rem;
    flex: 0 0 3.1rem;
    display: grid;
    place-items: center;
    border-radius: 15px;
    color: white;
    padding: 0.58rem;
    background:
        radial-gradient(circle at 72% 18%, rgba(90, 231, 190, 0.46), transparent 35%),
        linear-gradient(145deg, #123936, #07805f);
    box-shadow: 0 13px 30px rgba(10, 83, 66, 0.24);
}

.ssl-logo svg {
    width: 100%;
    height: 100%;
    display: block;
}

.ssl-app-title {
    margin: 0;
    font-size: clamp(1.65rem, 4vw, 2.35rem);
    line-height: 1.08;
}

.ssl-app-subtitle {
    margin: 0.28rem 0 0;
    color: var(--ssl-muted);
    font-size: 0.95rem;
}

.ssl-status-pill {
    margin-left: auto;
    padding: 0.45rem 0.72rem;
    border-radius: 999px;
    background: var(--ssl-primary-soft);
    color: var(--ssl-primary);
    font-weight: 700;
    font-size: 0.78rem;
    white-space: nowrap;
}

.ssl-hero {
    padding: clamp(1.2rem, 4vw, 2rem);
    border-radius: 22px;
    color: white;
    background:
        linear-gradient(125deg, rgba(15, 23, 42, 0.98), rgba(8, 127, 91, 0.92));
    box-shadow: var(--ssl-shadow);
    margin: 1rem 0 1.2rem;
}

.ssl-hero h2 {
    color: white;
    margin: 0 0 0.35rem;
    font-size: clamp(1.4rem, 4vw, 2rem);
}

.ssl-hero p {
    color: rgba(255, 255, 255, 0.82);
    margin: 0;
    max-width: 50rem;
}

.ssl-page-intro {
    position: relative;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: clamp(1rem, 4vw, 3rem);
    padding: clamp(1rem, 3vw, 1.45rem);
    margin: 0.75rem 0 1rem;
    border: 1px solid var(--ssl-border);
    border-radius: 20px;
    background:
        linear-gradient(125deg, rgba(255, 255, 255, 0.98), rgba(225, 245, 238, 0.88));
    box-shadow: 0 12px 34px rgba(20, 58, 51, 0.07);
}

.ssl-page-intro::after {
    content: "";
    position: absolute;
    width: 9rem;
    height: 9rem;
    right: -3rem;
    top: -4rem;
    border-radius: 50%;
    background: rgba(8, 127, 91, 0.08);
}

.ssl-page-intro-copy {
    position: relative;
    z-index: 1;
    min-width: 0;
}

.ssl-page-intro-copy > span:first-child {
    display: block;
    color: var(--ssl-primary);
    font-size: 0.72rem;
    font-weight: 820;
    letter-spacing: 0.09em;
}

.ssl-page-intro h2 span {
    color: inherit;
    font-size: inherit;
    font-weight: inherit;
    letter-spacing: inherit;
}

.ssl-page-intro-icon {
    position: relative;
    z-index: 1;
    flex: 0 0 4.4rem;
    width: 4.4rem;
    height: 4.4rem;
    padding: 1rem;
    border-radius: 20px;
    color: var(--ssl-primary);
    background: rgba(255, 255, 255, 0.74);
    border: 1px solid rgba(8, 122, 92, 0.14);
    box-shadow: 0 12px 28px rgba(8, 122, 92, 0.10);
    backdrop-filter: blur(6px);
}

.ssl-page-intro-icon svg,
.ssl-section-icon svg {
    display: block;
    width: 100%;
    height: 100%;
}

.ssl-icon-dot {
    fill: currentColor;
    stroke: none;
}

.ssl-page-intro h2 {
    margin: 0.18rem 0 0.28rem;
    font-size: clamp(1.35rem, 4vw, 1.85rem);
}

.ssl-page-intro p {
    margin: 0;
    max-width: 54rem;
    color: var(--ssl-muted);
}

.ssl-card-grid,
.ssl-kpi-grid,
.ssl-favorite-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(245px, 1fr));
    gap: 0.85rem;
    margin: 0.8rem 0 1rem;
}

.ssl-card,
.ssl-kpi-card {
    background: rgba(255, 255, 255, 0.93);
    border: 1px solid var(--ssl-border);
    border-radius: 18px;
    padding: 1rem;
    box-shadow: 0 7px 24px rgba(15, 23, 42, 0.05);
}

.ssl-card {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
}

.ssl-card-top,
.ssl-card-footer,
.ssl-kpi-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.65rem;
}

.ssl-ticker {
    font-weight: 800;
    color: var(--ssl-ink);
    font-size: 1.05rem;
}

.ssl-badge {
    padding: 0.3rem 0.55rem;
    border-radius: 999px;
    font-size: 0.74rem;
    line-height: 1.15;
    font-weight: 750;
    text-align: center;
}

.ssl-positive { color: #067253; background: #e4f7f0; }
.ssl-watch { color: #8a5b08; background: var(--ssl-watch-soft); }
.ssl-caution { color: #a14b08; background: #fff0df; }
.ssl-negative { color: var(--ssl-danger); background: var(--ssl-danger-soft); }
.ssl-neutral { color: #475569; background: #eef2f7; }

.ssl-score-row {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.45rem;
}

.ssl-score {
    padding: 0.55rem;
    background: #f8fafc;
    border-radius: 11px;
}

.ssl-score span {
    display: block;
    color: var(--ssl-muted);
    font-size: 0.7rem;
}

.ssl-score strong {
    color: var(--ssl-ink);
    font-size: 1rem;
}

.ssl-card-footer {
    color: var(--ssl-muted);
    font-size: 0.78rem;
    border-top: 1px solid #edf1f5;
    padding-top: 0.7rem;
}

.ssl-kpi-card small {
    color: var(--ssl-muted);
    font-weight: 650;
    display: flex;
    align-items: center;
    gap: 0.45rem;
}

.ssl-kpi-card small::before {
    content: "";
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 3px;
    background: linear-gradient(145deg, var(--ssl-primary), var(--ssl-aqua));
    box-shadow: 0 0 0 4px var(--ssl-primary-soft);
}

.ssl-kpi-card strong {
    display: block;
    margin-top: 0.35rem;
    color: var(--ssl-ink);
    font-size: 1.35rem;
}

.ssl-kpi-card em {
    display: block;
    margin-top: 0.22rem;
    font-style: normal;
    font-size: 0.8rem;
    color: var(--ssl-muted);
}

[class*="st-key-section_subnavigation"] {
    min-height: 3.55rem;
    margin: 0.55rem 0 0.35rem;
}

[class*="st-key-section_subnavigation"] [data-testid="stSegmentedControl"],
[class*="st-key-section_subnavigation"] [data-testid="stButtonGroup"] {
    width: 100%;
}

/* Las dos barras de navegación son carriles independientes. Esta regla queda
   deliberadamente acotada a sus contenedores para no alterar los controles de
   filtros, riesgo o periodos que sí pueden envolver sus opciones. */
[class*="st-key-main_navigation_container"] [data-testid="stSegmentedControl"],
[class*="st-key-main_navigation_container"] [data-testid="stButtonGroup"],
[class*="st-key-section_subnavigation"] [data-testid="stSegmentedControl"],
[class*="st-key-section_subnavigation"] [data-testid="stButtonGroup"] {
    max-width: 100%;
}

/* Streamlit conserva brevemente la versión anterior de un elemento durante un
   rerun. No debemos convertirla de nuevo en un bloque visible: eso producía
   varias barras de navegación fantasma mientras se descargaban los precios. */
[data-stale="true"] [class*="st-key-section_subnavigation"],
[class*="st-key-section_subnavigation"][data-stale="true"] {
    display: none !important;
    min-height: 0 !important;
    margin: 0 !important;
}

.ssl-login-brand {
    max-width: 470px;
    margin: clamp(1rem, 7vh, 4rem) auto 1rem;
    text-align: center;
    position: relative;
}

.ssl-login-brand .ssl-logo {
    margin: 0 auto 0.9rem;
    width: 4rem;
    height: 4rem;
    border-radius: 19px;
}

.ssl-login-brand h1 {
    margin: 0;
    font-size: clamp(2rem, 8vw, 3rem);
}

.ssl-login-brand p {
    color: var(--ssl-muted);
    margin: 0.45rem auto 0;
}

div[data-testid="stForm"]:has(input[autocomplete="username"]) {
    max-width: 470px;
    margin-left: auto;
    margin-right: auto;
    padding: 1rem;
    background: rgba(255, 255, 255, 0.90);
    backdrop-filter: blur(12px);
    border: 1px solid var(--ssl-border);
    box-shadow: var(--ssl-shadow);
}

@media (max-width: 1050px) {
    [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap;
    }
    [data-testid="stColumn"] {
        min-width: 220px;
        flex: 1 1 220px;
    }
    .ssl-status-pill {
        display: none;
    }
    [class*="st-key-app_header_container"] [data-testid="stHorizontalBlock"] {
        flex-wrap: nowrap !important;
        align-items: center;
    }
    [class*="st-key-app_header_container"] [data-testid="stColumn"] {
        min-width: 0 !important;
    }
    [class*="st-key-app_header_container"] [data-testid="stColumn"]:last-child {
        flex: 0 0 8.5rem !important;
        width: 8.5rem !important;
    }
}

@media (max-width: 700px) {
    .block-container {
        padding: 3.75rem 0.85rem 2.4rem;
    }
    [data-testid="stHorizontalBlock"] {
        gap: 0.65rem;
    }
    [data-testid="stColumn"] {
        min-width: 100% !important;
        width: 100% !important;
        flex: 1 1 100% !important;
    }
    .ssl-app-header {
        align-items: flex-start;
    }
    .ssl-logo {
        width: 2.65rem;
        height: 2.65rem;
        flex-basis: 2.65rem;
        border-radius: 13px;
        padding: 0.48rem;
    }
    .ssl-page-intro {
        align-items: flex-start;
    }
    .ssl-page-intro-icon {
        flex-basis: 3.25rem;
        width: 3.25rem;
        height: 3.25rem;
        padding: 0.7rem;
        border-radius: 15px;
    }
    .ssl-card-grid,
    .ssl-kpi-grid,
    .ssl-favorite-grid {
        grid-template-columns: 1fr;
    }
    .ssl-score-row {
        gap: 0.35rem;
    }
    [data-testid="stSegmentedControl"],
    [data-testid="stButtonGroup"] {
        overflow: visible;
    }
    [data-testid="stSegmentedControl"] > div,
    [data-testid="stButtonGroup"] [role="radiogroup"] {
        min-width: 0 !important;
        width: 100% !important;
        flex-wrap: wrap !important;
    }
    [data-testid="stButtonGroup"] button {
        flex: 1 1 calc(33.333% - 0.5rem) !important;
        min-width: 6.2rem;
        white-space: nowrap;
    }
    [class*="st-key-main_navigation_container"],
    [class*="st-key-section_subnavigation"] {
        min-width: 0;
        width: 100%;
    }
    [class*="st-key-main_navigation_container"] [data-testid="stSegmentedControl"],
    [class*="st-key-main_navigation_container"] [data-testid="stButtonGroup"],
    [class*="st-key-section_subnavigation"] [data-testid="stSegmentedControl"],
    [class*="st-key-section_subnavigation"] [data-testid="stButtonGroup"],
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        display: block;
        max-width: 100%;
        overflow-x: auto !important;
        overflow-y: hidden !important;
        overscroll-behavior-x: contain;
        scrollbar-width: thin;
        scrollbar-color: rgba(8, 122, 92, 0.45) transparent;
        scroll-snap-type: x proximity;
        -webkit-overflow-scrolling: touch;
        touch-action: pan-x;
    }
    [class*="st-key-main_navigation_container"] [data-testid="stSegmentedControl"]::-webkit-scrollbar,
    [class*="st-key-main_navigation_container"] [data-testid="stButtonGroup"]::-webkit-scrollbar,
    [class*="st-key-section_subnavigation"] [data-testid="stSegmentedControl"]::-webkit-scrollbar,
    [class*="st-key-section_subnavigation"] [data-testid="stButtonGroup"]::-webkit-scrollbar,
    [data-testid="stTabs"] [data-baseweb="tab-list"]::-webkit-scrollbar {
        height: 4px;
    }
    [class*="st-key-main_navigation_container"] [data-testid="stSegmentedControl"] > div,
    [class*="st-key-main_navigation_container"] [data-testid="stSegmentedControl"] [role="radiogroup"],
    [class*="st-key-main_navigation_container"] [data-testid="stButtonGroup"] [role="radiogroup"],
    [class*="st-key-section_subnavigation"] [data-testid="stSegmentedControl"] > div,
    [class*="st-key-section_subnavigation"] [data-testid="stSegmentedControl"] [role="radiogroup"],
    [class*="st-key-section_subnavigation"] [data-testid="stButtonGroup"] [role="radiogroup"] {
        display: flex !important;
        flex-wrap: nowrap !important;
        width: max-content !important;
        min-width: 100% !important;
    }
    [class*="st-key-main_navigation_container"] [data-testid="stSegmentedControl"] button,
    [class*="st-key-main_navigation_container"] [data-testid="stButtonGroup"] button,
    [class*="st-key-section_subnavigation"] [data-testid="stSegmentedControl"] button,
    [class*="st-key-section_subnavigation"] [data-testid="stButtonGroup"] button {
        flex: 1 0 7rem !important;
        min-width: 7rem;
        min-height: 2.75rem;
        white-space: nowrap;
        scroll-snap-align: start;
    }
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        display: flex;
        flex-wrap: nowrap;
    }
    [data-testid="stTabs"] button[data-baseweb="tab"] {
        flex: 0 0 auto;
        padding-left: 0.65rem;
        padding-right: 0.65rem;
        white-space: nowrap;
        scroll-snap-align: start;
    }
    [class*="st-key-app_header_container"] [data-testid="stColumn"]:last-child {
        flex-basis: 4.5rem !important;
        width: 4.5rem !important;
    }
    [class*="st-key-app_header_container"] [data-testid="stPopover"] p {
        display: none;
    }
    .ssl-app-title {
        font-size: 1.35rem !important;
        white-space: nowrap;
    }
    .ssl-app-subtitle {
        display: none;
    }
    div[data-testid="stMetric"] {
        min-height: auto;
    }
    h1 {
        font-size: 1.8rem !important;
    }
}
"""
