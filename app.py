"""Interfaz Streamlit de Stock Signal Lab.

Ejecutar con: ``streamlit run app.py``
Versión de despliegue auditada: 2026-08-04.
"""

from __future__ import annotations

from datetime import date, timedelta
import html
import importlib

import pandas as pd
import streamlit as st

from config import BacktestConfig, StrategyConfig
from src.alerts import normalize_alert_preferences
from src.auth import (
    AuthConfig,
    load_auth_accounts,
    managed_usernames,
    persistent_journal_enabled,
    require_login,
)
from src.backtesting import BacktestResult, run_backtest
from src.brand import (
    BRAND_FAVICON_SVG,
    brand_mark_html,
    contextual_icon,
    icon_html,
)
from src.current_positions import (
    REFERENCE_COST,
    REFERENCE_ENTRY,
    REFERENCE_GAIN,
    REFERENCE_OPTIONS,
    REFERENCE_RETURN,
    estimate_current_position,
    snapshot_with_current_position,
)
from src.dashboard import build_position_dashboard
from src.data_loader import (
    DataDownloadError,
    TickerSearchResult,
    download_fundamental_snapshot,
    download_prices,
    resolve_analysis_ticker,
    search_result_market_group,
    search_instruments,
)
from src.data_sources import (
    ExternalDataError,
    FxSnapshot,
    PriceVerification,
    benchmark_for_ticker,
    compare_verified_price,
    convert_currency,
    download_alpha_vantage_latest_close,
    download_ecb_fx_snapshot,
    sector_benchmark,
)
from src.fundamentals import FundamentalResult, evaluate_fundamentals
from src.growth_momentum import (
    SECTOR_PROFILES,
    GrowthMomentumConfig,
    GrowthMomentumResult,
    calculate_growth_position_plan,
    evaluate_growth_momentum,
    quote_price_to_eur,
)
from src.email_sender import (
    EmailConfigurationError,
    EmailDeliveryError,
    load_email_config,
    send_test_email,
)
from src.favorite_tags import (
    FAVORITE_TAGS,
    favorite_tags_from_value,
    suggest_favorite_tags,
)
from src.indicators import add_indicators
from src.journal import DEFAULT_DDRIU_ACCOUNTS, MAX_FAVORITES, calculate_open_positions
from src.journal import (
    PRIVATE_INVESTMENT_PLATFORMS,
    PRIVATE_INVESTMENT_STATUSES,
    PORTFOLIO_ACCOUNT_STATUSES,
)
from src.msn_research import build_msn_research_links
from src.navigation import (
    analysis_refresh_tickers,
    growth_radar_ticker_groups,
    merge_analysis_ticker_sources,
    sanitize_favorite_selection,
)
from src.opportunity_catalog import build_opportunity_catalog
from src.supabase_journal import JournalStorageError
from src.storage import GROUP_PORTFOLIO_OWNER, create_journal
from src.opportunity import (
    OpportunityResult,
    RelativeStrengthResult,
    RiskResult,
    ValuationResult,
    combine_opportunity,
    evaluate_relative_strength,
    evaluate_risk,
    evaluate_valuation,
)
from src.portfolio import compare_switch, value_holding
from src.portfolio_export import build_portfolio_excel
from src.portfolio_history import build_portfolio_history
from src.portfolio_snapshot_import import (
    import_portfolio_workbook_snapshot,
    parse_portfolio_snapshot_excel,
)
from src import portfolio_snapshot as _portfolio_snapshot_module

_portfolio_snapshot_module = importlib.reload(_portfolio_snapshot_module)
group_portfolio_snapshot_for_home = (
    _portfolio_snapshot_module.group_portfolio_snapshot_for_home
)
latest_portfolio_snapshot = _portfolio_snapshot_module.latest_portfolio_snapshot
refresh_portfolio_snapshot_prices = (
    _portfolio_snapshot_module.refresh_portfolio_snapshot_prices
)
from src.portfolio_decisions import (
    build_portfolio_decision_rows,
    entry_opportunity_rows,
)
from src.recommendations import (
    build_entry_guide,
    build_profit_taking_plan,
    historical_forward_return_study,
)
from src.return_calibration import (
    MINIMUM_RELIABLE_SAMPLES,
    calibrate_score_returns,
    calibration_for_score,
)
from src.risk import calculate_position_plan
from src.sector_comparison import HORIZON_SESSIONS, compare_sector
from src.segofactoring_import import (
    import_segofactoring_rows,
    parse_segofactoring_excel,
)
from src.signal_engine import add_signal_columns, evaluate_latest_signal
from src.staircase_projection import (
    DEFAULT_SCENARIOS,
    StaircaseProjectionConfig,
    default_horizons,
    project_scenarios,
    simulate_projection_ranges,
    summarize_projection,
)
from src.ui import (
    APP_CSS,
    PROFILE_NAMES,
    signal_tone,
    strategy_profile_defaults,
)
from src.visualization import (
    annual_portfolio_chart,
    backtest_chart,
    correlation_heatmap,
    momentum_chart,
    normalized_comparison_chart,
    portfolio_evolution_chart,
    portfolio_snapshot_allocation_chart,
    portfolio_snapshot_assets_chart,
    portfolio_snapshot_history_chart,
    price_chart,
    private_investments_chart,
    return_calibration_chart,
    risk_return_chart,
    staircase_projection_chart,
    staircase_range_chart,
)


st.set_page_config(
    page_title="Stock Signal Lab",
    page_icon=BRAND_FAVICON_SVG,
    layout="wide",
)

MAIN_OPTIONS = ["Inicio", "Analizar", "Favoritos", "Carteras", "Más"]
ANALYSIS_OPTIONS = ["Radar", "Empresa", "Estrategias", "Más análisis"]
ANALYSIS_LABELS = {
    "Radar": "◎ Radar",
    "Empresa": "↗ Empresa",
    "Estrategias": "◱ Estrategias",
    "Más análisis": "··· Más",
}
STRATEGY_OPTIONS = ["Crecimiento", "Resultado tras 30+ días"]
ANALYSIS_TOOL_OPTIONS = [
    "Comparar empresas",
    "Historial",
    "Prueba con el pasado",
    "Plan de capital",
]
LEGACY_ANALYSIS_ROUTES = {
    "Oportunidades": ("Radar", None, None),
    "Crecimiento y momentum": ("Estrategias", "analysis_strategy_navigation", "Crecimiento"),
    "Objetivo 30+ días": (
        "Estrategias",
        "analysis_strategy_navigation",
        "Resultado tras 30+ días",
    ),
    "Comparador sectorial": (
        "Más análisis",
        "analysis_tool_navigation",
        "Comparar empresas",
    ),
    "Historial guardado": ("Más análisis", "analysis_tool_navigation", "Historial"),
    "Prueba histórica": (
        "Más análisis",
        "analysis_tool_navigation",
        "Prueba con el pasado",
    ),
    "Proyección de capital": (
        "Más análisis",
        "analysis_tool_navigation",
        "Plan de capital",
    ),
}
SIDEBAR_ANALYSIS_SECTIONS = {
    "Radar",
    "Estrategias",
    "Comparar empresas",
    "Prueba con el pasado",
}


def apply_visual_theme() -> None:
    """Aplica una capa visual responsive sin alterar los componentes financieros."""

    st.markdown(f"<style>{APP_CSS}</style>", unsafe_allow_html=True)


@st.cache_data(ttl=3_600, max_entries=350, show_spinner=False)
def cached_download(ticker: str, start: date, end: date, auto_adjust: bool) -> pd.DataFrame:
    """Caché de red; los indicadores se recalculan fuera con la configuración actual."""

    return download_prices(ticker, start, end, auto_adjust=auto_adjust)


@st.cache_data(ttl=21_600, max_entries=300, show_spinner=False)
def cached_fundamentals(ticker: str) -> dict[str, object]:
    """Caché más largo para datos empresariales, que cambian menos que el precio."""

    return download_fundamental_snapshot(ticker)


@st.cache_data(ttl=86_400, max_entries=2, show_spinner=False)
def cached_fx_rates() -> FxSnapshot:
    """Tipos de referencia diarios; una consulta basta para todas las empresas."""

    return download_ecb_fx_snapshot()


@st.cache_data(ttl=86_400, max_entries=100, show_spinner=False)
def cached_price_verification(ticker: str, api_key: str) -> PriceVerification:
    """Comprobación opcional del cierre mediante un segundo proveedor."""

    return download_alpha_vantage_latest_close(ticker, api_key)


@st.cache_data(ttl=86_400, max_entries=200, show_spinner=False)
def cached_company_search(query: str) -> list[TickerSearchResult]:
    """Evita repetir búsquedas iguales durante el día."""

    return search_instruments(query, max_results=25)


def apply_section_layout(section: str, analysis_section: str = "") -> None:
    """Reserva la barra lateral completa para la zona que realmente la necesita."""

    if section == "Analizar" and analysis_section in SIDEBAR_ANALYSIS_SECTIONS:
        return
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"],
        [data-testid="stSidebarCollapsedControl"] {
            display: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def parse_tickers(raw_value: str) -> list[str]:
    values = raw_value.replace(";", ",").replace("\n", ",").split(",")
    return list(
        dict.fromkeys(
            resolve_analysis_ticker(value)
            for value in values
            if value.strip()
        )
    )


def format_pct(value: float) -> str:
    return f"{value:+.2f}%"


PLAIN_SIGNAL_GUIDANCE = {
    "Entrada fuerte": (
        "El precio reúne una combinación especialmente fuerte de tendencia, impulso y liderazgo. "
        "Es el mejor momento técnico del radar, pero aún requiere revisar la empresa y el riesgo."
    ),
    "Entrada interesante": (
        "La empresa muestra una combinación fuerte de tendencia, impulso y liderazgo. "
        "Merece una revisión más profunda antes de decidir una entrada."
    ),
    "Vigilancia": (
        "Hay señales prometedoras, pero todavía falta alguna confirmación. Conviene seguirla "
        "sin tratarla aún como una entrada completa."
    ),
    "Esperar": (
        "La empresa puede ser interesante, pero el precio parece demasiado acelerado. "
        "Perseguirlo ahora aumenta el riesgo de comprar justo antes de una pausa."
    ),
    "Mantener": (
        "La tendencia sigue razonablemente sana, pero hoy no aparece una entrada especialmente clara."
    ),
    "Reducir": (
        "La fuerza se está debilitando. Si ya tienes la acción, conviene revisar cuánto riesgo "
        "quieres mantener; si no la tienes, todavía no destaca como nueva entrada."
    ),
    "Vender": (
        "La tendencia principal está dañada o se ha activado el límite de pérdida. "
        "No equivale a una certeza de caída, sino a una señal de deterioro."
    ),
}


def friendly_factor(text: str) -> str:
    """Convierte un fragmento técnico en una frase legible."""

    return text[:1].upper() + text[1:] if text else text


def build_favorite_catalog(
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
) -> tuple[list[str], dict[str, str]]:
    """Une favoritos privados y compartidos sin duplicar empresas."""

    sources: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    tags_by_ticker: dict[str, list[str]] = {}
    for frame, source in (
        (private_favorites, "privada"),
        (group_favorites, "grupo"),
    ):
        if frame.empty:
            continue
        for favorite in frame.itertuples(index=False):
            ticker = str(favorite.ticker).strip().upper()
            if not ticker:
                continue
            sources.setdefault(ticker, set()).add(source)
            name = str(getattr(favorite, "name", "") or ticker).strip()
            names.setdefault(ticker, name)
            tags = favorite_tags_from_value(getattr(favorite, "tags", ""))
            merged_tags = tags_by_ticker.setdefault(ticker, [])
            for tag in tags:
                if tag not in merged_tags:
                    merged_tags.append(tag)
    tickers = sorted(names, key=lambda item: (names[item].casefold(), item))
    labels = {
        ticker: (
            f"{names[ticker]} ({ticker}) · "
            + " y ".join(sorted(sources[ticker]))
            + (
                f" · {', '.join(tags_by_ticker.get(ticker, []))}"
                if tags_by_ticker.get(ticker)
                else ""
            )
        )
        for ticker in tickers
    }
    return tickers, labels


def _apply_selected_profile() -> None:
    profile_name = str(st.session_state.get("strategy_profile", "Equilibrado"))
    if profile_name == "Personalizado":
        return
    values = strategy_profile_defaults(profile_name)
    st.session_state["cfg_rsi_range"] = (
        int(values.pop("rsi_buy_min")),
        int(values.pop("rsi_buy_max")),
    )
    for setting, value in values.items():
        st.session_state[f"cfg_{setting}"] = value


def build_sidebar(
    favorite_tickers: list[str] | None = None,
    favorite_labels: dict[str, str] | None = None,
) -> tuple[
    list[str],
    date,
    date,
    bool,
    str,
    StrategyConfig,
    BacktestConfig,
    bool,
]:
    favorite_tickers = favorite_tickers or []
    favorite_labels = favorite_labels or {}
    previous_selection = st.session_state.get("selected_favorite_tickers", [])
    safe_selection = sanitize_favorite_selection(
        previous_selection,
        favorite_tickers,
    )
    if previous_selection != safe_selection:
        # Versiones anteriores introducían aquí tickers temporales abiertos desde
        # el buscador. Streamlit rechaza valores que no existen en ``options``.
        st.session_state["selected_favorite_tickers"] = safe_selection
    st.sidebar.markdown("## Actualizar datos")
    st.sidebar.caption(
        "Elige qué conjunto quieres recalcular. Para abrir una empresa concreta usa "
        "«Empresa» en la pantalla principal."
    )
    st.sidebar.markdown("### 1 · Empresas")
    selected_favorites = st.sidebar.multiselect(
        "Empresas que quieres actualizar",
        options=favorite_tickers,
        format_func=lambda ticker: favorite_labels.get(ticker, ticker),
        max_selections=25,
        help=(
            "Puedes guardar hasta 300 favoritas y analizar hasta 25 en profundidad "
            "cada vez. Las posiciones abiertas se actualizan automáticamente."
        ),
        key="selected_favorite_tickers",
    )
    with st.sidebar.expander("Modo avanzado: ticker exacto"):
        manual_value = st.text_area(
            "Símbolos bursátiles",
            "" if favorite_tickers else "AAPL, MSFT, SAN.MC",
            height=78,
            help=(
                "Sólo es necesario si el buscador no encuentra la empresa. Ejemplos: "
                "SAN.MC (Madrid), 7974.T (Tokio) o KAP.IL (Londres internacional)."
            ),
            key="manual_tickers",
        )
        manual_tickers = parse_tickers(manual_value)
        alias_changes = []
        for value in manual_value.replace(";", ",").replace("\n", ",").split(","):
            requested = value.strip().upper()
            if requested:
                resolved = resolve_analysis_ticker(requested)
                if requested != resolved:
                    alias_changes.append(f"{requested} → {resolved}")
        if alias_changes:
            st.info("Usaremos la cotización de análisis: " + ", ".join(alias_changes))
    selected_analysis_tickers = [
        resolve_analysis_ticker(ticker) for ticker in selected_favorites
    ]
    tickers = list(dict.fromkeys([*selected_analysis_tickers, *manual_tickers]))

    st.sidebar.divider()
    st.sidebar.markdown("### 2 · Lectura")
    years = st.sidebar.select_slider(
        "Historial utilizado",
        options=[1, 2, 3, 5, 10],
        value=5,
        format_func=lambda value: f"{value} años",
        help="Cinco años es el punto de partida recomendado.",
        key="analysis_years",
    )
    profile_name = st.sidebar.selectbox(
        "Estilo de análisis",
        PROFILE_NAMES,
        index=0,
        key="strategy_profile",
        on_change=_apply_selected_profile,
        help=(
            "Equilibrado mantiene los parámetros recomendados. Crecimiento tolera "
            "más impulso; Prudente exige señales más selectivas."
        ),
    )
    defaults = strategy_profile_defaults(profile_name)
    st.sidebar.caption(
        {
            "Equilibrado": "Señales equilibradas para empezar.",
            "Crecimiento": "Más tolerancia a empresas aceleradas y horizonte más largo.",
            "Prudente": "Menor riesgo y confirmaciones más exigentes.",
            "Personalizado": "Utiliza tus propios ajustes.",
        }[profile_name]
    )

    st.sidebar.markdown("#### Riesgo básico")
    quick_a, quick_b = st.sidebar.columns(2)
    stop_loss = quick_a.slider(
        "Stop",
        1.0,
        30.0,
        float(defaults["stop_loss"]),
        0.5,
        format="%.1f%%",
        help="Pérdida máxima orientativa desde la entrada.",
        key="cfg_stop_loss",
    )
    max_risk = quick_b.slider(
        "Riesgo",
        0.1,
        10.0,
        float(defaults["max_risk"]),
        0.1,
        format="%.1f%%",
        help="Capital total que aceptarías perder si se alcanza el stop.",
        key="cfg_max_risk",
    )
    with st.sidebar.expander("Objetivo y prueba histórica"):
        forward_horizon = st.selectbox(
            "Horizonte de la estimación",
            options=[10, 20, 40, 60],
            index=[10, 20, 40, 60].index(int(defaults["forward_horizon"])),
            format_func=lambda value: f"{value} sesiones",
            key="cfg_forward_horizon",
            help="Sólo afecta a la estimación futura y a su comparación histórica.",
        )
        initial_capital = st.number_input(
            "Capital para el backtest (€)",
            min_value=100.0,
            value=10_000.0,
            step=1_000.0,
            key="cfg_initial_capital",
            help="Es capital ficticio; no modifica tu cartera real.",
        )

    st.sidebar.divider()
    st.sidebar.markdown("### 3 · Ajustes opcionales")
    with st.sidebar.expander("Periodo y fuentes"):
        end = st.date_input(
            "Analizar hasta",
            value=date.today(),
            max_value=date.today(),
            key="analysis_end",
        )
        auto_adjust = st.checkbox(
            "Precios ajustados",
            value=True,
            help="Ajusta splits y dividendos según Yahoo.",
            key="auto_adjust_prices",
        )
        st.caption(
            "Yahoo aporta precios y contexto; SEC valida cuentas de EE. UU.; "
            "el BCE convierte divisas y MSN sirve como contraste externo."
        )
        alpha_vantage_key = st.text_input(
            "Alpha Vantage opcional",
            type="password",
            help="Contrasta el último cierre con un segundo proveedor gratuito.",
            key="alpha_vantage_key",
        )

    with st.sidebar.expander("Configuración técnica avanzada"):
        col_a, col_b, col_c = st.columns(3)
        sma_short = col_a.number_input(
            "Corta",
            min_value=2,
            max_value=100,
            value=int(defaults["sma_short"]),
            key="cfg_sma_short",
        )
        sma_medium = col_b.number_input(
            "Media",
            min_value=5,
            max_value=250,
            value=int(defaults["sma_medium"]),
            key="cfg_sma_medium",
        )
        sma_long = col_c.number_input(
            "Larga",
            min_value=20,
            max_value=500,
            value=int(defaults["sma_long"]),
            key="cfg_sma_long",
        )
        rsi_period = st.number_input(
            "Días para medir impulso",
            min_value=2,
            max_value=50,
            value=int(defaults["rsi_period"]),
            key="cfg_rsi_period",
        )
        rsi_range = st.slider(
            "Zona de impulso saludable",
            20,
            80,
            (int(defaults["rsi_buy_min"]), int(defaults["rsi_buy_max"])),
            key="cfg_rsi_range",
        )
        rsi_overbought = st.slider(
            "Precio demasiado acelerado",
            60,
            95,
            int(defaults["rsi_overbought"]),
            key="cfg_rsi_overbought",
        )
        max_distance = st.slider(
            "Distancia máxima de la media corta",
            2.0,
            30.0,
            float(defaults["max_distance"]),
            0.5,
            format="%.1f%%",
            key="cfg_max_distance",
        )
        watch_score = st.slider(
            "Vigilar desde",
            45,
            70,
            int(defaults["watch_score"]),
            key="cfg_watch_score",
        )
        buy_score = st.slider(
            "Entrada interesante desde",
            55,
            80,
            int(defaults["buy_score"]),
            key="cfg_buy_score",
        )
        strong_score = st.slider(
            "Entrada fuerte desde",
            65,
            95,
            int(defaults["strong_score"]),
            key="cfg_strong_score",
        )
        reduce_score = st.slider(
            "Debilidad desde",
            20,
            60,
            int(defaults["reduce_score"]),
            key="cfg_reduce_score",
        )
        sell_score = st.slider(
            "Deterioro severo",
            0,
            40,
            int(defaults["sell_score"]),
            key="cfg_sell_score",
        )
        confirmation_days = st.slider(
            "Confirmar señal negativa",
            1,
            5,
            int(defaults["confirmation_days"]),
            format="%d sesiones",
            key="cfg_confirmation_days",
        )
        breakout_period = st.slider(
            "Periodo para nuevo máximo",
            10,
            60,
            int(defaults["breakout_period"]),
            format="%d sesiones",
            key="cfg_breakout_period",
        )
        near_high = st.slider(
            "Distancia admitida del máximo anual",
            5.0,
            30.0,
            float(defaults["near_high"]),
            1.0,
            format="%.0f%%",
            key="cfg_near_high",
        )
        volume_normal = st.slider(
            "Actividad mínima normal",
            0.5,
            1.0,
            float(defaults["volume_normal"]),
            0.1,
            format="%.1fx",
            key="cfg_volume_normal",
        )
        volume_surge = st.slider(
            "Actividad destacada",
            1.1,
            3.0,
            float(defaults["volume_surge"]),
            0.1,
            format="%.1fx",
            key="cfg_volume_surge",
        )

    with st.sidebar.expander("Riesgo y costes avanzados"):
        trailing_stop = st.slider(
            "Protección dinámica de beneficios",
            0.0,
            30.0,
            float(defaults["trailing_stop"]),
            0.5,
            format="%.1f%%",
            key="cfg_trailing_stop",
        )
        exit_on_reduce = st.checkbox(
            "Cerrar en backtest si aparece Reducir",
            value=bool(defaults["exit_on_reduce"]),
            key="cfg_exit_on_reduce",
        )
        commission = st.number_input(
            "Coste de compra o venta (%)",
            min_value=0.0,
            value=0.10,
            step=0.05,
            format="%.2f",
            key="cfg_commission",
        )
        slippage = st.number_input(
            "Ejecución imperfecta (%)",
            min_value=0.0,
            value=0.05,
            step=0.05,
            format="%.2f",
            key="cfg_slippage",
        )

    selected_count = len(tickers)
    st.sidebar.divider()
    st.sidebar.caption(
        f"Selección manual: {selected_count} empresas. También se añadirán las "
        "posiciones abiertas de la cartera."
    )
    load_clicked = st.sidebar.button(
        (
            f"Actualizar {selected_count} empresas"
            if selected_count
            else "Actualizar posiciones abiertas"
        ),
        type="primary",
        width="stretch",
        icon=":material/refresh:",
    )
    start = end - timedelta(days=365 * int(years))
    strategy = StrategyConfig(
        sma_short=int(sma_short),
        sma_medium=int(sma_medium),
        sma_long=int(sma_long),
        rsi_period=int(rsi_period),
        rsi_buy_min=float(rsi_range[0]),
        rsi_buy_max=float(rsi_range[1]),
        rsi_overbought=float(rsi_overbought),
        distance_from_sma20_pct=float(max_distance),
        breakout_period=int(breakout_period),
        near_high_pct=float(near_high),
        volume_normal_ratio=float(volume_normal),
        volume_surge_ratio=float(volume_surge),
        watch_score_threshold=int(watch_score),
        buy_score_threshold=int(buy_score),
        strong_score_threshold=int(strong_score),
        forward_horizon_days=int(forward_horizon),
        reduce_score_threshold=int(reduce_score),
        sell_score_threshold=int(sell_score),
        trend_confirmation_days=int(confirmation_days),
        stop_loss_pct=float(stop_loss),
        trailing_stop_pct=float(trailing_stop),
        max_risk_per_trade_pct=float(max_risk),
        exit_on_reduce=bool(exit_on_reduce),
    )
    backtest = BacktestConfig(
        initial_capital=float(initial_capital),
        commission_pct=float(commission),
        slippage_pct=float(slippage),
    )
    return (
        tickers,
        start,
        end,
        auto_adjust,
        alpha_vantage_key,
        strategy,
        backtest,
        load_clicked,
    )


def load_market_data(
    tickers: list[str],
    start: date,
    end: date,
    auto_adjust: bool,
    alpha_vantage_key: str = "",
    *,
    fundamental_tickers: set[str] | None = None,
    merge_existing: bool = False,
) -> None:
    if not tickers:
        st.sidebar.error("Elige al menos una favorita o registra una posición.")
        return
    if len(tickers) > 200:
        st.sidebar.error(
            "La actualización admite hasta 200 empresas simultáneas. "
            "Reduce temporalmente la selección de favoritos."
        )
        return
    deep_tickers = set(tickers if fundamental_tickers is None else fundamental_tickers)
    if len(deep_tickers) > 25:
        deep_tickers = set(
            [ticker for ticker in tickers if ticker in deep_tickers][:25]
        )
        st.sidebar.warning(
            "Se hará análisis empresarial profundo de las primeras 25 empresas. "
            "Las demás conservarán valoración rápida de precio y tendencia."
        )
    downloaded: dict[str, pd.DataFrame] = (
        dict(st.session_state.get("market_data", {})) if merge_existing else {}
    )
    fundamentals: dict[str, dict[str, object]] = (
        dict(st.session_state.get("fundamental_data", {})) if merge_existing else {}
    )
    verifications: dict[str, PriceVerification] = (
        dict(st.session_state.get("price_verifications", {}))
        if merge_existing
        else {}
    )
    errors: list[str] = []
    progress = st.progress(0, text="Descargando precios…")
    for position, ticker in enumerate(tickers, start=1):
        ticker_start = (
            start
            if ticker in deep_tickers
            else max(start, end - timedelta(days=420))
        )
        try:
            downloaded[ticker] = cached_download(
                ticker,
                ticker_start,
                end,
                auto_adjust,
            )
        except (DataDownloadError, ValueError) as exc:
            errors.append(str(exc))
        if ticker in deep_tickers:
            try:
                fundamentals[ticker] = cached_fundamentals(ticker)
            except (DataDownloadError, ValueError) as exc:
                fundamentals[ticker] = {
                    "symbol": ticker,
                    "_fundamental_error": str(exc),
                }
                errors.append(str(exc))
            if alpha_vantage_key and ticker in downloaded:
                try:
                    verification = cached_price_verification(ticker, alpha_vantage_key)
                    verifications[ticker] = compare_verified_price(
                        downloaded[ticker],
                        verification,
                    )
                except (ExternalDataError, ValueError) as exc:
                    errors.append(str(exc))
        else:
            # Una actualización rápida de cartera no debe borrar un análisis
            # empresarial completo que ya existe en esta sesión.
            if ticker not in fundamentals:
                fundamentals[ticker] = {"symbol": ticker, "_quick_mode": True}
        mode = "análisis completo" if ticker in deep_tickers else "actualización rápida"
        progress.progress(
            position / len(tickers),
            text=f"{ticker}: {mode}",
        )
    reference_symbols: set[str] = set()
    for ticker in downloaded:
        reference_symbols.add(benchmark_for_ticker(ticker))
        sector_reference = sector_benchmark(
            str(fundamentals.get(ticker, {}).get("sector") or ""),
            ticker,
        )
        if sector_reference:
            reference_symbols.add(sector_reference)
    references: dict[str, pd.DataFrame] = (
        dict(st.session_state.get("reference_data", {})) if merge_existing else {}
    )
    missing_references = reference_symbols.difference(downloaded).difference(references)
    for symbol in sorted(missing_references):
        try:
            references[symbol] = cached_download(symbol, start, end, auto_adjust)
        except (DataDownloadError, ValueError) as exc:
            errors.append(f"No se pudo calcular la comparación con {symbol}: {exc}")
    try:
        fx_snapshot = cached_fx_rates()
    except (ExternalDataError, ValueError) as exc:
        fx_snapshot = FxSnapshot(as_of=None, rates_per_eur={"EUR": 1.0})
        errors.append(str(exc))
    progress.empty()
    st.session_state["market_data"] = downloaded
    st.session_state["fundamental_data"] = fundamentals
    st.session_state["reference_data"] = references
    st.session_state["price_verifications"] = verifications
    st.session_state["fx_snapshot"] = fx_snapshot
    quick_mode_tickers = (
        set(st.session_state.get("quick_mode_tickers", []))
        if merge_existing
        else set()
    )
    quick_mode_tickers.update(
        ticker
        for ticker in set(tickers).difference(deep_tickers)
        if fundamentals.get(ticker, {}).get("_quick_mode")
    )
    quick_mode_tickers.difference_update(deep_tickers)
    st.session_state["quick_mode_tickers"] = sorted(quick_mode_tickers)
    st.session_state["download_errors"] = errors
    # Los resultados dependen del histórico y de toda la configuración. Se
    # descartan sólo cuando se actualizan precios; cambiar de pestaña no obliga
    # a repetir la simulación.
    st.session_state.pop("backtest_results", None)


def prepare_data(
    raw_data: dict[str, pd.DataFrame],
    raw_fundamentals: dict[str, dict[str, object]],
    reference_data: dict[str, pd.DataFrame],
    config: StrategyConfig,
) -> tuple[
    dict[str, pd.DataFrame],
    list[dict[str, object]],
    dict[str, FundamentalResult],
    dict[str, ValuationResult],
    dict[str, RelativeStrengthResult],
    dict[str, RiskResult],
    dict[str, OpportunityResult],
]:
    prepared: dict[str, pd.DataFrame] = {}
    summary: list[dict[str, object]] = []
    fundamental_results: dict[str, FundamentalResult] = {}
    valuation_results: dict[str, ValuationResult] = {}
    relative_results: dict[str, RelativeStrengthResult] = {}
    risk_results: dict[str, RiskResult] = {}
    opportunity_results: dict[str, OpportunityResult] = {}
    for ticker, raw_frame in raw_data.items():
        try:
            frame = add_signal_columns(add_indicators(raw_frame, config), config)
            signal = evaluate_latest_signal(frame, config, ticker=ticker)
        except ValueError as exc:
            summary.append({"Ticker": ticker, "Estado": f"Sin señal: {exc}"})
            continue
        prepared[ticker] = frame
        fundamentals = evaluate_fundamentals(raw_fundamentals.get(ticker, {}), ticker)
        fundamental_results[ticker] = fundamentals
        valuation = evaluate_valuation(raw_fundamentals.get(ticker, {}), ticker)
        valuation_results[ticker] = valuation
        broad_name = benchmark_for_ticker(ticker)
        sector_name = sector_benchmark(fundamentals.sector, ticker)
        relative = evaluate_relative_strength(
            ticker,
            frame,
            reference_data.get(broad_name),
            broad_name=broad_name,
            sector=reference_data.get(sector_name) if sector_name else None,
            sector_name=sector_name,
        )
        relative_results[ticker] = relative
        risk = evaluate_risk(ticker, frame)
        risk_results[ticker] = risk
        opportunity = combine_opportunity(
            ticker,
            fundamentals,
            valuation,
            signal,
            relative,
            risk,
        )
        opportunity_results[ticker] = opportunity
        latest = frame.dropna(subset=["sma_long", "rsi"]).iloc[-1]
        summary.append(
            {
                "Ticker": ticker,
                "Oportunidad": opportunity.score,
                "Confianza datos": opportunity.confidence_pct,
                "Lectura conjunta": opportunity.label,
                "Calidad empresa": (
                    float(fundamentals.score) if fundamentals.score is not None else float("nan")
                ),
                "Valoración": (
                    float(valuation.score) if valuation.score is not None else float("nan")
                ),
                "Momento entrada": signal.score,
                "Fuerza relativa": (
                    float(relative.score) if relative.score is not None else float("nan")
                ),
                "Riesgo controlado": (
                    float(risk.score) if risk.score is not None else float("nan")
                ),
                "Lectura entrada": signal.label,
                "Si ya la tienes": signal.position_label,
                "Cierre": float(latest["close"]),
                "RSI": float(latest["rsi"]),
                "Fuerza 3 meses": float(latest["momentum_medium_pct"]),
                "Desde su máximo": float(latest["distance_high_pct"]),
                "Actividad": float(latest["volume_ratio"]),
                "Nuevo máximo reciente": "Sí" if bool(latest["breakout"]) else "No",
                "Fecha": signal.as_of.date(),
            }
        )
    return (
        prepared,
        summary,
        fundamental_results,
        valuation_results,
        relative_results,
        risk_results,
        opportunity_results,
    )


def render_analysis(
    ticker: str,
    frame: pd.DataFrame,
    strategy: StrategyConfig,
    settings: BacktestConfig,
    fundamentals: FundamentalResult,
    valuation: ValuationResult,
    relative: RelativeStrengthResult,
    risk: RiskResult,
    opportunity: OpportunityResult,
    raw_fundamentals: dict[str, object],
    verification: PriceVerification | None = None,
    journal: object | None = None,
) -> None:
    with st.expander("Si ya tienes esta acción"):
        entry_price = st.number_input(
            "Precio de entrada (0 = sin posición)",
            min_value=0.0,
            value=0.0,
            step=0.01,
            key=f"entry_price_{ticker}",
            help="Permite comprobar si el cierre ha activado el stop loss configurado.",
        )
    signal = evaluate_latest_signal(
        frame,
        strategy,
        ticker=ticker,
        entry_price=float(entry_price) if entry_price > 0 else None,
    )
    latest = frame.dropna(subset=["sma_long", "rsi"]).iloc[-1]
    study = historical_forward_return_study(
        frame,
        current_score=signal.score,
        horizon_days=strategy.forward_horizon_days,
    )
    expected_price = (
        float(latest["close"]) * (1 + float(study.median_return_pct) / 100)
        if study.reliable and study.median_return_pct is not None
        else None
    )
    auto_snapshot_saved = False
    if journal is not None:
        try:
            existing_snapshots = journal.list_analysis_snapshots(ticker)
            already_recorded = False
            if not existing_snapshots.empty:
                existing_dates = pd.to_datetime(
                    existing_snapshots["analyzed_at"], errors="coerce"
                ).dt.date
                target_date = pd.Timestamp(signal.as_of).date()
                same_day = existing_snapshots.loc[existing_dates == target_date]
                if not same_day.empty:
                    same_opportunity = pd.to_numeric(
                        same_day.get("opportunity_score"), errors="coerce"
                    ).eq(float(opportunity.score))
                    same_entry = pd.to_numeric(
                        same_day.get("entry_score"), errors="coerce"
                    ).eq(float(signal.score))
                    already_recorded = bool((same_opportunity & same_entry).any())
            if not already_recorded:
                journal.add_analysis_snapshot(
                    ticker=ticker,
                    analyzed_at=signal.as_of,
                    price=float(latest["close"]),
                    opportunity_score=opportunity.score,
                    company_score=fundamentals.score,
                    entry_score=signal.score,
                    valuation_score=valuation.score,
                    relative_score=relative.score,
                    risk_score=risk.score,
                    opportunity_label=opportunity.label,
                    entry_label=signal.label,
                    position_label=signal.position_label,
                    expected_return_pct=(
                        study.median_return_pct if study.reliable else None
                    ),
                    positive_rate_pct=(
                        study.positive_rate_pct if study.reliable else None
                    ),
                    expected_price=expected_price,
                    horizon_days=study.horizon_days if study.reliable else None,
                    sector=fundamentals.sector or "",
                    explanation=opportunity.explanation,
                    note="Seguimiento automático al abrir el análisis",
                )
                auto_snapshot_saved = True
        except (JournalStorageError, ValueError, KeyError, AttributeError):
            # El análisis sigue siendo utilizable aunque la base de datos esté
            # temporalmente indisponible; la opción de guardado manual mostrará
            # el error con más detalle.
            pass
    previous = frame["close"].iloc[-2]
    daily_change = (latest["close"] / previous - 1) * 100
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Oportunidad conjunta",
        f"{opportunity.score}/100",
        opportunity.label,
        help="Combina cinco familias de análisis sin ocultar sus notas individuales.",
    )
    col2.metric(
        "Confianza de los datos",
        f"{opportunity.confidence_pct}%",
        help="Cobertura disponible; no es una probabilidad de ganar.",
    )
    col3.metric(
        "Calidad empresarial",
        f"{fundamentals.score}/100" if fundamentals.score is not None else "N/D",
        help="Rentabilidad, crecimiento, deuda y caja. N/D significa que faltan datos suficientes.",
    )
    col4.metric(
        "Valoración",
        f"{valuation.score}/100" if valuation.score is not None else "N/D",
        help="Precio frente a beneficios, crecimiento, caja y patrimonio. Debe compararse dentro del sector.",
    )
    col5, col6, col7, col8 = st.columns(4)
    col5.metric(
        "Momento de entrada",
        f"{signal.score}/100",
        help="Resume tendencia, impulso, liderazgo, volumen y calidad de entrada.",
    )
    col6.metric(
        "Fortaleza frente al mercado",
        f"{relative.score}/100" if relative.score is not None else "N/D",
        help="Compara su avance con el índice general y, cuando existe, con su sector.",
    )
    col7.metric(
        "Riesgo controlado",
        f"{risk.score}/100" if risk.score is not None else "N/D",
        help="Una nota alta indica menor volatilidad, menor caída y mejor liquidez; no elimina el riesgo.",
    )
    col8.metric("Último cierre", f"{latest['close']:.2f}", format_pct(daily_change))
    if auto_snapshot_saved:
        st.caption(
            "✓ Seguimiento de hoy guardado automáticamente en «Historial guardado»."
        )
    if opportunity.label in {"Oportunidad destacada", "Candidata"}:
        st.success(f"**{opportunity.label}:** {opportunity.explanation}")
    elif opportunity.label == "Vigilancia":
        st.info(f"**{opportunity.label}:** {opportunity.explanation}")
    else:
        st.warning(f"**{opportunity.label}:** {opportunity.explanation}")
    st.caption(
        f"Lectura técnica: {signal.label} · Si ya la tienes: {signal.position_label} · "
        f"RSI: {latest['rsi']:.1f}"
    )
    plain_message = PLAIN_SIGNAL_GUIDANCE.get(signal.label, signal.explanation)
    if signal.label in {"Entrada fuerte", "Entrada interesante"}:
        st.success(f"**{signal.label}:** {plain_message}")
    elif signal.label == "Vigilancia":
        st.info(f"**{signal.label}:** {plain_message}")
    else:
        st.warning(f"**{signal.label}:** {plain_message}")

    if journal is not None:
        with st.expander("Guardar este análisis y consultar su evolución"):
            st.caption(
                "Se guarda una fotografía privada de las notas y del precio. "
                "Las gráficas se reconstruyen con información actual cuando vuelvas a abrirla."
            )
            personal_note = st.text_area(
                "Nota personal opcional",
                placeholder="Ejemplo: esperar resultados o revisar deuda antes de entrar",
                max_chars=1_000,
                key=f"analysis_note_{ticker}",
            )
            if st.button(
                "Guardar análisis",
                key=f"save_analysis_{ticker}",
                type="primary",
            ):
                try:
                    journal.add_analysis_snapshot(
                        ticker=ticker,
                        analyzed_at=signal.as_of,
                        price=float(latest["close"]),
                        opportunity_score=opportunity.score,
                        company_score=fundamentals.score,
                        entry_score=signal.score,
                        valuation_score=valuation.score,
                        relative_score=relative.score,
                        risk_score=risk.score,
                        opportunity_label=opportunity.label,
                        entry_label=signal.label,
                        position_label=signal.position_label,
                        expected_return_pct=(
                            study.median_return_pct if study.reliable else None
                        ),
                        positive_rate_pct=(
                            study.positive_rate_pct if study.reliable else None
                        ),
                        expected_price=expected_price,
                        horizon_days=(
                            study.horizon_days if study.reliable else None
                        ),
                        sector=fundamentals.sector or "",
                        explanation=opportunity.explanation,
                        note=personal_note,
                    )
                except (JournalStorageError, ValueError) as exc:
                    st.error(str(exc))
                else:
                    st.success(
                        f"Análisis de {ticker} guardado. Ya puedes comparar su evolución."
                    )

            try:
                recent_snapshots = journal.list_analysis_snapshots(ticker)
            except JournalStorageError as exc:
                st.warning(str(exc))
            else:
                if recent_snapshots.empty:
                    st.caption("Todavía no has guardado análisis anteriores de esta empresa.")
                else:
                    recent = recent_snapshots.head(8).copy()
                    recent["Cambio entrada"] = pd.to_numeric(
                        recent["entry_score"], errors="coerce"
                    ) - pd.to_numeric(
                        recent["entry_score"], errors="coerce"
                    ).shift(-1)
                    visible_recent = recent.loc[
                        :,
                        [
                            "analyzed_at",
                            "price",
                            "opportunity_score",
                            "entry_score",
                            "Cambio entrada",
                            "entry_label",
                            "expected_return_pct",
                            "note",
                        ],
                    ].rename(
                        columns={
                            "analyzed_at": "Fecha",
                            "price": "Precio",
                            "opportunity_score": "Oportunidad",
                            "entry_score": "Entrada",
                            "entry_label": "Lectura",
                            "expected_return_pct": "Retorno histórico",
                            "note": "Nota",
                        }
                    )
                    st.dataframe(
                        visible_recent,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Precio": st.column_config.NumberColumn(format="%.2f"),
                            "Oportunidad": st.column_config.ProgressColumn(
                                min_value=0, max_value=100, format="%d"
                            ),
                            "Entrada": st.column_config.ProgressColumn(
                                min_value=0, max_value=100, format="%d"
                            ),
                            "Cambio entrada": st.column_config.NumberColumn(
                                format="%+d"
                            ),
                            "Retorno histórico": st.column_config.NumberColumn(
                                format="%+.1f%%"
                            ),
                        },
                    )

    company_tab, entry_tab, risk_data_tab = st.tabs(
        ["Empresa y valoración", "Momento y liderazgo", "Riesgo y fuentes"]
    )
    with company_tab:
        if fundamentals.score is None:
            st.warning(
                f"Sólo está disponible el {fundamentals.coverage_pct}% de las métricas necesarias. "
                "La aplicación muestra N/D para no convertir datos ausentes en una nota engañosa."
            )
        else:
            context = " · ".join(
                value for value in (fundamentals.sector, fundamentals.country) if value
            )
            st.caption(
                f"Cobertura de datos: {fundamentals.coverage_pct}%"
                + (f" · {context}" if context else "")
            )
        good_company, company_risks = st.columns(2)
        with good_company:
            st.markdown("**Fortalezas empresariales**")
            if fundamentals.positive_factors:
                for factor in fundamentals.positive_factors:
                    st.markdown(f"- {friendly_factor(factor)}")
            else:
                st.caption("No hay suficientes fortalezas fundamentales disponibles.")
        with company_risks:
            st.markdown("**Debilidades empresariales**")
            if fundamentals.risk_factors:
                for factor in fundamentals.risk_factors:
                    st.markdown(f"- {friendly_factor(factor)}")
            else:
                st.caption("No aparecen debilidades fundamentales con los datos disponibles.")
        st.caption(
            "La nota de calidad no incluye valoración, riesgo país, divisa, regulación, "
            "gobierno corporativo ni ventajas competitivas."
        )
        st.markdown("**¿El precio parece razonable?**")
        if valuation.score is None:
            st.warning(
                f"Cobertura de valoración: {valuation.coverage_pct}%. "
                "Faltan múltiplos suficientes para asignar una nota."
            )
        else:
            st.caption(f"Cobertura de valoración: {valuation.coverage_pct}%")
        valuation_cols = st.columns(2)
        with valuation_cols[0]:
            for factor in valuation.positive_factors:
                st.markdown(f"- {friendly_factor(factor)}")
        with valuation_cols[1]:
            for factor in valuation.risk_factors:
                st.markdown(f"- {friendly_factor(factor)}")
        if valuation.metrics:
            metric_labels = {
                "PER futuro": "PER futuro",
                "PER histórico": "PER histórico",
                "PEG": "PEG",
                "Flujo de caja libre / capitalización (%)": "Rentabilidad de caja",
                "Precio / valor contable": "Precio/valor contable",
            }
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Métrica": metric_labels.get(name, name), "Valor": value}
                        for name, value in valuation.metrics
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

    with entry_tab:
        favorable, caution = st.columns(2)
        with favorable:
            st.markdown("**Lo que favorece una entrada**")
            if signal.positive_factors:
                for factor in signal.positive_factors:
                    st.markdown(f"- {friendly_factor(factor)}")
            else:
                st.caption("No hay factores técnicos positivos suficientes.")
        with caution:
            st.markdown("**Lo que conviene vigilar**")
            if signal.risk_factors:
                for factor in signal.risk_factors:
                    st.markdown(f"- {friendly_factor(factor)}")
            else:
                st.caption("No aparecen alertas técnicas importantes.")
        st.markdown("**Comparación con el mercado**")
        if relative.score is None:
            st.warning("No hay suficiente historial del índice para medir liderazgo relativo.")
        else:
            st.caption(
                f"Índice general: {relative.broad_benchmark or 'N/D'}"
                + (
                    f" · Referencia sectorial: {relative.sector_benchmark}"
                    if relative.sector_benchmark
                    else ""
                )
            )
            strength_cols = st.columns(3)
            strength_cols[0].metric(
                "Rentabilidad 3 meses",
                (
                    format_pct(relative.stock_return_3m_pct)
                    if relative.stock_return_3m_pct is not None
                    else "N/D"
                ),
            )
            strength_cols[1].metric(
                "Ventaja frente al mercado",
                (
                    f"{relative.broad_excess_3m_pct:+.1f} puntos"
                    if relative.broad_excess_3m_pct is not None
                    else "N/D"
                ),
            )
            strength_cols[2].metric(
                "Ventaja frente al sector",
                (
                    f"{relative.sector_excess_3m_pct:+.1f} puntos"
                    if relative.sector_excess_3m_pct is not None
                    else "N/D"
                ),
            )
            for factor in (*relative.positive_factors, *relative.risk_factors):
                st.markdown(f"- {friendly_factor(factor)}")

    with risk_data_tab:
        risk_cols = st.columns(4)
        risk_cols[0].metric(
            "Volatilidad anual",
            (
                f"{risk.annualized_volatility_pct:.1f}%"
                if risk.annualized_volatility_pct is not None
                else "N/D"
            ),
        )
        risk_cols[1].metric(
            "Peor caída en un año",
            (
                f"{risk.max_drawdown_1y_pct:.1f}%"
                if risk.max_drawdown_1y_pct is not None
                else "N/D"
            ),
        )
        risk_cols[2].metric(
            "Movimiento diario típico",
            f"{risk.atr_pct:.1f}%" if risk.atr_pct is not None else "N/D",
        )
        risk_cols[3].metric(
            "Negociación media diaria",
            (
                f"{risk.average_turnover_20d:,.0f}"
                if risk.average_turnover_20d is not None
                else "N/D"
            ),
        )
        for factor in (*risk.positive_factors, *risk.risk_factors):
            st.markdown(f"- {friendly_factor(factor)}")
        source_names = fundamentals.source_names or ("Yahoo Finance",)
        st.markdown("**Procedencia de los datos**")
        st.write(" · ".join(source_names))
        if fundamentals.official_period_end:
            st.caption(
                f"Último periodo oficial utilizado: {fundamentals.official_period_end}"
            )
        if fundamentals.official_url:
            st.markdown(f"[Abrir presentación oficial en SEC EDGAR]({fundamentals.official_url})")
        if verification:
            difference_text = (
                f"{verification.difference_pct:+.2f}%"
                if verification.difference_pct is not None
                else "no comparable"
            )
            if verification.status == "Revisar diferencia":
                st.error(
                    f"El cierre de {verification.provider} difiere {difference_text} "
                    f"del dato principal en {verification.as_of}."
                )
            else:
                st.info(
                    f"Comprobación {verification.provider}: {verification.status} "
                    f"({difference_text}, {verification.as_of})."
                )
        else:
            st.caption(
                "No se ha configurado una segunda fuente de precios. Puedes añadir una "
                "clave gratuita de Alpha Vantage en «Fuentes de datos»."
            )
        for warning in raw_fundamentals.get("_warnings", []):
            st.warning(str(warning))

        msn_links = build_msn_research_links(ticker)
        with st.expander("Contrastar este análisis con MSN Dinero"):
            st.caption(
                "MSN Dinero muestra datos de LSEG, noticias y previsiones de analistas. "
                "La aplicación no los copia ni los introduce automáticamente en el score "
                "porque Microsoft no ofrece una API pública estable para este uso."
            )
            msn_link_col, msn_home_col = st.columns(2)
            with msn_link_col:
                st.link_button(
                    f"Buscar la ficha de {ticker} en MSN",
                    msn_links.search_url,
                    width="stretch",
                )
            with msn_home_col:
                st.link_button(
                    "Abrir MSN Dinero",
                    msn_links.money_url,
                    width="stretch",
                )
            st.markdown(
                """
                **Qué conviene comparar**

                - Que ticker, bolsa y moneda coincidan con los de tu posición.
                - Próximos resultados y cambios recientes en previsiones de analistas.
                - Evolución de ingresos, márgenes, deuda y caja frente a la nota de empresa.
                - Noticias capaces de cambiar la tesis, no sólo el precio de una sesión.

                **Cómo usar el contraste**

                - Si coincide con la app, aumenta la confianza cualitativa, no el score.
                - Si difiere, no promedies los números: revisa fecha, moneda y proveedor.
                - Si aparece un riesgo nuevo o una rebaja fuerte de expectativas, reduce
                  el tamaño orientativo o espera hasta entender la causa.
                """
            )
            st.markdown(
                f"[Ver proveedor, retrasos y condiciones de MSN]({msn_links.disclaimer_url})"
            )

    if entry_price > 0 and signal.position_label in {"Reducir", "Vender"}:
        st.warning(
            f"Para la posición introducida, la lectura de gestión es «{signal.position_label}»."
        )

    plan = calculate_position_plan(
        capital=settings.initial_capital,
        entry_price=float(latest["close"]),
        stop_loss_pct=strategy.stop_loss_pct,
        max_risk_pct=strategy.max_risk_per_trade_pct,
    )
    with st.expander(
        "¿Cuánto arriesgaría si decidiera entrar?",
        expanded=signal.label in {"Entrada fuerte", "Entrada interesante"},
    ):
        st.caption(
            "Ejemplo matemático usando el último cierre como entrada. Asume que el capital y "
            "la acción están en la misma moneda; no incluye impuestos ni gaps."
        )
        risk_cols = st.columns(5)
        risk_cols[0].metric("Importe de la posición", f"{plan.position_value:,.2f}")
        risk_cols[1].metric("Unidades aproximadas", f"{plan.quantity:,.2f}")
        risk_cols[2].metric("Salida si falla", f"{plan.stop_price:,.2f}")
        risk_cols[3].metric("Pérdida prevista", f"{plan.loss_at_stop:,.2f}")
        risk_cols[4].metric("Referencia 2 a 1", f"{plan.reference_target_2r:,.2f}")
        st.caption(
            "«Referencia 2 a 1» significa que la ganancia potencial sería dos veces el riesgo. "
            "No es un precio objetivo ni una predicción."
        )

    entry_guide = build_entry_guide(
        fundamental_score=fundamentals.score,
        technical_score=signal.score,
        entry_label=signal.label,
        maximum_position_value=plan.position_value,
        current_price=float(latest["close"]),
        study=study,
    )
    st.subheader("Guía probabilística de compra")
    guide_cols = st.columns(4)
    guide_cols[0].metric("Guía", entry_guide.label)
    guide_cols[1].metric("Importe inicial orientativo", f"{entry_guide.initial_amount:,.2f}")
    guide_cols[2].metric("Unidades iniciales", f"{entry_guide.initial_quantity:,.3f}")
    guide_cols[3].metric(
        "Máximo según tu riesgo", f"{entry_guide.maximum_position_value:,.2f}"
    )
    st.write(entry_guide.rationale)
    if study.reliable:
        history_cols = st.columns(5)
        history_cols[0].metric("Casos históricos comparables", study.samples)
        history_cols[1].metric(
            f"Retorno mediano a {study.horizon_days} sesiones",
            format_pct(float(study.median_return_pct)),
        )
        history_cols[2].metric(
            "Casos positivos", f"{float(study.positive_rate_pct):.1f}%"
        )
        history_cols[3].metric(
            "Rango histórico central",
            f"{float(study.lower_quartile_pct):+.1f}% a "
            f"{float(study.upper_quartile_pct):+.1f}%",
        )
        history_cols[4].metric(
            "Precio estadístico orientativo",
            f"{float(expected_price):,.2f}",
        )
        st.caption(
            "La estimación es la mediana de señales históricas comparables y no una predicción. "
            "No incorpora resultados fundamentales futuros, noticias, impuestos ni gaps."
        )
    else:
        st.warning(
            f"Sólo se encontraron {study.samples} casos históricos comparables. "
            "Se necesitan al menos 8 para mostrar una estimación de retorno."
        )

    with st.expander("Ver explicación técnica completa"):
        st.write(signal.explanation)
    st.plotly_chart(
        price_chart(frame, ticker),
        width="stretch",
        config=PLOTLY_CONFIG,
    )
    st.plotly_chart(
        momentum_chart(frame, strategy.rsi_overbought),
        width="stretch",
        config=PLOTLY_CONFIG,
    )


def _backtest_cache_key(
    ticker: str,
    frame: pd.DataFrame,
    strategy: StrategyConfig,
    settings: BacktestConfig,
) -> tuple[object, ...]:
    """Identifica un resultado sin volver a simular en cada rerun de Streamlit."""

    return (
        ticker,
        len(frame),
        str(frame.index[0]) if not frame.empty else "",
        str(frame.index[-1]) if not frame.empty else "",
        round(float(frame["close"].iloc[-1]), 8) if not frame.empty else None,
        strategy,
        settings,
    )


def render_backtest(
    ticker: str,
    frame: pd.DataFrame,
    strategy: StrategyConfig,
    settings: BacktestConfig,
) -> None:
    st.caption(
        "Esta prueba simula qué habría pasado aplicando las mismas reglas en el pasado. "
        "Incluye costes y límites de pérdida, pero el pasado no garantiza resultados futuros."
    )
    cache_key = _backtest_cache_key(ticker, frame, strategy, settings)
    cached_results: dict[tuple[object, ...], BacktestResult] = st.session_state.setdefault(
        "backtest_results", {}
    )
    recalculate = st.button(
        "Recalcular prueba histórica",
        help="Úsalo si quieres repetir el cálculo. Al abrir esta pestaña se ejecuta automáticamente.",
    )
    if recalculate or cache_key not in cached_results:
        try:
            with st.spinner("Simulando estrategia…"):
                result = run_backtest(frame, strategy, settings)
            cached_results[cache_key] = result
            st.session_state["backtest_results"] = cached_results
        except ValueError as exc:
            st.error(str(exc))
            return
    result = cached_results[cache_key]
    metrics = result.metrics
    cols = st.columns(5)
    cols[0].metric("Resultado de las reglas", format_pct(float(metrics["total_return_pct"])))
    cols[1].metric("Comprar y mantener", format_pct(float(metrics["buy_hold_return_pct"])))
    cols[2].metric(
        "Peor caída temporal",
        format_pct(float(metrics["max_drawdown_pct"])),
        help="Mayor descenso sufrido desde un máximo anterior de la cartera.",
    )
    cols[3].metric(
        "Operaciones ganadoras",
        f"{float(metrics['win_rate_pct']):.1f}%",
        help="Porcentaje de operaciones cerradas con beneficio; no indica cuánto se ganó o perdió.",
    )
    cols[4].metric("Operaciones cerradas", int(metrics["completed_trades"]))
    st.plotly_chart(
        backtest_chart(result.equity_curve),
        width="stretch",
        config=PLOTLY_CONFIG,
    )
    with st.expander("Métricas adicionales"):
        st.json(
            {
                "Capital final": round(float(metrics["final_equity"]), 2),
                "Volatilidad anualizada (%)": round(float(metrics["annualized_volatility_pct"]), 2),
                "Sharpe (tasa 0)": round(float(metrics["sharpe_zero_rate"]), 2),
                "Exposición al mercado (%)": round(float(metrics["market_exposure_pct"]), 2),
            }
        )
    if result.trades.empty:
        st.info("No hubo operaciones cerradas con estas reglas.")
    else:
        st.dataframe(result.trades, width="stretch", hide_index=True)
        st.download_button(
            "Exportar operaciones del backtest",
            result.trades.to_csv(index=False).encode("utf-8"),
            file_name=f"backtest_{ticker}.csv",
            mime="text/csv",
        )


def render_long_horizon_calibration(
    prepared: dict[str, pd.DataFrame],
    summary: list[dict[str, object]],
    settings: BacktestConfig,
    fundamental_results: dict[str, FundamentalResult],
) -> None:
    """Traduce las señales históricas a objetivos de rentabilidad de 30+ días."""

    st.subheader("Objetivo de rentabilidad a 30 días o más")
    st.write(
        "Esta prueba no intenta ganar en una operación rápida. Simula una compra en "
        "la apertura posterior a cada nueva señal y mantiene la inversión durante todo "
        "el periodo elegido. Después compara el resultado neto con Segofactoring y "
        "Civislend en el mismo número de días."
    )
    if not prepared:
        st.info(
            "En el panel «Actualizar datos», elige tus favoritas y al menos cinco años "
            "de historial. Cuantas más empresas válidas, mejor será la muestra."
        )
        return

    horizon_options = {
        "≈ 30 días · 21 sesiones": 21,
        "≈ 2 meses · 42 sesiones": 42,
        "≈ 3 meses · 63 sesiones": 63,
        "≈ 6 meses · 126 sesiones": 126,
        "≈ 12 meses · 252 sesiones": 252,
    }
    control_a, control_b, control_c, control_d = st.columns(4)
    horizon_label = control_a.selectbox(
        "Tiempo mínimo invertido",
        list(horizon_options),
        index=0,
        key="calibration_horizon",
    )
    sego_rate = control_b.number_input(
        "Segofactoring anual (%)",
        min_value=0.0,
        max_value=100.0,
        value=5.5,
        step=0.5,
        key="calibration_sego_rate",
        help="Rentabilidad anual media utilizada como referencia, no como garantía.",
    )
    civislend_rate = control_c.number_input(
        "Civislend anual (%)",
        min_value=0.0,
        max_value=100.0,
        value=10.5,
        step=0.5,
        key="calibration_civislend_rate",
        help="Rentabilidad anual media utilizada como referencia, no como garantía.",
    )
    default_position = min(max(float(settings.initial_capital) / 10.0, 100.0), 5_000.0)
    position_value = control_d.number_input(
        "Importe simulado por empresa",
        min_value=10.0,
        value=float(default_position),
        step=100.0,
        key="calibration_position_value",
        help="Sirve para calcular el peso real de las comisiones fijas.",
    )

    try:
        result = calibrate_score_returns(
            prepared,
            horizon_sessions=horizon_options[horizon_label],
            sego_annual_rate_pct=float(sego_rate),
            civislend_annual_rate_pct=float(civislend_rate),
            position_value=float(position_value),
            fee_per_order=1.0,
            slippage_pct=float(settings.slippage_pct),
            minimum_samples=MINIMUM_RELIABLE_SAMPLES,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    target_cols = st.columns(4)
    target_cols[0].metric("Horizonte", f"{result.horizon_sessions} sesiones")
    target_cols[1].metric(
        "Meta Segofactoring equivalente",
        format_pct(result.sego_target_return_pct),
    )
    target_cols[2].metric(
        "Meta Civislend equivalente",
        format_pct(result.civislend_target_return_pct),
    )
    target_cols[3].metric(
        "Coste fijo simulado",
        "2,00 €",
        help="1 € en la compra y 1 € en la venta, además del deslizamiento configurado.",
    )
    st.caption(
        "Las metas se convierten desde una tasa anual compuesta al mismo horizonte. "
        "Así no se compara erróneamente un mes de bolsa con un año completo de una "
        "inversión alternativa."
    )

    if result.events.empty:
        st.warning(
            "No existen señales completas con tiempo posterior suficiente. Amplía el "
            "historial o carga más empresas."
        )
        return

    aggregate = result.by_score.loc[
        result.by_score["score_tier"] == "Todas las entradas · 65+"
    ].iloc[0]
    result_cols = st.columns(5)
    result_cols[0].metric("Señales estudiadas", int(aggregate["samples"]))
    result_cols[1].metric(
        "Rentabilidad mediana neta",
        format_pct(float(aggregate["median_net_return_pct"])),
    )
    result_cols[2].metric(
        "Terminó en positivo",
        f"{float(aggregate['positive_rate_pct']):.1f}%",
    )
    result_cols[3].metric(
        "Superó Segofactoring",
        f"{float(aggregate['beat_sego_rate_pct']):.1f}%",
    )
    result_cols[4].metric(
        "Superó Civislend",
        f"{float(aggregate['beat_civislend_rate_pct']):.1f}%",
    )
    if int(aggregate["samples"]) < MINIMUM_RELIABLE_SAMPLES:
        st.warning(
            f"Sólo hay {int(aggregate['samples'])} casos. La cifra todavía es orientativa; "
            f"se necesitan al menos {MINIMUM_RELIABLE_SAMPLES} señales no solapadas."
        )
    else:
        st.info(
            "La frecuencia de superar Civislend tiene un intervalo de incertidumbre del "
            f"95% entre {float(aggregate['beat_civislend_ci_low_pct']):.1f}% y "
            f"{float(aggregate['beat_civislend_ci_high_pct']):.1f}%. No es una garantía."
        )

    st.plotly_chart(
        return_calibration_chart(result.by_score),
        width="stretch",
        config=PLOTLY_CONFIG,
    )
    visible_calibration = result.by_score.rename(
        columns={
            "score_tier": "Nivel de entrada",
            "samples": "Casos",
            "enough_evidence": "30+ casos",
            "median_net_return_pct": "Mediana neta",
            "positive_rate_pct": "Positivos",
            "beat_sego_rate_pct": "Supera Sego",
            "beat_civislend_rate_pct": "Supera Civislend",
            "lower_quartile_pct": "Cuartil débil",
            "upper_quartile_pct": "Cuartil favorable",
            "median_drawdown_pct": "Caída mediana",
            "worst_decile_drawdown_pct": "Caída del 10% peor",
        }
    )
    visible_calibration["30+ casos"] = visible_calibration["30+ casos"].map(
        {True: "Sí", False: "Todavía no"}
    )
    st.dataframe(
        visible_calibration.loc[
            :,
            [
                "Nivel de entrada",
                "Casos",
                "30+ casos",
                "Mediana neta",
                "Positivos",
                "Supera Sego",
                "Supera Civislend",
                "Cuartil débil",
                "Cuartil favorable",
                "Caída mediana",
                "Caída del 10% peor",
            ],
        ],
        width="stretch",
        hide_index=True,
        column_config={
            column: st.column_config.NumberColumn(format="%+.1f%%")
            for column in [
                "Mediana neta",
                "Cuartil débil",
                "Cuartil favorable",
                "Caída mediana",
                "Caída del 10% peor",
            ]
        }
        | {
            column: st.column_config.NumberColumn(format="%.1f%%")
            for column in ["Positivos", "Supera Sego", "Supera Civislend"]
        },
    )

    current_rows: list[dict[str, object]] = []
    for row in summary:
        ticker = str(row.get("Ticker") or "")
        current_score = int(row.get("Momento entrada") or 0)
        calibrated = calibration_for_score(result, current_score)
        fundamentals = fundamental_results.get(ticker)
        current_rows.append(
            {
                "Ticker": ticker,
                "Empresa /100": fundamentals.score if fundamentals else None,
                "Entrada /100": current_score,
                "Lectura": row.get("Lectura entrada"),
                "Casos del nivel": (
                    int(calibrated["samples"]) if calibrated is not None else None
                ),
                "Mediana neta": (
                    float(calibrated["median_net_return_pct"])
                    if calibrated is not None
                    else None
                ),
                "Supera Civislend": (
                    float(calibrated["beat_civislend_rate_pct"])
                    if calibrated is not None
                    else None
                ),
                "Evidencia": (
                    "Suficiente"
                    if calibrated is not None
                    and bool(calibrated["enough_evidence"])
                    else "Insuficiente"
                ),
            }
        )
    if current_rows:
        st.markdown("### Cómo se traduce al seguimiento actual")
        st.caption("Pulsa una fila para abrir el análisis completo de esa empresa.")
        render_ticker_dataframe(
            pd.DataFrame(current_rows).sort_values(
                ["Entrada /100", "Empresa /100"],
                ascending=False,
                na_position="last",
            ),
            key="calibration_current_companies",
            column_config={
                "Empresa /100": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                ),
                "Entrada /100": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                ),
                "Mediana neta": st.column_config.NumberColumn(format="%+.1f%%"),
                "Supera Civislend": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )

    currencies = sorted(
        {
            result.currency
            for result in fundamental_results.values()
            if result.currency and result.currency != "EUR"
        }
    )
    if currencies:
        st.warning(
            "Hay cotizaciones en " + ", ".join(currencies) + ". Esta primera calibración "
            "mide el rendimiento en la moneda de cotización; el cambio histórico a euros "
            "puede mejorar o empeorar el resultado real."
        )
    with st.expander("Ver los casos históricos utilizados"):
        events = result.events.rename(
            columns={
                "ticker": "Ticker",
                "signal_date": "Fecha señal",
                "entry_date": "Fecha compra simulada",
                "exit_date": "Fecha final",
                "score": "Score",
                "score_tier": "Nivel",
                "net_return_pct": "Rentabilidad neta",
                "maximum_drawdown_pct": "Peor caída durante el periodo",
                "beat_sego": "Superó Sego",
                "beat_civislend": "Superó Civislend",
            }
        )
        render_ticker_dataframe(
            events.loc[
                :,
                [
                    "Ticker",
                    "Fecha señal",
                    "Fecha compra simulada",
                    "Fecha final",
                    "Score",
                    "Nivel",
                    "Rentabilidad neta",
                    "Peor caída durante el periodo",
                    "Superó Sego",
                    "Superó Civislend",
                ],
            ],
            key="calibration_historical_events",
        )
    st.caption(
        "Usa precios ajustados para incorporar splits y dividendos distribuidos por el "
        "proveedor. La calidad empresarial actual se muestra aparte porque no disponemos "
        "todavía de fundamentales históricos punto en el tiempo; introducirlos en el "
        "pasado produciría look-ahead bias."
    )


def render_operation_form(
    journal: object,
    *,
    form_key: str,
    fixed_fee: float,
    owner_label: str | None = None,
    flash_key: str = "_journal_flash",
    recorded_by: str = "",
    notes_label: str = "Notas",
) -> None:
    """Formulario común para que un usuario o el administrador registre operaciones."""

    heading = (
        f"Registrar operación para {owner_label}"
        if owner_label
        else "Registrar operación"
    )
    st.subheader(heading)
    with st.form(form_key, clear_on_submit=True):
        ticker = st.text_input("Ticker")
        side = st.selectbox("Tipo", ["Compra", "Venta"])
        quantity = st.number_input(
            "Cantidad",
            min_value=0.000001,
            value=1.0,
            format="%.6f",
        )
        price = st.number_input(
            "Precio",
            min_value=0.000001,
            value=1.0,
            format="%.4f",
        )
        currency = st.selectbox(
            "Moneda",
            ["EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD"],
        )
        fees = st.number_input(
            "Comisión pagada",
            min_value=0.0,
            value=float(fixed_fee),
            format="%.2f",
            help="Por defecto se utiliza 1 unidad monetaria por operación.",
        )
        executed_at = st.date_input("Fecha", value=date.today())
        notes = st.text_area(notes_label)
        submitted = st.form_submit_button("Guardar", type="primary")
    if not submitted:
        return
    if not ticker.strip():
        st.error("El ticker es obligatorio.")
        return
    try:
        journal.add_operation(
            ticker,
            side,
            quantity,
            price,
            fees,
            executed_at,
            notes,
            currency=currency,
            recorded_by=recorded_by,
        )
    except (ValueError, JournalStorageError) as exc:
        st.error(str(exc))
        return
    st.session_state[flash_key] = (
        f"Operación guardada para {owner_label}."
        if owner_label
        else f"Operación guardada en {getattr(journal, 'backend_name', 'el diario')}."
    )
    st.rerun()


def operation_history_for_display(operations: pd.DataFrame) -> pd.DataFrame:
    """Presenta el diario con etiquetas sencillas sin alterar los datos exportados."""

    return operations.rename(
        columns={
            "id": "ID",
            "ticker": "Empresa",
            "side": "Tipo",
            "quantity": "Cantidad",
            "price": "Precio",
            "fees": "Comisión",
            "executed_at": "Fecha",
            "notes": "Motivo / notas",
            "currency": "Moneda",
            "recorded_by": "Registrado por",
            "created_at": "Guardado el",
        }
    )


def render_current_position_form(
    journal: object,
    *,
    portfolio_snapshots: pd.DataFrame,
    accounts: pd.DataFrame,
    actor_username: str,
    view_key: str,
) -> None:
    """Alta guiada de una posición actual, sin exigir una fecha de compra inventada."""

    expanded = portfolio_snapshots.empty
    with st.expander("Añadir o actualizar una posición", expanded=expanded):
        st.caption(
            "Copia lo que ves ahora en tu bróker. No hace falta conocer la fecha de compra: "
            "la app guarda una foto de hoy y calcula coste, ganancia y rentabilidad."
        )

        with st.form(f"{view_key}_current_position_search_form"):
            query = st.text_input(
                "Buscar empresa o ETF",
                placeholder="Nintendo, BAE Systems, Netflix…",
                key=f"{view_key}_current_position_query",
            )
            search_submitted = st.form_submit_button(
                "Buscar empresa",
                icon=":material/search:",
            )
        search_state_key = f"{view_key}_current_position_results"
        if search_submitted:
            try:
                found_results = cached_company_search(query)
                st.session_state[search_state_key] = found_results
                if found_results:
                    st.session_state[f"{view_key}_current_position_direct"] = False
            except (DataDownloadError, ValueError) as exc:
                st.session_state[search_state_key] = []
                st.error(str(exc))

        results: list[TickerSearchResult] = st.session_state.get(search_state_key, [])
        selected_result: TickerSearchResult | None = None
        direct_entry = st.checkbox(
            "Escribir el ticker directamente",
            value=not bool(results),
            key=f"{view_key}_current_position_direct",
            help="Úsalo si el buscador no encuentra una cotización concreta.",
        )
        if results and not direct_entry:
            market = st.selectbox(
                "Mercado",
                _search_market_options(results),
                key=f"{view_key}_current_position_market",
            )
            result_indices = _search_result_indices(results, market)
            if result_indices:
                selected_index = st.selectbox(
                    "Empresa y cotización",
                    result_indices,
                    format_func=lambda index: _search_result_label(results[index]),
                    key=f"{view_key}_current_position_result",
                )
                selected_result = results[selected_index]
                if selected_result.details:
                    st.caption(selected_result.details)
            else:
                st.info("No hay resultados en ese mercado.")

        if direct_entry:
            identity_cols = st.columns(2)
            raw_ticker = identity_cols[0].text_input(
                "Ticker",
                placeholder="NFLX, NTDOY, BA.L…",
                key=f"{view_key}_current_position_ticker",
            )
            asset_name = identity_cols[1].text_input(
                "Nombre de la empresa",
                placeholder="Netflix",
                key=f"{view_key}_current_position_name",
            )
        elif selected_result is not None:
            raw_ticker = selected_result.ticker
            asset_name = selected_result.name
        else:
            raw_ticker = ""
            asset_name = ""

        existing_accounts = (
            accounts["account_name"].dropna().astype(str).tolist()
            if not accounts.empty and "account_name" in accounts
            else []
        )
        common_accounts = ["Trade Republic", "Revolut", "MyInvestor"]
        account_options = list(dict.fromkeys([*existing_accounts, *common_accounts]))
        account_options.append("Otra plataforma")
        chosen_account = st.selectbox(
            "¿Dónde la tienes?",
            account_options,
            key=f"{view_key}_current_position_account",
        )
        platform = (
            st.text_input(
                "Nombre de la plataforma",
                placeholder="Interactive Brokers, DEGIRO…",
                key=f"{view_key}_current_position_custom_account",
            )
            if chosen_account == "Otra plataforma"
            else chosen_account
        )

        value_col, reference_col = st.columns(2)
        current_value = value_col.number_input(
            "Valor actual de la posición (€)",
            min_value=0.0,
            value=100.0,
            step=10.0,
            format="%.2f",
            key=f"{view_key}_current_position_value",
            help="El importe total que aparece hoy para esa empresa, ya convertido a euros.",
        )
        reference_kind = reference_col.selectbox(
            "¿Qué otro dato muestra tu bróker?",
            REFERENCE_OPTIONS,
            key=f"{view_key}_current_position_reference_kind",
        )

        reference_value: float | None = None
        quantity: float | None = None
        average_entry_price: float | None = None
        buy_fee = 0.0
        if reference_kind == REFERENCE_GAIN:
            reference_value = st.number_input(
                "Ganancia o pérdida total (€)",
                value=0.0,
                step=5.0,
                format="%.2f",
                key=f"{view_key}_current_position_gain",
                help="Escribe una pérdida con signo menos, por ejemplo -52,89.",
            )
        elif reference_kind == REFERENCE_RETURN:
            reference_value = st.number_input(
                "Rentabilidad desde la compra (%)",
                value=0.0,
                step=0.5,
                format="%.2f",
                key=f"{view_key}_current_position_return",
                help="Escribe -9,67 si el bróker muestra una pérdida del 9,67%.",
            )
        elif reference_kind == REFERENCE_COST:
            reference_value = st.number_input(
                "Dinero invertido (€)",
                min_value=0.0,
                value=100.0,
                step=10.0,
                format="%.2f",
                key=f"{view_key}_current_position_cost",
            )
        else:
            exact_cols = st.columns(3)
            quantity = exact_cols[0].number_input(
                "Cantidad",
                min_value=0.0,
                value=1.0,
                step=0.1,
                format="%.6f",
                key=f"{view_key}_current_position_quantity",
            )
            average_entry_price = exact_cols[1].number_input(
                "Precio medio de compra (€)",
                min_value=0.0,
                value=100.0,
                step=1.0,
                format="%.4f",
                key=f"{view_key}_current_position_entry",
            )
            buy_fee = exact_cols[2].number_input(
                "Comisión de compra (€)",
                min_value=0.0,
                value=1.0,
                step=0.5,
                format="%.2f",
                key=f"{view_key}_current_position_fee",
            )

        with st.expander("Datos opcionales"):
            if reference_kind != REFERENCE_ENTRY:
                optional_cols = st.columns(2)
                optional_quantity = optional_cols[0].number_input(
                    "Cantidad de acciones (si la sabes)",
                    min_value=0.0,
                    value=0.0,
                    step=0.1,
                    format="%.6f",
                    key=f"{view_key}_current_position_optional_quantity",
                )
                optional_entry = optional_cols[1].number_input(
                    "Precio medio de compra en euros (si lo sabes)",
                    min_value=0.0,
                    value=0.0,
                    step=1.0,
                    format="%.4f",
                    key=f"{view_key}_current_position_optional_entry",
                )
                quantity = float(optional_quantity) or None
                average_entry_price = float(optional_entry) or None
            valuation_date = st.date_input(
                "Fecha de esta valoración",
                value=date.today(),
                max_value=date.today(),
                key=f"{view_key}_current_position_date",
                help="Déjala en hoy salvo que estés copiando una captura anterior.",
            )
            comments = st.text_input(
                "Nota personal",
                placeholder="Cuenta de largo plazo, posición parcial…",
                key=f"{view_key}_current_position_comments",
            )

        estimate = None
        try:
            estimate = estimate_current_position(
                current_value_eur=float(current_value),
                reference_kind=reference_kind,
                reference_value=reference_value,
                quantity=quantity,
                average_entry_price=average_entry_price,
                buy_fee_eur=float(buy_fee),
            )
        except ValueError as exc:
            st.caption(f"Completa los importes para calcular el resultado: {exc}")
        if estimate is not None:
            preview_cols = st.columns(3)
            preview_cols[0].metric("Coste calculado", f"{estimate.cost_estimate_eur:,.2f} €")
            preview_cols[1].metric("Ganancia / pérdida", f"{estimate.gain_loss_eur:+,.2f} €")
            preview_cols[2].metric("Rentabilidad", f"{estimate.return_pct:+.2f}%")
            if estimate.warning:
                st.warning(estimate.warning)

        save_disabled = estimate is None or not raw_ticker.strip() or not platform.strip()
        if st.button(
            "Guardar en mi cartera actual",
            type="primary",
            width="stretch",
            disabled=save_disabled,
            key=f"{view_key}_save_current_position",
        ):
            try:
                ticker = resolve_analysis_ticker(raw_ticker)
                display_name = asset_name.strip() or ticker
                assert estimate is not None
                position = {
                    "platform": platform.strip(),
                    "asset_name": display_name,
                    "raw_identifier": raw_ticker.strip().upper(),
                    "analysis_ticker": ticker,
                    "asset_type": (
                        selected_result.instrument_type
                        if selected_result is not None
                        else "Acción / ETF"
                    ),
                    "portfolio_block": "Cartera actual",
                    "quantity": estimate.quantity,
                    "current_price": estimate.current_price,
                    "currency": "EUR",
                    "value_eur": estimate.current_value_eur,
                    "return_pct": estimate.return_pct,
                    "cost_estimate_eur": estimate.cost_estimate_eur,
                    "gain_loss_eur": estimate.gain_loss_eur,
                    "comments": comments,
                    "source": "Introducida manualmente por el usuario",
                    "notes": (
                        "Posición actual calculada con datos declarados por el usuario; "
                        "no representa una operación histórica."
                    ),
                }
                updated_snapshot = snapshot_with_current_position(
                    portfolio_snapshots,
                    snapshot_date=valuation_date,
                    position=position,
                )
                journal.upsert_portfolio_snapshot_positions(
                    updated_snapshot,
                    recorded_by=actor_username,
                )
                platform_rows = updated_snapshot.loc[
                    updated_snapshot["platform"].fillna("").astype(str) == platform.strip()
                ]
                non_cash = (
                    platform_rows["asset_type"].fillna("").astype(str).str.casefold()
                    != "efectivo"
                )
                platform_value = float(
                    pd.to_numeric(
                        platform_rows.loc[non_cash, "value_eur"], errors="coerce"
                    ).fillna(0.0).sum()
                )
                existing_account = (
                    accounts.loc[
                        accounts["account_name"].fillna("").astype(str) == platform.strip()
                    ].head(1)
                    if not accounts.empty
                    else pd.DataFrame()
                )
                cash_balance = (
                    float(existing_account.iloc[0]["cash_balance"])
                    if not existing_account.empty
                    and pd.notna(existing_account.iloc[0]["cash_balance"])
                    else 0.0
                )
                journal.upsert_portfolio_account(
                    account_name=platform.strip(),
                    account_type="Bróker",
                    investments_value=platform_value,
                    cash_balance=cash_balance,
                    currency="EUR",
                    status="Actualizada",
                    notes=f"Calculada con las posiciones del {valuation_date.isoformat()}.",
                )
            except (AssertionError, ValueError, JournalStorageError) as exc:
                st.error(str(exc))
            else:
                st.success(f"{display_name} se ha guardado en tu cartera.")
                st.rerun()


def render_private_investments(
    journal: object,
    *,
    actor_username: str,
    view_key: str,
    include_alternative_investments: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Gestiona la cartera actual y, sólo para ddriu, proyectos alternativos."""

    st.subheader("Mi cartera actual")
    st.caption(
        "Añade lo que tienes en cada bróker y consulta el valor, la ganancia o pérdida "
        "y la distribución de tu cartera. Cada usuario ve únicamente sus propios datos."
    )
    if include_alternative_investments:
        try:
            investments = journal.list_private_investments()
        except JournalStorageError:
            st.error(
                "La tabla de inversiones privadas todavía no existe en Supabase. "
                "Ejecuta `supabase/migration_private_investments.sql` en SQL Editor."
            )
            investments = pd.DataFrame()
    else:
        investments = pd.DataFrame()
    try:
        accounts = journal.list_portfolio_accounts()
    except (JournalStorageError, AttributeError):
        st.error(
            "La tabla de cuentas todavía no existe en Supabase. "
            "Ejecuta `supabase/migration_portfolio_accounts.sql` en SQL Editor."
        )
        accounts = pd.DataFrame()
    snapshot_storage_ready = hasattr(journal, "list_portfolio_snapshot_positions")
    if snapshot_storage_ready:
        try:
            portfolio_snapshots = journal.list_portfolio_snapshot_positions()
        except JournalStorageError:
            portfolio_snapshots = pd.DataFrame()
            snapshot_storage_ready = False
    else:
        portfolio_snapshots = pd.DataFrame()

    if (
        include_alternative_investments
        and accounts.empty
        and hasattr(journal, "upsert_portfolio_account")
    ):
        try:
            for account_name, account_type in DEFAULT_DDRIU_ACCOUNTS:
                journal.upsert_portfolio_account(
                    account_name=account_name,
                    account_type=account_type,
                    status="Pendiente de actualizar",
                    notes="Cuenta provisional: faltan posiciones e importes.",
                )
            accounts = journal.list_portfolio_accounts()
        except (JournalStorageError, ValueError):
            # La migración remota puede estar todavía pendiente; el mensaje anterior
            # explica cómo habilitar la tabla sin bloquear el resto de la cartera.
            accounts = pd.DataFrame()

    if snapshot_storage_ready:
        render_current_position_form(
            journal,
            portfolio_snapshots=portfolio_snapshots,
            accounts=accounts,
            actor_username=actor_username,
            view_key=view_key,
        )
    else:
        st.error(
            "No se pueden guardar posiciones hasta crear la tabla de cartera en Supabase."
        )

    with st.expander("Importar una fotografía completa de mi cartera (.xlsx)"):
        st.caption(
            "Guarda las posiciones tal como aparecen en el archivo, sin inventar compras. "
            "Si faltan cantidades o el coste es estimado, la app lo indica. Volver a subir "
            "la misma fecha actualiza esa fotografía; una fecha nueva crea histórico."
        )
        portfolio_file = st.file_uploader(
            "Excel de cartera",
            type=["xlsx"],
            key=f"{view_key}_portfolio_snapshot_excel",
        )
        if portfolio_file is not None:
            try:
                workbook_snapshot = parse_portfolio_snapshot_excel(
                    portfolio_file.getvalue()
                )
            except (ValueError, ImportError) as exc:
                st.error(str(exc))
            else:
                preview = workbook_snapshot.positions
                preview_cost = float(
                    pd.to_numeric(preview["cost_estimate_eur"], errors="coerce").sum()
                )
                preview_value = float(preview["value_eur"].sum())
                preview_pnl = float(
                    pd.to_numeric(preview["gain_loss_eur"], errors="coerce").sum()
                )
                preview_cols = st.columns(4)
                preview_cols[0].metric("Fecha", workbook_snapshot.snapshot_date)
                preview_cols[1].metric("Valor declarado", f"{preview_value:,.2f} €")
                preview_cols[2].metric("Coste estimado", f"{preview_cost:,.2f} €")
                preview_cols[3].metric("Resultado estimado", f"{preview_pnl:+,.2f} €")
                missing_quantities = int(preview["quantity"].isna().sum())
                if missing_quantities:
                    st.info(
                        f"{missing_quantities} líneas no incluyen cantidad. Se guardarán "
                        "como valoración, no como operaciones de compra."
                    )
                st.dataframe(
                    workbook_snapshot.accounts.rename(
                        columns={
                            "account_name": "Cuenta",
                            "investments_value": "Inversiones €",
                            "cash_balance": "Efectivo €",
                        }
                    ).loc[:, ["Cuenta", "Inversiones €", "Efectivo €"]],
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Inversiones €": st.column_config.NumberColumn(format="%.2f €"),
                        "Efectivo €": st.column_config.NumberColumn(format="%.2f €"),
                    },
                )
                if not snapshot_storage_ready:
                    st.error(
                        "Falta la tabla de fotografías. Ejecuta "
                        "`supabase/migration_portfolio_snapshots.sql` en Supabase."
                    )
                elif st.button(
                    "Guardar esta fotografía histórica",
                    type="primary",
                    key=f"{view_key}_save_portfolio_snapshot",
                ):
                    try:
                        import_result = import_portfolio_workbook_snapshot(
                            journal,
                            workbook_snapshot,
                            recorded_by=actor_username,
                        )
                    except (ValueError, JournalStorageError) as exc:
                        st.error(str(exc))
                    else:
                        st.success(
                            f"Guardadas {import_result.positions_saved} posiciones, "
                            f"{import_result.accounts_saved} cuentas y "
                            f"{import_result.civislend_created} proyectos nuevos de Civislend."
                        )
                        st.rerun()

    if not accounts.empty:
        account_view = accounts.copy()
        account_view["investments_value"] = pd.to_numeric(
            account_view["investments_value"], errors="coerce"
        ).fillna(0.0)
        account_view["cash_balance"] = pd.to_numeric(
            account_view["cash_balance"], errors="coerce"
        ).fillna(0.0)
        project_values: dict[str, float] = {}
        if not investments.empty:
            project_values = (
                investments.groupby("platform")["current_value"].sum().astype(float).to_dict()
            )
        account_view["source"] = "Valor provisional manual"
        for row_index, row in account_view.iterrows():
            account_name = str(row["account_name"])
            if account_name in project_values:
                account_view.at[row_index, "investments_value"] = project_values[account_name]
                account_view.at[row_index, "source"] = "Calculado con proyectos registrados"
        account_view["total_value"] = (
            account_view["investments_value"] + account_view["cash_balance"]
        )
        euro_accounts = account_view.loc[account_view["currency"] == "EUR"]
        total_accounts = float(euro_accounts["total_value"].sum())
        total_cash = float(euro_accounts["cash_balance"].sum())
        pending_accounts = int(
            (account_view["status"] == "Pendiente de actualizar").sum()
        )
        account_cols = st.columns(3)
        account_cols[0].metric("Valor agregado provisional", f"{total_accounts:,.2f} €")
        account_cols[1].metric("Efectivo declarado", f"{total_cash:,.2f} €")
        account_cols[2].metric("Cuentas pendientes", pending_accounts)
        if len(euro_accounts) != len(account_view):
            st.caption(
                "El total superior suma sólo cuentas declaradas en EUR; los importes en "
                "otras monedas permanecen visibles en la tabla sin mezclarse."
            )
        account_display = account_view.rename(
            columns={
                "account_name": "Cuenta",
                "account_type": "Tipo",
                "investments_value": "Inversiones",
                "cash_balance": "Efectivo",
                "total_value": "Total",
                "currency": "Moneda",
                "status": "Estado",
                "source": "Origen del valor",
                "updated_at": "Actualizada el",
            }
        )
        st.dataframe(
            account_display.loc[
                :,
                [
                    "Cuenta", "Tipo", "Inversiones", "Efectivo", "Total", "Moneda",
                    "Estado", "Origen del valor", "Actualizada el",
                ],
            ],
            width="stretch",
            hide_index=True,
            column_config={
                "Inversiones": st.column_config.NumberColumn(format="%.2f"),
                "Efectivo": st.column_config.NumberColumn(format="%.2f"),
                "Total": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        if total_accounts <= 0:
            st.info(
                "Las cuentas están a cero hasta que introduzcas sus posiciones o importes."
            )
        with st.expander("Actualizar el total provisional de una cuenta"):
            account_names = account_view["account_name"].astype(str).tolist()
            selected_account_name = st.selectbox(
                "Cuenta",
                account_names,
                key=f"{view_key}_portfolio_account_name",
            )
            selected_account = account_view.loc[
                account_view["account_name"] == selected_account_name
            ].iloc[0]
            with st.form(f"{view_key}_portfolio_account_form"):
                account_amount_cols = st.columns(2)
                provisional_investments = account_amount_cols[0].number_input(
                    "Valor de las inversiones",
                    min_value=0.0,
                    value=float(selected_account["investments_value"]),
                    step=100.0,
                    help=(
                        "Para Civislend y Segofactoring, los proyectos registrados debajo "
                        "tienen prioridad sobre este total manual."
                    ),
                )
                provisional_cash = account_amount_cols[1].number_input(
                    "Efectivo disponible",
                    min_value=0.0,
                    value=float(selected_account["cash_balance"]),
                    step=100.0,
                )
                account_currency = st.selectbox(
                    "Moneda",
                    ["EUR", "USD", "GBP", "CHF", "JPY"],
                    index=(
                        ["EUR", "USD", "GBP", "CHF", "JPY"].index(
                            str(selected_account["currency"])
                        )
                        if str(selected_account["currency"])
                        in ["EUR", "USD", "GBP", "CHF", "JPY"]
                        else 0
                    ),
                )
                selected_status = str(selected_account["status"])
                account_status = st.selectbox(
                    "Estado de los datos",
                    PORTFOLIO_ACCOUNT_STATUSES,
                    index=(
                        PORTFOLIO_ACCOUNT_STATUSES.index(selected_status)
                        if selected_status in PORTFOLIO_ACCOUNT_STATUSES
                        else 0
                    ),
                )
                account_notes = st.text_area(
                    "Notas",
                    value=str(selected_account.get("notes") or ""),
                )
                save_account = st.form_submit_button("Actualizar cuenta", type="primary")
            if save_account:
                try:
                    journal.upsert_portfolio_account(
                        account_name=selected_account_name,
                        account_type=str(selected_account["account_type"]),
                        investments_value=float(provisional_investments),
                        cash_balance=float(provisional_cash),
                        currency=account_currency,
                        status=account_status,
                        notes=account_notes,
                    )
                except (ValueError, JournalStorageError) as exc:
                    st.error(str(exc))
                else:
                    st.success(f"{selected_account_name} actualizada.")
                    st.rerun()
    else:
        st.info("Todavía no hay cuentas agregadas para mostrar.")

    if not portfolio_snapshots.empty:
        snapshot_view = portfolio_snapshots.copy()
        snapshot_view["snapshot_date"] = pd.to_datetime(
            snapshot_view["snapshot_date"], errors="coerce"
        )
        latest_date = snapshot_view["snapshot_date"].max()
        latest_positions = snapshot_view.loc[
            snapshot_view["snapshot_date"] == latest_date
        ].copy()
        for column in [
            "value_eur", "cost_estimate_eur", "gain_loss_eur", "return_pct"
        ]:
            latest_positions[column] = pd.to_numeric(
                latest_positions[column], errors="coerce"
            )
        latest_value = float(latest_positions["value_eur"].sum())
        latest_cost = float(latest_positions["cost_estimate_eur"].sum())
        latest_pnl = float(latest_positions["gain_loss_eur"].sum())
        st.subheader("Fotografía de posiciones")
        st.caption(
            f"Última fotografía guardada: {latest_date:%d/%m/%Y}. "
            "Es una valoración histórica; no sustituye el diario de compras y ventas."
        )
        snapshot_cols = st.columns(4)
        snapshot_cols[0].metric("Valor declarado", f"{latest_value:,.2f} €")
        snapshot_cols[1].metric("Coste estimado", f"{latest_cost:,.2f} €")
        snapshot_cols[2].metric("Resultado estimado", f"{latest_pnl:+,.2f} €")
        snapshot_cols[3].metric("Líneas de cartera", len(latest_positions))
        chart_cols = st.columns(2)
        with chart_cols[0]:
            st.plotly_chart(
                portfolio_snapshot_allocation_chart(latest_positions),
                width="stretch",
                config=PLOTLY_CONFIG,
            )
        with chart_cols[1]:
            st.plotly_chart(
                portfolio_snapshot_history_chart(snapshot_view),
                width="stretch",
                config=PLOTLY_CONFIG,
            )

        snapshot_display = latest_positions.rename(
            columns={
                "platform": "Plataforma",
                "asset_name": "Activo",
                "analysis_ticker": "Ticker para analizar",
                "asset_type": "Tipo",
                "portfolio_block": "Bloque",
                "quantity": "Cantidad",
                "currency": "Moneda",
                "value_eur": "Valor €",
                "return_pct": "Rentabilidad estimada %",
                "cost_estimate_eur": "Coste estimado €",
                "gain_loss_eur": "Resultado estimado €",
                "comments": "Comentarios",
            }
        )
        st.caption("Pulsa una fila con ticker reconocido para abrir su análisis.")
        render_ticker_dataframe(
            snapshot_display.loc[
                :,
                [
                    "Plataforma", "Activo", "Ticker para analizar", "Tipo", "Bloque",
                    "Cantidad", "Moneda", "Valor €", "Coste estimado €",
                    "Resultado estimado €", "Rentabilidad estimada %", "Comentarios",
                ],
            ],
            key=f"{view_key}_portfolio_snapshot_positions",
            ticker_column="Ticker para analizar",
            column_config={
                "Cantidad": st.column_config.NumberColumn(format="%.4f"),
                "Valor €": st.column_config.NumberColumn(format="%.2f €"),
                "Coste estimado €": st.column_config.NumberColumn(format="%.2f €"),
                "Resultado estimado €": st.column_config.NumberColumn(format="%+.2f €"),
                "Rentabilidad estimada %": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )
        analyzable = latest_positions.loc[
            latest_positions["analysis_ticker"].fillna("").astype(str).str.strip() != ""
        ]
        if not analyzable.empty:
            analysis_options = {
                f"{row.analysis_ticker} · {row.asset_name} · {row.platform}": str(
                    row.analysis_ticker
                )
                for row in analyzable.itertuples(index=False)
            }
            analysis_cols = st.columns([3, 1])
            selected_snapshot_position = analysis_cols[0].selectbox(
                "Abrir una posición en el análisis",
                list(analysis_options),
                key=f"{view_key}_snapshot_analysis_position",
            )
            analysis_cols[1].button(
                "Analizar posición",
                type="primary",
                key=f"{view_key}_open_snapshot_analysis",
                on_click=_open_ticker_analysis,
                args=(analysis_options[selected_snapshot_position],),
            )

    if not include_alternative_investments:
        return investments, accounts

    st.divider()
    st.subheader("Proyectos de Civislend y Segofactoring")
    st.caption(
        "Estas inversiones no cotizan en bolsa. Su valor no se descarga: debes "
        "actualizarlo según los datos de cada plataforma."
    )
    with st.expander("Importar o actualizar el Excel de Segofactoring"):
        st.caption(
            "Puedes volver a subir el resumen cuando cambie. La app actualiza las "
            "operaciones ya importadas, conserva participaciones repetidas y no borra "
            "proyectos añadidos manualmente."
        )
        segofactoring_file = st.file_uploader(
            "Resumen de operaciones (.xlsx)",
            type=["xlsx"],
            key=f"{view_key}_segofactoring_excel",
        )
        if segofactoring_file is not None:
            try:
                segofactoring_rows = parse_segofactoring_excel(
                    segofactoring_file.getvalue()
                )
            except (ValueError, ImportError) as exc:
                st.error(str(exc))
            else:
                active_rows = segofactoring_rows.loc[
                    segofactoring_rows["status"] != "Finalizada"
                ]
                completed_rows = segofactoring_rows.loc[
                    segofactoring_rows["status"] == "Finalizada"
                ]
                identity_columns = [
                    "project_name", "start_date", "maturity_date", "invested_amount"
                ]
                duplicate_groups = int(
                    (
                        segofactoring_rows.groupby(identity_columns, dropna=False).size()
                        > 1
                    ).sum()
                )
                import_cols = st.columns(4)
                import_cols[0].metric("Operaciones", len(segofactoring_rows))
                import_cols[1].metric(
                    "Capital pendiente",
                    f"{active_rows['current_value'].sum():,.2f} €",
                )
                import_cols[2].metric("Ya cobradas", len(completed_rows))
                import_cols[3].metric(
                    "Ganancia neta registrada",
                    f"{segofactoring_rows['net_profit'].sum():,.2f} €",
                )
                if duplicate_groups:
                    st.info(
                        f"Hay {duplicate_groups} referencias repetidas. Se conservarán "
                        "como participaciones independientes, igual que en el Excel."
                    )
                st.dataframe(
                    segofactoring_rows.rename(
                        columns={
                            "project_name": "Operación",
                            "source_status": "Estado original",
                            "start_date": "Inversión",
                            "maturity_date": "Vencimiento",
                            "invested_amount": "Invertido €",
                            "net_profit": "Ganancia neta €",
                        }
                    ).loc[
                        :,
                        [
                            "Operación", "Estado original", "Inversión", "Vencimiento",
                            "Invertido €", "Ganancia neta €",
                        ],
                    ],
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Invertido €": st.column_config.NumberColumn(format="%.2f €"),
                        "Ganancia neta €": st.column_config.NumberColumn(format="%+.2f €"),
                    },
                )
                file_is_current = st.checkbox(
                    "Este archivo está actualizado a hoy",
                    value=False,
                    key=f"{view_key}_segofactoring_current",
                    help=(
                        "Déjalo desmarcado para un piloto antiguo. La cuenta seguirá "
                        "indicando que necesita revisión."
                    ),
                )
                if st.button(
                    "Importar o actualizar estas operaciones",
                    type="primary",
                    key=f"{view_key}_import_segofactoring",
                ):
                    try:
                        import_result = import_segofactoring_rows(
                            journal,
                            segofactoring_rows,
                            recorded_by=actor_username,
                        )
                        existing_sego = (
                            accounts.loc[accounts["account_name"] == "Segofactoring"]
                            if not accounts.empty and "account_name" in accounts.columns
                            else pd.DataFrame()
                        )
                        cash_balance = (
                            float(existing_sego.iloc[0]["cash_balance"])
                            if not existing_sego.empty
                            else 0.0
                        )
                        journal.upsert_portfolio_account(
                            account_name="Segofactoring",
                            account_type="Inversión alternativa",
                            investments_value=float(active_rows["current_value"].sum()),
                            cash_balance=cash_balance,
                            currency="EUR",
                            status=(
                                "Actualizada"
                                if file_is_current
                                else "Pendiente de actualizar"
                            ),
                            notes=(
                                f"Excel importado: {len(segofactoring_rows)} operaciones; "
                                f"{len(active_rows)} pendientes y {len(completed_rows)} cobradas."
                            ),
                        )
                    except (ValueError, JournalStorageError) as exc:
                        st.error(str(exc))
                    else:
                        st.success(
                            f"Importación terminada: {import_result.created} nuevas y "
                            f"{import_result.updated} actualizadas."
                        )
                        st.rerun()
    if investments.empty:
        st.info("Todavía no has registrado proyectos de estas plataformas.")
    else:
        open_investments = investments.loc[investments["status"] != "Finalizada"].copy()
        invested = float(
            pd.to_numeric(open_investments["invested_amount"], errors="coerce").sum()
        )
        current = float(
            pd.to_numeric(open_investments["current_value"], errors="coerce").sum()
        )
        active = int(open_investments["status"].isin(["Activa", "Retrasada"]).sum())
        completed = int((investments["status"] == "Finalizada").sum())
        expected_values = pd.to_numeric(
            open_investments["expected_return_pct"], errors="coerce"
        )
        forecast_mask = expected_values.notna() & (expected_values != 0)
        forecast_investments = open_investments.loc[forecast_mask]
        forecast_invested = float(
            pd.to_numeric(
                forecast_investments["invested_amount"], errors="coerce"
            ).sum()
        )
        weighted_return = (
            float(
                (
                    pd.to_numeric(
                        forecast_investments["invested_amount"], errors="coerce"
                    )
                    * pd.to_numeric(
                        forecast_investments["expected_return_pct"], errors="coerce"
                    )
                ).sum()
                / forecast_invested
            )
            if forecast_invested > 0
            else None
        )
        metric_cols = st.columns(4)
        metric_cols[0].metric("Capital abierto", f"{invested:,.2f} €")
        metric_cols[1].metric("Valor pendiente actual", f"{current:,.2f} €")
        metric_cols[2].metric("Diferencia abierta", f"{current - invested:+,.2f} €")
        metric_cols[3].metric(
            "Rentabilidad esperada media",
            f"{weighted_return:.2f}%" if weighted_return is not None else "N/D",
            help=(
                "Media ponderada sólo cuando el proyecto aporta una previsión. "
                "El resumen de Segofactoring no incluye ese dato."
            ),
        )
        st.caption(
            f"{active} proyectos activos o retrasados y {completed} finalizados. "
            "Los finalizados siguen en el histórico, pero no se cuentan como capital abierto."
        )
        st.plotly_chart(
            private_investments_chart(investments),
            width="stretch",
            config=PLOTLY_CONFIG,
        )
        display = investments.rename(
            columns={
                "id": "ID",
                "platform": "Plataforma",
                "project_name": "Proyecto",
                "invested_amount": "Invertido €",
                "current_value": "Valor actual €",
                "expected_return_pct": "Rentabilidad esperada %",
                "start_date": "Inicio",
                "maturity_date": "Vencimiento",
                "status": "Estado",
                "notes": "Notas",
            }
        )
        st.dataframe(
            display.loc[
                :,
                [
                    "ID", "Plataforma", "Proyecto", "Invertido €", "Valor actual €",
                    "Rentabilidad esperada %", "Inicio", "Vencimiento", "Estado", "Notas",
                ],
            ],
            width="stretch",
            hide_index=True,
            column_config={
                "Invertido €": st.column_config.NumberColumn(format="%.2f €"),
                "Valor actual €": st.column_config.NumberColumn(format="%.2f €"),
                "Rentabilidad esperada %": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )

    add_tab, update_tab = st.tabs(["Añadir proyecto", "Actualizar o eliminar"])
    with add_tab:
        with st.form(f"{view_key}_private_investment_form", clear_on_submit=True):
            platform = st.selectbox("Plataforma", PRIVATE_INVESTMENT_PLATFORMS)
            project_name = st.text_input(
                "Proyecto o referencia",
                placeholder="Ej.: Préstamo promoción Madrid 2026",
            )
            amount_cols = st.columns(3)
            invested_amount = amount_cols[0].number_input(
                "Importe invertido (€)", min_value=0.01, value=1_000.0, step=100.0
            )
            current_value = amount_cols[1].number_input(
                "Valor actual (€)",
                min_value=0.0,
                value=1_000.0,
                step=100.0,
                help="Introduce el capital pendiente más intereses ya cobrados, según tu criterio.",
            )
            expected_return = amount_cols[2].number_input(
                "Rentabilidad esperada (%)",
                min_value=-100.0,
                max_value=1_000.0,
                value=8.0,
                step=0.25,
            )
            date_cols = st.columns(2)
            start_date = date_cols[0].date_input("Fecha de inversión", value=date.today())
            maturity_date = date_cols[1].date_input(
                "Vencimiento previsto", value=date.today() + timedelta(days=365)
            )
            status = st.selectbox("Estado", PRIVATE_INVESTMENT_STATUSES)
            notes = st.text_area("Notas y riesgos")
            submitted = st.form_submit_button("Guardar proyecto", type="primary")
        if submitted:
            try:
                journal.add_private_investment(
                    platform=platform,
                    project_name=project_name,
                    invested_amount=float(invested_amount),
                    current_value=float(current_value),
                    expected_return_pct=float(expected_return),
                    start_date=start_date,
                    maturity_date=maturity_date,
                    status=status,
                    notes=notes,
                    recorded_by=actor_username,
                )
            except (ValueError, JournalStorageError) as exc:
                st.error(str(exc))
            else:
                st.success("Proyecto guardado.")
                st.rerun()

    with update_tab:
        if investments.empty:
            st.caption("Añade un proyecto para poder actualizarlo.")
        else:
            labels = {
                int(row.id): f"{row.platform} · {row.project_name}"
                for row in investments.itertuples(index=False)
            }
            selected_id = st.selectbox(
                "Proyecto",
                list(labels),
                format_func=lambda value: labels[int(value)],
                key=f"{view_key}_private_investment_id",
            )
            selected = investments.loc[investments["id"] == selected_id].iloc[0]
            with st.form(f"{view_key}_update_private_investment"):
                updated_value = st.number_input(
                    "Valor actual (€)",
                    min_value=0.0,
                    value=float(selected["current_value"]),
                    step=50.0,
                )
                updated_status = st.selectbox(
                    "Estado",
                    PRIVATE_INVESTMENT_STATUSES,
                    index=PRIVATE_INVESTMENT_STATUSES.index(str(selected["status"])),
                )
                updated_notes = st.text_area("Notas", value=str(selected["notes"] or ""))
                update_submitted = st.form_submit_button("Actualizar", type="primary")
            if update_submitted:
                try:
                    journal.update_private_investment(
                        int(selected_id),
                        current_value=float(updated_value),
                        status=updated_status,
                        notes=updated_notes,
                    )
                except (ValueError, JournalStorageError) as exc:
                    st.error(str(exc))
                else:
                    st.success("Proyecto actualizado.")
                    st.rerun()
            if st.button(
                "Eliminar proyecto seleccionado",
                key=f"{view_key}_delete_private_investment",
            ):
                journal.delete_private_investment(int(selected_id))
                st.rerun()
    return investments, accounts


def render_portfolio_evolution(
    *,
    operations: pd.DataFrame,
    positions_dashboard: pd.DataFrame,
    prepared: dict[str, pd.DataFrame],
    fx_snapshot: FxSnapshot,
    private_investments: pd.DataFrame,
    portfolio_accounts: pd.DataFrame,
    view_key: str,
) -> None:
    """Muestra evolución diaria/anual y prepara un Excel completo."""

    st.subheader("Evolución y resumen por años")
    st.caption(
        "Las compras añaden capital y las ventas lo retiran. Así puedes ver por separado "
        "el dinero aportado, el valor de lo que mantienes y el resultado acumulado."
    )
    if operations.empty:
        st.info("Registra al menos una compra para crear el seguimiento histórico.")
        return
    result = build_portfolio_history(
        operations,
        prepared,
        fx_snapshot.rates_per_eur,
    )
    if result.missing_tickers:
        st.warning(
            "Para completar la gráfica faltan precios de: "
            + ", ".join(result.missing_tickers)
            + ". Añádelas al radar desde Analizar y actualiza los datos."
        )
    if result.missing_currencies:
        st.warning(
            "No se pudieron convertir a euros estas monedas: "
            + ", ".join(result.missing_currencies)
            + "."
        )
    if not result.daily.empty:
        st.plotly_chart(
            portfolio_evolution_chart(result.daily),
            width="stretch",
            config=PLOTLY_CONFIG,
        )
    if not result.annual.empty:
        st.plotly_chart(
            annual_portfolio_chart(result.annual),
            width="stretch",
            config=PLOTLY_CONFIG,
        )
        st.dataframe(
            result.annual,
            width="stretch",
            hide_index=True,
            column_config={
                "Compras EUR": st.column_config.NumberColumn(format="%.2f €"),
                "Ventas EUR": st.column_config.NumberColumn(format="%.2f €"),
                "Aportación neta EUR": st.column_config.NumberColumn(format="%+.2f €"),
                "Comisiones EUR": st.column_config.NumberColumn(format="%.2f €"),
                "Resultado realizado EUR": st.column_config.NumberColumn(format="%+.2f €"),
                "Valor al cierre EUR": st.column_config.NumberColumn(format="%.2f €"),
                "Resultado acumulado EUR": st.column_config.NumberColumn(format="%+.2f €"),
                "Resultado acumulado %": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )
    st.caption(
        "Estimación de seguimiento: emplea cierres de mercado ajustados y el tipo de cambio "
        "actual del BCE para todos los años. No sustituye el extracto del bróker ni el cálculo fiscal."
    )
    try:
        workbook = build_portfolio_excel(
            operations=operation_history_for_display(operations),
            positions=positions_dashboard,
            annual=result.annual,
            daily=result.daily,
            private_investments=private_investments,
            portfolio_accounts=portfolio_accounts,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        st.error(f"No se pudo preparar Excel: {exc}")
    else:
        st.download_button(
            "Descargar cartera completa en Excel",
            workbook,
            file_name=f"cartera_{view_key}_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{view_key}_download_excel",
            type="primary",
        )


def render_journal(
    prepared: dict[str, pd.DataFrame],
    fundamental_results: dict[str, FundamentalResult],
    opportunity_results: dict[str, OpportunityResult],
    strategy: StrategyConfig,
    fx_snapshot: FxSnapshot,
    journal: object,
    *,
    view_key: str,
    title: str,
    description: str,
    actor_username: str,
    shared: bool = False,
    can_delete_all: bool = False,
) -> None:
    header_a, header_b = st.columns([1.7, 1])
    header_a.subheader(title)
    header_a.caption(description)
    header_b.button(
        "Actualizar precios",
        icon=":material/refresh:",
        width="stretch",
        key=f"{view_key}_refresh_portfolio_prices",
        on_click=_request_portfolio_market_refresh,
    )
    market_dates = [
        pd.Timestamp(frame.index[-1]).date().isoformat()
        for frame in prepared.values()
        if not frame.empty
    ]
    st.caption(
        f"Precios hasta {max(market_dates) if market_dates else 'pendientes'} · "
        f"Almacenamiento: {getattr(journal, 'backend_name', 'diario')}"
    )
    flash_key = f"_{view_key}_journal_flash"
    flash_message = st.session_state.pop(flash_key, None)
    if flash_message:
        st.success(str(flash_message))
    fixed_fee = st.number_input(
        "Comisión fija por cada compra o venta",
        min_value=0.0,
        value=1.0,
        step=0.5,
        format="%.2f",
        help=(
            "Se interpreta en euros. Cuando hay tipos del BCE, la app lo convierte "
            "a la moneda de la acción."
        ),
        key=f"{view_key}_fixed_fee",
    )
    current_portfolio_enabled = not shared
    alternative_investments_enabled = (
        current_portfolio_enabled and actor_username.strip().lower() == "ddriu"
    )
    tab_labels = [
        "Posiciones analizadas",
        "Comprar / vender",
        "Evolución por años",
        "Comparar un cambio",
        "Historial",
    ]
    if current_portfolio_enabled:
        tab_labels.append("Mi cartera actual")
    journal_tabs = st.tabs(tab_labels)
    positions_tab, register_tab, evolution_tab, switch_tab, history_tab = journal_tabs[:5]
    private_tab = journal_tabs[5] if current_portfolio_enabled else None

    with register_tab:
        render_operation_form(
            journal,
            form_key=f"{view_key}_operation_form",
            fixed_fee=float(fixed_fee),
            owner_label="la cartera del grupo" if shared else None,
            flash_key=flash_key,
            recorded_by=actor_username,
            notes_label="Motivo o acuerdo del grupo" if shared else "Notas",
        )

    private_investments = pd.DataFrame()
    portfolio_accounts = pd.DataFrame()
    if private_tab is not None:
        with private_tab:
            private_investments, portfolio_accounts = render_private_investments(
                journal,
                actor_username=actor_username,
                view_key=view_key,
                include_alternative_investments=alternative_investments_enabled,
            )

    with history_tab:
        st.subheader("Histórico")
        operations = journal.list_operations()
        if operations.empty:
            st.info("El diario todavía está vacío.")
        else:
            if shared:
                contributors = sorted(
                    value
                    for value in operations["recorded_by"].fillna("").astype(str).unique()
                    if value
                )
                if contributors:
                    st.caption(
                        "Movimientos registrados por: " + ", ".join(contributors) + "."
                    )
            st.dataframe(
                operation_history_for_display(operations),
                width="stretch",
                hide_index=True,
            )
            st.download_button(
                "Exportar diario a CSV",
                operations.to_csv(index=False).encode("utf-8"),
                file_name=f"diario_{view_key}.csv",
                mime="text/csv",
                key=f"{view_key}_download_history",
            )
            deletable = operations
            if shared and not can_delete_all:
                recorded_by = operations["recorded_by"].fillna("").astype(str).str.lower()
                deletable = operations.loc[recorded_by == actor_username.lower()]
            if deletable.empty:
                if shared:
                    st.caption(
                        "Puedes eliminar únicamente los movimientos que registraste tú."
                    )
            else:
                operation_id = st.selectbox(
                    "ID para eliminar",
                    deletable["id"].tolist(),
                    key=f"{view_key}_delete_operation_id",
                )
                if st.button(
                    "Eliminar operación seleccionada",
                    key=f"{view_key}_delete_operation",
                ):
                    journal.delete_operation(int(operation_id))
                    st.rerun()

    positions = journal.open_positions()
    latest_prices = {
        ticker: float(frame["close"].iloc[-1])
        for ticker, frame in prepared.items()
        if not frame.empty
    }
    positions_dashboard, portfolio_kpis = build_position_dashboard(
        operations,
        positions,
        latest_prices,
        fx_snapshot.rates_per_eur,
        sell_fee_eur=float(fixed_fee),
    )
    with evolution_tab:
        render_portfolio_evolution(
            operations=operations,
            positions_dashboard=positions_dashboard,
            prepared=prepared,
            fx_snapshot=fx_snapshot,
            private_investments=private_investments,
            portfolio_accounts=portfolio_accounts,
            view_key=view_key,
        )
    analysis_rows: list[dict[str, object]] = []
    details: dict[str, dict[str, object]] = {}
    for position in positions.itertuples(index=False):
        ticker = str(position.ticker)
        key = f"{ticker} · {position.currency}"
        if ticker not in prepared:
            analysis_rows.append(
                {
                    "Ticker": ticker,
                    "Moneda": position.currency,
                    "Cantidad": position.quantity,
                    "Coste medio": position.average_cost,
                    "Lectura": "Faltan precios",
                }
            )
            continue
        frame = prepared[ticker]
        latest_price = float(frame["close"].iloc[-1])
        signal = evaluate_latest_signal(
            frame,
            strategy,
            ticker=ticker,
            entry_price=float(position.average_cost),
        )
        fundamentals = fundamental_results[ticker]
        opportunity = opportunity_results[ticker]
        quote_currency = fundamentals.currency
        comparable = quote_currency is None or quote_currency == position.currency
        try:
            sell_fee_in_position_currency = convert_currency(
                float(fixed_fee),
                "EUR",
                str(position.currency),
                fx_snapshot.rates_per_eur,
            )
        except ValueError:
            sell_fee_in_position_currency = float(fixed_fee)
        valuation = value_holding(
            quantity=float(position.quantity),
            average_cost=float(position.average_cost),
            cost_basis=float(position.cost_basis),
            current_price=latest_price,
            sell_fee=sell_fee_in_position_currency,
        )
        analysis_rows.append(
            {
                "Ticker": ticker,
                "Moneda": position.currency,
                "Cantidad": float(position.quantity),
                "Coste medio": float(position.average_cost),
                "Precio actual": latest_price,
                "Beneficio neto": valuation.net_pnl if comparable else float("nan"),
                "Rentabilidad neta": (
                    valuation.net_return_pct if comparable else float("nan")
                ),
                "Beneficio ya realizado": float(position.realized_pnl),
                "Comisiones pagadas": float(position.paid_fees),
                "Empresa": (
                    float(fundamentals.score)
                    if fundamentals.score is not None
                    else float("nan")
                ),
                "Entrada": signal.score,
                "Oportunidad": opportunity.score,
                "Lectura": signal.position_label,
            }
        )
        details[key] = {
            "position": position,
            "signal": signal,
            "fundamentals": fundamentals,
            "opportunity": opportunity,
            "frame": frame,
            "price": latest_price,
            "valuation": valuation,
            "comparable": comparable,
            "sell_fee": sell_fee_in_position_currency,
        }

    with positions_tab:
        if positions.empty:
            st.info("Registra una compra para empezar a analizar tu cartera.")
        else:
            portfolio_cols = st.columns(5)
            portfolio_cols[0].metric(
                "Capital pendiente",
                f"{portfolio_kpis.invested_eur:,.2f} EUR",
                help="Coste de las posiciones abiertas, incluidas las compras y sus comisiones.",
            )
            portfolio_cols[1].metric(
                "Valor neto actual",
                f"{portfolio_kpis.current_net_value_eur:,.2f} EUR",
                help="Valor de las posiciones con precio disponible, descontando una comisión de salida.",
            )
            portfolio_cols[2].metric(
                "Resultado latente",
                f"{portfolio_kpis.unrealized_pnl_eur:+,.2f} EUR",
                delta=f"{portfolio_kpis.unrealized_return_pct:+.2f}%",
                help="Beneficio o pérdida si se cerrasen ahora las posiciones valoradas.",
            )
            portfolio_cols[3].metric(
                "Resultado realizado",
                f"{portfolio_kpis.realized_pnl_eur:+,.2f} EUR",
                help="Resultado acumulado de las ventas registradas.",
            )
            portfolio_cols[4].metric(
                "Comisiones pagadas",
                f"{portfolio_kpis.fees_eur:,.2f} EUR",
            )
            st.caption(
                f"{portfolio_kpis.priced_positions_count} de "
                f"{portfolio_kpis.open_positions_count} posiciones tienen precio actualizado. "
                "Los totales convierten monedas con el último tipo del BCE disponible."
            )
            if analysis_rows:
                st.caption("Pulsa una posición para abrir su análisis completo.")
                render_ticker_dataframe(
                    pd.DataFrame(analysis_rows),
                    key=f"{view_key}_open_positions_analysis",
                    column_config={
                        "Coste medio": st.column_config.NumberColumn(format="%.2f"),
                        "Precio actual": st.column_config.NumberColumn(format="%.2f"),
                        "Beneficio neto": st.column_config.NumberColumn(format="%+.2f"),
                        "Rentabilidad neta": st.column_config.NumberColumn(format="%+.2f%%"),
                        "Beneficio ya realizado": st.column_config.NumberColumn(format="%+.2f"),
                        "Comisiones pagadas": st.column_config.NumberColumn(format="%.2f"),
                        "Empresa": st.column_config.ProgressColumn(
                            min_value=0, max_value=100, format="%d"
                        ),
                        "Entrada": st.column_config.ProgressColumn(
                            min_value=0, max_value=100, format="%d"
                        ),
                        "Oportunidad": st.column_config.ProgressColumn(
                            min_value=0, max_value=100, format="%d"
                        ),
                    },
                )
            missing = [
                str(row.ticker)
                for row in positions.itertuples(index=False)
                if str(row.ticker) not in prepared
            ]
            if missing:
                st.warning(
                    "Faltan precios para: "
                    + ", ".join(missing)
                    + ". Abre «Analizar» y pulsa «Actualizar análisis»; las posiciones "
                    "guardadas se incluyen automáticamente."
                )
            if details:
                selected_position = st.selectbox(
                    "Ver diagnóstico de una posición",
                    list(details),
                    key=f"{view_key}_holding_detail",
                )
                detail = details[selected_position]
                position = detail["position"]
                signal = detail["signal"]
                valuation = detail["valuation"]
                decision_text = {
                    "Mantener": "La tendencia no activa una salida. Mantener bajo vigilancia.",
                    "Reducir": "La tendencia se debilita. Conviene revisar o reducir el riesgo.",
                    "Vender": "La tendencia o el stop están dañados. Salida a estudiar.",
                }.get(signal.position_label, "Revisar la posición.")
                if signal.position_label == "Mantener":
                    st.success(f"**{signal.position_label}:** {decision_text}")
                else:
                    st.warning(f"**{signal.position_label}:** {decision_text}")
                detail_cols = st.columns(4)
                detail_cols[0].metric("Coste total pendiente", f"{position.cost_basis:,.2f}")
                detail_cols[1].metric("Valor neto si vendes", f"{valuation.net_exit_value:,.2f}")
                detail_cols[2].metric("Punto de equilibrio", f"{valuation.break_even_price:,.2f}")
                detail_cols[3].metric("Comisiones ya pagadas", f"{position.paid_fees:,.2f}")
                if not detail["comparable"]:
                    st.error(
                        "La moneda guardada no coincide con la moneda de cotización. "
                        "No se calcula beneficio ni plan de ventas hasta corregirla."
                    )
                    st.stop()
                profit_plan = build_profit_taking_plan(
                    quantity=float(position.quantity),
                    average_cost=float(position.average_cost),
                    current_price=float(detail["price"]),
                    stop_loss_pct=strategy.stop_loss_pct,
                    fee_per_sale=float(detail["sell_fee"]),
                )
                st.subheader("Plan probabilístico de toma de beneficios")
                if profit_plan.suggested_sell_now_quantity > 0:
                    st.success(
                        f"El precio ya ha alcanzado niveles de beneficio. La guía propone "
                        f"estudiar la venta de {profit_plan.suggested_sell_now_quantity:,.3f} "
                        f"unidades ({profit_plan.suggested_sell_now_pct:.0f}%) y mantener "
                        f"{profit_plan.trailing_quantity:,.3f} con protección dinámica."
                    )
                    sale_cols = st.columns(2)
                    sale_cols[0].metric(
                        "Beneficio neto de esa venta al precio actual",
                        f"{profit_plan.net_profit_if_sold_now:+,.2f}",
                    )
                    sale_cols[1].metric(
                        "Comisión descontada",
                        f"{float(detail['sell_fee']):,.2f} {position.currency}",
                    )
                else:
                    st.info(
                        "Todavía no se ha alcanzado el primer nivel de beneficio. "
                        "La guía no propone una venta parcial por objetivo."
                    )
                level_rows = [
                    {
                        "Nivel": level.name,
                        "Precio orientativo": level.target_price,
                        "Cantidad del tramo": level.quantity,
                        "Beneficio neto del tramo": level.net_profit_vs_cost,
                        "Estado": "Alcanzado" if level.reached else "Pendiente",
                    }
                    for level in profit_plan.levels
                ]
                st.dataframe(
                    pd.DataFrame(level_rows),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Precio orientativo": st.column_config.NumberColumn(format="%.2f"),
                        "Cantidad del tramo": st.column_config.NumberColumn(format="%.3f"),
                        "Beneficio neto del tramo": st.column_config.NumberColumn(format="%+.2f"),
                    },
                )
                holding_study = historical_forward_return_study(
                    detail["frame"],
                    current_score=signal.score,
                    horizon_days=strategy.forward_horizon_days,
                )
                if holding_study.reliable:
                    expected_price = float(detail["price"]) * (
                        1 + float(holding_study.median_return_pct) / 100
                    )
                    expected_incremental_profit = (
                        float(position.quantity)
                        * (expected_price - float(detail["price"]))
                        - float(detail["sell_fee"])
                    )
                    estimate_cols = st.columns(3)
                    estimate_cols[0].metric(
                        f"Estimación mediana a {holding_study.horizon_days} sesiones",
                        format_pct(float(holding_study.median_return_pct)),
                    )
                    estimate_cols[1].metric(
                        "Precio estadístico orientativo", f"{expected_price:,.2f}"
                    )
                    estimate_cols[2].metric(
                        "Beneficio incremental estimado",
                        f"{expected_incremental_profit:+,.2f}",
                    )
                    st.caption(
                        f"Basado en {holding_study.samples} casos históricos no solapados. "
                        "Es una estimación retrospectiva, no un beneficio prometido."
                    )
                else:
                    st.caption(
                        "No hay suficientes casos comparables para estimar un beneficio esperado."
                    )
    with switch_tab:
        if not details:
            st.info("Registra y actualiza al menos una posición para comparar cambios.")
        else:
            current_key = st.selectbox(
                "Acción que venderías",
                list(details),
                key=f"{view_key}_switch_current",
            )
            current = details[current_key]
            current_ticker = str(current["position"].ticker)
            alternatives = [ticker for ticker in prepared if ticker != current_ticker]
            if not alternatives:
                st.info("Añade otras empresas al radar para compararlas con tu posición.")
            else:
                candidate_ticker = st.selectbox(
                    "Acción que comprarías",
                    alternatives,
                    key=f"{view_key}_switch_candidate",
                )
                candidate_frame = prepared[candidate_ticker]
                candidate_price = float(candidate_frame["close"].iloc[-1])
                candidate_signal = evaluate_latest_signal(
                    candidate_frame, strategy, ticker=candidate_ticker
                )
                candidate_fundamentals = fundamental_results[candidate_ticker]
                current_currency = str(current["position"].currency)
                candidate_currency = candidate_fundamentals.currency
                if not current["comparable"]:
                    st.error(
                        "La moneda guardada para la posición actual no coincide con su "
                        "moneda de cotización. Corrígela antes de comparar el cambio."
                    )
                elif candidate_currency is None:
                    st.warning(
                        "No se conoce la moneda de la alternativa. Actualiza los datos fundamentales."
                    )
                else:
                    comparison = compare_switch(
                        quantity=float(current["position"].quantity),
                        current_price=float(current["price"]),
                        candidate_price=candidate_price,
                        current_currency=current_currency,
                        candidate_currency=candidate_currency,
                        sell_fee=float(fixed_fee),
                        buy_fee=float(fixed_fee),
                        fx_rates_per_eur=fx_snapshot.rates_per_eur,
                        fee_currency="EUR",
                        fx_as_of=(
                            fx_snapshot.as_of.isoformat()
                            if fx_snapshot.as_of
                            else None
                        ),
                    )
                    if not comparison.compatible_currency:
                        st.error(
                            f"No se calcula el cambio: {current_ticker} está registrado en "
                            f"{current_currency} y {candidate_ticker} cotiza en {candidate_currency}. "
                            "El BCE no ofrece el tipo de cambio necesario."
                        )
                    else:
                        switch_cols = st.columns(5)
                        switch_cols[0].metric("Comisión de venta", f"{fixed_fee:.2f} EUR")
                        switch_cols[1].metric("Comisión de compra", f"{fixed_fee:.2f} EUR")
                        switch_cols[2].metric(
                            "Dinero reinvertido",
                            f"{comparison.cash_invested:,.2f} {candidate_currency}",
                        )
                        switch_cols[3].metric(
                            f"Unidades de {candidate_ticker}",
                            f"{comparison.candidate_quantity:,.4f}",
                        )
                        switch_cols[4].metric(
                            "Ventaja mínima para cubrir costes",
                            f"{comparison.fee_hurdle_pct:.3f}%",
                        )
                        technical_gain = candidate_signal.score - current["signal"].score
                        opportunity_gain = (
                            opportunity_results[candidate_ticker].score
                            - current["opportunity"].score
                        )
                        current_quality = current["fundamentals"].score
                        candidate_quality = candidate_fundamentals.score
                        quality_ok = (
                            current_quality is None
                            or candidate_quality is None
                            or candidate_quality >= current_quality - 5
                        )
                        candidate_is_entry = candidate_signal.label in {
                            "Entrada fuerte",
                            "Entrada interesante",
                        }
                        current_is_weak = current["signal"].position_label in {
                            "Reducir",
                            "Vender",
                        }
                        change_worth_studying = (
                            current_is_weak
                            and candidate_is_entry
                            and technical_gain >= 10
                            and opportunity_gain >= 5
                            and quality_ok
                        )
                        if change_worth_studying:
                            st.success(
                                f"**Cambio para estudiar:** {candidate_ticker} mejora "
                                f"{technical_gain:+d} puntos el momento técnico y "
                                f"{opportunity_gain:+d} puntos la oportunidad conjunta, sin una "
                                "calidad claramente inferior con los datos disponibles."
                            )
                        else:
                            st.info(
                                "**No aparece una ventaja suficientemente clara para cambiar.** "
                                "La aplicación exige deterioro de la posición actual, una entrada "
                                "atractiva, 10 puntos de mejora técnica y 5 de oportunidad conjunta."
                            )
                        if comparison.conversion_rate not in (None, 1.0):
                            st.caption(
                                f"Conversión aplicada: 1 {current_currency} = "
                                f"{comparison.conversion_rate:.6f} {candidate_currency} "
                                f"(BCE {comparison.fx_as_of or 'sin fecha disponible'})."
                            )
                        st.caption(
                            "El cálculo descuenta 1 comisión al vender y 1 al comprar. No incluye "
                            "impuestos, spread, coste adicional de cambio del broker ni la futura "
                            "venta de la nueva acción."
                        )


def render_admin_panel(
    accounts: dict[str, AuthConfig],
    prepared: dict[str, pd.DataFrame],
    fx_snapshot: FxSnapshot,
    admin_username: str,
) -> None:
    """Vista agregada y mantenimiento de las carteras de los usuarios."""

    st.subheader("Administración de carteras")
    st.caption(
        "Vista privada del administrador. Cada usuario sólo puede consultar y modificar "
        "su propia cartera."
    )
    flash_message = st.session_state.pop("_admin_flash", None)
    if flash_message:
        st.success(str(flash_message))

    usernames = managed_usernames(accounts)
    if not usernames:
        st.warning("No hay cuentas de usuario configuradas.")
        return

    fixed_fee = st.number_input(
        "Comisión orientativa para valoraciones administrativas",
        min_value=0.0,
        value=1.0,
        step=0.5,
        format="%.2f",
        key="admin_fixed_fee",
    )
    latest_prices = {
        ticker: float(frame["close"].iloc[-1])
        for ticker, frame in prepared.items()
        if not frame.empty
    }
    snapshots: dict[str, dict[str, object]] = {}
    summary_rows: list[dict[str, object]] = []

    for username in usernames:
        journal = create_journal(username)
        operations = journal.list_operations()
        positions = calculate_open_positions(operations)
        portfolio_snapshot = pd.DataFrame()
        portfolio_snapshot_summary = None
        if hasattr(journal, "list_portfolio_snapshot_positions"):
            stored_portfolio_snapshots = journal.list_portfolio_snapshot_positions()
            portfolio_snapshot, portfolio_snapshot_summary = latest_portfolio_snapshot(
                stored_portfolio_snapshots
            )
        dashboard, kpis = build_position_dashboard(
            operations,
            positions,
            latest_prices,
            fx_snapshot.rates_per_eur,
            sell_fee_eur=float(fixed_fee),
        )
        snapshots[username] = {
            "journal": journal,
            "operations": operations,
            "positions": positions,
            "dashboard": dashboard,
            "kpis": kpis,
            "portfolio_snapshot": portfolio_snapshot,
            "portfolio_snapshot_summary": portfolio_snapshot_summary,
        }
        summary_rows.append(
            {
                "Usuario": username,
                "Nombre": accounts[username].display_name,
                "Operaciones": kpis.operations_count,
                "Posiciones": kpis.open_positions_count,
                "Con precio": (
                    f"{kpis.priced_positions_count}/{kpis.open_positions_count}"
                ),
                "Capital pendiente EUR": kpis.invested_eur,
                "Valor neto EUR": kpis.current_net_value_eur,
                "Resultado latente EUR": kpis.unrealized_pnl_eur,
                "Rentabilidad latente": kpis.unrealized_return_pct,
                "Resultado realizado EUR": kpis.realized_pnl_eur,
                "Comisiones EUR": kpis.fees_eur,
                "Fotografía": (
                    portfolio_snapshot_summary.snapshot_date
                    if portfolio_snapshot_summary is not None
                    else "Sin fotografía"
                ),
                "Valor fotografía EUR": (
                    portfolio_snapshot_summary.value_eur
                    if portfolio_snapshot_summary is not None
                    else None
                ),
                "Última actividad": kpis.latest_activity or "Sin operaciones",
            }
        )

    active_users = sum(
        1
        for snapshot in snapshots.values()
        if len(snapshot["operations"]) > 0
        or snapshot["portfolio_snapshot_summary"] is not None
    )
    total_operations = sum(
        int(snapshot["kpis"].operations_count) for snapshot in snapshots.values()
    )
    total_positions = sum(
        int(snapshot["kpis"].open_positions_count) for snapshot in snapshots.values()
    )
    total_invested = sum(
        float(snapshot["kpis"].invested_eur) for snapshot in snapshots.values()
    )
    total_unrealized = sum(
        float(snapshot["kpis"].unrealized_pnl_eur) for snapshot in snapshots.values()
    )
    admin_cols = st.columns(5)
    admin_cols[0].metric("Usuarios", len(usernames))
    admin_cols[1].metric("Con actividad", active_users)
    admin_cols[2].metric("Operaciones", total_operations)
    admin_cols[3].metric("Posiciones abiertas", total_positions)
    admin_cols[4].metric(
        "Resultado latente conjunto",
        f"{total_unrealized:+,.2f} EUR",
        help=f"Sobre {total_invested:,.2f} EUR de capital pendiente convertible.",
    )

    overview_tab, register_tab, import_tab, detail_tab = st.tabs(
        [
            "Resumen de usuarios",
            "Añadir posición",
            "Importar fotografía",
            "Detalle e historial",
        ]
    )
    with overview_tab:
        st.dataframe(
            pd.DataFrame(summary_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Capital pendiente EUR": st.column_config.NumberColumn(format="%.2f"),
                "Valor neto EUR": st.column_config.NumberColumn(format="%.2f"),
                "Resultado latente EUR": st.column_config.NumberColumn(format="%+.2f"),
                "Rentabilidad latente": st.column_config.NumberColumn(format="%+.2f%%"),
                "Resultado realizado EUR": st.column_config.NumberColumn(format="%+.2f"),
                "Comisiones EUR": st.column_config.NumberColumn(format="%.2f"),
                "Valor fotografía EUR": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        if total_positions and not latest_prices:
            st.info(
                "Abre «Analizar» y pulsa «Actualizar análisis» para valorar las "
                "posiciones con precios actuales y calcular sus rendimientos."
            )

    with register_tab:
        selected_owner = st.selectbox(
            "Usuario al que pertenece la operación",
            usernames,
            format_func=lambda value: (
                f"{accounts[value].display_name} ({value})"
            ),
            key="admin_register_owner",
        )
        render_operation_form(
            snapshots[selected_owner]["journal"],
            form_key=f"admin_operation_form_{selected_owner}",
            fixed_fee=float(fixed_fee),
            owner_label=accounts[selected_owner].display_name,
            flash_key="_admin_flash",
            recorded_by=admin_username,
        )

    with import_tab:
        st.caption(
            "Carga una valoración de cartera para un usuario sin inventar compras, "
            "cantidades ni fechas de ejecución. La portada utilizará la fotografía "
            "más reciente de ese usuario."
        )
        selected_import_owner = st.selectbox(
            "Usuario propietario de la fotografía",
            usernames,
            format_func=lambda value: f"{accounts[value].display_name} ({value})",
            key="admin_snapshot_owner",
        )
        snapshot_file = st.file_uploader(
            "Excel de cartera",
            type=["xlsx"],
            key="admin_snapshot_file",
        )
        if snapshot_file is not None:
            try:
                workbook_snapshot = parse_portfolio_snapshot_excel(
                    snapshot_file.getvalue()
                )
            except (ValueError, ImportError) as exc:
                st.error(str(exc))
            else:
                preview = workbook_snapshot.positions
                preview_value = float(preview["value_eur"].sum())
                preview_cost = float(
                    pd.to_numeric(
                        preview["cost_estimate_eur"], errors="coerce"
                    ).sum()
                )
                preview_pnl = float(
                    pd.to_numeric(preview["gain_loss_eur"], errors="coerce").sum()
                )
                preview_cols = st.columns(4)
                preview_cols[0].metric("Fecha", workbook_snapshot.snapshot_date)
                preview_cols[1].metric("Posiciones", len(preview))
                preview_cols[2].metric("Valor", f"{preview_value:,.2f} €")
                preview_cols[3].metric(
                    "Resultado estimado", f"{preview_pnl:+,.2f} €"
                )
                st.caption(f"Coste estimado total: {preview_cost:,.2f} €")
                st.dataframe(
                    preview.rename(
                        columns={
                            "asset_name": "Activo",
                            "raw_identifier": "Símbolo original",
                            "analysis_ticker": "Ticker de análisis",
                            "value_eur": "Valor €",
                            "return_pct": "Rentabilidad",
                        }
                    ).loc[
                        :,
                        [
                            "Activo",
                            "Símbolo original",
                            "Ticker de análisis",
                            "Valor €",
                            "Rentabilidad",
                        ],
                    ],
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Valor €": st.column_config.NumberColumn(format="%.2f €"),
                        "Rentabilidad": st.column_config.NumberColumn(format="%+.2f%%"),
                    },
                )
                if st.button(
                    f"Guardar para {accounts[selected_import_owner].display_name}",
                    type="primary",
                    key="admin_save_snapshot",
                ):
                    try:
                        import_result = import_portfolio_workbook_snapshot(
                            snapshots[selected_import_owner]["journal"],
                            workbook_snapshot,
                            recorded_by=admin_username,
                        )
                    except (ValueError, JournalStorageError) as exc:
                        st.error(str(exc))
                    else:
                        st.session_state["_admin_flash"] = (
                            f"Guardadas {import_result.positions_saved} posiciones "
                            f"para {accounts[selected_import_owner].display_name}."
                        )
                        st.rerun()

    with detail_tab:
        selected_detail = st.selectbox(
            "Usuario que quieres revisar",
            usernames,
            format_func=lambda value: (
                f"{accounts[value].display_name} ({value})"
            ),
            key="admin_detail_owner",
        )
        selected_snapshot = snapshots[selected_detail]
        dashboard = selected_snapshot["dashboard"]
        operations = selected_snapshot["operations"]
        if dashboard.empty:
            st.info("Este usuario no tiene posiciones abiertas.")
        else:
            visible_dashboard = dashboard.rename(
                columns={
                    "ticker": "Ticker",
                    "currency": "Moneda",
                    "quantity": "Cantidad",
                    "average_cost": "Coste medio",
                    "cost_basis": "Capital pendiente",
                    "current_price": "Precio actual",
                    "net_value_eur": "Valor neto EUR",
                    "net_pnl_eur": "Resultado EUR",
                    "net_return_pct": "Rentabilidad",
                    "allocation_pct": "Peso en cartera",
                }
            )
            render_ticker_dataframe(
                visible_dashboard,
                key="admin_user_open_positions",
                column_config={
                    "Coste medio": st.column_config.NumberColumn(format="%.2f"),
                    "Capital pendiente": st.column_config.NumberColumn(format="%.2f"),
                    "Precio actual": st.column_config.NumberColumn(format="%.2f"),
                    "Valor neto EUR": st.column_config.NumberColumn(format="%.2f"),
                    "Resultado EUR": st.column_config.NumberColumn(format="%+.2f"),
                    "Rentabilidad": st.column_config.NumberColumn(format="%+.2f%%"),
                    "Peso en cartera": st.column_config.ProgressColumn(
                        min_value=0,
                        max_value=100,
                        format="%.1f%%",
                    ),
                },
            )
        st.markdown("#### Operaciones registradas")
        if operations.empty:
            st.caption("Todavía no hay operaciones.")
        else:
            st.dataframe(operations, width="stretch", hide_index=True)


def _favorites_matching_tags(
    favorites: pd.DataFrame,
    selected_tags: list[str],
) -> pd.DataFrame:
    if favorites.empty or not selected_tags:
        return favorites
    mask = favorites.get("tags", pd.Series("", index=favorites.index)).map(
        lambda value: bool(
            set(favorite_tags_from_value(value)).intersection(selected_tags)
        )
    )
    return favorites.loc[mask].copy()


def render_favorite_list(
    favorites: pd.DataFrame,
    *,
    title: str,
    journal,
    actor_username: str,
    tag_filter: list[str] | None = None,
    shared: bool = False,
    can_delete_all: bool = False,
) -> None:
    favorites = favorites.copy()
    if "tags" not in favorites.columns:
        favorites["tags"] = ""
    st.markdown(f"#### {title}")
    st.caption(f"{len(favorites)} de {MAX_FAVORITES} empresas guardadas")
    if favorites.empty:
        st.info("Todavía no hay empresas en esta lista.")
        return

    scope_key = "group" if shared else "private"
    search_value = st.text_input(
        "Buscar dentro de esta lista",
        placeholder="Nombre o ticker",
        key=f"favorite_list_search_{scope_key}",
    ).strip().casefold()
    filtered = _favorites_matching_tags(favorites, tag_filter or [])
    if search_value:
        name_values = filtered["name"].fillna("").astype(str).str.casefold()
        ticker_values = filtered["ticker"].fillna("").astype(str).str.casefold()
        filtered = filtered.loc[
            name_values.str.contains(search_value, regex=False)
            | ticker_values.str.contains(search_value, regex=False)
        ]
    if filtered.empty:
        st.info("Ninguna empresa coincide con la búsqueda o las etiquetas elegidas.")
    else:
        page_size = 25
        page_count = (len(filtered) + page_size - 1) // page_size
        page = 1
        if page_count > 1:
            page = st.selectbox(
                "Página",
                options=list(range(1, page_count + 1)),
                format_func=lambda value: f"Página {value} de {page_count}",
                key=f"favorite_page_{scope_key}",
            )
        page_frame = filtered.iloc[(page - 1) * page_size : page * page_size]
        first_visible = (page - 1) * page_size + 1
        last_visible = first_visible + len(page_frame) - 1
        st.caption(
            f"Mostrando {first_visible}–{last_visible} de {len(filtered)}"
        )
        visible = page_frame.loc[
            :, ["ticker", "name", "exchange", "tags", "recorded_by"]
        ].rename(
            columns={
                "ticker": "Ticker",
                "name": "Empresa",
                "exchange": "Mercado",
                "tags": "Etiquetas",
                "recorded_by": "Añadida por",
            }
        ).reset_index(drop=True)
        visible["Ticker"] = visible["Ticker"].fillna("").astype(str).str.upper()
        visible["Etiquetas"] = visible["Etiquetas"].map(favorite_tags_from_value)
        visible["Analizar"] = False
        visible["Quitar"] = False
        if not shared:
            visible = visible.drop(columns=["Añadida por"])

        revision_key = f"favorite_editor_revision_{scope_key}"
        editor_revision = int(st.session_state.get(revision_key, 0) or 0)
        edited = st.data_editor(
            visible,
            width="stretch",
            height=min(820, 38 + 35 * len(visible)),
            hide_index=True,
            disabled=[
                column
                for column in ["Ticker", "Empresa", "Mercado", "Añadida por"]
                if column in visible.columns
            ],
            key=f"favorite_editor_{scope_key}_{page}_{editor_revision}",
            column_config={
                "Ticker": st.column_config.TextColumn(width="small"),
                "Empresa": st.column_config.TextColumn(width="large"),
                "Mercado": st.column_config.TextColumn(width="medium"),
                "Etiquetas": st.column_config.MultiselectColumn(
                    options=list(FAVORITE_TAGS),
                    width="large",
                    help="Edita la clasificación directamente en la tabla.",
                ),
                "Analizar": st.column_config.CheckboxColumn(
                    width="small",
                    help="Marca para abrir el análisis de esta empresa.",
                ),
                "Quitar": st.column_config.CheckboxColumn(
                    width="small",
                    help="Marca para quitarla de favoritos; no afecta a tu cartera.",
                ),
            },
        )

        original_by_ticker = {
            str(row.get("ticker", "")).strip().upper(): row
            for _, row in page_frame.iterrows()
        }
        for _, edited_row in edited.iterrows():
            selected_ticker = str(edited_row.get("Ticker", "")).strip().upper()
            original_row = original_by_ticker.get(selected_ticker)
            if original_row is None:
                continue
            selected_owner = str(original_row.get("recorded_by", "") or "").lower()
            can_edit_selected = (
                not shared
                or can_delete_all
                or selected_owner == actor_username.lower()
            )
            original_tags = favorite_tags_from_value(original_row.get("tags", ""))
            edited_tags = favorite_tags_from_value(edited_row.get("Etiquetas", []))
            wants_remove = bool(edited_row.get("Quitar", False))
            wants_analysis = bool(edited_row.get("Analizar", False))

            if wants_remove:
                if not can_edit_selected:
                    st.warning(
                        "En la lista del grupo sólo puedes quitar las empresas que tú añadiste."
                    )
                else:
                    try:
                        journal.delete_favorite(selected_ticker)
                    except (JournalStorageError, ValueError) as exc:
                        st.error(str(exc))
                    else:
                        st.success(f"{selected_ticker} ya no está en esta lista.")
                st.session_state[revision_key] = editor_revision + 1
                st.rerun()

            if edited_tags != original_tags:
                if not can_edit_selected:
                    st.warning(
                        "En la lista del grupo sólo puedes editar las empresas que tú añadiste."
                    )
                else:
                    try:
                        journal.update_favorite_tags(selected_ticker, edited_tags)
                    except (JournalStorageError, ValueError) as exc:
                        st.error(str(exc))
                    else:
                        st.success(f"Etiquetas de {selected_ticker} actualizadas.")
                st.session_state[revision_key] = editor_revision + 1
                st.rerun()

            if wants_analysis:
                st.session_state[revision_key] = editor_revision + 1
                _open_ticker_analysis(selected_ticker)
                st.rerun()


def render_favorite_tabs(
    private_journal,
    group_journal,
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
    *,
    actor_username: str,
    is_admin: bool,
) -> None:
    """Muestra únicamente las listas y sus filtros opcionales."""

    with st.expander("Filtrar por etiquetas"):
        tag_filter = st.multiselect(
            "Etiquetas",
            FAVORITE_TAGS,
            key="favorite_tag_filter",
            help="Si eliges varias, se muestran las empresas que tengan al menos una.",
            label_visibility="collapsed",
        )
    private_tab, group_tab = st.tabs(
        [
            f"Mi lista · {len(private_favorites)}",
            f"Grupo · {len(group_favorites)}",
        ]
    )
    with private_tab:
        render_favorite_list(
            private_favorites,
            title="Mi lista privada",
            journal=private_journal,
            actor_username=actor_username,
            tag_filter=tag_filter,
        )
    with group_tab:
        render_favorite_list(
            group_favorites,
            title="Lista del grupo",
            journal=group_journal,
            actor_username=actor_username,
            tag_filter=tag_filter,
            shared=True,
            can_delete_all=is_admin,
        )


def render_favorites_manager(
    private_journal,
    group_journal,
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
    *,
    actor_username: str,
    is_admin: bool,
    favorite_view: str,
) -> None:
    if favorite_view == "Mis listas":
        render_page_intro(
            "FAVORITOS",
            "Mis listas",
            "Consulta, clasifica o quita empresas directamente desde su fila.",
        )
        render_favorite_tabs(
            private_journal,
            group_journal,
            private_favorites,
            group_favorites,
            actor_username=actor_username,
            is_admin=is_admin,
        )
        return

    render_page_intro(
        "FAVORITOS",
        "Añadir empresa",
        "Busca por nombre y elige la cotización y el mercado correctos.",
    )
    with st.form("company_search_form"):
        search_col, button_col = st.columns([4, 1])
        query = search_col.text_input(
            "Nombre o símbolo",
            placeholder="Ejemplo: Nintendo, Kazatomprom, Inditex o Microsoft",
            help=(
                "Puedes escribir el nombre. El buscador muestra acciones y ETF de los "
                "mercados internacionales disponibles en Yahoo."
            ),
            key="favorite_search_query",
        )
        submitted = button_col.form_submit_button(
            "Buscar",
            type="primary",
            width="stretch",
        )
    if submitted:
        st.session_state.pop("favorite_search_result", None)
        st.session_state.pop("favorite_market_filter", None)
        try:
            st.session_state["favorite_search_results"] = cached_company_search(query)
        except (DataDownloadError, ValueError) as exc:
            st.session_state["favorite_search_results"] = []
            st.error(str(exc))

    results: list[TickerSearchResult] = st.session_state.get(
        "favorite_search_results",
        [],
    )
    if submitted and not results:
        st.warning(
            "No se encontraron acciones o ETF. Prueba con el nombre en inglés o abre "
            "el modo avanzado de ticker exacto."
        )
    if results:
        market = st.selectbox(
            "Filtrar por mercado o país",
            _search_market_options(results),
            key="favorite_market_filter",
            help="Útil cuando una empresa cotiza en varios países o monedas.",
            on_change=_clear_session_key,
            args=("favorite_search_result",),
        )
        result_indices = _search_result_indices(results, market)
        result_index = st.selectbox(
            "Elige la cotización correcta",
            options=result_indices,
            format_func=lambda index: _search_result_label(results[index]),
            key="favorite_search_result",
        )
        destination = st.radio(
            "Dónde guardarla",
            ["Mi lista privada", "Lista del grupo"],
            horizontal=True,
            help="Guardar una empresa no es una recomendación de compra.",
        )
        selected = results[result_index]
        st.caption(
            f"Código: {selected.ticker} · Mercado: {selected.exchange or 'no indicado'}"
            + (f" · {selected.details}" if selected.details else "")
        )
        if selected.listing_type in {"ADR / OTC", "Cotización OTC", "GDR internacional"}:
            st.info(
                "Esta es una cotización internacional o extrabursátil. Puede tener "
                "moneda, horario y liquidez distintos de la acción local."
            )
        try:
            selected_fundamentals = cached_fundamentals(selected.ticker)
        except (DataDownloadError, ValueError):
            selected_fundamentals = {}
        suggested_tags = suggest_favorite_tags(
            selected.ticker,
            selected.name,
            selected.instrument_type,
            selected_fundamentals,
        )
        selected_tags = st.multiselect(
            "Etiquetas",
            FAVORITE_TAGS,
            default=suggested_tags,
            max_selections=5,
            key=f"new_favorite_tags_{selected.ticker}",
            help="Puedes aceptar la clasificación automática o corregirla.",
        )
        save_col, analyze_col = st.columns(2)
        save_clicked = save_col.button(
            f"Guardar {selected.ticker}",
            type="primary",
            key="save_favorite",
            width="stretch",
        )
        analyze_col.button(
            "Analizar sin guardar",
            key="analyze_search_result",
            width="stretch",
            on_click=_open_ticker_analysis,
            args=(selected.ticker,),
        )
        if save_clicked:
            target = (
                private_journal
                if destination == "Mi lista privada"
                else group_journal
            )
            try:
                target.add_favorite(
                    selected.ticker,
                    selected.name,
                    selected.exchange,
                    tags=selected_tags,
                    recorded_by=actor_username,
                )
            except (JournalStorageError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success(
                    f"{selected.name} se ha guardado en "
                    f"{destination.lower()}."
                )
                st.session_state.pop("favorite_search_results", None)
                st.session_state["_return_to_favorite_lists"] = True
                st.rerun()

    with st.expander("Modo avanzado: añadir un ticker exacto"):
        st.caption(
            "Úsalo sólo si la búsqueda por nombre no funciona. Los sufijos identifican "
            "el mercado: .MC España, .T Japón, .L Londres y .IL Londres internacional."
        )
        with st.form("manual_favorite_form", clear_on_submit=True):
            manual_ticker = st.text_input(
                "Ticker",
                placeholder="Ejemplo: SAN.MC, 7974.T o KAP.IL",
            )
            manual_name = st.text_input(
                "Nombre opcional",
                placeholder="Si lo dejas vacío se mostrará el ticker",
            )
            manual_tags = st.multiselect(
                "Etiquetas",
                FAVORITE_TAGS,
                max_selections=5,
                key="manual_favorite_tags",
            )
            manual_destination = st.radio(
                "Lista",
                ["Mi lista privada", "Lista del grupo"],
                horizontal=True,
                key="manual_favorite_destination",
            )
            manual_submitted = st.form_submit_button(
                "Guardar ticker",
                type="primary",
                width="stretch",
            )
        if manual_submitted:
            normalized_ticker = manual_ticker.strip().upper()
            target = (
                private_journal
                if manual_destination == "Mi lista privada"
                else group_journal
            )
            resolved_tags = manual_tags
            if normalized_ticker and not resolved_tags:
                try:
                    manual_fundamentals = cached_fundamentals(normalized_ticker)
                except (DataDownloadError, ValueError):
                    manual_fundamentals = {}
                resolved_tags = suggest_favorite_tags(
                    normalized_ticker,
                    manual_name,
                    fundamentals=manual_fundamentals,
                )
            try:
                target.add_favorite(
                    normalized_ticker,
                    manual_name,
                    tags=resolved_tags,
                    recorded_by=actor_username,
                )
            except (JournalStorageError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success(f"{normalized_ticker} se ha guardado.")
                st.session_state["_return_to_favorite_lists"] = True
                st.rerun()


PLOTLY_CONFIG = {
    "displayModeBar": False,
    "responsive": True,
    "scrollZoom": False,
}


def _numeric_score(value: object) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_text(value: object) -> str:
    score = _numeric_score(value)
    return f"{score:.0f}" if score is not None else "N/D"


def _logout_current_user() -> None:
    st.session_state.pop("_authenticated_user", None)


def render_app_header(user: AuthConfig) -> None:
    role = "Administrador" if user.is_admin else f"Hola, {user.display_name}"
    header_col, account_col = st.columns([7, 1], vertical_alignment="center")
    with header_col:
        st.markdown(
            f"""
            <div class="ssl-app-header">
                {brand_mark_html()}
                <div>
                    <h1 class="ssl-app-title">Stock Signal Lab</h1>
                    <p class="ssl-app-subtitle">
                        {html.escape(role)} · señales explicadas, cartera y riesgo
                    </p>
                </div>
                <span class="ssl-status-pill">Sólo análisis</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with account_col:
        with st.popover(
            "Mi cuenta",
            icon=":material/account_circle:",
            width="stretch",
        ):
            st.caption(f"Sesión iniciada como {user.display_name}.")
            st.button(
                "Cerrar sesión",
                width="stretch",
                key="header_logout",
                on_click=_logout_current_user,
            )


def render_page_intro(eyebrow: str, title: str, description: str) -> None:
    """Cabecera breve y consistente para distinguir cada herramienta."""

    st.markdown(
        f"""
        <section class="ssl-page-intro">
            <div class="ssl-page-intro-copy">
                <span>{html.escape(eyebrow)}</span>
                <h2>{html.escape(title)}</h2>
                <p>{html.escape(description)}</p>
            </div>
            {icon_html(contextual_icon(eyebrow, title), 'ssl-page-intro-icon')}
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_subnavigation(
    label: str,
    options: list[str],
    *,
    key: str,
    format_func=None,
    on_change=None,
) -> str:
    """Mantiene todos los submenús en la misma franja visual."""

    with st.container(key=f"section_subnavigation_{key}"):
        selected = st.segmented_control(
            label,
            options,
            key=key,
            required=True,
            label_visibility="collapsed",
            format_func=format_func,
            on_change=on_change,
        )
    return str(selected or options[0])


def render_opportunity_cards(
    summary: list[dict[str, object]],
    *,
    limit: int = 6,
    key_prefix: str = "opportunity_card",
) -> None:
    if not summary:
        return
    ordered = sorted(
        summary,
        key=lambda row: (
            _numeric_score(row.get("Oportunidad")) or -1,
            _numeric_score(row.get("Momento entrada")) or -1,
        ),
        reverse=True,
    )[:limit]
    columns = st.columns(3)
    for index, row in enumerate(ordered):
        raw_ticker = str(row.get("Ticker") or "N/D")
        ticker = html.escape(raw_ticker)
        label = str(row.get("Lectura conjunta") or row.get("Estado") or "Sin lectura")
        position_label = str(row.get("Si ya la tienes") or "Sin posición")
        tone = signal_tone(label)
        close = _numeric_score(row.get("Cierre"))
        close_text = f"{close:,.2f}" if close is not None else "N/D"
        date_value = html.escape(str(row.get("Fecha") or "Sin fecha"))
        confidence = _numeric_score(row.get("Confianza datos"))
        data_text = (
            f"Cobertura {confidence:.0f}%"
            if confidence is not None
            else str(row.get("Comprobación") or "Pendiente de actualizar")
        )
        with columns[index % len(columns)]:
            with st.container(border=True):
                st.markdown(
                    f"""
                    <div class="ssl-card-top">
                        <span class="ssl-ticker">{ticker}</span>
                        <span class="ssl-badge ssl-{tone}">{html.escape(label)}</span>
                    </div>
                    <div class="ssl-score-row">
                        <div class="ssl-score"><span>Oportunidad</span><strong>{_score_text(row.get("Oportunidad"))}/100</strong></div>
                        <div class="ssl-score"><span>Empresa</span><strong>{_score_text(row.get("Calidad empresa"))}</strong></div>
                        <div class="ssl-score"><span>Entrada</span><strong>{_score_text(row.get("Momento entrada"))}</strong></div>
                    </div>
                    <div class="ssl-card-footer"><span>Cierre {close_text}</span><span>Si la tienes: {html.escape(position_label)}</span></div>
                    <div class="ssl-card-footer"><span>{html.escape(data_text)}</span><span>{date_value}</span></div>
                    """,
                    unsafe_allow_html=True,
                )
                st.button(
                    f"Ver análisis de {raw_ticker}",
                    key=f"{key_prefix}_{index}_{raw_ticker}",
                    width="stretch",
                    on_click=_open_ticker_analysis,
                    args=(raw_ticker,),
                )


def _portfolio_snapshot(
    journal: object,
    prepared: dict[str, pd.DataFrame],
    fx_snapshot: FxSnapshot,
) -> tuple[pd.DataFrame, object]:
    operations = journal.list_operations()
    positions = calculate_open_positions(operations)
    latest_prices = {
        ticker: float(frame["close"].iloc[-1])
        for ticker, frame in prepared.items()
        if not frame.empty
    }
    return build_position_dashboard(
        operations,
        positions,
        latest_prices,
        fx_snapshot.rates_per_eur,
        sell_fee_eur=1.0,
    )


def _portfolio_tracking_tickers(journal: object) -> list[str]:
    """Une posiciones reconstruidas y tickers de la última fotografía."""

    tickers: list[str] = []
    try:
        positions = journal.open_positions()
        if not positions.empty and "ticker" in positions:
            tickers.extend(positions["ticker"].fillna("").astype(str).tolist())
    except (JournalStorageError, AttributeError, ValueError):
        pass
    if hasattr(journal, "list_portfolio_snapshot_positions"):
        try:
            stored = journal.list_portfolio_snapshot_positions()
            latest, _ = latest_portfolio_snapshot(stored)
            if not latest.empty and "analysis_ticker" in latest:
                tickers.extend(
                    latest["analysis_ticker"].fillna("").astype(str).tolist()
                )
        except (JournalStorageError, AttributeError, ValueError):
            pass
    return list(
        dict.fromkeys(
            resolve_analysis_ticker(ticker)
            for ticker in tickers
            if str(ticker).strip()
        )
    )


def _request_portfolio_market_refresh() -> None:
    """Solicita una descarga nueva desde Inicio o Cartera en el siguiente rerun."""

    st.session_state["_portfolio_market_refresh_requested"] = True


def _portfolio_allocations(
    dashboard: pd.DataFrame,
    snapshot: pd.DataFrame,
) -> dict[str, float]:
    """Devuelve pesos aproximados sin sumar dos veces operaciones y fotografía."""

    if not snapshot.empty and {"analysis_ticker", "value_eur"}.issubset(snapshot.columns):
        listed = snapshot.copy()
        listed["analysis_ticker"] = (
            listed["analysis_ticker"].fillna("").astype(str).str.strip().str.upper()
        )
        listed["value_eur"] = pd.to_numeric(listed["value_eur"], errors="coerce")
        total = float(listed["value_eur"].fillna(0.0).sum())
        if total > 0:
            grouped = listed.loc[listed["analysis_ticker"] != ""].groupby(
                "analysis_ticker"
            )["value_eur"].sum()
            return {
                str(ticker): float(value) / total * 100.0
                for ticker, value in grouped.items()
                if pd.notna(value)
            }
    if not dashboard.empty and {"ticker", "allocation_pct"}.issubset(dashboard.columns):
        return {
            str(row.ticker).strip().upper(): float(row.allocation_pct)
            for row in dashboard.itertuples(index=False)
            if pd.notna(row.allocation_pct)
        }
    return {}


def _search_result_label(result: TickerSearchResult) -> str:
    details = f" — {result.details}" if result.details else ""
    return f"{result.label}{details}"


def _search_market_options(results: list[TickerSearchResult]) -> list[str]:
    markets = sorted(
        {search_result_market_group(result) for result in results},
        key=lambda value: (value == "Otros mercados", value.casefold()),
    )
    return ["Todos los mercados", *markets]


def _search_result_indices(
    results: list[TickerSearchResult],
    market: str,
) -> list[int]:
    return [
        index
        for index, result in enumerate(results)
        if market == "Todos los mercados"
        or search_result_market_group(result) == market
    ]


def _clear_session_key(key: str) -> None:
    """Descarta una selección dependiente cuando cambia su filtro."""

    st.session_state.pop(key, None)


def _set_navigation(
    section: str,
    subsection_key: str | None = None,
    subsection: str | None = None,
) -> None:
    st.session_state["main_navigation"] = section
    if subsection_key and subsection:
        st.session_state[subsection_key] = subsection


def _set_analysis_tool(tool: str) -> None:
    """Abre una herramienta secundaria sin exponer rutas técnicas al usuario."""

    st.session_state["main_navigation"] = "Analizar"
    st.session_state["analysis_navigation"] = "Más análisis"
    st.session_state["analysis_tool_navigation"] = tool


def _sync_analysis_ticker(source_key: str) -> None:
    """Mantiene la misma empresa activa entre las herramientas de Analizar."""

    ticker = str(st.session_state.get(source_key, "") or "").strip()
    if ticker:
        st.session_state["analysis_ticker"] = resolve_analysis_ticker(ticker)


def _reset_analysis_company_picker() -> None:
    """Limpia el texto de búsqueda sin olvidar la empresa que está abierta."""

    st.session_state["analysis_picker_revision"] = int(
        st.session_state.get("analysis_picker_revision", 0) or 0
    ) + 1
    st.session_state.pop("analysis_picker_results", None)
    st.session_state.pop("analysis_picker_market", None)
    st.session_state.pop("analysis_picker_result", None)


def _continue_search_in_favorites(
    query: str,
    results: list[TickerSearchResult],
) -> None:
    """Lleva una búsqueda rápida a la pantalla donde puede guardarse."""

    st.session_state["main_navigation"] = "Favoritos"
    st.session_state["favorite_search_query"] = query
    st.session_state["favorite_search_results"] = results
    st.session_state.pop("favorite_market_filter", None)
    st.session_state.pop("favorite_search_result", None)


def _open_ticker_analysis(ticker: str) -> None:
    """Abre una favorita y solicita sus datos sin obligar a volver a escribirla."""

    if not ticker.strip():
        return
    normalized = resolve_analysis_ticker(ticker)
    recent = st.session_state.get("recent_analysis_tickers", [])
    st.session_state["recent_analysis_tickers"] = merge_analysis_ticker_sources(
        [normalized],
        recent if isinstance(recent, list) else [],
    )[:20]
    # No se modifican directamente claves pertenecientes a widgets. Streamlit
    # rechaza esos cambios si los menús ya se crearon durante la interacción.
    st.session_state["_requested_main_navigation"] = "Analizar"
    st.session_state["_requested_analysis_navigation"] = "Empresa"
    st.session_state["_requested_analysis_ticker"] = normalized
    st.session_state["_pending_analysis_ticker"] = normalized
    _reset_analysis_company_picker()


def _open_ticker_comparison(tickers: list[str]) -> None:
    """Lleva una selección múltiple al comparador sin perder sus empresas."""

    selected = merge_analysis_ticker_sources(tickers)[:10]
    if len(selected) < 2:
        return
    st.session_state["_comparison_seed_tickers"] = selected
    st.session_state["_requested_main_navigation"] = "Analizar"
    st.session_state["_requested_analysis_navigation"] = "Más análisis"
    st.session_state["analysis_tool_navigation"] = "Comparar empresas"


def _clear_table_selection(revision_key: str, revision: int) -> None:
    st.session_state[revision_key] = revision + 1


def render_ticker_dataframe(
    frame: pd.DataFrame,
    *,
    key: str,
    ticker_column: str = "Ticker",
    column_config: dict[str, object] | None = None,
    height: int | str = "auto",
) -> list[str]:
    """Muestra una tabla con selección múltiple y acciones sin doble significado."""

    visible = frame.reset_index(drop=True)
    revision_key = f"{key}_selection_revision"
    revision = int(st.session_state.get(revision_key, 0) or 0)
    event = st.dataframe(
        visible,
        width="stretch",
        height=height,
        hide_index=True,
        column_config=column_config,
        key=f"{key}_{revision}",
        on_select="rerun",
        selection_mode="multi-row",
    )
    selected_rows = list(getattr(event.selection, "rows", []) or [])
    if not selected_rows or ticker_column not in visible.columns:
        return []
    selected_tickers = merge_analysis_ticker_sources(
        str(visible.iloc[int(row)].get(ticker_column, "") or "")
        for row in selected_rows
        if 0 <= int(row) < len(visible)
    )
    if not selected_tickers:
        return []

    st.caption(
        f"{len(selected_tickers)} seleccionada(s). Abrir muestra una empresa; "
        "Comparar utiliza varias."
    )
    action_a, action_b, action_c = st.columns([1.3, 1.3, 0.8])
    open_ticker = selected_tickers[0]
    if len(selected_tickers) > 1:
        open_ticker = action_a.selectbox(
            "Empresa que quieres abrir",
            selected_tickers,
            key=f"{key}_open_choice_{revision}",
            label_visibility="collapsed",
        )
    else:
        action_a.button(
            f"Abrir {open_ticker}",
            icon=":material/open_in_new:",
            type="primary",
            width="stretch",
            key=f"{key}_open_{revision}",
            on_click=_open_ticker_analysis,
            args=(open_ticker,),
        )
    if len(selected_tickers) > 1:
        action_b.button(
            f"Comparar ({min(len(selected_tickers), 10)})",
            icon=":material/compare_arrows:",
            type="primary",
            width="stretch",
            key=f"{key}_compare_{revision}",
            on_click=_open_ticker_comparison,
            args=(selected_tickers,),
        )
        action_a.button(
            f"Abrir {open_ticker}",
            icon=":material/open_in_new:",
            width="stretch",
            key=f"{key}_open_many_{revision}_{open_ticker}",
            on_click=_open_ticker_analysis,
            args=(open_ticker,),
        )
    else:
        action_b.button(
            "Comparar",
            disabled=True,
            width="stretch",
            key=f"{key}_compare_disabled_{revision}",
            help="Selecciona al menos dos empresas.",
        )
    action_c.button(
        "Limpiar",
        icon=":material/close:",
        width="stretch",
        key=f"{key}_clear_{revision}",
        on_click=_clear_table_selection,
        args=(revision_key, revision),
    )
    return selected_tickers


def _set_growth_radar_group(group: str) -> None:
    """Activa uno de los accesos rápidos del radar dinámico."""

    st.session_state["growth_radar_group"] = group


def _select_growth_radar_ticker(ticker: str) -> None:
    """Selecciona una empresa sin abandonar el plan de crecimiento y momentum."""

    normalized = resolve_analysis_ticker(ticker)
    if normalized:
        st.session_state["growth_selected_ticker"] = normalized


def _close_quick_company_search() -> None:
    """Fuerza un panel nuevo y cerrado después de completar una acción."""

    revision = int(st.session_state.get("quick_company_search_revision", 0) or 0)
    st.session_state["quick_company_search_revision"] = revision + 1
    st.session_state.pop("quick_company_search_results", None)
    st.session_state.pop("quick_company_market_filter", None)
    st.session_state.pop("quick_company_search_result", None)


def _open_quick_company_analysis(ticker: str) -> None:
    _open_ticker_analysis(ticker)
    _close_quick_company_search()


def _save_quick_company_favorite(
    query: str,
    results: list[TickerSearchResult],
) -> None:
    _continue_search_in_favorites(query, results)
    _close_quick_company_search()


def render_quick_company_search() -> None:
    """Buscador compacto disponible sin mantener abierta toda la configuración."""

    revision = int(st.session_state.get("quick_company_search_revision", 0) or 0)
    with st.popover(
        "Buscar empresa",
        icon=":material/search:",
        width="stretch",
        key=f"quick_company_search_popover_{revision}",
    ):
        st.caption(
            "Escribe el nombre normal. No necesitas saber códigos como .T, .MC o .IL."
        )
        with st.form("quick_company_search_form"):
            query = st.text_input(
                "Empresa, ETF o ticker",
                placeholder="Nintendo, Kazatomprom, Inditex…",
                key="quick_company_search_query",
            )
            submitted = st.form_submit_button(
                "Buscar",
                type="primary",
                width="stretch",
            )
        if submitted:
            st.session_state.pop("quick_company_market_filter", None)
            st.session_state.pop("quick_company_search_result", None)
            try:
                st.session_state["quick_company_search_results"] = (
                    cached_company_search(query)
                )
            except (DataDownloadError, ValueError) as exc:
                st.session_state["quick_company_search_results"] = []
                st.error(str(exc))

        results: list[TickerSearchResult] = st.session_state.get(
            "quick_company_search_results",
            [],
        )
        if submitted and not results:
            st.warning("No se encontraron acciones o ETF con ese nombre.")
        if not results:
            return

        market = st.selectbox(
            "Mercado",
            _search_market_options(results),
            key="quick_company_market_filter",
            on_change=_clear_session_key,
            args=("quick_company_search_result",),
        )
        result_indices = _search_result_indices(results, market)
        if not result_indices:
            st.info("No hay resultados en ese mercado.")
            return
        selected_index = st.selectbox(
            "Cotización",
            result_indices,
            format_func=lambda index: _search_result_label(results[index]),
            key="quick_company_search_result",
        )
        selected = results[selected_index]
        if selected.details:
            st.caption(selected.details)
        open_col, save_col = st.columns(2)
        open_col.button(
            "Abrir análisis",
            type="primary",
            width="stretch",
            key="quick_open_analysis",
            on_click=_open_quick_company_analysis,
            args=(selected.ticker,),
        )
        save_col.button(
            "Guardar",
            width="stretch",
            key="quick_save_favorite",
            on_click=_save_quick_company_favorite,
            args=(query, results),
        )


def render_analysis_company_picker(
    favorite_tickers: list[str],
    favorite_labels: dict[str, str],
    journal: object,
    raw_fundamentals: dict[str, dict[str, object]],
) -> None:
    """Un único selector para favoritas, recientes, guardadas y búsquedas nuevas."""

    try:
        snapshots = journal.list_analysis_snapshots()
    except JournalStorageError:
        snapshots = pd.DataFrame()
    saved_tickers = (
        snapshots["ticker"].dropna().astype(str).tolist()
        if not snapshots.empty and "ticker" in snapshots.columns
        else []
    )
    recent_state = st.session_state.get("recent_analysis_tickers", [])
    recent_tickers = recent_state if isinstance(recent_state, list) else []
    suggestion_tickers = merge_analysis_ticker_sources(
        favorite_tickers,
        recent_tickers,
        saved_tickers,
    )

    labels: dict[str, str] = {}
    for ticker in suggestion_tickers:
        if ticker in favorite_labels:
            labels[ticker] = favorite_labels[ticker]
            continue
        fundamentals = raw_fundamentals.get(ticker, {})
        name = str(
            fundamentals.get("longName")
            or fundamentals.get("shortName")
            or ticker
        ).strip()
        source = (
            "vista recientemente"
            if ticker in recent_tickers
            else "análisis guardado"
        )
        labels[ticker] = (
            f"{name} ({ticker}) · {source}" if name != ticker else f"{ticker} · {source}"
        )
    label_to_ticker = {label: ticker for ticker, label in labels.items()}
    ticker_lookup = {ticker.casefold(): ticker for ticker in suggestion_tickers}

    revision = int(st.session_state.get("analysis_picker_revision", 0) or 0)
    picker_key = f"analysis_company_query_{revision}"
    active_ticker = str(st.session_state.get("analysis_ticker", "") or "").strip()

    with st.container(border=True):
        st.markdown("#### Elige una empresa")
        st.caption(
            "Empieza a escribir un nombre o ticker. En la misma lista aparecen tus "
            "favoritas, las recientes y los análisis guardados."
        )
        if active_ticker:
            st.caption(f"Empresa abierta ahora: **{active_ticker}**")
        selector_col, action_col = st.columns([4, 1])
        with selector_col:
            search_choice = st.selectbox(
                "Buscar empresa",
                [labels[ticker] for ticker in suggestion_tickers],
                index=None,
                placeholder="Escribe un nombre o ticker…",
                accept_new_options=True,
                filter_mode="fuzzy",
                key=picker_key,
                help=(
                    "Si la empresa no está en tu historial, buscaremos su cotización "
                    "correcta en los mercados disponibles."
                ),
            )
        known_ticker = label_to_ticker.get(str(search_choice or ""))
        if known_ticker is None and search_choice:
            known_ticker = ticker_lookup.get(str(search_choice).strip().casefold())
        with action_col:
            st.markdown("<div style='height: 1.75rem'></div>", unsafe_allow_html=True)
            search_action = st.button(
                "Abrir" if known_ticker else "Buscar",
                icon=":material/search:",
                type="primary",
                width="stretch",
                disabled=not search_choice,
                key=f"analysis_company_action_{revision}",
            )

        if search_action and search_choice:
            if known_ticker:
                _open_ticker_analysis(known_ticker)
                st.rerun()
            query = str(search_choice).strip()
            st.session_state.pop("analysis_picker_market", None)
            st.session_state.pop("analysis_picker_result", None)
            try:
                st.session_state["analysis_picker_results"] = cached_company_search(query)
            except (DataDownloadError, ValueError) as exc:
                st.session_state["analysis_picker_results"] = []
                st.error(str(exc))

        results: list[TickerSearchResult] = st.session_state.get(
            "analysis_picker_results",
            [],
        )
        if search_action and search_choice and not known_ticker and not results:
            st.warning("No se encontraron cotizaciones con ese nombre o ticker.")
        if results:
            market = st.selectbox(
                "Mercado de cotización",
                _search_market_options(results),
                key="analysis_picker_market",
                on_change=_clear_session_key,
                args=("analysis_picker_result",),
            )
            result_indices = _search_result_indices(results, market)
            if result_indices:
                selected_index = st.selectbox(
                    "Resultado",
                    result_indices,
                    format_func=lambda index: _search_result_label(results[index]),
                    key="analysis_picker_result",
                )
                selected_result = results[selected_index]
                if st.button(
                    f"Analizar {selected_result.ticker}",
                    type="primary",
                    icon=":material/open_in_new:",
                    width="stretch",
                    key="analysis_picker_open_result",
                ):
                    _open_ticker_analysis(selected_result.ticker)
                    st.rerun()


def render_home(
    user: AuthConfig,
    journal: object,
    group_journal: object,
    prepared: dict[str, pd.DataFrame],
    summary: list[dict[str, object]],
    fx_snapshot: FxSnapshot,
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
    section: str = "Hoy",
) -> None:
    latest_snapshot = pd.DataFrame()
    snapshot_summary = None
    snapshot_refresh = None
    if hasattr(journal, "list_portfolio_snapshot_positions"):
        try:
            stored_snapshots = journal.list_portfolio_snapshot_positions()
            latest_snapshot, snapshot_summary = latest_portfolio_snapshot(
                stored_snapshots
            )
        except (JournalStorageError, ValueError) as exc:
            st.warning(f"No se pudo leer la fotografía de cartera: {exc}")

    latest_prices = {
        ticker: float(frame["close"].iloc[-1])
        for ticker, frame in prepared.items()
        if not frame.empty
    }
    price_dates = {
        ticker: pd.Timestamp(frame.index[-1])
        for ticker, frame in prepared.items()
        if not frame.empty
    }
    if not latest_snapshot.empty:
        latest_snapshot, snapshot_refresh = refresh_portfolio_snapshot_prices(
            latest_snapshot,
            latest_prices,
            fx_snapshot.rates_per_eur,
            price_dates=price_dates,
        )
        # Recalcula total, resultado y rentabilidad con las filas revalorizadas.
        latest_snapshot, snapshot_summary = latest_portfolio_snapshot(latest_snapshot)

    update_dates = list(price_dates.values())
    if update_dates:
        update_text = f"precios de mercado {max(update_dates).date().isoformat()}"
    elif snapshot_summary is not None:
        update_text = f"cartera valorada el {snapshot_summary.snapshot_date}"
    else:
        update_text = "pendiente de actualización"
    if section == "Mi cartera":
        render_page_intro(
            "INICIO",
            "Mi cartera",
            f"Distribución, posiciones y resultados guardados · {update_text}.",
        )
    elif section == "Alertas":
        render_page_intro(
            "INICIO",
            "Alertas",
            f"Señales que merecen revisión hoy · {update_text}.",
        )
    else:
        render_page_intro(
            "INICIO",
            "Hoy",
            f"Hola, {user.display_name}. Tu situación y lo que merece atención · {update_text}.",
        )

    refresh_a, refresh_b = st.columns([1.6, 1])
    with refresh_a:
        if snapshot_refresh is not None:
            st.caption(
                f"{snapshot_refresh.market_priced_count} posiciones con precio reciente · "
                f"{snapshot_refresh.manual_count} valores manuales · "
                f"{snapshot_refresh.pending_count} pendientes."
            )
    refresh_b.button(
        "Actualizar cartera ahora",
        icon=":material/refresh:",
        width="stretch",
        key="home_refresh_portfolio",
        on_click=_request_portfolio_market_refresh,
    )

    try:
        private_dashboard, private_kpis = _portfolio_snapshot(
            journal, prepared, fx_snapshot
        )
        group_dashboard, group_kpis = _portfolio_snapshot(
            group_journal, prepared, fx_snapshot
        )
    except JournalStorageError as exc:
        st.warning(f"No se pudo construir el resumen de carteras: {exc}")
        private_dashboard = pd.DataFrame()
        group_dashboard = pd.DataFrame()
        private_kpis = None
        group_kpis = None

    if section in {"Resumen", "Hoy"} and (
        snapshot_summary is not None or private_kpis is not None
    ):
        if snapshot_summary is not None:
            result_label = "Resultado estimado"
            value_text = f"{snapshot_summary.value_eur:,.2f} €"
            result_text = (
                f"{snapshot_summary.gain_loss_eur:+,.2f} €"
                if snapshot_summary.gain_loss_eur is not None
                else "N/D"
            )
            result_detail = (
                f"{snapshot_summary.return_pct:+.2f}% estimado sobre el coste"
                if snapshot_summary.return_pct is not None
                else "El archivo no incluye un coste completo"
            )
            positions_text = snapshot_summary.investment_count
            positions_detail = (
                f"{snapshot_summary.line_count} partidas · "
                f"{snapshot_summary.platform_count} plataformas"
            )
            if snapshot_refresh is not None and snapshot_refresh.market_priced_count:
                value_detail = (
                    f"{snapshot_refresh.market_priced_count} cotizaciones hasta "
                    f"{snapshot_refresh.market_as_of or 'el último cierre'}; resto manual"
                )
            else:
                value_detail = f"Fotografía del {snapshot_summary.snapshot_date}"
        else:
            result_label = "Resultado latente"
            value_text = (
                f"{private_kpis.current_net_value_eur:,.0f} €"
                if private_kpis and private_kpis.priced_positions_count
                else "Sin actualizar"
            )
            result_text = (
                f"{private_kpis.unrealized_pnl_eur:+,.0f} €"
                if private_kpis and private_kpis.priced_positions_count
                else "—"
            )
            result_detail = (
                f"{private_kpis.unrealized_return_pct:+.2f}% sobre posiciones valoradas"
                if private_kpis and private_kpis.priced_positions_count
                else "Actualiza para conocer el resultado"
            )
            positions_text = private_kpis.open_positions_count if private_kpis else 0
            positions_detail = (
                f"{private_kpis.operations_count} operaciones registradas"
                if private_kpis
                else "Sin operaciones registradas"
            )
            value_detail = (
                f"{private_kpis.priced_positions_count}/{private_kpis.open_positions_count} "
                "posiciones con precio"
                if private_kpis
                else "Sin posiciones valoradas"
            )
        st.markdown(
            f"""
            <div class="ssl-kpi-grid">
                <div class="ssl-kpi-card">
                    <small>Valor de mi cartera</small>
                    <strong>{value_text}</strong>
                    <em>{value_detail}</em>
                </div>
                <div class="ssl-kpi-card">
                    <small>{result_label}</small>
                    <strong>{result_text}</strong>
                    <em>{result_detail}</em>
                </div>
                <div class="ssl-kpi-card">
                    <small>Inversiones declaradas</small>
                    <strong>{positions_text}</strong>
                    <em>{positions_detail}</em>
                </div>
                <div class="ssl-kpi-card">
                    <small>Seguimiento</small>
                    <strong>{len(private_favorites)} + {len(group_favorites)}</strong>
                    <em>favoritas privadas y del grupo</em>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if group_kpis is not None and group_kpis.open_positions_count:
            st.caption(
                f"Cartera del grupo: {group_kpis.open_positions_count} posiciones · "
                f"resultado latente valorado {group_kpis.unrealized_pnl_eur:+,.2f} EUR."
            )

    if section == "Mi cartera" and snapshot_summary is not None:
        st.markdown("### Mi cartera")
        st.caption(
            "Distribución basada en los últimos valores guardados, no en cotizaciones en tiempo real. "
            f"{snapshot_summary.analyzable_count} partidas tienen ticker reconocible para análisis. "
            "Civislend y Segofactoring aparecen agrupados para mostrar el capital total invertido."
        )
        home_snapshot = group_portfolio_snapshot_for_home(latest_snapshot)
        chart_a, chart_b = st.columns(2)
        with chart_a:
            st.plotly_chart(
                portfolio_snapshot_allocation_chart(home_snapshot),
                width="stretch",
                config=PLOTLY_CONFIG,
            )
        with chart_b:
            st.plotly_chart(
                portfolio_snapshot_assets_chart(home_snapshot),
                width="stretch",
                config=PLOTLY_CONFIG,
            )
        position_summary = home_snapshot.copy()
        for column in ["value_eur", "gain_loss_eur", "return_pct"]:
            position_summary[column] = pd.to_numeric(
                position_summary[column], errors="coerce"
            )
        position_summary = position_summary.rename(
            columns={
                "asset_name": "Empresa",
                "analysis_ticker": "Ticker",
                "platform": "Cuenta",
                "value_eur": "Valor actual",
                "gain_loss_eur": "Ganancia / pérdida",
                "return_pct": "Rentabilidad",
            }
        )
        st.caption("Pulsa una posición con ticker para abrir su análisis.")
        render_ticker_dataframe(
            position_summary.loc[
                :,
                [
                    "Empresa",
                    "Ticker",
                    "Cuenta",
                    "Valor actual",
                    "Ganancia / pérdida",
                    "Rentabilidad",
                ],
            ],
            key="home_portfolio_positions",
            column_config={
                "Valor actual": st.column_config.NumberColumn(format="%.2f €"),
                "Ganancia / pérdida": st.column_config.NumberColumn(format="%+.2f €"),
                "Rentabilidad": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )

    if section == "Mi cartera" and snapshot_summary is None:
        st.info(
            "Todavía no hay una fotografía de cartera disponible. Puedes importarla "
            "desde «Carteras» o reconstruir las posiciones con operaciones."
        )

    if section in {"Resumen", "Hoy"}:
        action_a, action_b, action_c = st.columns(3)
        action_a.button(
            "Analizar empresas",
            icon=":material/monitoring:",
            width="stretch",
            type="primary",
            on_click=_set_navigation,
            args=("Analizar",),
        )
        action_b.button(
            "Buscar y guardar",
            icon=":material/favorite:",
            width="stretch",
            on_click=_set_navigation,
            args=("Favoritos",),
        )
        action_c.button(
            "Abrir mi cartera",
            icon=":material/account_balance_wallet:",
            width="stretch",
            on_click=_set_navigation,
            args=("Carteras", "portfolio_navigation", "Privada"),
        )

    held_tickers = _portfolio_tracking_tickers(journal)
    allocations = _portfolio_allocations(private_dashboard, latest_snapshot)
    decision_rows = build_portfolio_decision_rows(
        summary,
        held_tickers,
        allocations_pct=allocations,
    )
    strong_entries, candidate_entries = entry_opportunity_rows(
        summary,
        held_tickers,
    )
    st.markdown("### Panel de decisiones")
    st.caption(
        "Separa lo que ya tienes de las nuevas entradas. «Posible ampliar» exige "
        "una posición sana, una entrada atractiva y un peso no excesivo."
    )
    decision_metrics = st.columns(4)
    decision_metrics[0].metric("Posiciones", len(decision_rows))
    decision_metrics[1].metric(
        "Requieren atención",
        sum(
            row["Decisión"] in {"Reducir", "Revisar venta", "Actualizar datos"}
            for row in decision_rows
        ),
    )
    decision_metrics[2].metric("Entradas fuertes", len(strong_entries))
    decision_metrics[3].metric("Candidatas", len(candidate_entries))
    decision_view = st.segmented_control(
        "Vista del panel de decisiones",
        ["Mi cartera", "Entradas fuertes", "Candidatas"],
        default="Mi cartera",
        key="home_decision_view",
        label_visibility="collapsed",
    )
    if decision_view == "Mi cartera":
        if decision_rows:
            render_ticker_dataframe(
                pd.DataFrame(decision_rows),
                key="home_portfolio_decisions",
                column_config={
                    "Oportunidad": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%d"
                    ),
                    "Peso": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%.1f%%"
                    ),
                },
            )
        else:
            st.info(
                "Todavía no hay posiciones registradas ni tickers reconocibles en la "
                "última fotografía."
            )
    else:
        entry_rows = strong_entries if decision_view == "Entradas fuertes" else candidate_entries
        if not entry_rows:
            st.info(
                "No hay empresas actualizadas en este grupo. Esto no obliga a comprar: "
                "puede ser mejor esperar a que aparezca una señal válida."
            )
        else:
            entry_frame = pd.DataFrame(entry_rows)
            entry_columns = [
                column
                for column in [
                    "Ticker",
                    "Momento entrada",
                    "Oportunidad",
                    "Calidad empresa",
                    "Lectura entrada",
                    "Lectura conjunta",
                    "Cierre",
                    "Fecha",
                ]
                if column in entry_frame.columns
            ]
            render_ticker_dataframe(
                entry_frame.loc[:, entry_columns],
                key=(
                    "home_strong_entries"
                    if decision_view == "Entradas fuertes"
                    else "home_candidate_entries"
                ),
                column_config={
                    "Momento entrada": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%d"
                    ),
                    "Oportunidad": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%d"
                    ),
                    "Calidad empresa": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%d"
                    ),
                },
            )

    risk_alerts = [
        row
        for row in summary
        if row.get("Si ya la tienes") in {"Reducir", "Vender"}
    ]
    entry_alerts = [
        row
        for row in summary
        if row.get("Lectura conjunta") in {"Oportunidad destacada", "Candidata"}
    ]
    st.markdown("### Atención hoy")
    if not summary:
        st.info(
            "Abre «Analizar → Radar» y actualiza las empresas que sigues. Las posiciones "
            "abiertas se añadirán automáticamente."
        )
    elif not risk_alerts and not entry_alerts:
        st.success(
            "No aparecen alertas prioritarias. Revisa las empresas en vigilancia "
            "antes de tomar decisiones."
        )
    else:
        for row in risk_alerts[:3]:
            st.warning(
                f"**{row['Ticker']} · {row['Si ya la tienes']}:** "
                f"momento de entrada {row.get('Momento entrada', 'N/D')}/100."
            )
        for row in entry_alerts[:3]:
            st.success(
                f"**{row['Ticker']} · {row['Lectura conjunta']}:** "
                f"oportunidad {row.get('Oportunidad', 'N/D')}/100. Requiere revisión."
            )

    if summary:
        st.markdown("### Empresas que más destacan")
        render_opportunity_cards(summary, limit=3)


def render_opportunities_page(
    raw_data: dict[str, pd.DataFrame],
    prepared: dict[str, pd.DataFrame],
    summary: list[dict[str, object]],
    strategy: StrategyConfig,
    backtest: BacktestConfig,
    fundamental_results: dict[str, FundamentalResult],
    valuation_results: dict[str, ValuationResult],
    relative_results: dict[str, RelativeStrengthResult],
    risk_results: dict[str, RiskResult],
    opportunity_results: dict[str, OpportunityResult],
    raw_fundamentals: dict[str, dict[str, object]],
    price_verifications: dict[str, PriceVerification],
    journal: object,
    favorite_tickers: list[str],
    include_company_detail: bool = True,
) -> None:
    render_page_intro(
        "RADAR",
        "Qué merece atención",
        "Ordena tus favoritas para decidir cuáles revisar primero. Una nota alta abre "
        "una investigación; no es una orden de compra.",
    )
    try:
        saved_snapshots = journal.list_analysis_snapshots()
    except JournalStorageError as exc:
        saved_snapshots = pd.DataFrame()
        st.warning(f"No se pudo leer el último análisis guardado: {exc}")
    catalog_summary = build_opportunity_catalog(
        favorite_tickers,
        saved_snapshots,
        summary,
    )
    if not catalog_summary:
        st.info(
            "Todavía no tienes empresas en el radar. Guarda una favorita o abre "
            "«Empresa» para buscar y analizar la primera."
        )
        return

    updated_count = sum(
        row.get("Comprobación") == "Actualizado en esta sesión"
        for row in catalog_summary
    )
    pending_count = len(catalog_summary) - updated_count
    st.caption(
        f"{len(catalog_summary)} empresas en el radar · {updated_count} comprobadas "
        f"en esta sesión · {pending_count} pendientes de actualizar."
    )
    held_tickers = set(_portfolio_tracking_tickers(journal))
    radar_views = ["Todas", "Entradas fuertes", "Candidatas", "Mi cartera", "Reducir / vender"]
    radar_view = st.segmented_control(
        "Qué quieres revisar",
        radar_views,
        default="Todas",
        key="opportunity_radar_view",
        label_visibility="collapsed",
    )
    if radar_view == "Entradas fuertes":
        visible_catalog = [
            row
            for row in catalog_summary
            if row.get("Lectura entrada") == "Entrada fuerte"
            and row.get("Ticker") not in held_tickers
        ]
    elif radar_view == "Candidatas":
        visible_catalog = [
            row
            for row in catalog_summary
            if row.get("Lectura entrada") in {"Entrada interesante", "Entrada candidata"}
            and row.get("Ticker") not in held_tickers
        ]
    elif radar_view == "Mi cartera":
        visible_catalog = [
            row for row in catalog_summary if row.get("Ticker") in held_tickers
        ]
    elif radar_view == "Reducir / vender":
        visible_catalog = [
            row
            for row in catalog_summary
            if row.get("Ticker") in held_tickers
            and row.get("Si ya la tienes") in {"Reducir", "Vender"}
        ]
    else:
        visible_catalog = catalog_summary
    st.caption(
        f"Vista «{radar_view}»: {len(visible_catalog)} empresas. Las notas antiguas "
        "siguen marcadas como pendientes hasta volver a actualizarlas."
    )
    scored_summary = [
        row
        for row in visible_catalog
        if _numeric_score(row.get("Oportunidad")) is not None
    ]
    if scored_summary:
        render_opportunity_cards(scored_summary)
    elif visible_catalog:
        st.info("Estas empresas todavía no tienen una nota utilizable.")
    else:
        st.info("Ahora mismo no hay empresas dentro de este filtro.")
    alerts = [
        row
        for row in catalog_summary
        if row.get("Lectura conjunta") in {"Oportunidad destacada", "Candidata"}
        or row.get("Si ya la tienes") in {"Reducir", "Vender"}
    ]
    st.caption(f"{len(alerts)} alertas prioritarias con las reglas configuradas.")
    if not visible_catalog:
        return

    st.markdown(f"### Lista · {radar_view}")
    st.caption(
        "Una nota guardada sirve como referencia, pero no se presenta como actual. "
        "La columna Comprobación te indica cuáles debes volver a revisar. Marca una "
        "empresa para abrirla o varias para compararlas."
    )
    radar = pd.DataFrame(visible_catalog)
    if "Momento entrada" in radar.columns:
        radar = radar.sort_values(
            ["Oportunidad", "Momento entrada"],
            ascending=[False, False],
            na_position="last",
        ).reset_index(drop=True)
        radar.insert(0, "Ranking", range(1, len(radar) + 1))
    essential_columns = [
        "Ranking",
        "Ticker",
        "Oportunidad",
        "Calidad empresa",
        "Momento entrada",
        "Lectura conjunta",
        "Si ya la tienes",
        "Comprobación",
        "Fecha",
    ]
    essential_columns = [
        column for column in essential_columns if column in radar.columns
    ]
    render_ticker_dataframe(
        radar.loc[:, essential_columns],
        key="opportunity_essential_table",
        column_config={
            "Oportunidad": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%d"
            ),
            "Calidad empresa": st.column_config.ProgressColumn(
                "Empresa /100", min_value=0, max_value=100, format="%d"
            ),
            "Momento entrada": st.column_config.ProgressColumn(
                "Entrada /100", min_value=0, max_value=100, format="%d"
            ),
        },
    )

    with st.expander("Ver indicadores y ranking completo"):
        render_ticker_dataframe(
            radar,
            key="opportunity_full_table",
            column_config={
                "Oportunidad": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                ),
                "Confianza datos": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d%%"
                ),
                "Calidad empresa": st.column_config.ProgressColumn(
                    "Empresa /100", min_value=0, max_value=100, format="%d"
                ),
                "Momento entrada": st.column_config.ProgressColumn(
                    "Entrada /100", min_value=0, max_value=100, format="%d"
                ),
                "Valoración": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                ),
                "Fuerza relativa": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                ),
                "Riesgo controlado": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                ),
                "Fuerza 3 meses": st.column_config.NumberColumn(format="%+.1f%%"),
                "Desde su máximo": st.column_config.NumberColumn(format="%+.1f%%"),
                "Actividad": st.column_config.NumberColumn(format="%.2fx"),
            },
        )

    if not include_company_detail:
        return
    if not raw_data:
        st.info(
            "Tus favoritas ya están en el radar. Pulsa «Actualizar cartera y análisis» "
            "para comprobar de nuevo sus precios y señales."
        )
        return
    if not prepared:
        st.error(
            "Las favoritas permanecen en el radar, pero no hay suficiente histórico "
            "válido para recalcular sus señales."
        )
        return

    if st.session_state.get("analysis_ticker") not in prepared:
        st.session_state.pop("analysis_ticker", None)
    selected = st.selectbox(
        "Empresa que quieres entender mejor",
        list(prepared),
        key="analysis_ticker",
    )
    recent = st.session_state.get("recent_analysis_tickers", [])
    st.session_state["recent_analysis_tickers"] = merge_analysis_ticker_sources(
        [selected],
        recent if isinstance(recent, list) else [],
    )[:20]
    render_analysis(
        selected,
        prepared[selected],
        strategy,
        backtest,
        fundamental_results[selected],
        valuation_results[selected],
        relative_results[selected],
        risk_results[selected],
        opportunity_results[selected],
        raw_fundamentals.get(selected, {}),
        price_verifications.get(selected),
        journal,
    )


def render_company_analysis_page(
    prepared: dict[str, pd.DataFrame],
    strategy: StrategyConfig,
    backtest: BacktestConfig,
    fundamental_results: dict[str, FundamentalResult],
    valuation_results: dict[str, ValuationResult],
    relative_results: dict[str, RelativeStrengthResult],
    risk_results: dict[str, RiskResult],
    opportunity_results: dict[str, OpportunityResult],
    raw_fundamentals: dict[str, dict[str, object]],
    price_verifications: dict[str, PriceVerification],
    journal: object,
) -> None:
    """Muestra sólo la empresa activa; el radar no repite este detalle."""

    selected_value = str(st.session_state.get("analysis_ticker", "") or "").strip()
    if not selected_value:
        st.info("Elige una empresa arriba para ver su lectura completa.")
        return
    selected = resolve_analysis_ticker(selected_value)
    if selected not in prepared:
        st.info(
            f"{selected} está seleccionada, pero todavía no tiene datos suficientes. "
            "Vuelve al Radar, abre «Actualizar datos» y prueba de nuevo."
        )
        return
    recent = st.session_state.get("recent_analysis_tickers", [])
    st.session_state["recent_analysis_tickers"] = merge_analysis_ticker_sources(
        [selected],
        recent if isinstance(recent, list) else [],
    )[:20]
    render_analysis(
        selected,
        prepared[selected],
        strategy,
        backtest,
        fundamental_results[selected],
        valuation_results[selected],
        relative_results[selected],
        risk_results[selected],
        opportunity_results[selected],
        raw_fundamentals.get(selected, {}),
        price_verifications.get(selected),
        journal,
    )


def render_staircase_projection(
    *,
    username: str,
    liquid_capital: float,
    monthly_total: float,
    current_strategy_value: float,
    initial_staircase_pct: float,
) -> None:
    """Simula aportaciones fijas y una ampliación condicionada de la escalera."""

    is_ddriu = username.strip().lower() == "ddriu"
    st.markdown("### Capital inicial, aportaciones y horizonte")
    st.caption(
        "Separa el dinero que tú aportas del rendimiento estimado. La escalera sólo "
        "recibe más aportación después de un año favorable; nunca se amplía una pérdida."
    )
    with st.expander("1. Capital que ya tienes", expanded=True):
        initial_a, initial_b, initial_c, initial_d = st.columns(4)
        initial_civislend = initial_a.number_input(
            "Civislend actual (€)",
            min_value=0.0,
            value=1_500.0 if is_ddriu else 0.0,
            step=250.0,
            key="projection_initial_civislend",
        )
        initial_factoring = initial_b.number_input(
            "Sego/facturas actual (€)",
            min_value=0.0,
            value=1_850.0 if is_ddriu else 0.0,
            step=250.0,
            key="projection_initial_factoring",
        )
        initial_equities = initial_c.number_input(
            "Acciones actuales (€)",
            min_value=0.0,
            value=float(liquid_capital),
            step=100.0,
            key="projection_initial_equities",
        )
        suggested_dynamic = min(float(current_strategy_value), float(initial_equities))
        if is_ddriu and suggested_dynamic <= 0:
            suggested_dynamic = min(640.0, float(initial_equities))
        initial_dynamic = initial_d.number_input(
            "De ellas, escalera (€)",
            min_value=0.0,
            value=float(suggested_dynamic),
            step=50.0,
            help="Incluye las posiciones abiertas siguiendo esta estrategia, no todas tus acciones.",
            key="projection_initial_dynamic",
        )

    with st.expander("2. Aportación mensual y reparto", expanded=True):
        monthly_a, monthly_b, monthly_c, monthly_d = st.columns(4)
        monthly_total = monthly_a.number_input(
            "Aportación mensual total (€)",
            min_value=0.0,
            value=float(monthly_total),
            step=50.0,
            help=(
                "Todo el dinero nuevo que prevés repartir entre Civislend, facturas, "
                "acciones tradicionales y la estrategia escalonada."
            ),
            key="projection_monthly_total",
        )
        monthly_civislend = monthly_b.number_input(
            "Aportación mensual Civislend (€)",
            min_value=0.0,
            value=250.0 if is_ddriu else 0.0,
            step=50.0,
            key="projection_monthly_civislend",
        )
        monthly_factoring = monthly_c.number_input(
            "Aportación mensual facturas (€)",
            min_value=0.0,
            value=250.0 if is_ddriu else 0.0,
            step=50.0,
            key="projection_monthly_factoring",
        )
        initial_staircase_pct = monthly_d.slider(
            "% inicial para la escalera",
            0.0,
            100.0,
            float(initial_staircase_pct),
            1.0,
            format="%.0f%%",
            help="Porcentaje del total mensual reservado inicialmente a esta estrategia.",
            key="projection_initial_staircase_pct",
        )

        fixed_monthly = float(monthly_civislend) + float(monthly_factoring)
        initial_dynamic_eur = float(monthly_total) * float(initial_staircase_pct) / 100.0
        initial_traditional_eur = (
            float(monthly_total) - fixed_monthly - initial_dynamic_eur
        )
        if fixed_monthly > float(monthly_total):
            st.error(
                "Las aportaciones a Civislend y facturas superan el dinero mensual total."
            )
            return
        if initial_traditional_eur < 0:
            st.error(
                "El porcentaje dinámico deja una aportación negativa para acciones tradicionales."
            )
            return

        split_a, split_b, split_c, split_d = st.columns(4)
        split_a.metric("Civislend/mes", f"{float(monthly_civislend):,.0f} €")
        split_b.metric("Facturas/mes", f"{float(monthly_factoring):,.0f} €")
        split_c.metric("Acciones tradicionales/mes", f"{initial_traditional_eur:,.0f} €")
        split_d.metric("Escalera inicial/mes", f"{initial_dynamic_eur:,.0f} €")

    with st.expander("3. Regla de ampliación y supuestos", expanded=False):
        scale_a, scale_b, scale_c = st.columns(3)
        step_pct = scale_a.slider(
            "Aumento tras un año favorable",
            0.0,
            15.0,
            5.0,
            1.0,
            format="%.0f puntos",
            key="projection_step_pct",
        )
        available_pct = (
            100.0
            if float(monthly_total) <= 0
            else 100.0 * (float(monthly_total) - fixed_monthly) / float(monthly_total)
        )
        default_maximum = min(max(float(initial_staircase_pct), 40.0), available_pct)
        maximum_dynamic_pct = scale_b.number_input(
            "Máximo mensual para la escalera (%)",
            min_value=0.0,
            max_value=100.0,
            value=float(default_maximum),
            step=5.0,
            key="projection_maximum_dynamic_pct",
        )
        scale_threshold = scale_c.number_input(
            "Rentabilidad mínima para ampliarla (%)",
            min_value=0.0,
            max_value=50.0,
            value=10.0,
            step=1.0,
            key="projection_scale_threshold",
        )

        civislend_return = 10.5
        factoring_return = 6.0
        traditional_return = 8.0
        central_dynamic_return = 10.0
        traditional_volatility = 16.0
        dynamic_volatility = 28.0
        show_projection_assumptions = st.checkbox(
            "Editar rentabilidades y volatilidad",
            value=False,
            key="projection_show_assumptions",
            help=(
                "Déjalo cerrado para utilizar supuestos centrales fáciles de comparar. "
                "Ábrelo sólo si quieres construir otro escenario."
            ),
        )
        if show_projection_assumptions:
            return_a, return_b, return_c, return_d = st.columns(4)
            civislend_return = return_a.number_input(
                "Civislend anual (%)",
                value=10.5,
                step=0.5,
                key="projection_civislend_return",
            )
            factoring_return = return_b.number_input(
                "Facturas anual (%)",
                value=6.0,
                step=0.5,
                key="projection_factoring_return",
            )
            traditional_return = return_c.number_input(
                "Acciones tradicionales (%)",
                value=8.0,
                step=0.5,
                key="projection_traditional_return",
            )
            central_dynamic_return = return_d.number_input(
                "Escalera esperada en la simulación (%)",
                value=10.0,
                step=0.5,
                key="projection_dynamic_return",
            )
            volatility_a, volatility_b = st.columns(2)
            traditional_volatility = volatility_a.number_input(
                "Volatilidad tradicional (%)",
                min_value=0.0,
                value=16.0,
                step=1.0,
                key="projection_traditional_volatility",
            )
            dynamic_volatility = volatility_b.number_input(
                "Volatilidad escalera (%)",
                min_value=0.0,
                value=28.0,
                step=1.0,
                key="projection_dynamic_volatility",
            )
        else:
            st.caption(
                "Supuestos centrales: Civislend 10,5% · facturas 6% · acciones 8% · "
                "escalera 10%. Volatilidad: acciones 16% y escalera 28%."
            )

    projection_config = StaircaseProjectionConfig(
        start_date=date.today(),
        initial_civislend=float(initial_civislend),
        initial_factoring=float(initial_factoring),
        initial_equities=float(initial_equities),
        initial_staircase=float(initial_dynamic),
        monthly_total=float(monthly_total),
        monthly_civislend=float(monthly_civislend),
        monthly_factoring=float(monthly_factoring),
        initial_staircase_pct=float(initial_staircase_pct),
        staircase_step_pct=float(step_pct),
        maximum_staircase_pct=float(maximum_dynamic_pct),
        scale_return_threshold_pct=float(scale_threshold),
        civislend_return_pct=float(civislend_return),
        factoring_return_pct=float(factoring_return),
        traditional_equity_return_pct=float(traditional_return),
        traditional_equity_volatility_pct=float(traditional_volatility),
        staircase_volatility_pct=float(dynamic_volatility),
    )
    try:
        projection_config.validate()
        projections = project_scenarios(
            projection_config,
            DEFAULT_SCENARIOS,
            months=120,
        )
        projection_summary = summarize_projection(
            projections,
            default_horizons(projection_config.start_date),
        )
        simulation = simulate_projection_ranges(
            projection_config,
            expected_staircase_return_pct=float(central_dynamic_return),
            simulations=1_000,
            months=120,
            seed=42,
        )
    except ValueError as exc:
        st.error(f"Configuración de proyección inválida: {exc}")
        return

    central_projection = projections.loc[projections["scenario"] == "Central 10%"]
    central_ten_year = central_projection.loc[central_projection["month"] == 120].iloc[0]
    simulation_ten_year = simulation.loc[simulation["month"] == 120].iloc[0]
    projection_metrics = st.columns(4)
    projection_metrics[0].metric(
        "Capital inicial incluido",
        f"{projection_config.initial_total:,.0f} €",
    )
    projection_metrics[1].metric(
        "Aportado en 10 años",
        f"{central_ten_year['contributed']:,.0f} €",
    )
    projection_metrics[2].metric(
        "Central determinista",
        f"{central_ten_year['total_value']:,.0f} €",
        f"{central_ten_year['estimated_profit']:,.0f} € sobre aportaciones",
    )
    projection_metrics[3].metric(
        "Recorridos simulados por encima de aportaciones",
        (
            ">99%"
            if simulation_ten_year["probability_above_contributions_pct"] >= 99.5
            else f"{simulation_ten_year['probability_above_contributions_pct']:.0f}%"
        ),
        help=(
            "Frecuencia dentro de 1.000 recorridos y de estos supuestos. No es la "
            "probabilidad real ni una garantía."
        ),
    )

    display_summary = projection_summary.drop(columns=["Meses"]).copy()
    st.dataframe(
        display_summary,
        width="stretch",
        hide_index=True,
        column_config={
            column: st.column_config.NumberColumn(format="%.0f €")
            for column in display_summary.columns
            if column != "Horizonte"
        },
    )
    st.plotly_chart(
        staircase_projection_chart(projections),
        width="stretch",
        config={"displaylogo": False},
        key="capital_staircase_projection_chart",
    )
    st.plotly_chart(
        staircase_range_chart(simulation),
        width="stretch",
        config={"displaylogo": False},
        key="capital_staircase_range_chart",
    )

    uncertainty_rows = []
    for label, month in default_horizons(projection_config.start_date):
        row = simulation.loc[simulation["month"] == month]
        if row.empty:
            continue
        current = row.iloc[0]
        uncertainty_rows.append(
            {
                "Horizonte": label,
                "Desfavorable P10": current["p10"],
                "Central P50": current["p50"],
                "Favorable P90": current["p90"],
                "Recorridos sobre aportaciones": current[
                    "probability_above_contributions_pct"
                ],
                "% medio destinado a escalera": current["average_staircase_allocation_pct"],
            }
        )
    uncertainty_frame = pd.DataFrame(uncertainty_rows)
    with st.expander("Ver intervalos y probabilidad por horizonte"):
        st.dataframe(
            uncertainty_frame,
            width="stretch",
            hide_index=True,
            column_config={
                "Desfavorable P10": st.column_config.NumberColumn(format="%.0f €"),
                "Central P50": st.column_config.NumberColumn(format="%.0f €"),
                "Favorable P90": st.column_config.NumberColumn(format="%.0f €"),
                "Recorridos sobre aportaciones": st.column_config.NumberColumn(
                    format="%.1f%%"
                ),
                "% medio destinado a escalera": st.column_config.NumberColumn(
                    format="%.1f%%"
                ),
            },
        )
    st.warning(
        "El stop limita el riesgo planificado, pero un gap puede ejecutar la venta a un "
        "precio peor. Las proyecciones no incluyen impuestos ni garantizan rentabilidad."
    )


def render_capital_projection_page(username: str) -> None:
    """Aísla la simulación patrimonial del radar de selección de empresas."""

    is_ddriu = username.strip().lower() == "ddriu"
    default_liquid = 4_400.0 if is_ddriu else 10_000.0
    liquid_capital = float(
        st.session_state.get("growth_liquid_capital", default_liquid) or default_liquid
    )
    monthly_total = float(
        st.session_state.get("growth_monthly_investable", 1_000.0) or 0.0
    )
    current_strategy_value = float(
        st.session_state.get(
            "growth_current_strategy_value",
            640.0 if is_ddriu else 0.0,
        )
        or 0.0
    )
    initial_staircase_pct = float(
        st.session_state.get("growth_monthly_allocation", 20.0) or 0.0
    )

    render_page_intro(
        "PLANIFICACIÓN",
        "Proyección de capital",
        "Separa cuánto aportas de cuánto podría valer el conjunto desde diciembre hasta "
        "10 años. No interviene en el score ni promete una rentabilidad.",
    )
    st.info(
        "Los valores iniciales toman como referencia la configuración de Crecimiento y "
        "momentum cuando ya la has utilizado. Puedes cambiarlos aquí sin alterar el radar."
    )
    render_staircase_projection(
        username=username,
        liquid_capital=liquid_capital,
        monthly_total=monthly_total,
        current_strategy_value=current_strategy_value,
        initial_staircase_pct=initial_staircase_pct,
    )


def render_growth_momentum_page(
    prepared: dict[str, pd.DataFrame],
    raw_fundamentals: dict[str, dict[str, object]],
    reference_data: dict[str, pd.DataFrame],
    relative_results: dict[str, RelativeStrengthResult],
    risk_results: dict[str, RiskResult],
    fx_snapshot: FxSnapshot,
    journal: object,
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
    username: str = "",
) -> None:
    """Muestra el plan mensual dinámico sin modificar el motor equilibrado."""

    is_ddriu = username.strip().lower() == "ddriu"
    render_page_intro(
        "ESTRATEGIA DINÁMICA",
        "Crecimiento y momentum",
        "Busca crecimiento empresarial y fortaleza de precio para una parte del dinero "
        "nuevo, sin modificar Sego, Civislend ni el análisis principal.",
    )
    st.info(
        "Busca empresas que ya muestran crecimiento y fortaleza. Si no aparece una "
        "entrada válida, la aportación mensual queda disponible: no es obligatorio comprar."
    )

    st.markdown("### Universo de favoritas")
    scan_scope = st.radio(
        "Listas que quieres revisar",
        ["Mi lista privada", "Mi lista privada y la del grupo"],
        horizontal=True,
        key="growth_favorite_scope",
        help="La lista del grupo puede contener ideas compartidas por los cuatro usuarios.",
    )
    included_group = (
        group_favorites
        if scan_scope == "Mi lista privada y la del grupo"
        else pd.DataFrame()
    )
    favorite_universe, _ = build_favorite_catalog(
        private_favorites,
        included_group,
    )
    favorite_universe = list(
        dict.fromkeys(resolve_analysis_ticker(ticker) for ticker in favorite_universe)
    )
    loaded_favorites = [ticker for ticker in favorite_universe if ticker in prepared]
    complete_fundamentals = [
        ticker
        for ticker in loaded_favorites
        if raw_fundamentals.get(ticker)
        and not raw_fundamentals.get(ticker, {}).get("_quick_mode")
        and not raw_fundamentals.get(ticker, {}).get("_fundamental_error")
    ]
    scan_cols = st.columns(3)
    scan_cols[0].metric("Favoritas en la lista", len(favorite_universe))
    scan_cols[1].metric("Precio y momentum cargados", len(loaded_favorites))
    scan_cols[2].metric("Análisis empresarial completo", len(complete_fundamentals))
    completed_scan = int(st.session_state.pop("_growth_scan_completed", 0) or 0)
    if completed_scan:
        st.success(
            f"Se han actualizado {completed_scan} empresas. El radar conserva también "
            "los bloques revisados anteriormente."
        )
    if favorite_universe:
        missing_favorites = [
            ticker for ticker in favorite_universe if ticker not in prepared
        ]
        scan_batch = (
            missing_favorites[:200]
            if missing_favorites
            else favorite_universe[:200]
        )
        scan_label = (
            f"Revisar favoritas pendientes ({len(scan_batch)})"
            if missing_favorites
            else f"Actualizar radar de favoritas ({len(scan_batch)})"
        )
        if st.button(
            scan_label,
            type="primary",
            width="stretch",
            icon=":material/travel_explore:",
            key="growth_scan_favorites",
        ):
            st.session_state["_growth_scan_tickers"] = scan_batch
            st.rerun()
        if len(missing_favorites) > len(scan_batch):
            st.caption(
                f"Quedarán {len(missing_favorites) - len(scan_batch)} empresas para el "
                "siguiente bloque. Se divide la descarga para no bloquear el alojamiento gratuito."
            )
    else:
        st.warning(
            "Esta lista todavía no tiene favoritas. Añádelas en Favoritos o utiliza "
            "las empresas cargadas manualmente en la barra lateral."
        )

    with st.expander("1. Capital disponible", expanded=True):
        capital_a, capital_b, capital_c, capital_d = st.columns(4)
        liquid_capital = capital_a.number_input(
            "Cartera líquida aproximada (€)",
            min_value=100.0,
            value=4_400.0 if is_ddriu else 10_000.0,
            step=500.0,
            help="No incluye Sego ni Civislend si no puedes disponer de ese dinero rápidamente.",
            key="growth_liquid_capital",
        )
        monthly_investable = capital_b.number_input(
            "Dinero mensual total para invertir (€)",
            min_value=0.0,
            value=1_000.0,
            step=50.0,
            help=(
                "Incluye las aportaciones a Civislend, facturas, acciones tradicionales "
                "y la estrategia escalonada."
            ),
            key="growth_monthly_investable",
        )
        current_strategy_value = capital_c.number_input(
            "Ya invertido con esta estrategia (€)",
            min_value=0.0,
            value=640.0 if is_ddriu else 0.0,
            step=100.0,
            help="Sirve para no superar el techo reservado al bloque dinámico.",
            key="growth_current_strategy_value",
        )
        current_open_risk = capital_d.number_input(
            "Riesgo ya abierto (€)",
            min_value=0.0,
            value=0.0,
            step=10.0,
            help="Suma de las pérdidas previstas hasta la invalidación de las posiciones dinámicas abiertas.",
            key="growth_current_open_risk",
        )

    with st.expander("2. Límites de la estrategia", expanded=True):
        limits_a, limits_b, limits_c = st.columns(3)
        monthly_allocation = limits_a.slider(
            "% mensual para esta estrategia",
            5.0,
            50.0,
            20.0,
            1.0,
            format="%.0f%%",
            key="growth_monthly_allocation",
        )
        strategy_cap = limits_b.slider(
            "Techo sobre cartera líquida",
            5.0,
            40.0,
            15.0,
            1.0,
            format="%.0f%%",
            key="growth_strategy_cap",
        )
        normal_risk = limits_c.slider(
            "Riesgo normal por entrada",
            0.10,
            1.50,
            0.50,
            0.05,
            format="%.2f%%",
            key="growth_normal_risk",
        )

        max_open_risk = 2.0
        max_sector = 20.0
        watch_score = 65
        candidate_score = 75
        strong_score = 82
        minimum_turnover_millions = 5.0
        commission_per_order = 1.0
        show_growth_advanced = st.checkbox(
            "Mostrar ajustes avanzados",
            value=False,
            key="growth_show_advanced",
        )
        if show_growth_advanced:
            advanced_a, advanced_b = st.columns(2)
            max_open_risk = advanced_a.slider(
                "Riesgo máximo simultáneo",
                0.50,
                5.0,
                2.0,
                0.25,
                format="%.2f%%",
                key="growth_max_open_risk",
            )
            max_sector = advanced_b.slider(
                "Máximo del bloque dinámico en un sector",
                5.0,
                40.0,
                20.0,
                1.0,
                format="%.0f%%",
                key="growth_max_sector",
            )
            score_a, score_b, score_c = st.columns(3)
            watch_score = score_a.slider(
                "Vigilancia desde", 50, 75, 65, key="growth_watch_score"
            )
            candidate_score = score_b.slider(
                "Entrada candidata", 60, 90, 75, key="growth_candidate_score"
            )
            strong_score = score_c.slider(
                "Entrada fuerte", 70, 95, 82, key="growth_strong_score"
            )
            execution_a, execution_b = st.columns(2)
            minimum_turnover_millions = execution_a.number_input(
                "Liquidez diaria preferida (millones €)",
                min_value=0.0,
                value=5.0,
                step=1.0,
                key="growth_min_turnover_millions",
            )
            commission_per_order = execution_b.number_input(
                "Comisión por compra o venta (€)",
                min_value=0.0,
                value=1.0,
                step=0.25,
                key="growth_commission_per_order",
            )

    config = GrowthMomentumConfig(
        monthly_allocation_pct=float(monthly_allocation),
        strategy_cap_pct=float(strategy_cap),
        normal_risk_pct=float(normal_risk),
        max_open_risk_pct=float(max_open_risk),
        max_sector_pct=float(max_sector),
        min_turnover_eur=float(minimum_turnover_millions) * 1_000_000.0,
        watch_score=int(watch_score),
        candidate_score=int(candidate_score),
        strong_score=int(strong_score),
        commission_per_order_eur=float(commission_per_order),
    )
    try:
        config.validate()
    except ValueError as exc:
        st.error(f"Configuración dinámica inválida: {exc}")
        return

    monthly_budget = float(monthly_investable) * config.monthly_allocation_pct / 100
    strategy_limit = float(liquid_capital) * config.strategy_cap_pct / 100
    remaining_limit = max(strategy_limit - float(current_strategy_value), 0.0)
    summary_cols = st.columns(4)
    summary_cols[0].metric(
        "Reserva mensual dinámica",
        f"{monthly_budget:,.2f} €",
        help="Puede acumularse si este mes no aparece una entrada válida.",
    )
    summary_cols[1].metric(
        "Espacio hasta el techo",
        f"{remaining_limit:,.2f} €",
        help=f"Techo configurado: {strategy_limit:,.2f} €.",
    )
    summary_cols[2].metric(
        "Pérdida normal máxima",
        f"{float(liquid_capital) * config.normal_risk_pct / 100:,.2f} €",
        help="Los sectores de mayor incertidumbre utilizan una cifra inferior.",
    )
    maximum_open_risk_eur = float(liquid_capital) * config.max_open_risk_pct / 100
    summary_cols[3].metric(
        "Riesgo abierto disponible",
        f"{max(maximum_open_risk_eur - float(current_open_risk), 0.0):,.2f} €",
        help="Suma de las pérdidas previstas si se activaran todos los niveles de invalidación.",
    )
    st.caption(
        "La estimación del capital a diciembre, 1, 2, 3, 4 y 10 años está ahora "
        "separada del radar."
    )
    st.button(
        "Abrir proyección de capital",
        icon=":material/monitoring:",
        width="stretch",
        key="growth_open_capital_projection",
        on_click=_set_analysis_tool,
        args=("Plan de capital",),
    )

    radar_prepared = prepared
    if favorite_universe:
        only_favorites = st.checkbox(
            "Mostrar sólo favoritas en este radar",
            value=True,
            key="growth_only_favorites",
            help="Desmárcalo para incluir también tickers cargados manualmente o posiciones abiertas.",
        )
        if only_favorites:
            radar_prepared = {
                ticker: prepared[ticker]
                for ticker in favorite_universe
                if ticker in prepared
            }

    if not radar_prepared:
        st.warning(
            "Todavía no hay empresas de este universo cargadas. Pulsa el botón de "
            "revisión de favoritas o actualiza empresas desde la barra lateral."
        )
        return

    results: dict[str, GrowthMomentumResult] = {}
    errors: list[str] = []
    for ticker, frame in radar_prepared.items():
        try:
            results[ticker] = evaluate_growth_momentum(
                ticker=ticker,
                frame=frame,
                info=raw_fundamentals.get(ticker, {}),
                relative=relative_results.get(ticker),
                risk=risk_results.get(ticker),
                broad_market=reference_data.get(benchmark_for_ticker(ticker)),
                config=config,
            )
        except ValueError as exc:
            errors.append(f"{ticker}: {exc}")
    for error in errors:
        st.warning(error)
    if not results:
        st.error("No hay suficiente información para calcular el perfil dinámico.")
        return

    st.markdown("### 3. Radar independiente")
    st.caption(
        "Crecimiento, momentum y contexto permanecen separados. La confianza indica "
        "cobertura de datos, no la probabilidad de ganar."
    )
    radar_rows = [
        {
            "Ticker": result.ticker,
            "Lectura": result.label,
            "Total": result.score,
            "Crecimiento": (
                float(result.growth_score)
                if result.growth_score is not None
                else float("nan")
            ),
            "Momentum": result.momentum_score,
            "Mercado y riesgo": result.context_score,
            "Confianza datos": result.confidence_pct,
            "Perfil": result.sector_label,
            "Small cap": "Sí" if result.is_small_cap else "No",
            "Riesgo por entrada": result.suggested_risk_pct,
            "Stop por volatilidad": result.atr_stop_pct,
            "Último cierre": result.price,
            "Moneda": str(
                raw_fundamentals.get(result.ticker, {}).get("currency") or "N/D"
            ),
            "Datos hasta": result.as_of.date(),
        }
        for result in sorted(
            results.values(),
            key=lambda item: (item.score, item.momentum_score, item.confidence_pct),
            reverse=True,
        )
    ]
    radar_frame = pd.DataFrame(radar_rows)
    radar_groups = growth_radar_ticker_groups(
        zip(radar_frame["Ticker"], radar_frame["Lectura"])
    )
    radar_group_specs = [
        ("all", "Empresas revisadas", ":material/domain_verification:"),
        ("strong", "Entradas fuertes", ":material/rocket_launch:"),
        ("candidates", "Entradas candidatas", ":material/trending_up:"),
        ("watch", "En vigilancia", ":material/visibility:"),
        ("pending", "Pendientes de datos", ":material/pending_actions:"),
    ]
    valid_group_keys = {group_key for group_key, _, _ in radar_group_specs}
    if st.session_state.get("growth_radar_group") not in valid_group_keys:
        st.session_state["growth_radar_group"] = "all"
    active_group = str(st.session_state["growth_radar_group"])
    radar_cols = st.columns(5)
    for column, (group_key, group_label, group_icon) in zip(
        radar_cols,
        radar_group_specs,
    ):
        column.button(
            f"{group_label} · {len(radar_groups[group_key])}",
            icon=group_icon,
            type="primary" if active_group == group_key else "secondary",
            width="stretch",
            key=f"growth_radar_group_button_{group_key}",
            on_click=_set_growth_radar_group,
            args=(group_key,),
            help=f"Mostrar las empresas de «{group_label}».",
        )

    active_group = str(st.session_state.get("growth_radar_group", "all"))
    active_group_label = next(
        label for key, label, _ in radar_group_specs if key == active_group
    )
    active_group_tickers = radar_groups[active_group]
    radar_lookup = radar_frame.set_index("Ticker").to_dict(orient="index")

    def radar_company_label(ticker: str) -> str:
        row = radar_lookup[ticker]
        info = raw_fundamentals.get(ticker, {})
        company_name = str(info.get("shortName") or info.get("longName") or "").strip()
        identity = (
            f"{ticker} · {company_name}"
            if company_name and company_name.upper() != ticker
            else ticker
        )
        return f"{identity} — {row['Lectura']} · {float(row['Total']):.0f}/100"

    with st.container(border=True):
        st.markdown(
            f"**{active_group_label}** · {len(active_group_tickers)} empresas"
        )
        if not active_group_tickers:
            st.info("Ahora mismo no hay empresas dentro de este grupo.")
        else:
            radar_pick = st.selectbox(
                "Elige una empresa del grupo",
                active_group_tickers,
                format_func=radar_company_label,
                key=f"growth_radar_pick_{active_group}",
            )
            radar_action_a, radar_action_b = st.columns(2)
            radar_action_a.button(
                f"Preparar plan de {radar_pick}",
                icon=":material/checklist:",
                width="stretch",
                key=f"growth_radar_plan_{active_group}",
                on_click=_select_growth_radar_ticker,
                args=(radar_pick,),
            )
            radar_action_b.button(
                f"Abrir análisis completo de {radar_pick}",
                icon=":material/open_in_new:",
                type="primary",
                width="stretch",
                key=f"growth_radar_analysis_{active_group}",
                on_click=_open_ticker_analysis,
                args=(radar_pick,),
            )

    st.caption(
        "Marca una empresa para abrirla o varias para llevarlas al comparador."
    )
    render_ticker_dataframe(
        radar_frame,
        key="growth_momentum_radar",
        column_config={
            "Total": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
            "Crecimiento": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
            "Momentum": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
            "Mercado y riesgo": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
            "Confianza datos": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%"),
            "Riesgo por entrada": st.column_config.NumberColumn(format="%.2f%%"),
            "Stop por volatilidad": st.column_config.NumberColumn(format="%.1f%%"),
            "Último cierre": st.column_config.NumberColumn(format="%.2f"),
            "Datos hasta": st.column_config.DateColumn(format="DD/MM/YYYY"),
        },
    )
    st.download_button(
        "Descargar radar completo en CSV",
        radar_frame.to_csv(index=False).encode("utf-8-sig"),
        file_name="radar_favoritos_crecimiento_momentum.csv",
        mime="text/csv",
        width="stretch",
        key="growth_download_radar",
    )

    ordered_tickers = [row["Ticker"] for row in radar_rows]
    quick_tickers = [
        ticker
        for ticker in ordered_tickers
        if raw_fundamentals.get(ticker, {}).get("_quick_mode")
        or raw_fundamentals.get(ticker, {}).get("_fundamental_error")
    ]
    if quick_tickers:
        deep_batch = quick_tickers[:25]
        st.info(
            f"{len(quick_tickers)} empresas tienen ya precio y momentum, pero les falta "
            "la revisión empresarial. Mientras tanto no se mostrarán como entradas completas."
        )
        if st.button(
            f"Completar las siguientes {len(deep_batch)} empresas",
            width="stretch",
            icon=":material/fact_check:",
            key="growth_complete_fundamentals",
        ):
            st.session_state["_growth_scan_tickers"] = deep_batch
            st.rerun()
    if st.session_state.get("growth_selected_ticker") not in ordered_tickers:
        st.session_state.pop("growth_selected_ticker", None)
    selected = st.selectbox(
        "Empresa para preparar el plan",
        ordered_tickers,
        key="growth_selected_ticker",
    )
    automatic_result = results[selected]
    sector_options = ["automatic", *SECTOR_PROFILES]
    sector_choice = st.selectbox(
        "Perfil sectorial aplicado",
        sector_options,
        index=0,
        format_func=lambda value: (
            f"Automático · {automatic_result.sector_label}"
            if value == "automatic"
            else SECTOR_PROFILES[value].label
        ),
        help="Corrígelo si el proveedor clasifica mal una empresa híbrida.",
        key=f"growth_sector_override_{selected}",
    )
    selected_result = automatic_result
    if sector_choice != "automatic":
        selected_result = evaluate_growth_momentum(
            ticker=selected,
            frame=prepared[selected],
            info=raw_fundamentals.get(selected, {}),
            relative=relative_results.get(selected),
            risk=risk_results.get(selected),
            broad_market=reference_data.get(benchmark_for_ticker(selected)),
            config=config,
            sector_override=sector_choice,
        )

    score_cols = st.columns(5)
    score_cols[0].metric("Lectura dinámica", f"{selected_result.score}/100", selected_result.label)
    score_cols[1].metric(
        "Crecimiento",
        f"{selected_result.growth_score}/100"
        if selected_result.growth_score is not None
        else "N/D",
    )
    score_cols[2].metric("Momentum", f"{selected_result.momentum_score}/100")
    score_cols[3].metric("Mercado y riesgo", f"{selected_result.context_score}/100")
    score_cols[4].metric("Datos disponibles", f"{selected_result.confidence_pct}%")
    message = (
        f"**{selected_result.label}.** La nota total es {selected_result.score}/100. "
        "Antes de utilizar dinero real deben completarse las comprobaciones sectoriales."
    )
    if selected_result.label in {"Entrada fuerte", "Entrada candidata"}:
        st.success(message)
    elif selected_result.label in {"Vigilancia activa", "Esperar mejor precio"}:
        st.info(message)
    else:
        st.warning(message)

    factor_a, factor_b = st.columns(2)
    with factor_a:
        st.markdown("**Lo que apoya la hipótesis**")
        if selected_result.positive_factors:
            for factor in selected_result.positive_factors:
                st.markdown(f"- {friendly_factor(factor)}")
        else:
            st.caption("No hay suficientes factores favorables cuantificados.")
    with factor_b:
        st.markdown("**Lo que puede hacerla fallar**")
        if selected_result.risk_factors:
            for factor in selected_result.risk_factors:
                st.markdown(f"- {friendly_factor(factor)}")
        else:
            st.caption("No aparecen alertas cuantificadas importantes.")

    st.markdown("### 4. Tamaño orientativo de la entrada")
    quote_currency = str(
        raw_fundamentals.get(selected, {}).get("currency") or ""
    ).strip()
    plan_inputs_a, plan_inputs_b, plan_inputs_c = st.columns(3)
    entry_price = plan_inputs_a.number_input(
        (
            f"Precio que estás considerando ({quote_currency})"
            if quote_currency
            else "Precio que estás considerando"
        ),
        min_value=0.01,
        value=float(selected_result.price),
        step=0.01,
        key=f"growth_entry_price_{selected}",
    )
    manual_stop = plan_inputs_b.number_input(
        "Distancia hasta la invalidación",
        min_value=1.0,
        max_value=40.0,
        value=float(selected_result.atr_stop_pct),
        step=0.5,
        format="%.1f",
        help="La propuesta usa ATR y el perfil sectorial. Puedes sustituirla por un soporte razonado.",
        key=f"growth_stop_{selected}_{selected_result.sector_key}",
    )
    current_sector_value = plan_inputs_c.number_input(
        "Ya invertido en el mismo sector o tema (€)",
        min_value=0.0,
        value=0.0,
        step=100.0,
        help=(
            "Incluye empresas diferentes que dependan de la misma narrativa, como nuclear y uranio. "
            "El límite sectorial se calcula dentro del bloque dinámico."
        ),
        key=f"growth_sector_value_{selected_result.sector_key}",
    )
    try:
        entry_price_eur = quote_price_to_eur(
            float(entry_price),
            quote_currency,
            fx_snapshot.rates_per_eur,
        )
    except ValueError as exc:
        st.error(
            "No se puede calcular una cantidad fiable sin convertir la cotización "
            f"a euros: {exc}"
        )
        st.caption(
            "Completa primero el análisis empresarial para obtener la moneda o espera "
            "a que el Banco Central Europeo publique un tipo compatible."
        )
        return
    if quote_currency.upper() != "EUR":
        st.caption(
            f"Conversión aplicada: una acción a {float(entry_price):,.2f} {quote_currency} "
            f"equivale aproximadamente a {entry_price_eur:,.2f} € por unidad."
        )
    try:
        plan = calculate_growth_position_plan(
            result=selected_result,
            config=config,
            liquid_capital=float(liquid_capital),
            monthly_investable=float(monthly_investable),
            current_strategy_value=float(current_strategy_value),
            current_sector_value=float(current_sector_value),
            current_open_risk=float(current_open_risk),
            entry_price=float(entry_price),
            entry_price_eur=entry_price_eur,
            manual_stop_pct=float(manual_stop),
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    plan_cols = st.columns(4)
    plan_cols[0].metric(
        "Entrada máxima este mes",
        f"{plan.suggested_position_value:,.2f} €",
        help="Es el menor límite entre presupuesto mensual, riesgo, tamaño sectorial y techo de estrategia.",
    )
    plan_cols[1].metric("Cantidad aproximada", f"{plan.quantity:,.4f}")
    plan_cols[2].metric(
        "Invalidación orientativa",
        f"{plan.stop_price:,.2f} {quote_currency}",
        f"-{plan.stop_distance_pct:.1f}%",
    )
    plan_cols[3].metric(
        "Pérdida si se ejecutara allí",
        f"{plan.loss_at_stop:,.2f} €",
        help="Puede ser mayor durante un gap o si el activo tiene poca liquidez.",
    )
    st.caption(
        f"Referencia 2R: {plan.reference_target_2r:,.2f} {quote_currency} · Comisión de ida y vuelta: "
        f"{plan.round_trip_commission:,.2f} € ({plan.commission_drag_pct:.2f}% de la entrada) · "
        f"Riesgo sectorial utilizado: {selected_result.suggested_risk_pct:.3f}% de la cartera líquida · "
        f"Capacidad restante del sector/tema: {plan.remaining_sector_capacity:,.2f} €."
    )
    if plan.suggested_position_value <= 0:
        st.error("El bloque dinámico ya ha alcanzado su techo; no queda capacidad configurada.")
    elif selected_result.label not in {"Entrada fuerte", "Entrada candidata"}:
        st.warning(
            "El cálculo de tamaño no convierte la señal en compra. Con la lectura actual, "
            "la aplicación reservaría el dinero y esperaría confirmación."
        )

    profile = SECTOR_PROFILES[selected_result.sector_key]
    with st.expander(f"5. Validación obligatoria · {profile.label}", expanded=True):
        st.write(profile.description)
        st.caption(
            f"Tamaño máximo individual: {selected_result.max_position_pct:.1f}% de la cartera líquida · "
            f"Máximo por sector/tema: {config.max_sector_pct:.1f}% del bloque dinámico · "
            f"Riesgo simultáneo total: {config.max_open_risk_pct:.2f}%."
        )
        for check in selected_result.manual_checks:
            st.checkbox(
                check,
                value=False,
                key=f"growth_check_{selected}_{selected_result.sector_key}_{abs(hash(check))}",
            )
        st.caption(
            "Estas casillas son una lista de preparación. La aplicación no afirma que "
            "una comprobación esté resuelta sólo porque la marques."
        )

    with st.expander("Guardar esta evaluación para medir si funcionó"):
        note = st.text_area(
            "Hipótesis o nota personal",
            placeholder="Ejemplo: crecimiento de contratos; invalidar si recorta guía o pierde soporte",
            max_chars=1_000,
            key=f"growth_note_{selected}",
        )
        if st.button(
            "Guardar evaluación dinámica",
            type="primary",
            key=f"growth_save_{selected}",
        ):
            explanation = (
                "Estrategia Crecimiento y momentum. "
                f"Total {selected_result.score}/100; crecimiento "
                f"{selected_result.growth_score if selected_result.growth_score is not None else 'N/D'}; "
                f"momentum {selected_result.momentum_score}; contexto {selected_result.context_score}; "
                f"lectura {selected_result.label}; perfil {selected_result.sector_label}; "
                f"entrada orientativa máxima {plan.suggested_position_value:.2f} €."
            )
            try:
                journal.add_analysis_snapshot(
                    ticker=selected,
                    analyzed_at=selected_result.as_of,
                    price=float(entry_price),
                    opportunity_score=selected_result.score,
                    company_score=selected_result.growth_score,
                    entry_score=selected_result.momentum_score,
                    valuation_score=None,
                    relative_score=(
                        relative_results[selected].score
                        if selected in relative_results
                        else None
                    ),
                    risk_score=selected_result.context_score,
                    opportunity_label=f"Dinámica · {selected_result.label}",
                    entry_label=selected_result.label,
                    position_label="Plan mensual",
                    horizon_days=126,
                    sector=selected_result.sector_label,
                    explanation=explanation,
                    note=note,
                )
            except (JournalStorageError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success(
                    "Evaluación guardada. Podrás contrastarla después con el precio real."
                )


def _comparison_groups(
    prepared: dict[str, pd.DataFrame],
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
    fundamental_results: dict[str, FundamentalResult],
) -> dict[str, list[str]]:
    groups: dict[str, set[str]] = {"Todas las cargadas": set(prepared)}
    for ticker, result in fundamental_results.items():
        if ticker in prepared and result.sector:
            groups.setdefault(f"Sector · {result.sector}", set()).add(ticker)
    for favorites in (private_favorites, group_favorites):
        if favorites.empty:
            continue
        for favorite in favorites.itertuples(index=False):
            ticker = str(favorite.ticker).strip().upper()
            if ticker not in prepared:
                continue
            for tag in favorite_tags_from_value(getattr(favorite, "tags", "")):
                groups.setdefault(f"Etiqueta · {tag}", set()).add(ticker)
    return {
        name: sorted(tickers)
        for name, tickers in groups.items()
        if len(tickers) >= 2
    }


def render_sector_comparison(
    prepared: dict[str, pd.DataFrame],
    fundamental_results: dict[str, FundamentalResult],
    valuation_results: dict[str, ValuationResult],
    risk_results: dict[str, RiskResult],
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
) -> None:
    st.subheader("Comparador sectorial")
    st.write(
        "Compara empresas semejantes sin confundir sus precios nominales. Todas parten "
        "de 100 y la nota de liderazgo sólo indica quién ha destacado dentro del grupo elegido."
    )
    if len(prepared) < 2:
        st.info(
            "En «Analizar», selecciona al menos dos favoritas en «Actualizar datos» "
            "para utilizar el comparador."
        )
        return

    groups = _comparison_groups(
        prepared,
        private_favorites,
        group_favorites,
        fundamental_results,
    )
    seeded_tickers = [
        ticker
        for ticker in st.session_state.pop("_comparison_seed_tickers", [])
        if ticker in prepared
    ][:10]
    if len(seeded_tickers) >= 2 and "Todas las cargadas" in groups:
        st.session_state["comparison_group"] = "Todas las cargadas"
        st.session_state["comparison_tickers_Todas las cargadas"] = seeded_tickers
    group_name = st.selectbox(
        "Sector o grupo",
        options=list(groups),
        key="comparison_group",
        help=(
            "Los sectores proceden de los datos empresariales y las etiquetas son las "
            "categorías que habéis guardado en favoritos."
        ),
    )
    available = groups[group_name]
    selection_key = f"comparison_tickers_{group_name}"
    previous_selection = st.session_state.get(selection_key, [])
    valid_previous = [ticker for ticker in previous_selection if ticker in available]
    default_selection = valid_previous or available[: min(5, len(available))]
    selected_tickers = st.multiselect(
        "Empresas que quieres comparar",
        options=available,
        default=default_selection,
        max_selections=10,
        key=selection_key,
    )
    horizon = st.segmented_control(
        "Horizonte",
        options=list(HORIZON_SESSIONS),
        default="6 meses",
        key="comparison_horizon",
    )
    if len(selected_tickers) < 2:
        st.info("Elige entre 2 y 10 empresas.")
        return

    try:
        comparison = compare_sector(
            prepared,
            selected_tickers,
            horizon_label=str(horizon or "6 meses"),
        )
    except ValueError as exc:
        st.warning(str(exc))
        return

    st.plotly_chart(
        normalized_comparison_chart(
            comparison.normalized_prices,
            title=f"Evolución comparable · {comparison.horizon_label}",
        ),
        width="stretch",
        config=PLOTLY_CONFIG,
    )
    st.caption(
        "100 es el punto de partida común. Un valor de 118 significa una subida del "
        "18% desde ese comienzo; no significa que la acción cueste 118."
    )

    metrics = comparison.metrics.copy()
    metrics["company_score"] = [
        fundamental_results.get(ticker).score
        if fundamental_results.get(ticker)
        else None
        for ticker in metrics.index
    ]
    metrics["valuation_score"] = [
        valuation_results.get(ticker).score
        if valuation_results.get(ticker)
        else None
        for ticker in metrics.index
    ]
    metrics["risk_score"] = [
        risk_results.get(ticker).score if risk_results.get(ticker) else None
        for ticker in metrics.index
    ]
    leader = str(metrics.index[0])
    leader_score = metrics.iloc[0]["leadership_score"]
    st.success(
        f"**Líder bursátil del grupo: {leader} ({leader_score:.0f}/100).** "
        "Ha combinado mejor comportamiento relativo y control de caídas entre las "
        "seleccionadas; no implica que esté barata ni que vaya a seguir liderando."
    )

    visible_metrics = metrics.reset_index().rename(
        columns={
            "ticker": "Ticker",
            "leadership_score": "Liderazgo",
            "horizon_return_pct": f"Rentabilidad {comparison.horizon_label}",
            "return_1m_pct": "1 mes",
            "return_3m_pct": "3 meses",
            "return_6m_pct": "6 meses",
            "return_1y_pct": "1 año",
            "annualized_volatility_pct": "Volatilidad",
            "max_drawdown_pct": "Peor caída",
            "distance_high_pct": "Desde máximo anual",
            "company_score": "Empresa",
            "valuation_score": "Valoración",
            "risk_score": "Riesgo controlado",
        }
    )
    st.caption("Pulsa una fila para abrir el análisis completo de la empresa.")
    render_ticker_dataframe(
        visible_metrics.loc[
            :,
            [
                "Ticker",
                "Liderazgo",
                f"Rentabilidad {comparison.horizon_label}",
                "1 mes",
                "3 meses",
                "6 meses",
                "1 año",
                "Volatilidad",
                "Peor caída",
                "Desde máximo anual",
                "Empresa",
                "Valoración",
                "Riesgo controlado",
            ],
        ],
        key="sector_comparison_companies",
        column_config={
            "Liderazgo": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%d"
            ),
            "Empresa": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%d"
            ),
            "Valoración": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%d"
            ),
            "Riesgo controlado": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%d"
            ),
            **{
                column: st.column_config.NumberColumn(format="%+.1f%%")
                for column in [
                    f"Rentabilidad {comparison.horizon_label}",
                    "1 mes",
                    "3 meses",
                    "6 meses",
                    "1 año",
                    "Peor caída",
                    "Desde máximo anual",
                ]
            },
            "Volatilidad": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )

    chart_col, explanation_col = st.columns([3, 2])
    with chart_col:
        st.plotly_chart(
            risk_return_chart(
                comparison.metrics,
                horizon_label=comparison.horizon_label,
            ),
            width="stretch",
            config=PLOTLY_CONFIG,
        )
    with explanation_col:
        st.markdown("#### Cómo interpretarlo")
        st.markdown(
            """
            - **Arriba e izquierda:** mejor rentabilidad con movimientos más contenidos.
            - **Arriba e derecha:** más rentabilidad, pero también más variación y riesgo.
            - **Abajo:** ha perdido terreno durante el periodo elegido.
            - **Liderazgo:** combina 3, 6 y 12 meses con la caída máxima anual.

            Calidad, valoración y riesgo permanecen separados para que una subida reciente
            no convierta automáticamente una empresa cara o débil en una buena candidata.
            """
        )

    with st.expander("Ver si estas empresas se mueven casi igual"):
        st.plotly_chart(
            correlation_heatmap(comparison.correlations),
            width="stretch",
            config=PLOTLY_CONFIG,
        )
        st.caption(
            "Cerca de 1 significa movimientos diarios muy parecidos; cerca de 0, poca "
            "relación. La correlación histórica puede cambiar y no garantiza diversificación."
        )


def render_saved_analysis_history(journal: object) -> None:
    st.subheader("Historial de análisis guardados")
    st.write(
        "Comprueba cómo han cambiado el precio y las notas desde cada revisión. "
        "Al abrir el detalle de una empresa se guarda como máximo una fotografía "
        "automática por día y combinación de notas. También puedes añadir notas manuales. "
        "Es un seguimiento de decisiones, no un registro de operaciones."
    )
    try:
        snapshots = journal.list_analysis_snapshots()
    except JournalStorageError as exc:
        st.error(str(exc))
        st.info(
            "Si es la primera vez que utilizas esta función, aplica la versión actual "
            "de supabase/schema.sql."
        )
        return
    if snapshots.empty:
        st.info(
            "Todavía no hay análisis guardados. Abre el detalle de una empresa: "
            "la primera revisión del día se registrará automáticamente."
        )
        return

    tickers = sorted(snapshots["ticker"].dropna().astype(str).unique())
    selected_ticker = st.selectbox(
        "Empresa",
        tickers,
        key="saved_history_ticker",
    )
    selected = snapshots.loc[snapshots["ticker"] == selected_ticker].copy()
    selected["analyzed_at"] = pd.to_datetime(
        selected["analyzed_at"], errors="coerce"
    )
    selected = selected.sort_values("analyzed_at")
    action_col, count_col = st.columns([2, 3])
    action_col.button(
        f"Abrir análisis actualizado de {selected_ticker}",
        type="primary",
        width="stretch",
        on_click=_open_ticker_analysis,
        args=(selected_ticker,),
    )
    count_col.info(f"{len(selected)} revisiones guardadas de {selected_ticker}.")

    evolution = selected.set_index("analyzed_at")[
        ["opportunity_score", "entry_score", "company_score"]
    ].rename(
        columns={
            "opportunity_score": "Oportunidad",
            "entry_score": "Entrada",
            "company_score": "Empresa",
        }
    )
    st.line_chart(evolution, height=330)

    visible = selected.sort_values("analyzed_at", ascending=False).rename(
        columns={
            "id": "ID",
            "analyzed_at": "Fecha",
            "price": "Precio",
            "opportunity_score": "Oportunidad",
            "company_score": "Empresa",
            "entry_score": "Entrada",
            "valuation_score": "Valoración",
            "risk_score": "Riesgo",
            "opportunity_label": "Lectura conjunta",
            "entry_label": "Lectura entrada",
            "expected_return_pct": "Retorno histórico",
            "positive_rate_pct": "Casos positivos",
            "expected_price": "Precio estadístico",
            "horizon_days": "Sesiones",
            "note": "Nota personal",
        }
    )
    st.dataframe(
        visible.loc[
            :,
            [
                "ID",
                "Fecha",
                "Precio",
                "Oportunidad",
                "Empresa",
                "Entrada",
                "Valoración",
                "Riesgo",
                "Lectura conjunta",
                "Lectura entrada",
                "Retorno histórico",
                "Casos positivos",
                "Precio estadístico",
                "Sesiones",
                "Nota personal",
            ],
        ],
        width="stretch",
        hide_index=True,
        column_config={
            "Precio": st.column_config.NumberColumn(format="%.2f"),
            "Precio estadístico": st.column_config.NumberColumn(format="%.2f"),
            "Retorno histórico": st.column_config.NumberColumn(format="%+.1f%%"),
            "Casos positivos": st.column_config.NumberColumn(format="%.1f%%"),
            **{
                column: st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                )
                for column in [
                    "Oportunidad",
                    "Empresa",
                    "Entrada",
                    "Valoración",
                    "Riesgo",
                ]
            },
        },
    )

    with st.expander("Eliminar una fotografía guardada"):
        ids = selected.sort_values("analyzed_at", ascending=False)["id"].astype(int)
        labels = {
            int(row.id): (
                f"{pd.Timestamp(row.analyzed_at).date()} · "
                f"entrada {int(row.entry_score)}/100"
            )
            for row in selected.itertuples(index=False)
        }
        snapshot_id = st.selectbox(
            "Revisión",
            options=ids.tolist(),
            format_func=lambda value: labels.get(value, str(value)),
            key="delete_snapshot_id",
        )
        if st.button("Eliminar revisión", key="delete_snapshot_button"):
            try:
                journal.delete_analysis_snapshot(int(snapshot_id))
            except JournalStorageError as exc:
                st.error(str(exc))
            else:
                st.success("La revisión se ha eliminado.")
                st.rerun()


def render_email_alert_settings(journal: object) -> None:
    """Preferencias privadas y prueba controlada del canal de correo."""

    st.subheader("Alertas por correo")
    st.write(
        "Recibe un único resumen cuando una favorita pase a entrada interesante o "
        "cuando una posición registrada cambie a reducir o vender."
    )
    st.info(
        "Una alerta invita a revisar los datos; no compra, vende ni envía órdenes a "
        "ningún broker. El precio puede cambiar antes de que leas el mensaje."
    )
    try:
        preferences = journal.get_alert_preferences()
    except (JournalStorageError, AttributeError) as exc:
        st.error(f"No se pudieron cargar las alertas: {exc}")
        st.caption(
            "El administrador debe ejecutar la versión actual de supabase/schema.sql."
        )
        return

    with st.form("email_alert_preferences_form"):
        email_address = st.text_input(
            "Correo que recibirá los avisos",
            value=preferences.email,
            placeholder="tu.nombre@gmail.com",
            help="Sólo el backend privado puede leer esta dirección.",
        )
        enabled = st.toggle(
            "Activar mis alertas automáticas",
            value=preferences.enabled,
        )
        type_a, type_b, type_c = st.columns(3)
        with type_a:
            alert_buy = st.checkbox(
                "Entradas interesantes",
                value=preferences.alert_buy,
                help=(
                    "Avisa sobre favoritas que no están en tu cartera cuando su momento "
                    "técnico alcanza el mínimo elegido."
                ),
            )
        with type_b:
            alert_reduce = st.checkbox(
                "Revisar / reducir",
                value=preferences.alert_reduce,
                help="Avisa cuando una posición pierde fuerza o la media intermedia.",
            )
        with type_c:
            alert_sell = st.checkbox(
                "Posible salida",
                value=preferences.alert_sell,
                help="Avisa al perder la tendencia larga o activar el stop configurado.",
            )
        minimum_buy_score = st.slider(
            "Puntuación mínima para avisar de una posible entrada",
            min_value=55,
            max_value=85,
            value=min(max(preferences.minimum_buy_score, 55), 85),
            step=5,
            help=(
                "65 incluye entradas interesantes; 75 es más selectivo y normalmente "
                "produce menos avisos."
            ),
        )
        include_group = st.checkbox(
            "Incluir favoritas y posiciones de la cartera del grupo",
            value=preferences.include_group,
        )
        only_changes = st.checkbox(
            "Avisar sólo cuando cambie la señal",
            value=preferences.only_changes,
            help="Evita recibir el mismo aviso todos los días.",
        )
        saved = st.form_submit_button(
            "Guardar mis preferencias",
            type="primary",
            width="stretch",
        )
    if saved:
        try:
            values = normalize_alert_preferences(
                owner=preferences.owner,
                email=email_address,
                enabled=enabled,
                alert_buy=alert_buy,
                alert_reduce=alert_reduce,
                alert_sell=alert_sell,
                include_group=include_group,
                minimum_buy_score=minimum_buy_score,
                only_changes=only_changes,
            )
            journal.save_alert_preferences(values)
            st.success("Preferencias guardadas.")
            st.rerun()
        except (ValueError, JournalStorageError) as exc:
            st.error(str(exc))

    mail_configured = load_email_config().configured
    if mail_configured:
        st.success(
            "El canal de correo está conectado. La revisión automática se ejecuta "
            "por la mañana, de lunes a viernes."
        )
    else:
        st.warning(
            "Las preferencias pueden guardarse, pero el administrador todavía debe "
            "conectar la cuenta Gmail del proyecto."
        )
    test_disabled = not bool(preferences.email) or not mail_configured
    if st.button(
        "Enviarme un correo de prueba",
        disabled=test_disabled,
        width="stretch",
    ):
        try:
            send_test_email(preferences.email)
            st.success("Correo enviado. Si no aparece, revisa también la carpeta de spam.")
        except (EmailConfigurationError, EmailDeliveryError) as exc:
            st.error(str(exc))

    with st.expander("¿Qué empresas se revisan y cuándo se avisa?"):
        st.markdown(
            """
            - Tus favoritas privadas y todas tus posiciones abiertas.
            - Opcionalmente, las favoritas y posiciones de la cartera del grupo.
            - **Compra:** sólo para empresas que todavía no figuran en tu cartera.
            - **Reducir o vender:** sólo para posiciones registradas, utilizando su
              coste medio para comprobar también el stop loss.
            - Un mismo estado no vuelve a enviarse hasta que la señal cambie.

            Los precios diarios gratuitos pueden contener retrasos, huecos o ajustes.
            Comprueba siempre la cotización y las noticias antes de actuar.
            """
        )
    try:
        states = journal.list_alert_states()
    except (JournalStorageError, AttributeError):
        states = pd.DataFrame()
    if not states.empty:
        st.caption("Últimas empresas revisadas automáticamente")
        visible = states.rename(
            columns={
                "ticker": "Ticker",
                "entry_score": "Entrada",
                "entry_label": "Momento",
                "position_label": "Si ya la tienes",
                "price": "Último cierre",
                "evaluated_at": "Revisada",
            }
        )
        render_ticker_dataframe(
            visible.loc[
                :,
                [
                    "Ticker",
                    "Entrada",
                    "Momento",
                    "Si ya la tienes",
                    "Último cierre",
                    "Revisada",
                ],
            ],
            key="alert_states_companies",
        )


def render_methodology() -> None:
    st.subheader("Guía sencilla")
    st.markdown(
        """
        **Cómo leer las señales**

        - **Entrada fuerte (75+):** configuración técnica especialmente completa; no garantiza una subida.
        - **Entrada interesante (65+):** reúne suficiente fuerza para estudiarla con más detalle.
        - **Vigilancia (55+):** promete, pero todavía le falta confirmación.
        - **Esperar:** puede ser una buena empresa, pero el precio está demasiado acelerado.
        - **Mantener / Reducir / Vender:** son lecturas separadas para quien ya posee la acción.

        **Diccionario rápido**

        - **Tendencia:** dirección general del precio durante varios meses.
        - **Impulso:** velocidad con la que está subiendo o bajando.
        - **Nuevo máximo o ruptura:** supera una zona que antes no podía superar.
        - **Actividad o volumen:** cuánto se negocia comparado con un día normal.
        - **Peor caída temporal:** mayor pérdida sufrida desde un máximo anterior.
        - **Oportunidad conjunta:** combina calidad, valoración, momento, fortaleza
          frente al mercado y riesgo. No sustituye las cinco notas individuales.
        - **Confianza de datos:** indica cuántas métricas están disponibles; no es
          la probabilidad de ganar dinero.
        - **Cambio de acción:** descuenta una comisión en euros al vender y otra al
          comprar. Si cambia la moneda utiliza el último tipo de referencia del BCE.
        - **R:** riesgo inicial por acción. Con un stop del 8%, 1R equivale al 8%
          del precio medio de compra.
        - **Beneficio esperado:** mediana histórica de señales parecidas, no una
          rentabilidad garantizada.
        - **Objetivo 30+ días:** frecuencia histórica con la que una nueva señal,
          mantenida durante todo el periodo, terminó en positivo o superó la tasa
          anual equivalente de Segofactoring y Civislend. Exige 30 casos para
          considerar suficiente la muestra.
        - **Crecimiento y momentum:** estrategia anexa para una parte del dinero
          mensual nuevo. Mantiene separadas las notas de crecimiento empresarial,
          fortaleza del precio y contexto de mercado/riesgo. Adapta el tamaño y las
          comprobaciones a tecnología, consumo, energía, biotecnología, industria,
          finanzas y ETF sin modificar Sego, Civislend ni el score principal.
        - **Stop por volatilidad:** distancia orientativa calculada con ATR y un
          multiplicador sectorial. Sirve para dimensionar; un gap puede ejecutarse
          peor y en biotecnología no protege de un resultado clínico adverso.
        - **Liderazgo sectorial:** posición relativa dentro de las empresas elegidas,
          basada en rentabilidad y caídas; no mide monopolio ni garantiza continuidad.

        La nota de empresa utiliza rentabilidad, crecimiento, deuda y caja. La valoración
        examina múltiplos y generación de caja. La nota de entrada combina tendencia,
        impulso de uno y tres meses, MACD, rupturas, cercanía a máximos, volumen y calidad
        del precio. La fortaleza relativa compara la acción con el mercado y el sector.
        Ninguna de estas notas es una probabilidad calibrada de beneficio.

        **Controles contra sesgos incluidos**

        - Los indicadores sólo usan datos presentes y pasados; no se rellenan hacia atrás.
        - La señal del cierre se ejecuta en la siguiente apertura.
        - El backtest incluye comisión, deslizamiento y gaps a través del stop.
        - Las estimaciones de retorno usan eventos no solapados y no se muestran
          con menos de ocho casos comparables.
        - La calibración de 30+ días compra en la apertura siguiente, descuenta
          comisiones fijas y muestra un intervalo de incertidumbre del 95%.

        **Limitaciones que siguen abiertas**

        - *Sobreoptimización:* cambiar muchos parámetros hasta maximizar el pasado degrada la validez futura.
          Reserva un periodo fuera de muestra y usa walk-forward antes de extraer conclusiones.
        - *Sesgo de supervivencia:* una lista actual de tickers omite empresas deslistadas o quebradas.
        - *Look-ahead e intradía:* con velas diarias no conocemos la secuencia exacta entre máximo y mínimo.
          El trailing stop actualizado al cierre se aplica desde la sesión siguiente.
        - Yahoo/yfinance continúa siendo la fuente práctica de precios y contexto. Para
          tickers estadounidenses se intentan contrastar las cuentas con SEC EDGAR.
        - La comparación opcional con Alpha Vantage detecta diferencias, pero no convierte
          los datos gratuitos en cotizaciones profesionales en tiempo real.
        - MSN Dinero abre una segunda lectura basada en datos de LSEG, noticias y
          expectativas. Se consulta manualmente y no modifica el score ni el backtest.
        - Dividendos, fiscalidad, préstamos de valores y el coste de cambio del broker no están modelados.
        - Los tipos del BCE son referencias informativas y pueden diferir del cambio real del broker.
        - La primera calibración de 30+ días mide cada cotización en su propia moneda;
          para una comparación estricta con objetivos en euros falta incorporar el
          histórico diario de divisas.
        - Los niveles de venta y stops son referencias: una orden puede ejecutarse
          a otro precio durante gaps o mercados volátiles.
        - Las correlaciones y el liderazgo dependen del periodo y del grupo elegido;
          pueden cambiar cuando cambia el régimen de mercado.
        """
    )


def main() -> None:
    apply_visual_theme()
    authenticated_user = require_login()
    accounts = load_auth_accounts()
    try:
        journal = create_journal(authenticated_user.username)
        group_journal = create_journal(GROUP_PORTFOLIO_OWNER)
    except JournalStorageError as exc:
        st.error(str(exc))
        st.stop()
    render_app_header(authenticated_user)
    requested_main_navigation = st.session_state.pop(
        "_requested_main_navigation", None
    )
    if requested_main_navigation in MAIN_OPTIONS:
        st.session_state["main_navigation"] = requested_main_navigation
    requested_analysis_navigation = st.session_state.pop(
        "_requested_analysis_navigation", None
    )
    if st.session_state.get("main_navigation") not in MAIN_OPTIONS:
        st.session_state["main_navigation"] = "Inicio"
    requested_route = requested_analysis_navigation or st.session_state.get(
        "analysis_navigation"
    )
    if requested_route in LEGACY_ANALYSIS_ROUTES:
        parent, child_key, child_value = LEGACY_ANALYSIS_ROUTES[str(requested_route)]
        st.session_state["analysis_navigation"] = parent
        if child_key and child_value:
            st.session_state[child_key] = child_value
    elif requested_route in ANALYSIS_OPTIONS:
        st.session_state["analysis_navigation"] = requested_route
    else:
        st.session_state["analysis_navigation"] = "Radar"
    if st.session_state.get("analysis_strategy_navigation") not in STRATEGY_OPTIONS:
        st.session_state["analysis_strategy_navigation"] = STRATEGY_OPTIONS[0]
    if st.session_state.get("analysis_tool_navigation") not in ANALYSIS_TOOL_OPTIONS:
        st.session_state["analysis_tool_navigation"] = ANALYSIS_TOOL_OPTIONS[0]

    # La navegación se dibuja antes de cualquier descarga o cálculo. Así permanece
    # estable y responde al instante incluso cuando actualizar el mercado tarda.
    selected_section = st.segmented_control(
        "Navegación principal",
        MAIN_OPTIONS,
        key="main_navigation",
        required=True,
        label_visibility="collapsed",
        format_func=lambda value: {
            "Inicio": "⌂  Inicio",
            "Analizar": "↗  Analizar",
            "Favoritos": "♡  Favoritos",
            "Carteras": "▱  Cartera",
            "Más": "···  Más",
        }[value],
    )
    analysis_section = ""
    analysis_detail = ""
    favorite_view = ""
    portfolio_section = ""
    more_section = ""
    if selected_section == "Analizar":
        analysis_section = render_subnavigation(
            "Qué quieres hacer",
            ANALYSIS_OPTIONS,
            key="analysis_navigation",
            format_func=lambda value: ANALYSIS_LABELS[value],
            on_change=_reset_analysis_company_picker,
        )
        if analysis_section == "Estrategias":
            analysis_detail = render_subnavigation(
                "Estrategia",
                STRATEGY_OPTIONS,
                key="analysis_strategy_navigation",
            )
        elif analysis_section == "Más análisis":
            analysis_detail = render_subnavigation(
                "Herramienta",
                ANALYSIS_TOOL_OPTIONS,
                key="analysis_tool_navigation",
            )
    elif selected_section == "Favoritos":
        favorite_options = ["Mis listas", "Añadir empresa"]
        if st.session_state.pop("_return_to_favorite_lists", False):
            st.session_state["favorite_view"] = "Mis listas"
        if st.session_state.get("favorite_view") not in favorite_options:
            st.session_state["favorite_view"] = "Mis listas"
        favorite_view = render_subnavigation(
            "Favoritos",
            favorite_options,
            key="favorite_view",
            format_func=lambda value: (
                "♡ Mis listas" if value == "Mis listas" else "+ Añadir empresa"
            ),
        )
    elif selected_section == "Carteras":
        portfolio_options = ["Privada", "Grupo"]
        if st.session_state.get("portfolio_navigation") not in portfolio_options:
            st.session_state["portfolio_navigation"] = "Privada"
        portfolio_section = render_subnavigation(
            "Cartera",
            portfolio_options,
            key="portfolio_navigation",
            format_func=lambda value: (
                "▱ Mi cartera" if value == "Privada" else "◎ Cartera del grupo"
            ),
        )
    elif selected_section == "Más":
        more_options = ["Alertas por correo"]
        if authenticated_user.is_admin:
            more_options.append("Administración")
        more_options.append("Guía y riesgos")
        if st.session_state.get("more_navigation") not in more_options:
            st.session_state["more_navigation"] = "Guía y riesgos"
        more_section = render_subnavigation(
            "Más secciones",
            more_options,
            key="more_navigation",
            format_func=lambda value: {
                "Alertas por correo": "◌ Alertas",
                "Administración": "⚙ Administración",
                "Guía y riesgos": "? Guía y riesgos",
            }.get(value, value),
        )

    layout_analysis_section = (
        analysis_detail if analysis_section == "Más análisis" else analysis_section
    )
    apply_section_layout(str(selected_section), layout_analysis_section)
    favorite_storage_error = ""
    try:
        private_favorites = journal.list_favorites()
        group_favorites = group_journal.list_favorites()
    except JournalStorageError as exc:
        private_favorites = pd.DataFrame()
        group_favorites = pd.DataFrame()
        favorite_storage_error = str(exc)
    favorite_tickers, favorite_labels = build_favorite_catalog(
        private_favorites,
        group_favorites,
    )
    (
        tickers,
        start,
        end,
        auto_adjust,
        alpha_vantage_key,
        strategy,
        backtest,
        load_clicked,
    ) = build_sidebar(favorite_tickers, favorite_labels)
    if favorite_storage_error:
        st.sidebar.warning(
            "Los favoritos todavía no están disponibles. Hay que aplicar la "
            "actualización de la base de datos."
        )
    try:
        strategy.validate()
        backtest.validate()
    except ValueError as exc:
        st.error(f"Configuración inválida: {exc}")
        st.stop()

    requested_pending_ticker = str(
        st.session_state.get("_pending_analysis_ticker", "")
    ).strip().upper()
    pending_analysis_ticker = (
        resolve_analysis_ticker(requested_pending_ticker)
        if requested_pending_ticker
        else ""
    )
    requested_active_ticker = str(
        st.session_state.get("analysis_ticker", "")
    ).strip().upper()
    active_analysis_ticker = (
        resolve_analysis_ticker(requested_active_ticker)
        if requested_active_ticker
        else ""
    )
    growth_scan_tickers = [
        resolve_analysis_ticker(str(ticker))
        for ticker in st.session_state.pop("_growth_scan_tickers", [])
        if str(ticker).strip()
    ]
    growth_scan_tickers = list(dict.fromkeys(growth_scan_tickers))[:200]
    if requested_pending_ticker and requested_pending_ticker != pending_analysis_ticker:
        st.info(
            f"{requested_pending_ticker} es el símbolo mostrado por el bróker; "
            f"el análisis utilizará {pending_analysis_ticker}."
        )
    portfolio_refresh_requested = bool(
        st.session_state.pop("_portfolio_market_refresh_requested", False)
    )
    auto_refresh_key = f"_portfolio_auto_refresh_done_{authenticated_user.username}"
    portfolio_auto_refresh = (
        selected_section in {"Inicio", "Carteras"}
        and not bool(st.session_state.get(auto_refresh_key, False))
    )
    if portfolio_auto_refresh:
        st.session_state[auto_refresh_key] = True

    should_load_market = bool(
        load_clicked
        or pending_analysis_ticker
        or growth_scan_tickers
        or portfolio_refresh_requested
        or portfolio_auto_refresh
    )
    if should_load_market:
        held_tickers: list[str] = []
        owners_to_load = [authenticated_user.username, GROUP_PORTFOLIO_OWNER]
        if authenticated_user.is_admin and load_clicked:
            owners_to_load.extend(managed_usernames(accounts))
        for owner in dict.fromkeys(owners_to_load):
            try:
                owner_journal = (
                    journal
                    if owner == authenticated_user.username
                    else group_journal
                    if owner == GROUP_PORTFOLIO_OWNER
                    else create_journal(owner)
                )
            except JournalStorageError as exc:
                st.sidebar.warning(
                    f"No se pudo consultar la cartera de {owner}: {exc}"
                )
                continue
            held_tickers.extend(_portfolio_tracking_tickers(owner_journal))
        held_tickers = list(dict.fromkeys(held_tickers))
        base_tickers = (
            tickers
            if load_clicked or pending_analysis_ticker
            else []
        )
        tickers_to_load = (
            growth_scan_tickers
            if growth_scan_tickers
            else analysis_refresh_tickers(
                base_tickers,
                held_tickers,
                pending_ticker=pending_analysis_ticker,
                active_ticker=active_analysis_ticker,
            )
        )
        if tickers_to_load:
            deep_tickers = (
                {
                    *(growth_scan_tickers or tickers),
                    *(
                        [pending_analysis_ticker]
                        if pending_analysis_ticker
                        else []
                    ),
                    *(
                        [active_analysis_ticker]
                        if active_analysis_ticker
                        else []
                    ),
                }
                if load_clicked or pending_analysis_ticker or growth_scan_tickers
                else set()
            )
            load_market_data(
                tickers_to_load,
                start,
                end,
                auto_adjust,
                alpha_vantage_key,
                fundamental_tickers=deep_tickers,
                merge_existing=bool(
                    growth_scan_tickers
                    or portfolio_refresh_requested
                    or portfolio_auto_refresh
                ),
            )
        if growth_scan_tickers:
            st.session_state["_growth_scan_completed"] = len(growth_scan_tickers)
        st.session_state.pop("_pending_analysis_ticker", None)
    for error in st.session_state.get("download_errors", []):
        st.warning(error)

    raw_data: dict[str, pd.DataFrame] = st.session_state.get("market_data", {})
    raw_fundamentals: dict[str, dict[str, object]] = st.session_state.get(
        "fundamental_data", {}
    )
    reference_data: dict[str, pd.DataFrame] = st.session_state.get("reference_data", {})
    price_verifications: dict[str, PriceVerification] = st.session_state.get(
        "price_verifications", {}
    )
    fx_snapshot: FxSnapshot = st.session_state.get(
        "fx_snapshot",
        FxSnapshot(as_of=None, rates_per_eur={"EUR": 1.0}),
    )
    (
        prepared,
        summary,
        fundamental_results,
        valuation_results,
        relative_results,
        risk_results,
        opportunity_results,
    ) = (
        prepare_data(raw_data, raw_fundamentals, reference_data, strategy)
        if raw_data
        else ({}, [], {}, {}, {}, {}, {})
    )
    requested_focus_value = str(
        st.session_state.get("_requested_analysis_ticker", "")
    ).strip()
    requested_focus_ticker = (
        resolve_analysis_ticker(requested_focus_value)
        if requested_focus_value
        else ""
    )
    if requested_focus_ticker and requested_focus_ticker in prepared:
        # Se aplica antes de crear el selectbox de detalle. Así el acceso desde
        # Favoritos no intenta modificar un widget ya instanciado.
        st.session_state["analysis_ticker"] = requested_focus_ticker
        st.session_state.pop("_requested_analysis_ticker", None)
    elif requested_focus_ticker and requested_focus_ticker not in raw_data:
        # La descarga falló o el proveedor no reconoce la cotización. El radar
        # conserva la favorita y el aviso de descarga explica cómo corregirla.
        st.session_state.pop("_requested_analysis_ticker", None)

    if selected_section == "Inicio":
        render_home(
            authenticated_user,
            journal,
            group_journal,
            prepared,
            summary,
            fx_snapshot,
            private_favorites,
            group_favorites,
            section="Hoy",
        )

    elif selected_section == "Analizar":
        if analysis_section == "Radar":
            render_opportunities_page(
                raw_data,
                prepared,
                summary,
                strategy,
                backtest,
                fundamental_results,
                valuation_results,
                relative_results,
                risk_results,
                opportunity_results,
                raw_fundamentals,
                price_verifications,
                journal,
                favorite_tickers,
                include_company_detail=False,
            )
        elif analysis_section == "Empresa":
            render_page_intro(
                "ANÁLISIS DE EMPRESA",
                "Entender una empresa",
                "Busca una sola vez y revisa qué apoya la inversión, qué puede fallar "
                "y si el precio ofrece una entrada razonable.",
            )
            render_analysis_company_picker(
                favorite_tickers,
                favorite_labels,
                journal,
                raw_fundamentals,
            )
            render_company_analysis_page(
                prepared,
                strategy,
                backtest,
                fundamental_results,
                valuation_results,
                relative_results,
                risk_results,
                opportunity_results,
                raw_fundamentals,
                price_verifications,
                journal,
            )
        elif analysis_section == "Estrategias" and analysis_detail == "Crecimiento":
            render_growth_momentum_page(
                prepared,
                raw_fundamentals,
                reference_data,
                relative_results,
                risk_results,
                fx_snapshot,
                journal,
                private_favorites,
                group_favorites,
                authenticated_user.username,
            )
        elif (
            analysis_section == "Estrategias"
            and analysis_detail == "Resultado tras 30+ días"
        ):
            render_long_horizon_calibration(
                prepared,
                summary,
                backtest,
                fundamental_results,
            )
        elif analysis_detail == "Comparar empresas":
            render_page_intro(
                "MÁS ANÁLISIS",
                "Comparar empresas",
                "Compara negocios semejantes; el precio nominal de una acción no indica "
                "si está más barata o es mejor que otra.",
            )
            render_sector_comparison(
                prepared,
                fundamental_results,
                valuation_results,
                risk_results,
                private_favorites,
                group_favorites,
            )
        elif analysis_detail == "Historial":
            render_page_intro(
                "MÁS ANÁLISIS",
                "Historial",
                "Consulta cómo cambiaron el precio y las notas entre revisiones; no es "
                "un registro de compras y ventas.",
            )
            render_saved_analysis_history(journal)
        elif analysis_detail == "Plan de capital":
            render_capital_projection_page(authenticated_user.username)
        elif analysis_detail == "Prueba con el pasado" and not prepared:
            st.info("Actualiza empresas antes de ejecutar una prueba histórica.")
        elif analysis_detail == "Prueba con el pasado":
            render_page_intro(
                "MÁS ANÁLISIS",
                "Prueba con el pasado",
                "Simula las reglas sobre datos históricos. Sirve para detectar una "
                "estrategia frágil; no predice la próxima subida.",
            )
            preferred_backtest = str(
                st.session_state.get("analysis_ticker", "") or ""
            )
            if preferred_backtest not in prepared:
                preferred_backtest = next(iter(prepared))
            if st.session_state.get("backtest_select") not in prepared:
                st.session_state["backtest_select"] = preferred_backtest
            selected_backtest = st.selectbox(
                "Empresa para la prueba histórica",
                list(prepared),
                key="backtest_select",
                on_change=_sync_analysis_ticker,
                args=("backtest_select",),
            )
            st.info(
                "El cálculo se ejecuta automáticamente con el historial ya descargado. "
                "Cambiar de pestaña no vuelve a consultar el mercado."
            )
            render_backtest(
                selected_backtest,
                prepared[selected_backtest],
                strategy,
                backtest,
            )

    elif selected_section == "Favoritos":
        if not persistent_journal_enabled():
            st.warning(
                "Los favoritos necesitan almacenamiento persistente. En local se "
                "guardan en SQLite; en la web hay que conectar Supabase."
            )
        elif favorite_storage_error:
            st.error(favorite_storage_error)
            st.info(
                "Ejecuta la versión actual de supabase/schema.sql para crear la "
                "tabla de favoritos."
            )
        else:
            render_favorites_manager(
                journal,
                group_journal,
                private_favorites,
                group_favorites,
                actor_username=authenticated_user.username,
                is_admin=authenticated_user.is_admin,
                favorite_view=favorite_view,
            )

    elif selected_section == "Carteras":
        if not persistent_journal_enabled():
            st.warning("Las carteras necesitan almacenamiento persistente.")
        else:
            target_journal = journal if portfolio_section == "Privada" else group_journal
            try:
                render_journal(
                    prepared,
                    fundamental_results,
                    opportunity_results,
                    strategy,
                    fx_snapshot,
                    target_journal,
                    view_key="private" if portfolio_section == "Privada" else "group",
                    title=(
                        "Mi cartera privada"
                        if portfolio_section == "Privada"
                        else "Cartera del grupo"
                    ),
                    description=(
                        "Sólo tú puedes consultar y modificar estos movimientos."
                        if portfolio_section == "Privada"
                        else (
                            "Compartida entre Luci, Fer, Xavi y ddriu. Todos pueden "
                            "verla y registrar decisiones."
                        )
                    ),
                    actor_username=authenticated_user.username,
                    shared=portfolio_section == "Grupo",
                    can_delete_all=authenticated_user.is_admin,
                )
            except JournalStorageError as exc:
                st.error(str(exc))
                st.info("Comprueba la conexión con Supabase y vuelve a intentarlo.")

    elif selected_section == "Más":
        if more_section == "Alertas por correo":
            if persistent_journal_enabled():
                render_email_alert_settings(journal)
            else:
                st.warning("Las alertas necesitan almacenamiento persistente.")
        elif more_section == "Administración":
            if persistent_journal_enabled():
                try:
                    render_admin_panel(
                        accounts,
                        prepared,
                        fx_snapshot,
                        authenticated_user.username,
                    )
                except JournalStorageError as exc:
                    st.error(f"No se pudo cargar el panel administrador: {exc}")
            else:
                st.warning("El panel multiusuario necesita almacenamiento persistente.")
        else:
            render_methodology()

    st.divider()
    st.caption(
        "Señales probabilísticas con fines informativos y educativos. No constituyen asesoramiento financiero "
        "ni garantizan resultados futuros."
    )


if __name__ == "__main__":
    main()
