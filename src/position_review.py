"""Manual position review from already prepared, versioned evidence.

No engines, downloads, orders or writes run here. Entry timing never invalidates
a holding thesis. Proposed position/sector caps are simple risk rules, not an
optimized portfolio or an allocation of available cash.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math

import pandas as pd

from src.analysis_policy import POLICY_VERSION, position_contexts_from_snapshot
from src.instruments import resolve_analysis_ticker
from src.market_data_quality import quote_freshness


DEFAULT_PRIORITY_TICKERS = ("ASTS", "XE", "DHR", "ANET", "HALO", "EXEL", "ASML")
COLUMNS = [
    "platform", "company", "ticker", "strategy", "strategy_id", "horizon", "thesis",
    "action", "reason", "latest_close", "quote_date", "price_label", "currency",
    "position_weight_pct", "company_weight_pct", "sector", "sector_weight_pct",
    "weights_current", "weight_basis", "target_company_weight_pct", "target_sector_weight_pct",
    "capacity_eur", "company_capacity_eur", "capacity_scope", "capacity_rule",
    "entry_zone", "entry_zone_lower", "entry_zone_upper", "entry_zone_reason",
    "risk_limitations", "policy_version", "policy_snapshot_id", "primary_status",
    "comparative_reading", "manual_review_only",
]


def _text(value: object, default: str = "") -> str:
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return default
    text = str(value).strip()
    return default if text.casefold() in {"nan", "nat", "none", "<na>", ""} else text


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _flag(value: object) -> bool:
    return _text(value).casefold() in {"true", "1", "sí", "si", "yes"}


def _ticker(row: Mapping[str, object]) -> str:
    raw = _text(row.get("analysis_ticker")) or _text(row.get("ticker")) or _text(row.get("Ticker"))
    return resolve_analysis_ticker(raw.upper()) if raw else ""


def _liquid(row: Mapping[str, object]) -> bool:
    liquidity = _text(row.get("liquidity_class")).casefold()
    if liquidity:
        return liquidity == "liquid"
    kind = _text(row.get("asset_type")).casefold()
    platform = _text(row.get("platform"))
    return kind not in {"cash", "efectivo", "liquidez", "factoring", "crowdlending", "préstamo", "prestamo"} and platform not in {"Civislend", "Segofactoring"}


def _valid_value(row: Mapping[str, object]) -> float | None:
    if "value_complete" in row and not _flag(row["value_complete"]):
        return None
    value = _number(row.get("value_eur"))
    return value if value is not None and value >= 0 else None


def _market_valued(row: Mapping[str, object], reference_date: object | None) -> bool:
    status = _text(row.get("valuation_status")).casefold()
    explicit = _flag(row.get("valuation_is_market")) or status.startswith(("precio actualizado", "precio aportado"))
    return explicit and quote_freshness(row.get("market_as_of"), reference_date=reference_date).fresh


def _latest_quote(frame: object) -> tuple[float | None, object | None, Mapping[str, object]]:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "close" not in frame:
        return None, None, {}
    # Do not fall back to an older valid price when the newest bar is corrupt.
    ordered = frame.sort_index()
    latest = ordered.iloc[-1]
    close = _number(latest.get("close"))
    return (close if close is not None and close > 0 else None), ordered.index[-1], latest.to_dict()


def _policy(source: Mapping[str, object]) -> Mapping[str, object]:
    policy = source.get("Política de análisis")
    if not isinstance(policy, Mapping) or policy.get("policy_version") != POLICY_VERSION:
        return {}
    if not all(isinstance(policy.get(key), Mapping) for key in ("position", "primary", "comparative")):
        return {}
    policy_ticker = _ticker(policy)
    if policy_ticker and policy_ticker != _ticker(source):
        return {}
    return policy


def _contract(row: Mapping[str, object], ticker: str, policy: Mapping[str, object]) -> dict[str, object]:
    local = position_contexts_from_snapshot([{**row, "analysis_ticker": ticker}]).get(ticker, {})
    stored = dict(policy.get("position", {}))
    # Each broker can hold a different documented contract. A consolidated
    # policy must not overwrite that per-position assignment with "mixed".
    explicit_local = _text(local.get("strategy_id")) not in {"", "previous_thesis"}
    result = dict(local) if explicit_local else {**local, **stored}
    if _flag(local.get("invalidated")):
        result.update(invalidated=True, invalidation_reason=local.get("invalidation_reason"))
    return result


def _entry_zone(primary: Mapping[str, object], latest: Mapping[str, object], close: float | None, ready: bool) -> tuple[str | None, float | None, float | None, str]:
    if not ready or not _flag(primary.get("entry_candidate")) or primary.get("data_status") != "complete":
        return None, None, None, "N/D: exige candidato principal vigente y evidencia técnica reciente."
    # These are already-computed moving averages, not new scores or a fair-value
    # model. The interval is a pullback reference, never an automatic limit order.
    short = _number(latest.get("sma_short"))
    medium = _number(latest.get("sma_medium"))
    if short is None or medium is None or close is None or min(short, medium) <= 0 or medium > short or close < medium:
        return None, None, None, "N/D: no hay medias corta/media coherentes que documenten una zona."
    lower, upper = medium, min(short, close)
    if lower > upper:
        return None, None, None, "N/D: referencias técnicas no coherentes."
    return f"{lower:,.2f} – {upper:,.2f}", lower, upper, "Referencia de retroceso entre las medias corta/media ya calculadas; confirmar soporte y volumen, no precio objetivo ni orden."


def build_position_review_rows(
    positions: pd.DataFrame,
    comparative_summary: Iterable[Mapping[str, object]] | pd.DataFrame,
    prepared: Mapping[str, pd.DataFrame],
    *,
    reference_date: object | None = None,
    max_company_weight_pct: float = 15.0,
    max_sector_weight_pct: float = 30.0,
    priority_tickers: Iterable[str] = DEFAULT_PRIORITY_TICKERS,
) -> pd.DataFrame:
    """Return one manual review row per actual quoted holding/account.

    All liquid holdings, including untickered funds, enter the weight denominator;
    cash and alternatives do not. Company weights aggregate tickers across brokers.
    Current capacity needs a complete, fresh market-valued denominator and full
    sector coverage. When only declared values exist, weights are explicitly
    historical/declarative and can support a concentration review, never sizing.

    ``capacity_eur`` is fixed-denominator headroom shared across all brokers, not
    available cash or the maximum external deposit. It appears only once for a
    company so summing the table cannot duplicate its capacity. None means N/D.
    """
    for value in (max_company_weight_pct, max_sector_weight_pct):
        if _number(value) is None or not 0 < float(value) <= 100:
            raise ValueError("Los límites de peso deben estar entre 0 y 100%.")
    if positions.empty:
        return pd.DataFrame(columns=COLUMNS)
    source_rows = comparative_summary.to_dict("records") if isinstance(comparative_summary, pd.DataFrame) else list(comparative_summary or [])
    by_ticker = {_ticker(row): row for row in source_rows if _ticker(row)}
    prepared = {resolve_analysis_ticker(str(key).upper()): value for key, value in prepared.items()}
    liquid = [dict(row) for row in positions.to_dict("records") if _liquid(row) and (_number(row.get("quantity")) is None or float(row["quantity"]) > 0)]
    liquid.sort(key=lambda row: (_ticker(row), _text(row.get("platform")) or _text(row.get("account_name"))))
    values = [_valid_value(row) for row in liquid]
    complete = bool(values) and all(value is not None for value in values)
    total = sum(values) if complete else None
    complete = complete and total is not None and total > 0
    current = complete and all(_market_valued(row, reference_date) for row in liquid)
    weight_basis = "Valoración de mercado fechada; cierre diario, no tiempo real" if current else "Valoración declarada/no actual; no peso cotizado vigente" if complete else "N/D: denominador líquido incompleto"
    sectors: dict[str, str] = {}
    company_values: dict[str, float] = {}
    for row, value in zip(liquid, values, strict=True):
        ticker = _ticker(row)
        key = ticker or _text(row.get("raw_identifier")) or _text(row.get("asset_name"))
        source = by_ticker.get(ticker, {})
        sector = _text(row.get("sector_label")) or _text(row.get("sector")) or _text(source.get("Sector"))
        if key in sectors and sectors[key] != sector:
            sectors[key] = ""
        else:
            sectors[key] = sector
        if value is not None:
            company_values[key] = company_values.get(key, 0.0) + value
    sector_complete = bool(liquid) and all(sectors.get(_ticker(row) or _text(row.get("raw_identifier")) or _text(row.get("asset_name"))) for row in liquid)
    sector_values: dict[str, float] = {}
    for key, value in company_values.items():
        if sectors.get(key):
            sector_values[sectors[key]] = sector_values.get(sectors[key], 0.0) + value
    result = []
    capacity_published: set[str] = set()
    for row, value in zip(liquid, values, strict=True):
        ticker = _ticker(row)
        # ISIN-only/missing-ticker assets remain in denominators but have no
        # invented company analysis. They are not market-review rows.
        if not ticker or len(ticker) == 12 and ticker[:2].isalpha() and ticker[-1].isdigit():
            continue
        source = by_ticker.get(ticker, {})
        policy = _policy(source)
        contract = _contract(row, ticker, policy)
        primary = policy.get("primary", {})
        comparative = policy.get("comparative", {})
        close, observed, technical = _latest_quote(prepared.get(ticker))
        freshness = quote_freshness(observed, reference_date=reference_date)
        primary_date = primary.get("as_of") or policy.get("as_of")
        analysis_fresh = quote_freshness(primary_date, reference_date=reference_date).fresh if policy else False
        sector = sectors.get(ticker, "")
        company_value = company_values.get(ticker, 0.0)
        company_weight = company_value / total * 100 if complete else None
        sector_weight = sector_values[sector] / total * 100 if complete and sector_complete and sector else None
        company_capacity = max(0.0, float(max_company_weight_pct) / 100 * total - company_value) if current else None
        sector_capacity = max(0.0, float(max_sector_weight_pct) / 100 * total - sector_values[sector]) if current and sector_complete and sector else None
        capacity = min(company_capacity, sector_capacity) if company_capacity is not None and sector_capacity is not None else None
        first_capacity_row = ticker not in capacity_published
        capacity_published.add(ticker)
        documented = _text(contract.get("strategy_id")) not in {"", "previous_thesis", "mixed"} and _text(contract.get("horizon")) not in {"", "No documentado", "Horizontes distintos / revisar por posición"} and bool(_text(contract.get("thesis")))
        reliable = bool(policy and close is not None and freshness.fresh and analysis_fresh and primary.get("data_status") == "complete")
        reasons: list[str] = []
        limitations = ["Propuesta para revisión manual; no ejecuta operaciones ni implica venta automática.", "Límites orientativos propuestos, no optimización ni presupuesto de efectivo."]
        if not policy:
            limitations.append("Falta política de análisis versionada compatible; no se sustituyen notas ni se calcula consenso.")
        if not freshness.fresh or close is None:
            limitations.append(f"Cotización pendiente: {freshness.status}; no inferir venta por falta de datos.")
        if policy and not analysis_fresh:
            limitations.append("La fecha del análisis principal falta o no está vigente.")
        if not current:
            limitations.append(weight_basis + "; capacidad de ampliación N/D.")
        if not sector_complete:
            limitations.append("Cobertura sectorial incompleta: no se afirma cumplimiento del límite sectorial.")
        if not documented:
            limitations.append("Completar estrategia, horizonte y tesis explícitos; no se asignan desde la puntuación.")
        if _flag(contract.get("invalidated")):
            action = "Estudiar venta"
            reasons.append("Invalidación de tesis declarada: " + _text(contract.get("invalidation_reason"), "contrastar el hecho y la evidencia."))
        elif (company_weight is not None and company_weight > max_company_weight_pct) or (sector_weight is not None and sector_weight > max_sector_weight_pct):
            action = "Reducir exposición"
            reasons.append("Revisar concentración por encima de la regla propuesta; no es una ruptura de tesis ni una orden de venta.")
        elif not reliable or not documented or not current or not sector_complete or comparative.get("data_status") != "complete":
            action = "Completar pendientes"
            reasons.append("Falta evidencia vigente, cobertura de valoración/sector o contrato de posición documentado; mantener la decisión pendiente, no vender por ausencia de datos.")
        elif _flag(primary.get("entry_candidate")) and current and capacity is not None and capacity > 0 and comparative.get("data_status") == "complete":
            action = "Revisar ampliación"
            reasons.append("Candidata del enfoque principal vigente; contrastar por separado calidad, valoración y riesgo antes de cualquier ampliación.")
        else:
            action = "Mantener"
            reasons.append("Sin invalidación explícita ni exceso de concentración; una entrada poco atractiva no obliga a vender.")
            if _flag(primary.get("entry_candidate")):
                reasons.append("La candidatura de entrada no autoriza ampliar sin datos de peso/sector, margen y contraste completos.")
        for factor in primary.get("risk_factors", ()) or ():
            if _text(factor):
                limitations.append(_text(factor))
        for check in primary.get("manual_checks", ()) or ():
            if _text(check):
                limitations.append(_text(check))
        entry, entry_lower, entry_upper, entry_reason = _entry_zone(primary, technical, close, reliable and documented)
        quote_date = freshness.market_date.isoformat() if freshness.market_date is not None else None
        scope = f"Margen compartido de {ticker} entre todas sus plataformas; no sumarlo por cuenta."
        if not first_capacity_row:
            scope += " Importe publicado en la primera fila de la empresa; no es cero ni un segundo margen."
        if not sector_complete:
            scope += " Margen individual condicionado; sector sin comprobar."
        result.append({
            "platform": _text(row.get("platform")) or _text(row.get("account_name"), "Sin plataforma"),
            "company": _text(row.get("asset_name"), ticker), "ticker": ticker,
            "strategy": _text(contract.get("strategy_label"), "No documentada"),
            "strategy_id": _text(contract.get("strategy_id"), "previous_thesis"),
            "horizon": _text(contract.get("horizon"), "No documentado"), "thesis": contract.get("thesis"),
            "action": action, "reason": " ".join(reasons), "latest_close": close,
            "quote_date": quote_date, "price_label": "Último cierre diario disponible; no tiempo real",
            "currency": _text(policy.get("currency")) or _text(row.get("currency")),
            "position_weight_pct": value / total * 100 if complete and value is not None else None,
            "company_weight_pct": company_weight, "sector": sector or "Sin sector informado",
            "sector_weight_pct": sector_weight, "weights_current": bool(current), "weight_basis": weight_basis,
            "target_company_weight_pct": float(max_company_weight_pct), "target_sector_weight_pct": float(max_sector_weight_pct),
            "capacity_eur": capacity if first_capacity_row else None,
            "company_capacity_eur": company_capacity if first_capacity_row else None,
            "capacity_scope": scope, "capacity_rule": "Margen hasta topes sobre valor líquido actual fijo; no incluye nuevas aportaciones, impuestos ni costes.",
            "entry_zone": entry, "entry_zone_lower": entry_lower, "entry_zone_upper": entry_upper,
            "entry_zone_reason": entry_reason, "risk_limitations": " ".join(dict.fromkeys(limitations)),
            "policy_version": policy.get("policy_version"), "policy_snapshot_id": policy.get("snapshot_id"),
            "primary_status": primary.get("status"), "comparative_reading": comparative.get("reading"),
            "manual_review_only": True,
        })
    priority = {resolve_analysis_ticker(str(ticker).upper()): index for index, ticker in enumerate(priority_tickers)}
    result.sort(key=lambda row: (priority.get(row["ticker"], len(priority)), row["ticker"], row["platform"]))
    return pd.DataFrame(result, columns=COLUMNS)
