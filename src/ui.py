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
    --ssl-ink: #0f172a;
    --ssl-muted: #64748b;
    --ssl-border: #dce3ec;
    --ssl-surface: #ffffff;
    --ssl-bg: #f5f7fb;
    --ssl-primary: #087f5b;
    --ssl-primary-soft: #e7f7f1;
    --ssl-blue: #2563eb;
    --ssl-watch: #b7791f;
    --ssl-watch-soft: #fff7df;
    --ssl-danger: #c2413a;
    --ssl-danger-soft: #fff0ef;
    --ssl-shadow: 0 12px 32px rgba(15, 23, 42, 0.07);
}

[data-testid="stAppViewContainer"] {
    background:
        radial-gradient(circle at 90% 0%, rgba(8, 127, 91, 0.08), transparent 28rem),
        var(--ssl-bg);
}

[data-testid="stHeader"] {
    background: rgba(245, 247, 251, 0.86);
    backdrop-filter: blur(10px);
}

.block-container {
    max-width: 1380px;
    padding-top: 1.35rem;
    padding-bottom: 3rem;
}

[data-testid="stSidebar"] {
    background: #fbfcfe;
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
    background: rgba(255, 255, 255, 0.88);
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
    background: linear-gradient(135deg, #087f5b, #0f9d72);
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
    font-size: 1.6rem;
    font-weight: 800;
    background: linear-gradient(145deg, #0f172a, #087f5b);
    box-shadow: 0 10px 24px rgba(15, 23, 42, 0.18);
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
    padding: clamp(1rem, 3vw, 1.45rem);
    margin: 0.75rem 0 1rem;
    border: 1px solid var(--ssl-border);
    border-radius: 20px;
    background:
        linear-gradient(125deg, rgba(255, 255, 255, 0.98), rgba(231, 247, 241, 0.82));
    box-shadow: 0 8px 25px rgba(15, 23, 42, 0.05);
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

.ssl-page-intro span {
    display: block;
    color: var(--ssl-primary);
    font-size: 0.72rem;
    font-weight: 820;
    letter-spacing: 0.09em;
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
    background: var(--ssl-surface);
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
    display: flex;
    align-items: flex-start;
}

[class*="st-key-section_subnavigation"] [data-testid="stSegmentedControl"] {
    width: 100%;
}

.ssl-login-brand {
    max-width: 470px;
    margin: clamp(1rem, 7vh, 4rem) auto 1rem;
    text-align: center;
}

.ssl-login-brand .ssl-logo {
    margin: 0 auto 0.9rem;
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
    background: white;
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
    }
    .ssl-card-grid,
    .ssl-kpi-grid,
    .ssl-favorite-grid {
        grid-template-columns: 1fr;
    }
    .ssl-score-row {
        gap: 0.35rem;
    }
    [data-testid="stSegmentedControl"] {
        overflow-x: auto;
    }
    [data-testid="stSegmentedControl"] > div {
        min-width: max-content;
    }
    div[data-testid="stMetric"] {
        min-height: auto;
    }
    h1 {
        font-size: 1.8rem !important;
    }
}
"""
