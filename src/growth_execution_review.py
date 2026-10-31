"""Independent, causal execution context; never edits the frozen signal engine.

The preset is an experimental entry filter, not a new score or a backtest. It
does not validate fundamentals, place orders, infer intrabar fills or certify a
stop. In particular, a candle touching two levels cannot reveal their order.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
import math

import pandas as pd

from src.daily_digest import _session_close
from src.data_sources import FxSnapshot, convert_currency
from src.price_units import normalize_price_frame_units, resolve_quote_unit
from src.stop_engine import AtrMethod, calculate_atr


EXECUTION_REVIEW_VERSION = "growth_execution_review_v1"


@dataclass(frozen=True)
class ExecutionReviewConfig:
    """One predeclared experimental preset; not fitted to past returns."""
    adv_sessions: int = 20
    breakout_sessions: int = 20
    retest_sessions: int = 5
    atr_period: int = 14
    min_turnover_eur: float = 5_000_000.0
    breakout_volume_ratio: float = 1.2
    min_close_location: float = 0.6
    max_extension_atr: float = 1.0
    retest_tolerance_atr: float = 0.5
    max_fx_age_days: int = 7

    def validate(self):
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (
            self.adv_sessions, self.breakout_sessions, self.retest_sessions, self.atr_period, self.max_fx_age_days)):
            raise ValueError("Execution windows must be integer session counts")
        if min(self.adv_sessions, self.breakout_sessions, self.atr_period) < 2 or self.retest_sessions < 1:
            raise ValueError("Execution windows must contain sufficient past bars")
        if not 0 < self.min_close_location <= 1 or self.max_fx_age_days < 0:
            raise ValueError("Invalid execution preset")
        if any(not math.isfinite(float(value)) or value <= 0 for value in (
            self.min_turnover_eur, self.breakout_volume_ratio, self.max_extension_atr, self.retest_tolerance_atr)):
            raise ValueError("Invalid execution preset")


def _read(source, key):
    return source.get(key) if isinstance(source, Mapping) else getattr(source, key, None)


def _number(value):
    if value is None or isinstance(value, bool): return None
    try: result = float(value)
    except (ValueError, TypeError, OverflowError): return None
    return result if math.isfinite(result) else None


def _day(value):
    try:
        stamp = pd.Timestamp(value)
        return None if pd.isna(stamp) else stamp.date()
    except (ValueError, TypeError, OverflowError): return None


def _currency(frame, info):
    attrs = frame.attrs
    if attrs.get("quote_units_normalized"):
        display = str(attrs.get("display_currency") or "").strip()
        if not display or resolve_quote_unit(display).converted_from_subunit:
            return None, "Una serie normalizada necesita acreditar su moneda principal."
        quote = resolve_quote_unit(display).display_currency
        raw = str(attrs.get("source_currency") or display)
    else:
        raw = str(attrs.get("quote_currency") or attrs.get("source_currency") or info.get("currency") or "").strip()
        quote = resolve_quote_unit(raw).display_currency
    declared = str(info.get("currency") or "").strip()
    if declared and resolve_quote_unit(declared).display_currency != quote:
        return None, "La moneda de las velas no coincide con la ficha; no se supone una conversión."
    if len(quote) != 3 or not quote.isalpha():
        return None, "Moneda o unidad de cotización sin verificar; negociación en EUR N/D."
    return (raw, quote), None


def _candle(row):
    spread = float(row.high - row.low)
    return {"body_to_range": abs(float(row.close - row.open)) / spread if spread > 0 else None,
            "upper_wick_to_range": float(row.high - max(row.open, row.close)) / spread if spread > 0 else None,
            "lower_wick_to_range": float(min(row.open, row.close) - row.low) / spread if spread > 0 else None,
            "close_location": float(row.close - row.low) / spread if spread > 0 else None}


def review_growth_execution(result, info, frame, fx_snapshot: FxSnapshot | None, *,
                            as_of, known_at=None, fx_available_at=None,
                            config: ExecutionReviewConfig | None = None) -> dict[str, object]:
    """Review only known, closed bars. No baseline label or score is changed.

    ``eligible_baseline`` merely records the supplied engine's entry label.
    ``entry_setup_eligible`` adds this versioned liquidity/candle experiment;
    callers must still apply their existing fundamental/portfolio risk gates.
    FX must be dated on/before the selected session and demonstrably available
    by known_at. Without an availability timestamp only an earlier-day FX quote
    is accepted. A reference-day label alone is not proof of publication time.
    """
    config = config or ExecutionReviewConfig()
    config.validate()
    info = info if isinstance(info, Mapping) else {}
    ticker = str(_read(result, "ticker") or info.get("symbol") or (frame.attrs.get("ticker") if isinstance(frame, pd.DataFrame) else "") or "").strip().upper()
    label = str(_read(result, "label") or "")
    score = _number(_read(result, "score"))
    baseline = label in {"Entrada fuerte", "Entrada candidata", "Entrada interesante"} and score is not None and 0 < score <= 100
    output = {"version": EXECUTION_REVIEW_VERSION, "rules": asdict(config), "ticker": ticker,
        "raw_score": score, "raw_label": label, "eligible_baseline": baseline,
        "entry_setup_eligible": False, "eligible": False, "status": "blocked_data",
        "setup": "not_evaluated", "session": None, "reference_close": None,
        "bar_availability_basis": "exchange_close_proxy",
        "currency": None, "adv_quote": None, "adv_eur": None, "fx_date": None, "fx_available_at": None,
        "atr_prior": None, "atr_reference_session": None, "prior_level": None, "extension_atr": None,
        "volume_ratio_prior": None, "candle": {}, "reasons": [],
        "limitations": ["Contexto experimental: no valida fundamentales ni autoriza compras.",
            "No simula órdenes ni presume ejecuciones al cierre de señal.",
            "Sin relojes explícitos de la fuente, el cierre de bolsa es un proxy de disponibilidad de las velas históricas.",
            "OHLC no permite conocer el orden intradía de niveles tocados ni garantizar stops."]}

    def blocked(message):
        output["reasons"].append(message)
        return output

    requested = _day(as_of)
    try:
        clock = pd.Timestamp.now(tz="UTC") if known_at is None else pd.Timestamp(known_at)
        if pd.isna(clock) or clock.tzinfo is None: return blocked("La hora de conocimiento debe incluir zona horaria.")
        clock = clock.tz_convert("UTC")
    except (ValueError, TypeError, OverflowError): return blocked("Hora de conocimiento no válida.")
    if requested is None or requested > clock.date(): return blocked("Fecha objetivo no válida o posterior al conocimiento.")
    if not ticker or not isinstance(frame, pd.DataFrame) or frame.empty:
        return blocked("Faltan instrumento o velas para revisar ejecución.")
    declared = str(info.get("symbol") or "").strip().upper()
    if declared and declared != ticker: return blocked("La ficha corresponde a otro instrumento.")
    dates = [_day(value) for value in frame.index]
    if any(value is None for value in dates): return blocked("Hay fechas de velas no verificables.")
    data = frame.loc[[value <= requested for value in dates]].copy()
    if data.empty: return blocked("No hay barras anteriores a la fecha objetivo.")
    if (not data.index.is_monotonic_increasing or data.index.has_duplicates
            or len({_day(value) for value in data.index}) != len(data)):
        return blocked("Las fechas deben estar ordenadas y no duplicadas.")
    required = max(config.adv_sessions + 1, config.breakout_sessions + config.retest_sessions + 1, config.atr_period + 2)
    data = data.tail(required + 1)
    closes = [_session_close(ticker, _day(value).isoformat()) for value in data.index]
    if any(value is None for value in closes): return blocked("Calendario de las velas sin verificar.")
    data = data.loc[[value <= clock for value in closes]].tail(required).copy()
    if len(data) < required: return blocked("Faltan sesiones cerradas suficientes para el preset de ejecución.")
    session = _day(data.index[-1])
    output["session"] = session.isoformat()
    availability_column = ("close_available_at" if "close_available_at" in data
                           else "source_close_available_at" if "source_close_available_at" in data else None)
    if availability_column:
        for index, raw_available in data[availability_column].items():
            try:
                available = pd.Timestamp(raw_available)
                if pd.isna(available) or available.tzinfo is None:
                    return blocked("La disponibilidad histórica de las velas no está acreditada.")
                available = available.tz_convert("UTC")
            except (ValueError, TypeError, OverflowError):
                return blocked("Disponibilidad de vela no verificable.")
            if available < _session_close(ticker, _day(index).isoformat()) or available > clock:
                return blocked("Una vela requerida aún no era conocida o precede a su cierre de mercado.")
        output["bar_availability_basis"] = "declared_close_availability"
    if _day(_read(result, "as_of")) != session:
        return blocked("La señal no corresponde a la última vela cerrada disponible.")
    currency, issue = _currency(data, info)
    if issue: return blocked(issue)
    raw_currency, quote_currency = currency
    data = normalize_price_frame_units(data, raw_currency)
    output["currency"] = quote_currency
    required_columns = ["open", "high", "low", "close", "volume"]
    if not set(required_columns).issubset(data.columns): return blocked("Faltan columnas OHLCV; no se inventa apertura o volumen.")
    data[required_columns] = data[required_columns].apply(lambda column: column.map(_number))
    if data[required_columns].isna().any().any():
        return blocked("Hay valores OHLCV ausentes o no finitos.")
    if ((data[["open", "high", "low", "close"]] <= 0).any().any() or (data.volume < 0).any()
            or (data.high < data[["open", "close"]].max(axis=1)).any()
            or (data.low > data[["open", "close"]].min(axis=1)).any()
            or (data.high < data.low).any()):
        return blocked("Las velas contienen OHLC imposible o volumen negativo.")
    if data.volume.iloc[-1] <= 0: return blocked("La última sesión no acredita volumen negociable.")
    last = data.iloc[-1]
    output["reference_close"] = float(last.close)
    adv = float((data.close * data.volume).tail(config.adv_sessions).mean())
    output["adv_quote"] = adv
    if quote_currency == "EUR":
        output["adv_eur"] = adv
    else:
        if not isinstance(fx_snapshot, FxSnapshot) or fx_snapshot.as_of is None:
            return blocked("Falta un tipo de cambio fechado; negociación en EUR N/D.")
        fx_day = _day(fx_snapshot.as_of)
        if fx_day is None or not 0 <= (session - fx_day).days <= config.max_fx_age_days:
            return blocked("El cambio de divisa es futuro o antiguo para esta sesión; negociación en EUR N/D.")
        if fx_available_at is None:
            if fx_day >= session or fx_day >= clock.date():
                return blocked("El cambio del mismo día no acredita su hora de publicación; negociación en EUR N/D.")
        else:
            try:
                available = pd.Timestamp(fx_available_at)
                if pd.isna(available) or available.tzinfo is None:
                    return blocked("La disponibilidad del cambio necesita una hora con zona horaria.")
                available = available.tz_convert("UTC")
            except (ValueError, TypeError, OverflowError):
                return blocked("Fecha de disponibilidad del cambio no verificable.")
            if available > clock or available.date() < fx_day:
                return blocked("El cambio de divisa todavía no era conocido en esta decisión; negociación en EUR N/D.")
            output["fx_available_at"] = available.isoformat()
        rates = fx_snapshot.rates_per_eur
        if any((rate := _number(rates.get(key))) is None or rate <= 0 for key in (quote_currency, "EUR")):
            return blocked("No hay tipos de cambio positivos para convertir la negociación a EUR.")
        output["fx_date"] = fx_day.isoformat()
        output["adv_eur"] = convert_currency(adv, quote_currency, "EUR", rates)
    atr = calculate_atr(data, period=config.atr_period, method=AtrMethod.SMA).shift(1)
    level = data.close.shift(1).rolling(config.breakout_sessions, min_periods=config.breakout_sessions).max()
    prior_volume = data.volume.shift(1).rolling(config.adv_sessions, min_periods=config.adv_sessions).mean()
    atr_now = _number(atr.iloc[-1])
    if atr_now is None or atr_now <= 0: return blocked("ATR previo no verificable; no se inventa un stop ni tolerancia.")
    output["atr_prior"] = atr_now
    atr_reference_session = _day(data.index[-2]).isoformat()
    output["candle"] = _candle(last)
    prior_volume_now = _number(prior_volume.iloc[-1])
    output["volume_ratio_prior"] = float(last.volume / prior_volume_now) if prior_volume_now and prior_volume_now > 0 else None
    current_level = float(level.iloc[-1])
    output["prior_level"] = current_level
    setup = "none"
    if last.close > current_level:
        setup = "breakout"
        extension = float((last.close - current_level) / atr_now)
        confirmed = (last.close > last.open and output["candle"]["close_location"] is not None
            and output["candle"]["close_location"] >= config.min_close_location
            and output["volume_ratio_prior"] is not None
            and output["volume_ratio_prior"] >= config.breakout_volume_ratio)
    else:
        extension, confirmed = None, False
        for position in range(len(data) - 2, max(-1, len(data) - 2 - config.retest_sessions), -1):
            old_atr, old_level = _number(atr.iloc[position]), _number(level.iloc[position])
            old_volume = _number(prior_volume.iloc[position])
            event = data.iloc[position]
            event_candle = _candle(event)
            if (not old_atr or old_level is None or not old_volume
                    or event.close <= old_level or event.close <= event.open
                    or event_candle["close_location"] is None or event_candle["close_location"] < config.min_close_location
                    or event.volume / old_volume < config.breakout_volume_ratio):
                continue
            tolerance = config.retest_tolerance_atr * old_atr
            survived = (data.close.iloc[position + 1:] >= old_level - tolerance).all()
            recovered = (abs(float(last.low - old_level)) <= tolerance and last.close > old_level
                and last.close > last.open and output["candle"]["close_location"] is not None
                and output["candle"]["close_location"] >= config.min_close_location)
            if survived and recovered:
                setup, confirmed = "retest", True
                current_level, atr_now = old_level, old_atr
                atr_reference_session = _day(data.index[position - 1]).isoformat()
                extension = float((last.close - old_level) / old_atr)
                break
    output.update(setup=setup, prior_level=current_level, extension_atr=extension,
                  atr_prior=atr_now, atr_reference_session=atr_reference_session)
    liquid = output["adv_eur"] >= config.min_turnover_eur
    not_extended = extension is not None and 0 < extension <= config.max_extension_atr
    eligible = bool(baseline and liquid and confirmed and not_extended)
    output.update(status="eligible" if eligible else "illiquid" if not liquid else "watch",
        entry_setup_eligible=eligible, eligible=eligible)
    if not baseline: output["reasons"].append("El motor original no aporta una etiqueta de entrada; este contexto no la sustituye.")
    if not liquid: output["reasons"].append("La negociación media convertida a EUR no alcanza el mínimo del preset experimental.")
    if setup == "none": output["reasons"].append("No hay ruptura o retest definido por este preset; no se inventa una señal de velas.")
    elif not confirmed: output["reasons"].append("La ruptura no confirma cierre/volumen según el preset experimental.")
    elif not not_extended: output["reasons"].append("El cierre se aleja del nivel más de lo admitido por el ATR previo; no perseguir esta entrada experimental.")
    else: output["reasons"].append("Ruptura/retest causal confirmado al cierre; cualquier ejecución sería posterior a conocerse esta señal.")
    return output
