"""Presentation data gates, independent of the frozen investment engines.

Annual observations stay annual. A newer balance-sheet date does not certify
the freshness of income/growth data. These checks never change a score or
create a sell instruction.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import hashlib
import json
import math

QUALITY_VERSION = "daily_data_quality_v1"
GROWTH_METRICS = (
    "revenueGrowth", "earningsGrowth", "operatingMargins",
    "returnOnEquity", "freeCashflow",
)
METRIC_NAMES = {
    "revenueGrowth": "crecimiento de ingresos",
    "earningsGrowth": "crecimiento del beneficio",
    "operatingMargins": "margen operativo",
    "returnOnEquity": "rentabilidad sobre recursos propios",
    "freeCashflow": "flujo de caja libre",
}
# Disclosure controls, not entry/exit thresholds or optimised score parameters.
MAX_ANNUAL_AGE_DAYS = 450
MAX_INTERIM_AGE_DAYS = 210


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _source(value: object) -> str:
    text = str(value or "").strip()
    if text in {"SEC EDGAR", "Yahoo Finance", "Yahoo Financial Statements"}:
        return text
    return "Fuente declarada" if text else "Sin fuente por métrica"


def _period_metadata(value: object, source: object) -> dict[str, object]:
    """Whitelist public provenance; never propagate arbitrary info fields."""
    raw = value if isinstance(value, Mapping) else {}
    period = str(raw.get("periodicity") or "unknown").lower()
    if period not in {"annual", "quarterly", "interim", "ttm", "instant", "provider_summary"}:
        period = "unknown"
    components = []
    for item in raw.get("components", ()) if isinstance(raw.get("components"), (tuple, list)) else ():
        if not isinstance(item, Mapping):
            continue
        components.append({
            "role": str(item.get("role") or "")[:80],
            "comparison_base": item.get("comparison_base") is True,
            "start": _date(item.get("start")).isoformat() if _date(item.get("start")) else None,
            "end": _date(item.get("end")).isoformat() if _date(item.get("end")) else None,
            "filed": _date(item.get("filed")).isoformat() if _date(item.get("filed")) else None,
            "form": str(item.get("form") or "")[:16],
            "unit": str(item.get("unit") or "")[:24],
            "concept": str(item.get("concept") or "")[:120],
            "namespace": str(item.get("namespace") or "")[:30],
            "value": _number(item.get("value")),
        })
    end, start, filed = (_date(raw.get(key)) for key in ("period_end", "period_start", "filed_at"))
    return {
        "source": _source(raw.get("source") or source), "periodicity": period,
        "period_end": end.isoformat() if end else None,
        "period_start": start.isoformat() if start else None,
        "filed_at": filed.isoformat() if filed else None,
        "comparable": raw.get("comparable") if isinstance(raw.get("comparable"), bool) else None,
        "components": components,
    }


def assess_analysis_data_quality(
    info: Mapping[str, object] | None,
    as_of: object = None,
) -> dict[str, object]:
    """Describe validity of a new-entry reading without editing raw scores.

    A disclosed annual observation can have non-blocking review status. Old,
    unknown or mismatched metrics and exceptional bases require validation
    before an engine label is promoted as a purchase candidate. Existing
    holdings retain their independent thesis and risk rules.
    """
    snapshot = info if isinstance(info, Mapping) else {}
    reference = _date(as_of) or datetime.now(timezone.utc).date()
    sources = snapshot.get("_sources") if isinstance(snapshot.get("_sources"), Mapping) else {}
    raw_periods = snapshot.get("_metric_periods") if isinstance(snapshot.get("_metric_periods"), Mapping) else {}
    periods: dict[str, dict[str, object]] = {}
    values = {key: number for key in GROWTH_METRICS if (number := _number(snapshot.get(key))) is not None}
    reasons: list[str] = []
    blocked = False
    unknown = False

    def reason(text: str, *, block: bool = False) -> None:
        nonlocal blocked
        if text not in reasons:
            reasons.append(text)
        blocked |= block

    quote_type = str(snapshot.get("quoteType") or "").upper()
    if quote_type in {"ETF", "MUTUALFUND"}:
        reason("Fondo: la lectura se basa en precio y contexto, no en ventas o beneficios propios.")
    elif len(values) < 2 or snapshot.get("_quick_mode") or snapshot.get("_fundamental_error"):
        unknown = True
        reason("Faltan datos empresariales verificables; completar el análisis antes de una nueva compra.", block=True)

    if quote_type not in {"ETF", "MUTUALFUND"}:
        for metric, value in values.items():
            metadata = _period_metadata(raw_periods.get(metric), sources.get(metric))
            periods[metric] = metadata
            label = METRIC_NAMES[metric]
            end = _date(metadata["period_end"])
            periodicity = metadata["periodicity"]
            if end is None or periodicity in {"unknown", "provider_summary"}:
                unknown = True
                reason("No consta el periodo de cada métrica empresarial; una fecha general de la fuente no acredita su actualización.", block=True)
            else:
                age = (reference - end).days
                if age < 0:
                    reason(f"El periodo de {label} es posterior a la fecha del análisis; revisar disponibilidad de los datos.", block=True)
                elif age > (MAX_ANNUAL_AGE_DAYS if periodicity == "annual" else MAX_INTERIM_AGE_DAYS):
                    reason(f"El dato de {label} corresponde a {end.isoformat()} y está desactualizado para validar una entrada.", block=True)
                elif periodicity == "annual":
                    reason("El crecimiento usa cuentas anuales: no equivale al último trimestre publicado.")
            if metadata["comparable"] is False:
                reason(f"La métrica de {label} mezcla periodos o unidades no comparables; validar antes de comprar.", block=True)
            if metric in {"revenueGrowth", "earningsGrowth"} and any(
                item.get("comparison_base") is True
                and item.get("value") is not None and item["value"] <= 0
                for item in metadata["components"]
            ):
                reason(f"La base anterior del {label} no es positiva: comprobar el cambio de signo antes de interpretar el porcentaje.", block=True)
            filed = _date(metadata["filed_at"])
            if filed is not None and filed > reference:
                reason("Hay cuentas publicadas después del cierre de precios usado; no interpretar la nota como una señal conocida en aquel cierre.")

        revenue = _number(snapshot.get("totalRevenue"))
        if revenue is not None and revenue <= 0:
            reason("Ingresos totales no positivos: revisar la base contable antes de interpretar crecimientos o márgenes.", block=True)
        operating_margin = _number(snapshot.get("operatingMargins"))
        if operating_margin is not None and operating_margin > 1:
            reason("El margen operativo supera el 100 % de los ingresos: comprobar conceptos, periodos y posibles extraordinarios.", block=True)
        earnings_growth = values.get("earningsGrowth")
        revenue_growth = values.get("revenueGrowth")
        if earnings_growth is not None and abs(earnings_growth) >= 10:
            reason("El crecimiento del beneficio es excepcional: revisar efecto base y extraordinarios; la nota no prueba que sea sostenible.", block=True)
        if revenue_growth is not None and abs(revenue_growth) >= 5:
            reason("El crecimiento de ingresos es excepcional: validar adquisiciones, base de comparación y conceptos contables.", block=True)

    status = "blocked" if blocked and not unknown else "unknown" if unknown else "review" if reasons else "ok"
    fingerprint = {
        "version": QUALITY_VERSION, "quote_type": quote_type,
        "metrics": {key: {"value": value, "provenance": periods.get(key)} for key, value in values.items()},
    }
    comparability = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "version": QUALITY_VERSION, "status": status, "block_entry": blocked,
        "reasons": reasons, "metric_periods": periods,
        "comparability_key": comparability, "as_of": reference.isoformat(),
        "not_a_sell_signal": True, "affects_engine_scores": False,
    }
