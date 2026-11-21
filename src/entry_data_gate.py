"""Fail-closed data eligibility for current entry screens; no scoring or I/O."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from math import isclose, isfinite

import pandas as pd

from src.hourly_market_schedule import validated_completed_daily_prices
from src.radar_review import RadarReview


ENTRY_DATA_REASONS = {
    "review_not_verified": "Sin revisión de Radar confirmada",
    "review_pending": "Pendiente en la revisión actual",
    "review_outside_universe": "Fuera del universo de esta revisión",
    "review_failed": "La revisión no confirmó un cierre utilizable",
    "analysis_partial": "Precio guardado, análisis incompleto",
    "analysis_blocked": "La calidad de los datos impide confirmar entradas",
    "snapshot_mismatch": "El análisis en memoria no coincide con el resultado guardado",
    "stale_close": "Falta el último cierre esperado de su mercado",
    "invalid_ohlc": "Histórico de precios incoherente",
    "invalid_date_labels": "Fechas diarias incoherentes",
    "instrument_mismatch": "La fuente no confirmó el instrumento solicitado",
    "no_data": "La fuente no entregó observaciones utilizables",
    "currency_mismatch": "Moneda o unidad de cotización incoherente",
    "currency_unverified": "No se ha acreditado la moneda y unidad del precio",
    "unverified_session_labels": "No se pudo verificar la fecha de las velas",
    "unsupported_market": "Calendario de mercado sin verificar",
    "missing_completed_close": "Sin cierres verificables",
    "adjustment_unverified": "La fuente no acredita los ajustes del histórico",
}
_SUPPORTED_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CNY", "HKD", "KRW", "CHF", "CAD", "AUD",
    "ZAR", "ILS", "DKK", "SEK", "NOK", "TWD", "SGD", "BRL", "MXN", "NZD",
    "PLN", "CZK", "HUF", "TRY", "INR", "IDR", "MYR", "THB", "PHP", "AED", "SAR",
})


def normalized_quote_currency_verified(frame: pd.DataFrame) -> bool:
    """Internal unit contract, not an independent second-source price check."""
    return (frame.attrs.get("quote_units_normalized") is True
            and frame.attrs.get("display_currency") in _SUPPORTED_CURRENCIES)


@dataclass(frozen=True)
class EntryDataEligibility:
    prices: dict[str, pd.DataFrame]
    excluded: dict[str, str]


def verified_entry_prices(
    prepared: Mapping[str, pd.DataFrame], *, review: RadarReview | None,
    policies: Mapping[str, object] | None, now: object,
) -> EntryDataEligibility:
    """Admit only the exact saved analysis and the exchange's expected close.

    A failed refresh cannot reuse an old frame as a new entry. A recent frame
    alone cannot certify fundamentals or a persisted analysis. Historical
    frames remain in the caller's memory and are never modified here.
    """
    accepted, excluded = {}, {}
    tickers = list(review.tickers) if review is not None else list(prepared)
    tickers.extend(ticker for ticker in prepared if ticker not in tickers)
    for ticker in tickers:
        frame = prepared.get(ticker)
        outcome = review.outcomes.get(ticker) if review is not None else None
        if review is None:
            excluded[ticker] = "review_not_verified"
            continue
        if outcome is None:
            excluded[ticker] = "review_pending" if ticker in review.tickers else "review_outside_universe"
            continue
        if outcome.status != "complete":
            excluded[ticker] = "analysis_partial" if outcome.status == "partial" else "review_failed"
            continue
        if outcome.price_verified is not True:
            excluded[ticker] = "review_not_verified"
            continue
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            excluded[ticker] = "missing_completed_close"
            continue
        currency = str(frame.attrs.get("display_currency") or "")
        if not normalized_quote_currency_verified(frame):
            excluded[ticker] = "currency_unverified"
            continue
        policy = (policies or {}).get(ticker)
        if not isinstance(policy, dict) or not outcome.snapshot_id or policy.get("snapshot_id") != outcome.snapshot_id:
            excluded[ticker] = "snapshot_mismatch"
            continue
        if policy.get("currency") != currency:
            excluded[ticker] = "currency_mismatch"
            continue
        try:
            same_price = (isfinite(float(policy.get("price")))
                          and isclose(float(policy["price"]), float(frame["close"].iloc[-1]),
                                      rel_tol=1e-10, abs_tol=1e-8))
        except (TypeError, ValueError, KeyError, IndexError):
            same_price = False
        if (policy.get("ticker") != ticker or policy.get("as_of") != outcome.price_date or not same_price):
            excluded[ticker] = "snapshot_mismatch"
            continue
        quality = policy.get("data_quality")
        if (not isinstance(quality, dict) or quality.get("block_entry") is not False
                or any(not isinstance(policy.get(method), dict)
                       or policy[method].get("data_status") != "complete"
                       for method in ("primary", "comparative"))):
            excluded[ticker] = "analysis_blocked"
            continue
        try:
            checked = validated_completed_daily_prices(ticker, frame, now=now, require_currency=True)
            # Do not score indicators computed using a current partial bar or
            # an altered daily series, even if the filtered close is correct.
            if (not checked.index.equals(frame.index)
                    or pd.Timestamp(frame.index[-1]).date().isoformat() != outcome.price_date):
                excluded[ticker] = "stale_close"
                continue
        except (TypeError, ValueError) as exc:
            code = getattr(exc, "code", "missing_completed_close")
            excluded[ticker] = code if code in ENTRY_DATA_REASONS else "missing_completed_close"
            continue
        accepted[ticker] = frame
    return EntryDataEligibility(accepted, excluded)
