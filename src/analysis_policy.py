"""Lecturas independientes de crecimiento/momentum y calidad/valoración.

Este módulo sólo transforma datos ya calculados. No descarga información,
persiste registros, envía correos ni ejecuta operaciones. El total del motor
growth/momentum no se mezcla con las notas comparativas: tampoco se calcula una
nota de tesis o de estacionalidad que fingiría una precisión no disponible.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
import hashlib
import json
import math

from src.instruments import resolve_analysis_ticker
from src.analysis_data_quality import assess_analysis_data_quality


POLICY_VERSION = "dual_strategy_v1"
PRIMARY_STRATEGY_ID = "growth_momentum"
COMPARATIVE_STRATEGY_ID = "quality_value_thesis_risk"
PRIMARY_STRATEGY_LABEL = "Crecimiento + momentum"
COMPARATIVE_STRATEGY_LABEL = "Calidad / valoración / tesis / riesgo"
_THESIS_MARKER = "[[SSL_TESIS_INVALIDADA]]"
POSITION_PLAN_MARKER = "[[SSL_POSITION_PLAN]]"
POLICY_NOTE_MARKER = "[[SSL_ANALYSIS_POLICY]]"
_ENTRY_LABELS = frozenset({"Entrada fuerte", "Entrada candidata"})


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or str(value).casefold() in {"true", "false"}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _text(value: object) -> str:
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return ""
    result = str(value).strip()
    return "" if result.casefold() in {"nan", "nat", "none", "<na>"} else result


def _read(source: object, *keys: str) -> object:
    for key in keys:
        value = source.get(key) if isinstance(source, Mapping) else getattr(source, key, None)
        if _text(value):
            return value
    return None


def _score(value: object) -> int | float | None:
    parsed = _number(value)
    if parsed is None or not 0 <= parsed <= 100:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _flag(value: object) -> bool:
    return _text(value).casefold() in {"true", "sí", "si", "yes", "1", "invalidated"}


def _ticker(value: object) -> str:
    raw = _text(value)
    return resolve_analysis_ticker(raw) if raw else ""


def _timestamp(value: object) -> str | None:
    if not _text(value):
        return None
    formatter = getattr(value, "isoformat", None)
    return formatter() if callable(formatter) else _text(value)


def _daily_date(value: object) -> str | None:
    timestamp = _timestamp(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return timestamp


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return _timestamp(value)


def _snapshot_id(policy: Mapping[str, object]) -> str:
    contents = {key: value for key, value in policy.items() if key != "snapshot_id"}
    canonical = json.dumps(_json_safe(contents), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_policy_note(note: object, policy: Mapping[str, object]) -> str:
    """Conserva la nota humana y sustituye una única línea de metadata JSON."""

    retained = [line for line in _text(note).splitlines() if not line.strip().startswith(POLICY_NOTE_MARKER)]
    snapshot = dict(_json_safe(policy))
    snapshot["snapshot_id"] = _snapshot_id(snapshot)
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return "\n".join([*retained, f"{POLICY_NOTE_MARKER} {encoded}"]).strip()


def decode_policy_note(note: object) -> dict[str, object] | None:
    """Devuelve metadata comprobable, o None para una nota legacy/corrupta."""

    def reject_nonfinite(value: str) -> object:
        raise ValueError(f"Número JSON no finito: {value}")

    for raw_line in reversed(_text(note).splitlines()):
        line = raw_line.strip()
        if not line.startswith(POLICY_NOTE_MARKER):
            continue
        try:
            snapshot = json.loads(line.removeprefix(POLICY_NOTE_MARKER).strip(), parse_constant=reject_nonfinite)
            json.dumps(snapshot, allow_nan=False)
        except (TypeError, ValueError):
            return None
        if not isinstance(snapshot, dict) or not _text(snapshot.get("policy_version")):
            return None
        if not all(isinstance(snapshot.get(key), dict) for key in ("primary", "comparative", "position")):
            return None
        identifier = snapshot.get("snapshot_id")
        if identifier is not None and identifier != _snapshot_id(snapshot):
            return None
        return snapshot
    return None


def _manual_plan(comment: object) -> dict[str, object]:
    for raw in reversed(_text(comment).splitlines()):
        line = raw.strip()
        if not line.startswith(POSITION_PLAN_MARKER):
            continue
        try:
            decoded = json.loads(line.removeprefix(POSITION_PLAN_MARKER).strip())
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _comments(value: object) -> tuple[str, str]:
    lines: list[str] = []
    reason = ""
    for raw in _text(value).splitlines():
        line = raw.strip()
        if line.startswith(_THESIS_MARKER):
            reason = line.removeprefix(_THESIS_MARKER).strip(" :-") or "Ruptura de tesis declarada"
        elif line and not line.startswith(POSITION_PLAN_MARKER):
            lines.append(line)
    return "\n".join(lines), reason


def _strategy(value: object, explicit_id: object, *, held: bool) -> tuple[str, str]:
    label = _text(value)
    strategy_id = _text(explicit_id)
    if strategy_id:
        default = {
            PRIMARY_STRATEGY_ID: PRIMARY_STRATEGY_LABEL,
            COMPARATIVE_STRATEGY_ID: COMPARATIVE_STRATEGY_LABEL,
            "previous_thesis": "Tesis previa / estrategia no documentada",
            "mixed": "Estrategias distintas / revisar por posición",
        }.get(strategy_id, strategy_id)
        return strategy_id, label or default
    if not label:
        return (
            ("previous_thesis", "Tesis previa / estrategia no documentada")
            if held
            else (PRIMARY_STRATEGY_ID, PRIMARY_STRATEGY_LABEL)
        )
    normalized = label.casefold()
    if normalized in {"growth_momentum", "crecimiento + momentum", "crecimiento y momentum"}:
        return PRIMARY_STRATEGY_ID, PRIMARY_STRATEGY_LABEL if normalized == "growth_momentum" else label
    if normalized in {
        "quality_value_thesis_risk", "quality_value_risk", "calidad / valoración / tesis / riesgo",
        "calidad y valoración", "largo plazo", "calidad / valor",
    }:
        return COMPARATIVE_STRATEGY_ID, COMPARATIVE_STRATEGY_LABEL if normalized in {"quality_value_thesis_risk", "quality_value_risk"} else label
    return "custom", label


def _position(
    row: Mapping[str, object], context: Mapping[str, object], held: bool | None,
) -> dict[str, object]:
    original_comments = _read(context, "comments", "prior_comments") or row.get("comments")
    manual = _manual_plan(original_comments)
    context = {**manual, **context}
    in_portfolio = (
        held if held is not None
        else _flag(_read(context, "held", "En cartera")) or _flag(row.get("En cartera"))
    )
    comments, marker_reason = _comments(original_comments)
    invalidated = (
        _flag(_read(context, "invalidated", "Tesis invalidada"))
        or _flag(row.get("Tesis invalidada")) or bool(marker_reason)
    )
    thesis = _text(_read(context, "thesis", "Tesis", "Tesis posición")) or _text(
        _read(row, "thesis", "Tesis", "Tesis posición")
    ) or comments
    reason = _text(_read(context, "invalidation_reason", "Motivo tesis")) or _text(
        row.get("Motivo tesis")
    ) or marker_reason
    strategy_id, strategy_label = _strategy(
        _read(context, "strategy_label", "strategy", "Estrategia", "Estrategia posición")
        or _read(row, "strategy_label", "strategy", "Estrategia", "Estrategia posición"),
        _read(context, "strategy_id") or row.get("strategy_id"),
        held=bool(in_portfolio),
    )
    horizon = _text(_read(context, "horizon", "Horizonte", "Horizonte posición")) or _text(
        _read(row, "horizon", "Horizonte", "Horizonte posición")
    )
    if not horizon:
        horizon = "No documentado" if in_portfolio else "Medio plazo; confirmar antes de entrar"
    return {
        "held": bool(in_portfolio), "strategy_id": strategy_id,
        "strategy_label": strategy_label, "horizon": horizon,
        "thesis": thesis or None,
        "prior_comments": comments or None,
        "thesis_status": "invalidated" if invalidated else "documented" if thesis else "not_documented",
        "invalidated": invalidated, "invalidation_reason": reason or None,
        "contracts": _json_safe(context.get("contracts", [])),
    }


def _primary(row: Mapping[str, object], result: object) -> dict[str, object]:
    supplied = result is not None
    score = _score(_read(result, "score") if supplied else _read(row, "Nota principal", "growth_momentum_score"))
    growth = _score(_read(result, "growth_score") if supplied else _read(row, "Crecimiento", "growth_score"))
    momentum = _score(_read(result, "momentum_score") if supplied else _read(row, "Momentum", "momentum_score"))
    market = _score(_read(result, "context_score") if supplied else _read(row, "Contexto mercado", "context_score"))
    confidence = _score(_read(result, "confidence_pct") if supplied else _read(row, "Confianza principal", "growth_confidence_pct"))
    status = _text(_read(result, "label") if supplied else _read(row, "Lectura principal", "growth_label"))
    data_error = _text(_read(row, "Incidencia principal", "primary_error"))
    sector_key = _text(_read(result, "sector_key") or row.get("sector_key"))
    is_fund = sector_key == "etf" or _text(row.get("quoteType")).upper() in {"ETF", "MUTUALFUND"}
    missing_company = growth is None and not is_fund
    declared_incomplete = any(
        token in status.casefold() for token in ("pendiente", "insuficiente", "parcial", "sin datos")
    )
    complete = (
        score is not None and momentum is not None and market is not None
        and confidence is not None and confidence >= 45
        and bool(status) and not missing_company and not declared_incomplete and not data_error
    )
    if not status:
        status = "Lectura de crecimiento pendiente"
    if score is None or not supplied and all(value is None for value in (growth, momentum, market)):
        data_status = "pending"
    else:
        data_status = "complete" if complete else "partial"
    if not complete:
        # Una etiqueta aislada o una nota parcial nunca habilita una entrada.
        entry = "Completar datos antes de una nueva entrada"
        if status in _ENTRY_LABELS:
            status = "Datos de crecimiento incompletos"
    elif status in _ENTRY_LABELS:
        entry = "Candidata a nueva entrada; confirmar precio y tamaño"
    elif status == "Esperar mejor precio":
        entry = "Esperar mejor precio; no perseguir la subida"
    else:
        entry = "Sin nueva entrada confirmada"
    return {
        "id": PRIMARY_STRATEGY_ID, "strategy_id": PRIMARY_STRATEGY_ID,
        "label": PRIMARY_STRATEGY_LABEL, "score": score,
        "growth_score": growth, "momentum_score": momentum, "context_score": market,
        "confidence_pct": confidence, "status": status, "data_status": data_status,
        "data_error": data_error or None,
        "entry_reading": entry,
        "entry_candidate": complete and status in _ENTRY_LABELS,
        "source": "growth_momentum_result" if supplied else "explicit_row" if score is not None else "pending",
        "positive_factors": [_text(item) for item in (_read(result, "positive_factors") or ()) if _text(item)],
        "risk_factors": [_text(item) for item in (_read(result, "risk_factors") or ()) if _text(item)],
        "manual_checks": [_text(item) for item in (_read(result, "manual_checks") or ()) if _text(item)],
        "as_of": _daily_date(_read(result, "as_of")), "sector_key": sector_key or None,
    }


def _comparative(
    row: Mapping[str, object], fundamental_filter: object, position: Mapping[str, object],
) -> dict[str, object]:
    quality = _score(_read(row, "Calidad empresa", "Calidad comparativa", "company_score", "quality_score"))
    valuation = _score(_read(row, "Valoración", "Valoración comparativa", "valuation_score"))
    risk = _score(_read(row, "Riesgo controlado", "Riesgo comparativo", "risk_score"))
    data_error = _text(_read(row, "Incidencia comparativa", "comparative_error"))
    filters = _score(_read(fundamental_filter, "score"))
    filter_coverage = _score(_read(fundamental_filter, "coverage_pct"))
    if fundamental_filter is None:
        filters = _score(_read(row, "Filtro fundamental", "fundamental_filter_score"))
        filter_coverage = _score(_read(row, "Cobertura filtro", "fundamental_filter_coverage"))
    readings = []
    for name, score, favorable, weak, good_text, weak_text in (
        ("Calidad", quality, 65, 45, "sólida", "frágil"),
        ("Valoración", valuation, 60, 35, "razonable", "exigente"),
        ("Riesgo", risk, 60, 40, "más controlado", "elevado"),
    ):
        readings.append(
            f"{name}: sin datos" if score is None else
            f"{name}: {good_text}" if score >= favorable else
            f"{name}: {weak_text}" if score <= weak else f"{name}: mixto"
        )
    thesis_status = position["thesis_status"]
    readings.append({
        "invalidated": "Tesis: invalidación declarada; contrastar el hecho",
        "documented": "Tesis: conservar y revisar la evidencia previa",
        "not_documented": "Tesis: no documentada; no se puede inferir por el precio",
    }[str(thesis_status)])
    return {
        "id": COMPARATIVE_STRATEGY_ID, "strategy_id": COMPARATIVE_STRATEGY_ID,
        "label": COMPARATIVE_STRATEGY_LABEL, "quality_score": quality,
        "valuation_score": valuation, "risk_score": risk, "filter_score": filters,
        "filter_coverage_pct": filter_coverage,
        "quality_coverage_pct": _score(_read(row, "Cobertura fundamental", "fundamental_coverage_pct")),
        "thesis_status": thesis_status,
        "data_status": "complete" if not data_error and all(value is not None for value in (quality, valuation, risk)) else "partial",
        "data_error": data_error or None,
        "reading": "; ".join(readings),
    }


def _context(row: Mapping[str, object], provided: Mapping[str, object]) -> dict[str, object]:
    cycle = _text(_read(provided, "cycle", "cycle_context", "Contexto cíclico"))
    cycle_evidence = _text(_read(provided, "cycle_evidence"))
    seasonality = _text(_read(provided, "seasonality", "seasonal_context", "Contexto estacional"))
    seasonal_evidence = _text(_read(provided, "seasonality_evidence", "seasonal_evidence"))
    source = _text(_read(provided, "source", "Fuente"))
    sector = _text(row.get("Sector")).casefold()
    cyclical_sector = any(token in sector for token in (
        "energy", "material", "industrial", "consumer cyclical", "energía", "cíclic", "industria",
    ))
    cycle_status = "evidence_documented" if cycle and cycle_evidence else "hypothesis" if cycle else "sector_context" if cyclical_sector else "not_documented"
    if not cycle:
        cycle = (
            "Sensibilidad cíclica potencial por sector; validar fase, demanda y precios"
            if cyclical_sector else "Sin fase cíclica documentada"
        )
    elif cycle_status == "hypothesis":
        cycle = f"Hipótesis cíclica no validada: {cycle}"
    else:
        cycle = f"Contexto cíclico documentado: {cycle}"
    seasonal_validated = (
        _flag(_read(provided, "seasonality_validated", "seasonal_validated"))
        and bool(seasonal_evidence) and bool(source)
    )
    if seasonality:
        seasonal_status = "validated_context" if seasonal_validated else "unvalidated_hypothesis"
        seasonal_reading = f"{'Contexto estacional documentado' if seasonal_validated else 'Hipótesis estacional no validada'}: {seasonality}"
    else:
        seasonal_status = "not_documented"
        seasonal_reading = "Sin estacionalidad validada; el mes no es una señal de compra"
    return {
        "cycle": cycle, "cycle_status": cycle_status,
        "cycle_evidence": cycle_evidence or None,
        "seasonality": seasonal_reading, "seasonality_status": seasonal_status,
        "seasonality_evidence": seasonal_evidence or None, "source": source or None,
        "as_of": _timestamp(_read(provided, "as_of", "Fecha")),
        "affects_scores": False, "creates_entry_signal": False,
    }


def build_policy_snapshot(
    row: Mapping[str, object], *, growth_result: object = None,
    fundamental_filter: object = None, position_context: Mapping[str, object] | None = None,
    cycle_context: Mapping[str, object] | None = None, held: bool | None = None,
    config: Mapping[str, object] | None = None,
    fundamental_info: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Devuelve metadata JSON-safe reutilizable en app, notas y correos.

    ``row`` admite los nombres del resumen actual y nombres snake_case de
    snapshots. ``growth_result``/``fundamental_filter`` admiten resultados de
    los motores existentes o mappings. Los contextos sólo contienen evidencia
    ya aportada. Cero es una nota válida; NaN, infinito y booleanos no lo son.
    """

    position = _position(row, position_context or {}, held)
    primary = _primary(row, growth_result)
    comparative = _comparative(row, fundamental_filter, position)
    context = _context(row, cycle_context or {})
    technical = _text(_read(row, "Si ya la tienes", "position_label"))
    if not position["held"]:
        management = "No aplica: no hay posición declarada"
    elif position["invalidated"]:
        management = "Revisar posible salida por tesis declarada; contrastar el motivo"
    elif primary["data_status"] != "complete" and comparative["data_status"] != "complete":
        management = "Actualizar datos; no inferir una venta ni ampliar"
    elif technical in {"Vender", "Reducir"}:
        management = "Revisar riesgo técnico y exposición; no confundirlo con ruptura de tesis"
    elif primary["entry_candidate"]:
        management = "Mantener tesis previa; una nueva entrada no autoriza ampliar sin revisar peso y riesgo"
    else:
        management = "Mantener criterio de la tesis previa; sin ampliación confirmada"
    position["technical_reading"] = technical or None
    position["management_reading"] = management
    notes = (comparative["quality_score"], comparative["valuation_score"], comparative["risk_score"])
    if primary["data_status"] != "complete" or comparative["data_status"] != "complete":
        disagreement = {
            "status": "incomplete", "reading": "Falta evidencia para contrastar los dos enfoques; no hay consenso calculado",
        }
    elif primary["entry_candidate"] and any(float(value) < 45 for value in notes if value is not None):
        disagreement = {
            "status": "different_readings", "reading": "Momentum de entrada favorable, pero calidad, valoración o riesgo requieren cautela",
        }
    elif not primary["entry_candidate"] and all(float(value) >= threshold for value, threshold in zip(notes, (65, 60, 50), strict=True) if value is not None):
        disagreement = {
            "status": "different_readings", "reading": "Calidad y valoración favorables, sin nueva entrada de momentum confirmada",
        }
    else:
        disagreement = {
            "status": "independent", "reading": "Lecturas independientes; no se promedian ni generan una recomendación de consenso",
        }
    price = None
    for value in (row.get("Precio"), row.get("price"), row.get("Cierre"), _read(growth_result, "price")):
        price = _number(value)
        if price is not None:
            break
    policy = {
        "policy_version": POLICY_VERSION,
        "snapshot_method": "dual",
        "ticker": _ticker(_read(row, "Ticker", "ticker", "analysis_ticker")),
        "as_of": _daily_date(_read(row, "Fecha", "as_of", "analyzed_at", "Datos hasta") or _read(growth_result, "as_of")),
        "source": _text(_read(row, "Fuente", "source", "Origen")) or None,
        "price": price,
        "currency": _text(_read(row, "Moneda", "Currency", "currency")).upper() or None,
        "config": _json_safe(config or {}),
        "primary": primary, "comparative": comparative, "position": position,
        "context": context, "disagreement": disagreement,
    }
    if fundamental_info is not None:
        # This independent presentation gate never edits scores or engine
        # labels and never turns absent company data into a sell signal.
        policy["data_quality"] = assess_analysis_data_quality(fundamental_info, as_of=policy["as_of"])
    policy["snapshot_id"] = _snapshot_id(policy)
    return policy


def enrich_analysis_rows(
    rows: Iterable[Mapping[str, object]], *, growth_results: Mapping[str, object] | None = None,
    fundamental_filters: Mapping[str, object] | None = None,
    position_contexts: Mapping[str, Mapping[str, object]] | None = None,
    held_tickers: Iterable[object] = (),
    cycle_contexts: Mapping[str, Mapping[str, object]] | None = None,
    config: Mapping[str, object] | None = None,
    fundamental_info: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Añade columnas de presentación sin cambiar ni ordenar las filas originales."""

    def normalize(values: Mapping[str, object] | None) -> dict[str, object]:
        return {_ticker(key): value for key, value in (values or {}).items() if _ticker(key)}

    growth = normalize(growth_results)
    filters = normalize(fundamental_filters)
    positions = normalize(position_contexts)
    cycles = normalize(cycle_contexts)
    infos = normalize(fundamental_info)
    held = {_ticker(ticker) for ticker in held_tickers if _ticker(ticker)}
    enriched = []
    for source in rows:
        row = dict(source)
        ticker = _ticker(_read(row, "Ticker", "ticker", "analysis_ticker"))
        policy = build_policy_snapshot(
            row, growth_result=growth.get(ticker), fundamental_filter=filters.get(ticker),
            position_context=positions.get(ticker), cycle_context=cycles.get(ticker),
            held=True if ticker in held else None,
            config=config,
            fundamental_info=infos.get(ticker) if fundamental_info is not None else None,
        )
        primary, comparative, position = policy["primary"], policy["comparative"], policy["position"]
        row.update({
            "Enfoque principal": primary["label"], "Nota principal": primary["score"],
            "Lectura principal": primary["status"], "Crecimiento": primary["growth_score"],
            "Momentum": primary["momentum_score"], "Contexto mercado": primary["context_score"],
            "Confianza principal": primary["confidence_pct"],
            "Enfoque comparativo": comparative["label"], "Calidad comparativa": comparative["quality_score"],
            "Valoración comparativa": comparative["valuation_score"], "Riesgo comparativo": comparative["risk_score"],
            "Lectura comparativa": comparative["reading"], "Nueva entrada": primary["entry_reading"],
            "Gestión posición": position["management_reading"], "Estrategia posición": position["strategy_label"],
            "Horizonte posición": position["horizon"], "Tesis posición": position["thesis"],
            "Diferencia enfoques": policy["disagreement"]["reading"],
            "Contexto cíclico": policy["context"]["cycle"], "Contexto estacional": policy["context"]["seasonality"],
            "Política de análisis": policy,
        })
        enriched.append(row)
    return enriched


def position_contexts_from_snapshot(rows: Iterable[Mapping[str, object]]) -> dict[str, dict[str, object]]:
    """Lee una fotografía ya seleccionada, preservando comentarios y conflictos.

    La llamada con DataFrame se hace con ``snapshot.to_dict('records')``. Este
    helper no selecciona fechas ni reinterpreta lenguaje libre como un horizonte.
    Lotes del mismo ticker con contratos distintos se señalan como mixtos.
    """

    contexts: dict[str, dict[str, object]] = {}
    for row in rows:
        ticker = _ticker(_read(row, "analysis_ticker", "Ticker", "ticker"))
        if not ticker:
            continue
        context = _position(row, row, True)
        if ticker not in contexts:
            contexts[ticker] = context
            continue
        previous = contexts[ticker]
        if not previous["contracts"]:
            previous["contracts"] = [{key: value for key, value in previous.items() if key != "contracts"}]
        previous["contracts"].append({key: value for key, value in context.items() if key != "contracts"})
        if (previous["strategy_id"], previous["strategy_label"]) != (context["strategy_id"], context["strategy_label"]):
            previous["strategy_id"] = "mixed"
            previous["strategy_label"] = "Estrategias distintas / revisar por posición"
        if previous["horizon"] != context["horizon"]:
            previous["horizon"] = "Horizontes distintos / revisar por posición"
        theses = list(dict.fromkeys(value for value in (previous["thesis"], context["thesis"]) if value))
        previous["thesis"] = "\n".join(str(value) for value in theses) or None
        if context["invalidated"]:
            previous["invalidated"] = True
            previous["thesis_status"] = "invalidated"
            previous["invalidation_reason"] = context["invalidation_reason"]
        elif previous["thesis"] and not previous["invalidated"]:
            previous["thesis_status"] = "documented"
    return contexts
