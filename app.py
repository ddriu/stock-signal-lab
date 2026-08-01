"""Interfaz Streamlit de Stock Signal Lab.

Ejecutar con: ``streamlit run app.py``
"""

from __future__ import annotations

from datetime import date, timedelta
import html

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
from src.dashboard import build_position_dashboard
from src.data_loader import (
    DataDownloadError,
    TickerSearchResult,
    download_fundamental_snapshot,
    download_prices,
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
from src.navigation import analysis_refresh_tickers, sanitize_favorite_selection
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
from src.portfolio_snapshot import latest_portfolio_snapshot
from src.recommendations import (
    build_entry_guide,
    build_profit_taking_plan,
    historical_forward_return_study,
)
from src.risk import calculate_position_plan
from src.sector_comparison import HORIZON_SESSIONS, compare_sector
from src.segofactoring_import import (
    import_segofactoring_rows,
    parse_segofactoring_excel,
)
from src.signal_engine import add_signal_columns, evaluate_latest_signal
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
    risk_return_chart,
)


st.set_page_config(page_title="Stock Signal Lab", page_icon="📈", layout="wide")

MAIN_OPTIONS = ["Inicio", "Analizar", "Favoritos", "Carteras", "Más"]


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


def apply_section_layout(section: str) -> None:
    """Reserva la barra lateral completa para la zona que realmente la necesita."""

    if section == "Analizar":
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
    return list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))


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
    st.sidebar.markdown("## Configurar análisis")
    st.sidebar.caption(
        "Este panel sólo aparece en Analizar. Los cambios se aplican al pulsar "
        "«Actualizar análisis»."
    )
    selected_favorites = st.sidebar.multiselect(
        "Empresas que quieres analizar",
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
        manual_tickers = parse_tickers(
            st.text_area(
                "Símbolos bursátiles",
                "" if favorite_tickers else "AAPL, MSFT, SAN.MC",
                height=78,
                help=(
                    "Sólo es necesario si el buscador no encuentra la empresa. Ejemplos: "
                    "SAN.MC (Madrid), 7974.T (Tokio) o KAP.IL (Londres internacional)."
                ),
                key="manual_tickers",
            )
        )
    tickers = list(dict.fromkeys([*selected_favorites, *manual_tickers]))

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
    forward_horizon = st.sidebar.selectbox(
        "Horizonte de la estimación",
        options=[10, 20, 40, 60],
        index=[10, 20, 40, 60].index(int(defaults["forward_horizon"])),
        format_func=lambda value: f"{value} sesiones",
        key="cfg_forward_horizon",
    )
    initial_capital = st.sidebar.number_input(
        "Capital para simulaciones",
        min_value=100.0,
        value=10_000.0,
        step=1_000.0,
        key="cfg_initial_capital",
    )

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

    load_clicked = st.sidebar.button(
        "Actualizar análisis",
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
    downloaded: dict[str, pd.DataFrame] = {}
    fundamentals: dict[str, dict[str, object]] = {}
    verifications: dict[str, PriceVerification] = {}
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
                fundamentals[ticker] = {"symbol": ticker}
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
    references: dict[str, pd.DataFrame] = {}
    for symbol in sorted(reference_symbols.difference(downloaded)):
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
    st.session_state["quick_mode_tickers"] = sorted(set(tickers).difference(deep_tickers))
    st.session_state["download_errors"] = errors
    st.session_state.pop("backtest_result", None)


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


def render_backtest(ticker: str, frame: pd.DataFrame, strategy: StrategyConfig, settings: BacktestConfig) -> None:
    st.caption(
        "Esta prueba simula qué habría pasado aplicando las mismas reglas en el pasado. "
        "Incluye costes y límites de pérdida, pero el pasado no garantiza resultados futuros."
    )
    if st.button("Probar las reglas con datos pasados", type="primary"):
        try:
            with st.spinner("Simulando estrategia…"):
                result = run_backtest(frame, strategy, settings)
            st.session_state["backtest_result"] = result
            st.session_state["backtest_ticker"] = ticker
        except ValueError as exc:
            st.error(str(exc))
            return
    result: BacktestResult | None = st.session_state.get("backtest_result")
    if result is None or st.session_state.get("backtest_ticker") != ticker:
        st.info("Pulsa el botón para probar este ticker con datos pasados.")
        return
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


def render_private_investments(
    journal: object,
    *,
    actor_username: str,
    view_key: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Gestiona proyectos manuales de Civislend y Segofactoring para ddriu."""

    st.subheader("Mis cuentas y plataformas")
    st.caption(
        "Vista conjunta de MyInvestor, Trade Republic, Revolut, Segofactoring y Civislend. "
        "Los importes provisionales se pueden completar poco a poco."
    )
    try:
        investments = journal.list_private_investments()
    except JournalStorageError:
        st.error(
            "La tabla de inversiones privadas todavía no existe en Supabase. "
            "Ejecuta `supabase/migration_private_investments.sql` en SQL Editor."
        )
        return pd.DataFrame(), pd.DataFrame()
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

    if accounts.empty and hasattr(journal, "upsert_portfolio_account"):
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
                "Las cinco cuentas ya están creadas, pero sus valores están a cero hasta "
                "que introduzcas importes o proyectos."
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
        st.dataframe(
            snapshot_display.loc[
                :,
                [
                    "Plataforma", "Activo", "Ticker para analizar", "Tipo", "Bloque",
                    "Cantidad", "Moneda", "Valor €", "Coste estimado €",
                    "Resultado estimado €", "Rentabilidad estimada %", "Comentarios",
                ],
            ],
            width="stretch",
            hide_index=True,
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
    st.subheader(title)
    st.caption(description)
    st.caption(f"Almacenamiento: {getattr(journal, 'backend_name', 'diario')}")
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
    private_platforms_enabled = not shared and actor_username.strip().lower() == "ddriu"
    tab_labels = [
        "Posiciones analizadas",
        "Comprar / vender",
        "Evolución por años",
        "Comparar un cambio",
        "Historial",
    ]
    if private_platforms_enabled:
        tab_labels.append("Mis cuentas")
    journal_tabs = st.tabs(tab_labels)
    positions_tab, register_tab, evolution_tab, switch_tab, history_tab = journal_tabs[:5]
    private_tab = journal_tabs[5] if private_platforms_enabled else None

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
                st.dataframe(
                    pd.DataFrame(analysis_rows),
                    width="stretch",
                    hide_index=True,
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
                "Última actividad": kpis.latest_activity or "Sin operaciones",
            }
        )

    active_users = sum(
        1 for snapshot in snapshots.values() if len(snapshot["operations"]) > 0
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

    overview_tab, register_tab, detail_tab = st.tabs(
        ["Resumen de usuarios", "Añadir posición", "Detalle e historial"]
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
            st.dataframe(
                visible_dashboard,
                width="stretch",
                hide_index=True,
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
            f"Mostrando {first_visible}–{last_visible} de {len(filtered)}. "
            "Selecciona una fila para abrir el análisis."
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
        if not shared:
            visible = visible.drop(columns=["Añadida por"])
        table_event = st.dataframe(
            visible,
            width="stretch",
            height=min(820, 38 + 35 * len(visible)),
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key=f"favorite_table_{scope_key}_{page}",
            column_config={
                "Ticker": st.column_config.TextColumn(width="small"),
                "Empresa": st.column_config.TextColumn(width="large"),
                "Mercado": st.column_config.TextColumn(width="medium"),
                "Etiquetas": st.column_config.TextColumn(width="large"),
            },
        )
        selected_rows = list(getattr(table_event.selection, "rows", []))
        if selected_rows:
            selected_ticker = str(visible.iloc[selected_rows[0]]["Ticker"]).upper()
            open_label, open_action = st.columns([3, 1], vertical_alignment="center")
            open_label.caption(f"Has seleccionado {selected_ticker}.")
            open_action.button(
                f"Analizar {selected_ticker}",
                key=f"open_favorite_{scope_key}_{selected_ticker}",
                width="stretch",
                type="primary",
                on_click=_open_ticker_analysis,
                args=(selected_ticker,),
            )

    editable = favorites
    if shared and not can_delete_all:
        editable = favorites.loc[
            favorites["recorded_by"].fillna("").astype(str).str.lower()
            == actor_username.lower()
        ]
    if editable.empty:
        st.caption("Sólo puedes quitar del grupo las empresas que tú añadiste.")
        return

    options = editable["ticker"].astype(str).tolist()
    labels = {
        str(row.ticker): f"{row.name} ({row.ticker})"
        for row in editable.itertuples(index=False)
    }
    with st.expander("Editar etiquetas o quitar empresas"):
        selected = st.selectbox(
            "Empresa",
            options,
            format_func=lambda ticker: labels.get(ticker, ticker),
            key=f"edit_favorite_{scope_key}",
        )
        selected_row = editable.loc[editable["ticker"].astype(str) == selected].iloc[0]
        edited_tags = st.multiselect(
            "Etiquetas",
            FAVORITE_TAGS,
            default=favorite_tags_from_value(selected_row.get("tags", "")),
            max_selections=5,
            key=f"edit_favorite_tags_{scope_key}_{selected}",
            help="Una empresa puede pertenecer a varias categorías.",
        )
        edit_col, remove_col = st.columns(2)
        if edit_col.button(
            "Guardar etiquetas",
            key=f"update_favorite_tags_{scope_key}",
            type="primary",
            width="stretch",
        ):
            try:
                journal.update_favorite_tags(selected, edited_tags)
            except (JournalStorageError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success(f"Etiquetas de {selected} actualizadas.")
                st.rerun()
        if remove_col.button(
            "Quitar de la lista",
            key=f"remove_favorite_button_{scope_key}",
            width="stretch",
        ):
            try:
                journal.delete_favorite(selected)
            except (JournalStorageError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.success(f"{selected} ya no está en esta lista.")
                st.rerun()


def render_favorites_manager(
    private_journal,
    group_journal,
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
    *,
    actor_username: str,
    is_admin: bool,
) -> None:
    st.subheader("Favoritos y buscador internacional")
    st.write(
        "Busca por el nombre normal de la empresa. Verás sus distintas cotizaciones, "
        "el mercado y la moneda antes de guardarla o abrir su análisis."
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
                st.rerun()

    st.info(
        "Puedes guardar hasta 300 favoritas. Pulsa «Ver» en cualquier fila para abrir "
        "su ficha directamente; se hará análisis completo de hasta 25 empresas por "
        "actualización."
    )
    tag_filter = st.multiselect(
        "Filtrar ambas listas por etiquetas",
        FAVORITE_TAGS,
        key="favorite_tag_filter",
        help="Si eliges varias, se muestran las empresas que tengan al menos una.",
    )
    private_tab, group_tab = st.tabs(["Mi lista privada", "Lista del grupo"])
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
                <div class="ssl-logo" aria-hidden="true">↗</div>
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


def render_opportunity_cards(
    summary: list[dict[str, object]],
    *,
    limit: int = 6,
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
    cards: list[str] = []
    for row in ordered:
        ticker = html.escape(str(row.get("Ticker") or "N/D"))
        label = str(row.get("Lectura conjunta") or row.get("Estado") or "Sin lectura")
        position_label = str(row.get("Si ya la tienes") or "Sin posición")
        tone = signal_tone(label)
        close = _numeric_score(row.get("Cierre"))
        close_text = f"{close:,.2f}" if close is not None else "N/D"
        date_value = html.escape(str(row.get("Fecha") or "Sin fecha"))
        cards.append(
            f"""
            <article class="ssl-card">
                <div class="ssl-card-top">
                    <span class="ssl-ticker">{ticker}</span>
                    <span class="ssl-badge ssl-{tone}">{html.escape(label)}</span>
                </div>
                <div class="ssl-score-row">
                    <div class="ssl-score">
                        <span>Oportunidad</span>
                        <strong>{_score_text(row.get("Oportunidad"))}/100</strong>
                    </div>
                    <div class="ssl-score">
                        <span>Empresa</span>
                        <strong>{_score_text(row.get("Calidad empresa"))}</strong>
                    </div>
                    <div class="ssl-score">
                        <span>Entrada</span>
                        <strong>{_score_text(row.get("Momento entrada"))}</strong>
                    </div>
                </div>
                <div class="ssl-card-footer">
                    <span>Cierre {close_text}</span>
                    <span>Si la tienes: {html.escape(position_label)}</span>
                </div>
                <div class="ssl-card-footer">
                    <span>Datos {_score_text(row.get("Confianza datos"))}%</span>
                    <span>{date_value}</span>
                </div>
            </article>
            """
        )
    cards_html = ('<div class="ssl-card-grid">' + "".join(cards) + "</div>").replace(
        "\n", ""
    )
    st.markdown(cards_html, unsafe_allow_html=True)


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

    normalized = ticker.strip().upper()
    if not normalized:
        return
    st.session_state["main_navigation"] = "Analizar"
    st.session_state["analysis_navigation"] = "Oportunidades"
    st.session_state["analysis_ticker"] = normalized
    st.session_state["_pending_analysis_ticker"] = normalized


def render_quick_company_search() -> None:
    """Buscador compacto disponible sin mantener abierta toda la configuración."""

    with st.popover(
        "Buscar empresa",
        icon=":material/search:",
        width="stretch",
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
            on_click=_open_ticker_analysis,
            args=(selected.ticker,),
        )
        save_col.button(
            "Guardar",
            width="stretch",
            key="quick_save_favorite",
            on_click=_continue_search_in_favorites,
            args=(query, results),
        )


def render_home(
    user: AuthConfig,
    journal: object,
    group_journal: object,
    prepared: dict[str, pd.DataFrame],
    summary: list[dict[str, object]],
    fx_snapshot: FxSnapshot,
    private_favorites: pd.DataFrame,
    group_favorites: pd.DataFrame,
) -> None:
    latest_snapshot = pd.DataFrame()
    snapshot_summary = None
    if hasattr(journal, "list_portfolio_snapshot_positions"):
        try:
            stored_snapshots = journal.list_portfolio_snapshot_positions()
            latest_snapshot, snapshot_summary = latest_portfolio_snapshot(
                stored_snapshots
            )
        except (JournalStorageError, ValueError) as exc:
            st.warning(f"No se pudo leer la fotografía piloto: {exc}")

    update_dates = [
        pd.Timestamp(frame.index[-1])
        for frame in prepared.values()
        if not frame.empty
    ]
    if update_dates:
        update_text = f"precios de mercado {max(update_dates).date().isoformat()}"
    elif snapshot_summary is not None:
        update_text = f"fotografía piloto {snapshot_summary.snapshot_date}"
    else:
        update_text = "pendiente de actualización"
    st.markdown(
        f"""
        <section class="ssl-hero">
            <h2>Tu resumen de inversión</h2>
            <p>
                Hola, {html.escape(user.display_name)}. Aquí tienes lo importante sin
                perderte entre indicadores. Datos mostrados: {html.escape(update_text)}.
            </p>
        </section>
        """,
        unsafe_allow_html=True,
    )

    try:
        _, private_kpis = _portfolio_snapshot(journal, prepared, fx_snapshot)
        _, group_kpis = _portfolio_snapshot(group_journal, prepared, fx_snapshot)
    except JournalStorageError as exc:
        st.warning(f"No se pudo construir el resumen de carteras: {exc}")
        private_kpis = None
        group_kpis = None

    if snapshot_summary is not None or private_kpis is not None:
        if snapshot_summary is not None:
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
            value_detail = (
                f"Fotografía del {snapshot_summary.snapshot_date} · pendiente de actualizar"
            )
        else:
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
                    <small>Resultado de la fotografía</small>
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

    if snapshot_summary is not None:
        st.markdown("### Mi cartera piloto")
        st.caption(
            "Distribución basada en la fotografía guardada, no en cotizaciones en tiempo real. "
            f"{snapshot_summary.analyzable_count} partidas tienen ticker reconocible para análisis."
        )
        chart_a, chart_b = st.columns(2)
        with chart_a:
            st.plotly_chart(
                portfolio_snapshot_allocation_chart(latest_snapshot),
                width="stretch",
                config=PLOTLY_CONFIG,
            )
        with chart_b:
            st.plotly_chart(
                portfolio_snapshot_assets_chart(latest_snapshot),
                width="stretch",
                config=PLOTLY_CONFIG,
            )

    action_a, action_b, action_c = st.columns(3)
    action_a.button(
        "Analizar empresas",
        width="stretch",
        type="primary",
        on_click=_set_navigation,
        args=("Analizar",),
    )
    action_b.button(
        "Buscar y guardar",
        width="stretch",
        on_click=_set_navigation,
        args=("Favoritos",),
    )
    action_c.button(
        "Abrir mi cartera",
        width="stretch",
        on_click=_set_navigation,
        args=("Carteras", "portfolio_navigation", "Privada"),
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
            "Abre «Analizar», elige tus empresas y pulsa «Actualizar análisis». "
            "También puedes usar el buscador superior; las posiciones abiertas se "
            "añadirán automáticamente."
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
) -> None:
    st.subheader("Oportunidades")
    st.caption(
        "Primero ves una lectura sencilla; el detalle técnico y la tabla completa "
        "siguen disponibles debajo."
    )
    if not raw_data:
        st.info(
            "Usa «Buscar empresa» o guarda favoritas. En esta sección puedes elegirlas "
            "en el panel de configuración y pulsar «Actualizar análisis»."
        )
        return
    if not prepared:
        st.error("No hay suficiente histórico válido para generar señales.")
        return

    render_opportunity_cards(summary)
    alerts = [
        row
        for row in summary
        if row.get("Lectura conjunta") in {"Oportunidad destacada", "Candidata"}
        or row.get("Si ya la tienes") in {"Reducir", "Vender"}
    ]
    st.caption(f"{len(alerts)} alertas prioritarias con las reglas configuradas.")

    with st.expander("Ver ranking completo en tabla"):
        radar = pd.DataFrame(summary)
        if "Momento entrada" in radar.columns:
            radar = radar.sort_values(
                ["Oportunidad", "Confianza datos", "Momento entrada"],
                ascending=[False, False, False],
                na_position="last",
            ).reset_index(drop=True)
            radar.insert(0, "Ranking", range(1, len(radar) + 1))
        st.dataframe(
            radar,
            width="stretch",
            hide_index=True,
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

    if st.session_state.get("analysis_ticker") not in prepared:
        st.session_state.pop("analysis_ticker", None)
    selected = st.selectbox(
        "Empresa que quieres entender mejor",
        list(prepared),
        key="analysis_ticker",
    )
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
            "En «Analizar», selecciona al menos dos favoritas en el panel y pulsa "
            "«Actualizar análisis» para utilizar el comparador."
        )
        return

    groups = _comparison_groups(
        prepared,
        private_favorites,
        group_favorites,
        fundamental_results,
    )
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
    st.dataframe(
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
        width="stretch",
        hide_index=True,
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
            "Todavía no has guardado ningún análisis. Abre una empresa y utiliza "
            "«Guardar este análisis y consultar su evolución»."
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
        st.dataframe(
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
            width="stretch",
            hide_index=True,
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
    if st.session_state.get("main_navigation") not in MAIN_OPTIONS:
        st.session_state["main_navigation"] = "Inicio"
    current_section = str(st.session_state["main_navigation"])
    apply_section_layout(current_section)
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

    pending_analysis_ticker = str(
        st.session_state.get("_pending_analysis_ticker", "")
    ).strip().upper()
    active_analysis_ticker = str(
        st.session_state.get("analysis_ticker", "")
    ).strip().upper()
    if load_clicked or pending_analysis_ticker:
        held_tickers: list[str] = []
        owners_to_load = [authenticated_user.username, GROUP_PORTFOLIO_OWNER]
        if authenticated_user.is_admin:
            owners_to_load.extend(managed_usernames(accounts))
        for owner in dict.fromkeys(owners_to_load):
            try:
                owner_journal = (
                    journal
                    if owner == authenticated_user.username
                    else create_journal(owner)
                )
                saved_positions = owner_journal.open_positions()
            except JournalStorageError as exc:
                st.sidebar.warning(
                    f"No se pudo consultar la cartera de {owner}: {exc}"
                )
                continue
            if not saved_positions.empty:
                held_tickers.extend(saved_positions["ticker"].astype(str).tolist())
        tickers_to_load = analysis_refresh_tickers(
            tickers,
            held_tickers,
            pending_ticker=pending_analysis_ticker,
            active_ticker=active_analysis_ticker,
        )
        load_market_data(
            tickers_to_load,
            start,
            end,
            auto_adjust,
            alpha_vantage_key,
            fundamental_tickers={
                *tickers,
                *([pending_analysis_ticker] if pending_analysis_ticker else []),
                *([active_analysis_ticker] if active_analysis_ticker else []),
            },
        )
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

    navigation_container = st.container()
    with navigation_container:
        if current_section == "Favoritos":
            navigation_col = st.container()
            search_col = None
        else:
            navigation_col, search_col = st.columns(
                [6, 1],
                vertical_alignment="center",
            )
        with navigation_col:
            selected_section = st.segmented_control(
                "Navegación principal",
                MAIN_OPTIONS,
                key="main_navigation",
                required=True,
                label_visibility="collapsed",
                format_func=lambda value: {
                    "Inicio": "⌂ Inicio",
                    "Analizar": "⌁ Analizar",
                    "Favoritos": "☆ Favoritos",
                    "Carteras": "▣ Carteras",
                    "Más": "••• Más",
                }[value],
            )
        if search_col is not None:
            with search_col:
                render_quick_company_search()

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
        )

    elif selected_section == "Analizar":
        analysis_options = [
            "Oportunidades",
            "Comparador sectorial",
            "Historial guardado",
            "Prueba histórica",
        ]
        if st.session_state.get("analysis_navigation") not in analysis_options:
            st.session_state["analysis_navigation"] = "Oportunidades"
        analysis_section = st.segmented_control(
            "Tipo de análisis",
            analysis_options,
            key="analysis_navigation",
            required=True,
            label_visibility="collapsed",
        )
        if analysis_section == "Oportunidades":
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
            )
        elif analysis_section == "Comparador sectorial":
            render_sector_comparison(
                prepared,
                fundamental_results,
                valuation_results,
                risk_results,
                private_favorites,
                group_favorites,
            )
        elif analysis_section == "Historial guardado":
            render_saved_analysis_history(journal)
        elif not prepared:
            st.info("Actualiza empresas antes de ejecutar una prueba histórica.")
        else:
            selected_backtest = st.selectbox(
                "Empresa para la prueba histórica",
                list(prepared),
                key="backtest_select",
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
            )

    elif selected_section == "Carteras":
        portfolio_options = ["Privada", "Grupo"]
        if st.session_state.get("portfolio_navigation") not in portfolio_options:
            st.session_state["portfolio_navigation"] = "Privada"
        portfolio_section = st.segmented_control(
            "Cartera",
            portfolio_options,
            key="portfolio_navigation",
            required=True,
            label_visibility="collapsed",
            format_func=lambda value: (
                "Mi cartera privada" if value == "Privada" else "Cartera del grupo"
            ),
        )
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
        more_options = ["Alertas por correo"]
        if authenticated_user.is_admin:
            more_options.append("Administración")
        more_options.append("Guía y riesgos")
        if st.session_state.get("more_navigation") not in more_options:
            st.session_state["more_navigation"] = "Guía y riesgos"
        more_section = st.segmented_control(
            "Más secciones",
            more_options,
            key="more_navigation",
            required=True,
            label_visibility="collapsed",
        )
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
