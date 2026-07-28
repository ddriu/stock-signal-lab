"""Interfaz Streamlit de Stock Signal Lab.

Ejecutar con: ``streamlit run app.py``
"""

from __future__ import annotations

from datetime import date, timedelta
import html

import pandas as pd
import streamlit as st

from config import BacktestConfig, StrategyConfig
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
from src.indicators import add_indicators
from src.journal import MAX_FAVORITES, calculate_open_positions
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
from src.recommendations import (
    build_entry_guide,
    build_profit_taking_plan,
    historical_forward_return_study,
)
from src.risk import calculate_position_plan
from src.signal_engine import add_signal_columns, evaluate_latest_signal
from src.ui import (
    APP_CSS,
    PROFILE_NAMES,
    signal_tone,
    strategy_profile_defaults,
)
from src.visualization import backtest_chart, momentum_chart, price_chart


st.set_page_config(page_title="Stock Signal Lab", page_icon="📈", layout="wide")


def apply_visual_theme() -> None:
    """Aplica una capa visual responsive sin alterar los componentes financieros."""

    st.markdown(f"<style>{APP_CSS}</style>", unsafe_allow_html=True)


@st.cache_data(ttl=3_600, show_spinner=False)
def cached_download(ticker: str, start: date, end: date, auto_adjust: bool) -> pd.DataFrame:
    """Caché de red; los indicadores se recalculan fuera con la configuración actual."""

    return download_prices(ticker, start, end, auto_adjust=auto_adjust)


@st.cache_data(ttl=21_600, show_spinner=False)
def cached_fundamentals(ticker: str) -> dict[str, object]:
    """Caché más largo para datos empresariales, que cambian menos que el precio."""

    return download_fundamental_snapshot(ticker)


@st.cache_data(ttl=86_400, show_spinner=False)
def cached_fx_rates() -> FxSnapshot:
    """Tipos de referencia diarios; una consulta basta para todas las empresas."""

    return download_ecb_fx_snapshot()


@st.cache_data(ttl=86_400, show_spinner=False)
def cached_price_verification(ticker: str, api_key: str) -> PriceVerification:
    """Comprobación opcional del cierre mediante un segundo proveedor."""

    return download_alpha_vantage_latest_close(ticker, api_key)


@st.cache_data(ttl=86_400, show_spinner=False)
def cached_company_search(query: str) -> list[TickerSearchResult]:
    """Evita repetir búsquedas iguales durante el día."""

    return search_instruments(query)


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
    tickers = sorted(names, key=lambda item: (names[item].casefold(), item))
    labels = {
        ticker: (
            f"{names[ticker]} ({ticker}) · "
            + " y ".join(sorted(sources[ticker]))
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
    st.sidebar.markdown("## Radar de empresas")
    selected_favorites = st.sidebar.multiselect(
        "Empresas que quieres analizar",
        options=favorite_tickers,
        format_func=lambda ticker: favorite_labels.get(ticker, ticker),
        max_selections=25,
        help=(
            "Puedes guardar hasta 100 favoritas y analizar hasta 25 en profundidad "
            "cada vez. Las posiciones abiertas se actualizan automáticamente."
        ),
        key="selected_favorite_tickers",
    )
    with st.sidebar.expander("Añadir símbolos manualmente"):
        manual_tickers = parse_tickers(
            st.text_area(
                "Símbolos bursátiles",
                "" if favorite_tickers else "AAPL, MSFT, SAN.MC",
                height=78,
                help="Escribe símbolos separados por comas; SAN.MC es Banco Santander en Madrid.",
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
            "Yahoo aporta precios y contexto; SEC valida cuentas de EE. UU. y el BCE divisas."
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

    study = historical_forward_return_study(
        frame,
        current_score=signal.score,
        horizon_days=strategy.forward_horizon_days,
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
        expected_price = float(latest["close"]) * (
            1 + float(study.median_return_pct) / 100
        )
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
        history_cols[4].metric("Precio estadístico orientativo", f"{expected_price:,.2f}")
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
    positions_tab, register_tab, switch_tab, history_tab = st.tabs(
        ["Posiciones analizadas", "Registrar operación", "Comparar un cambio", "Historial"]
    )

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
    _, portfolio_kpis = build_position_dashboard(
        operations,
        positions,
        latest_prices,
        fx_snapshot.rates_per_eur,
        sell_fee_eur=float(fixed_fee),
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
                    + ". Pulsa «Descargar / actualizar»; las posiciones guardadas se incluyen automáticamente."
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
                "Pulsa «Descargar / actualizar» para valorar las posiciones con precios "
                "actuales y calcular sus rendimientos."
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


def render_favorite_list(
    favorites: pd.DataFrame,
    *,
    title: str,
    journal,
    actor_username: str,
    shared: bool = False,
    can_delete_all: bool = False,
) -> None:
    st.markdown(f"#### {title}")
    st.caption(f"{len(favorites)} de {MAX_FAVORITES} empresas guardadas")
    if favorites.empty:
        st.info("Todavía no hay empresas en esta lista.")
        return

    visible = favorites.loc[:, ["name", "ticker", "exchange", "recorded_by"]].rename(
        columns={
            "name": "Empresa",
            "ticker": "Ticker",
            "exchange": "Mercado",
            "recorded_by": "Añadida por",
        }
    )
    if not shared:
        visible = visible.drop(columns=["Añadida por"])
    st.dataframe(visible, width="stretch", hide_index=True)

    removable = favorites
    if shared and not can_delete_all:
        removable = favorites.loc[
            favorites["recorded_by"].fillna("").astype(str).str.lower()
            == actor_username.lower()
        ]
    if removable.empty:
        st.caption("Sólo puedes quitar del grupo las empresas que tú añadiste.")
        return
    options = removable["ticker"].astype(str).tolist()
    labels = {
        str(row.ticker): f"{row.name} ({row.ticker})"
        for row in removable.itertuples(index=False)
    }
    remove_col, button_col = st.columns([3, 1])
    selected = remove_col.selectbox(
        "Quitar de la lista",
        options,
        format_func=lambda ticker: labels.get(ticker, ticker),
        key=f"remove_favorite_{'group' if shared else 'private'}",
        label_visibility="collapsed",
    )
    if button_col.button(
        "Quitar",
        key=f"remove_favorite_button_{'group' if shared else 'private'}",
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
    st.subheader("Favoritos y buscador de empresas")
    st.write(
        "Busca por el nombre normal de la empresa, guárdala en tu lista o en la del "
        "grupo y después selecciónala en la barra izquierda para analizarla."
    )
    with st.form("company_search_form"):
        search_col, button_col = st.columns([4, 1])
        query = search_col.text_input(
            "Nombre o símbolo",
            placeholder="Ejemplo: Taiwan Semiconductor, Inditex o Microsoft",
            help="El buscador muestra acciones y ETF de los mercados disponibles en Yahoo.",
        )
        submitted = button_col.form_submit_button(
            "Buscar",
            type="primary",
            width="stretch",
        )
    if submitted:
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
            "No se encontraron acciones o ETF. Prueba con el nombre en inglés o usa "
            "el símbolo manual en la barra izquierda."
        )
    if results:
        result_index = st.selectbox(
            "Resultado correcto",
            options=range(len(results)),
            format_func=lambda index: results[index].label,
            key="favorite_search_result",
        )
        destination = st.radio(
            "Dónde guardarla",
            ["Mi lista privada", "Lista del grupo"],
            horizontal=True,
            help="Guardar una empresa no es una recomendación de compra.",
        )
        selected = results[result_index]
        if st.button(
            f"Guardar {selected.ticker}",
            type="primary",
            key="save_favorite",
        ):
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

    st.info(
        "Para no ralentizar la aplicación, puedes guardar 100 favoritas y elegir hasta "
        "25 para análisis completo en cada actualización. Todas las posiciones abiertas "
        "se incorporan automáticamente con una actualización de precio y tendencia."
    )
    private_col, group_col = st.columns(2)
    with private_col:
        render_favorite_list(
            private_favorites,
            title="Mi lista privada",
            journal=private_journal,
            actor_username=actor_username,
        )
    with group_col:
        render_favorite_list(
            group_favorites,
            title="Lista del grupo",
            journal=group_journal,
            actor_username=actor_username,
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


def render_app_header(user: AuthConfig) -> None:
    role = "Administrador" if user.is_admin else f"Hola, {user.display_name}"
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


def _set_navigation(
    section: str,
    subsection_key: str | None = None,
    subsection: str | None = None,
) -> None:
    st.session_state["main_navigation"] = section
    if subsection_key and subsection:
        st.session_state[subsection_key] = subsection


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
    update_dates = [
        pd.Timestamp(frame.index[-1])
        for frame in prepared.values()
        if not frame.empty
    ]
    update_text = (
        max(update_dates).date().isoformat()
        if update_dates
        else "pendiente de actualización"
    )
    st.markdown(
        f"""
        <section class="ssl-hero">
            <h2>Tu resumen de inversión</h2>
            <p>
                Hola, {html.escape(user.display_name)}. Aquí tienes lo importante sin
                perderte entre indicadores. Últimos precios: {html.escape(update_text)}.
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

    if private_kpis is not None:
        value_text = (
            f"{private_kpis.current_net_value_eur:,.0f} €"
            if private_kpis.priced_positions_count
            else "Sin actualizar"
        )
        result_text = (
            f"{private_kpis.unrealized_pnl_eur:+,.0f} €"
            if private_kpis.priced_positions_count
            else "—"
        )
        result_detail = (
            f"{private_kpis.unrealized_return_pct:+.2f}% sobre posiciones valoradas"
            if private_kpis.priced_positions_count
            else "Actualiza para conocer el resultado"
        )
        st.markdown(
            f"""
            <div class="ssl-kpi-grid">
                <div class="ssl-kpi-card">
                    <small>Valor de mi cartera</small>
                    <strong>{value_text}</strong>
                    <em>{private_kpis.priced_positions_count}/{private_kpis.open_positions_count}
                    posiciones con precio</em>
                </div>
                <div class="ssl-kpi-card">
                    <small>Resultado latente</small>
                    <strong>{result_text}</strong>
                    <em>{result_detail}</em>
                </div>
                <div class="ssl-kpi-card">
                    <small>Posiciones abiertas</small>
                    <strong>{private_kpis.open_positions_count}</strong>
                    <em>{private_kpis.operations_count} operaciones registradas</em>
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
            "Selecciona empresas en la barra lateral y pulsa «Actualizar análisis». "
            "Las posiciones abiertas se añadirán automáticamente."
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
) -> None:
    st.subheader("Oportunidades")
    st.caption(
        "Primero ves una lectura sencilla; el detalle técnico y la tabla completa "
        "siguen disponibles debajo."
    )
    if not raw_data:
        st.info(
            "Busca empresas en «Favoritos», selecciónalas en la barra lateral y "
            "pulsa «Actualizar análisis»."
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
        - Dividendos, fiscalidad, préstamos de valores y el coste de cambio del broker no están modelados.
        - Los tipos del BCE son referencias informativas y pueden diferir del cambio real del broker.
        - Los niveles de venta y stops son referencias: una orden puede ejecutarse
          a otro precio durante gaps o mercados volátiles.
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

    if load_clicked:
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
        tickers_to_load = list(dict.fromkeys([*tickers, *held_tickers]))
        load_market_data(
            tickers_to_load,
            start,
            end,
            auto_adjust,
            alpha_vantage_key,
            fundamental_tickers=set(tickers),
        )
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

    main_options = ["Inicio", "Analizar", "Favoritos", "Carteras", "Más"]
    if st.session_state.get("main_navigation") not in main_options:
        st.session_state["main_navigation"] = "Inicio"
    selected_section = st.segmented_control(
        "Navegación principal",
        main_options,
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
        analysis_options = ["Oportunidades", "Prueba histórica"]
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
            )
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
        more_options = (
            ["Administración", "Guía y riesgos"]
            if authenticated_user.is_admin
            else ["Guía y riesgos"]
        )
        if st.session_state.get("more_navigation") not in more_options:
            st.session_state["more_navigation"] = "Guía y riesgos"
        more_section = st.segmented_control(
            "Más secciones",
            more_options,
            key="more_navigation",
            required=True,
            label_visibility="collapsed",
        )
        if more_section == "Administración":
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
