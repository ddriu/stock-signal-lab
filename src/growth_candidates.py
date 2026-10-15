"""Modelo de presentación compacto del radar principal, sin efectos externos.

Sólo los resultados de crecimiento/momentum habilitan nuevas entradas. Los
datos comparativos se consultan exclusivamente para conservar una invalidación
de tesis declarada; no se promedian notas ni se infieren compras o ventas.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math

import pandas as pd

from src.analysis_policy import decode_policy_note
from src.instruments import resolve_analysis_ticker
from src.market_data_quality import quote_freshness


GROWTH_CANDIDATE_GROUPS = ("Nuevas entradas", "Mi cartera", "Esperar")
_ENTRY_LABELS = frozenset({"Entrada fuerte", "Entrada candidata"})
_MISSING_TEXT = frozenset({"", "nan", "nat", "none", "<na>"})
_NONQUOTED_TERMS = (
    "civislend", "segofactoring", "sego factoring", "crowdlending",
)
_NONQUOTED_TYPES = frozenset({
    "efectivo", "cash", "liquidez", "manual", "fondo manual", "fondo no cotizado",
    "inmueble", "crowdlending", "alternativo",
})


def _text(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.casefold() in _MISSING_TEXT else text


def _number(value: object) -> float | None:
    if value is None or pd.api.types.is_bool(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _score(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _read(source: object, key: str) -> object:
    return source.get(key) if isinstance(source, Mapping) else getattr(source, key, None)


def _ticker(value: object) -> str:
    text = _text(value)
    return resolve_analysis_ticker(text) if text else ""


def _date(value: object) -> str | None:
    if not _text(value):
        return None
    try:
        parsed = pd.Timestamp(value)
        return None if pd.isna(parsed) else parsed.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _normalized_mapping(source: Mapping[str, object] | None) -> dict[str, object]:
    """Un alias se resuelve una vez; una clave canónica tiene preferencia."""

    output: dict[str, object] = {}
    for key, value in (source or {}).items():
        ticker = _ticker(key)
        if ticker and (ticker not in output or _text(key).upper() == ticker):
            output[ticker] = value
    return output


def _freshness_issue(value: object, reference_date: object) -> str | None:
    parsed = _date(value)
    if parsed is None:
        return "Fecha de precio sin verificar"
    try:
        freshness = quote_freshness(parsed, reference_date=reference_date)
    except (TypeError, ValueError, OverflowError):
        return "Fecha de precio sin verificar"
    return None if freshness.fresh else freshness.status


def _history_issue(frame: object, as_of: str | None, reference_date: object) -> str | None:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "close" not in frame:
        return "Histórico de precios pendiente"
    price = _number(frame["close"].iloc[-1])
    if price is None or price <= 0:
        return "Último cierre no válido; actualizar precios"
    issue = _freshness_issue(frame.index[-1], reference_date)
    if issue:
        return issue
    indicator_columns = {"sma_medium", "sma_long"}
    if indicator_columns.intersection(frame.columns):
        # Las ventanas son configurables: una media calculada es mejor evidencia
        # que imponer aquí un número fijo de sesiones a todos los perfiles.
        if not indicator_columns.issubset(frame.columns) or any(
            (value := _number(frame[column].iloc[-1])) is None or value <= 0
            for column in indicator_columns
        ):
            return "Histórico o medias insuficientes"
    else:
        valid = frame["close"].map(_number).map(lambda value: value is not None and value > 0)
        if frame.index[valid].nunique() < 200:
            return "Histórico o medias insuficientes"
    if as_of is not None and _date(frame.index[-1]) != as_of:
        return "Análisis pendiente con el último cierre"
    return None


def _primary_reading(
    result: object, frame: object, *, check_frame: bool, ticker: str, reference_date: object,
) -> tuple[bool, bool, str, str | None, float | None]:
    label = _text(_read(result, "label"))
    as_of = _date(_read(result, "as_of"))
    price = _number(_read(result, "price"))
    if result is None:
        return False, False, "Análisis de crecimiento y momentum pendiente", as_of, price
    result_ticker = _ticker(_read(result, "ticker"))
    if result_ticker and result_ticker != ticker:
        return False, False, "Resultado de otro instrumento; actualizar análisis", as_of, price
    if price is None or price <= 0:
        return False, False, "Precio de referencia no válido; actualizar precios", as_of, None
    issue = _freshness_issue(as_of, reference_date)
    if issue:
        return False, False, issue, as_of, price
    if check_frame:
        issue = _history_issue(frame, as_of, reference_date)
        if issue:
            return False, False, issue, as_of, price
    scores = [_score(_read(result, key)) for key in ("score", "momentum_score", "context_score", "confidence_pct")]
    growth = _score(_read(result, "growth_score"))
    incomplete_label = not label or any(
        word in label.casefold() for word in ("pendiente", "insuficiente", "parcial", "sin datos")
    )
    if (
        any(value is None for value in scores) or scores[3] < 45
        or growth is None and _text(_read(result, "sector_key")) != "etf"
        or incomplete_label
    ):
        return False, False, label if incomplete_label and label else "Datos de crecimiento y momentum incompletos", as_of, price
    candidate = label in _ENTRY_LABELS
    # Estos mínimos forman parte de las etiquetas del motor, no de una nueva
    # puntuación. Un resultado contradictorio o corrupto no habilita entradas.
    if candidate and (
        scores[0] <= 0 or scores[1] < (75 if label == "Entrada fuerte" else 65)
        or scores[2] < (50 if label == "Entrada fuerte" else 40)
    ):
        return False, False, "Señal principal incoherente; actualizar análisis", as_of, price
    return True, candidate, label, as_of, price


def _latest_snapshot(snapshot: pd.DataFrame | None) -> tuple[pd.DataFrame, str | None]:
    if snapshot is None or snapshot.empty:
        return pd.DataFrame(), None
    latest = snapshot.copy(deep=True)
    if "snapshot_date" not in latest:
        return latest, None
    dates = latest["snapshot_date"].map(_date)
    valid = dates.dropna()
    if valid.empty:
        return latest.iloc[0:0].copy(), None
    snapshot_date = str(valid.max())
    return latest.loc[dates == snapshot_date].copy(), snapshot_date


def _snapshot_weights(snapshot: pd.DataFrame) -> dict[str, float]:
    if not {"analysis_ticker", "value_eur"}.issubset(snapshot.columns):
        return {}
    values: dict[str, float] = {}
    for row in snapshot.to_dict("records"):
        ticker = _ticker(row.get("analysis_ticker"))
        value = _number(row.get("value_eur"))
        description = " ".join(_text(row.get(column)).casefold() for column in (
            "platform", "asset_name", "asset_type", "portfolio_block",
        ))
        if (
            not ticker or value is None or value <= 0
            or ticker in {"CASH", "EUR", "USD", "EFECTIVO"}
            or any(term in description for term in _NONQUOTED_TERMS)
            or any(_text(row.get(key)).casefold() in _NONQUOTED_TYPES for key in ("asset_type", "portfolio_block"))
        ):
            continue
        values[ticker] = values.get(ticker, 0.0) + value
    total = sum(values.values())
    if not math.isfinite(total) or total <= 0:
        return {}
    return {ticker: value / total * 100 for ticker, value in values.items()}


def _flag(value: object) -> bool:
    return _text(value).casefold() in {"true", "1", "sí", "si", "yes", "invalidated"}


def _invalidation(row: Mapping[str, object]) -> str | None:
    """Lee únicamente flags/metadata explícitos, nunca sentimiento o precio."""

    position: Mapping[str, object] = {}
    for key in ("Política de análisis", "analysis_policy", "policy"):
        policy = row.get(key)
        if isinstance(policy, Mapping) and isinstance(policy.get("position"), Mapping):
            position = policy["position"]
            break
    if not position:
        policy = decode_policy_note(row.get("note"))
        if policy is not None:
            position = policy["position"]
    explicit = (
        _flag(row.get("Tesis invalidada")) or _flag(row.get("invalidated"))
        or _text(row.get("thesis_status")) == "invalidated"
        or _flag(position.get("invalidated")) or _text(position.get("thesis_status")) == "invalidated"
    )
    reason = _text(position.get("invalidation_reason")) or _text(row.get("Motivo tesis")) or _text(row.get("invalidation_reason"))
    for raw in _text(row.get("comments")).splitlines():
        line = raw.strip()
        if line.startswith("[[SSL_TESIS_INVALIDADA]]"):
            explicit = True
            reason = line.removeprefix("[[SSL_TESIS_INVALIDADA]]").strip(" :-") or reason
            break
    return reason or "Invalidación de tesis declarada" if explicit else None


def _factor(result: object, key: str) -> str:
    values = _read(result, key)
    if isinstance(values, str):
        return " ".join(values.split())
    if isinstance(values, Iterable) and not isinstance(values, Mapping):
        return next((" ".join(_text(value).split()) for value in values if _text(value)), "")
    return ""


def _price_text(value: float) -> str:
    return f"{value:,.2f}" if value >= 1 else f"{value:.4g}"


def build_growth_candidate_rows(
    results: Mapping[str, object], *, held_tickers: Iterable[object] = (),
    portfolio_snapshot: pd.DataFrame | None = None, names: Mapping[str, object] | None = None,
    currencies: Mapping[str, str] | None = None,
    prepared: Mapping[str, pd.DataFrame] | None = None, universe: Iterable[object] = (),
    comparison_rows: Iterable[Mapping[str, object]] | None = None, reference_date: object = None,
) -> list[dict[str, object]]:
    """Devuelve una fila por instrumento de resultados, universo o cartera.

    ``held_tickers`` declara las posiciones vigentes; una fotografía sólo aporta
    su peso por valor en euros y su fecha, nunca cantidades ni operaciones. El
    denominador del peso incluye únicamente valores positivos cotizados de la
    última fotografía; una posición sin valor comprobable mantiene peso ``None``.
    Si se aporta ``prepared``, también se comprueba el último cierre/histórico.
    Todos los objetos de entrada se conservan sin mutaciones y no hay descargas.
    """

    growth = _normalized_mapping(results)
    frames = _normalized_mapping(prepared)
    company_names = _normalized_mapping(names)
    quote_currencies = _normalized_mapping(currencies)
    held = {_ticker(value) for value in held_tickers if _ticker(value)}
    tickers = dict.fromkeys([*growth, *(_ticker(value) for value in universe), *sorted(held)])
    latest, snapshot_date = _latest_snapshot(portfolio_snapshot)
    weights = _snapshot_weights(latest)
    invalidations: dict[str, str] = {}
    source_rows = [*latest.to_dict("records"), *(comparison_rows or ())]
    for source in source_rows:
        if not isinstance(source, Mapping):
            continue
        ticker = next((_ticker(source.get(key)) for key in ("Ticker", "ticker", "analysis_ticker") if _text(source.get(key))), "")
        if not ticker:
            continue
        if ticker not in company_names and _text(source.get("asset_name")):
            company_names[ticker] = source["asset_name"]
        invalidation = _invalidation(source)
        if invalidation:
            invalidations[ticker] = invalidation
    rows: list[dict[str, object]] = []
    for ticker in tickers:
        if not ticker:
            continue
        result = growth.get(ticker)
        complete, candidate, reading, as_of, price = _primary_reading(
            result, frames.get(ticker), check_frame=prepared is not None,
            ticker=ticker, reference_date=reference_date,
        )
        in_portfolio = ticker in held
        invalidation = invalidations.get(ticker) if in_portfolio else None
        group = "Mi cartera" if in_portfolio else "Nuevas entradas" if candidate else "Esperar"
        reason = _factor(result, "positive_factors") if candidate else _factor(result, "risk_factors")
        if invalidation:
            state = "Revisión prioritaria de tesis"
            next_step = "Contrastar invalidación declarada; sin venta automática"
            reason = invalidation
        elif not complete:
            state = "Gestionar según tesis" if in_portfolio else "Datos pendientes"
            next_step = "Completar datos; gestionar según tesis" if in_portfolio else "Completar datos antes de entrar"
            reason = reading
        elif in_portfolio:
            state = "Revisar ampliación" if candidate else "Gestionar según tesis"
            next_step = "Revisar ampliación: peso, tesis y riesgo" if candidate else "Gestionar según tesis; sin ampliación confirmada"
            reason = reason or reading
        elif candidate:
            state = "Revisar nueva entrada"
            next_step = "Confirmar precio, tamaño y riesgo antes de entrar"
            reason = reason or "Crecimiento y momentum favorables"
        else:
            state = "Esperar"
            next_step = "Esperar mejor precio" if reading == "Esperar mejor precio" else "Esperar confirmación de crecimiento y momentum"
            reason = reason or reading
        entry_price = price if complete and candidate and not invalidation else None
        raw_currency = quote_currencies.get(ticker)
        currency = _text(raw_currency) if isinstance(raw_currency, str) else ""
        currency = currency if currency == "GBp" else currency.upper()
        if not currency.isalpha() or not 3 <= len(currency) <= 5:
            currency = ""
        stop_pct = _number(_read(result, "atr_stop_pct"))
        stop = (
            entry_price * (1 - stop_pct / 100)
            if entry_price is not None and stop_pct is not None and 0 < stop_pct < 100 else None
        )
        if stop is not None and not 0 < stop < entry_price:
            stop = None
        reference = "Pendiente de datos" if not complete else "Sin nueva entrada confirmada"
        if invalidation:
            reference = "Revisar tesis antes de ampliar"
        elif entry_price is not None:
            reference = f"Referencia último cierre {_price_text(entry_price)}"
            reference += f" · stop teórico {_price_text(stop)}" if stop is not None else " · stop pendiente"
            reference += f" · {currency or 'moneda N/D'}"
        name = company_names.get(ticker)
        if isinstance(name, Mapping):
            name = next((_text(name.get(key)) for key in ("shortName", "longName", "name", "Empresa") if _text(name.get(key))), "")
        name = _text(name)
        rows.append({
            "Ticker": ticker, "Empresa": f"{name} · {ticker}" if name and name.upper() != ticker else ticker,
            "Grupo": group, "Estado": state, "En cartera": in_portfolio,
            "Peso cartera %": weights.get(ticker) if in_portfolio else None,
            "Fecha peso cartera": snapshot_date, "Qué revisar": next_step,
            "Entrada / stop orientativos": reference, "Motivo": reason, "Fecha": as_of,
            "Precio referencia": entry_price, "Stop orientativo": stop,
            "Datos completos": complete, "Lectura principal": _text(_read(result, "label")),
            "Nota principal": _score(_read(result, "score")), "Moneda": currency or None,
        })
    return rows
