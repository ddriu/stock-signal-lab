"""Read-only investment workspace built from one observed, sealed report.

This module is presentation policy, not a new signal engine. It never fetches,
persists, combines strategy scores or turns missing evidence into an order.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from math import isfinite
import re
import unicodedata

import pandas as pd

from src.analysis_policy import COMPARATIVE_STRATEGY_ID, PRIMARY_STRATEGY_ID, decode_policy_note
from src.daily_digest import _session_close, daily_score_coverage, daily_score_reading, seal_daily_score_report
from src.instruments import resolve_analysis_ticker, resolve_company_name
from src.market_data_quality import quote_freshness


WORKSPACE_VERSION = "investment-workspace-v1"
_HORIZONS = {
    "days": ("Días", "Revisión diaria de operaciones de varios días", "Momento técnico"),
    "weeks": ("Semanas", "Crecimiento y momentum, sin mezclar las otras notas", "Crecimiento + momentum"),
    "long_term": ("Largo plazo", "Tesis, calidad y valoración; la nota de entrada no decide una salida", "Calidad, valoración y tesis"),
}
_ENTRY_LABELS = {"Entrada fuerte", "Entrada interesante", "Entrada candidata"}


def _text(value: object, limit: int = 500) -> str:
    if value is None:
        return ""
    result = str(value).strip()
    return "" if result.casefold() in {"nan", "none", "<na>", "nat"} else result[:limit]


def _ticker(value: object) -> str:
    try:
        return resolve_analysis_ticker(_text(value, 64))
    except ValueError:
        return ""


def _score(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not isfinite(number) or not 0 <= number <= 100:
        return None
    return int(number) if number.is_integer() else number


def _day(value: object) -> date | None:
    try:
        stamp = pd.Timestamp(value)
        return None if pd.isna(stamp) else stamp.date()
    except (ValueError, TypeError, OverflowError):
        return None


def _stamp(value: object) -> pd.Timestamp | None:
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            return None
        return stamp.tz_convert("UTC")
    except (ValueError, TypeError, OverflowError):
        return None


def _stored_stamp(value: object) -> pd.Timestamp | None:
    """SQLite legacy UTC text follows the report builder's UTC convention."""
    try:
        stamp = pd.to_datetime(value, errors="coerce", utc=True)
        return None if pd.isna(stamp) else pd.Timestamp(stamp)
    except (ValueError, TypeError, OverflowError):
        return None


def _records(value: object) -> list[dict]:
    if isinstance(value, pd.DataFrame):
        return value.to_dict("records")
    if isinstance(value, Mapping):
        if any(key in value for key in ("ticker", "analysis_ticker", "Ticker")):
            return [dict(value)]
        return [dict(item, ticker=key) if isinstance(item, Mapping) else {"ticker": key}
                for key, item in value.items()]
    if isinstance(value, str):
        return [{"ticker": value}]
    return [dict(item) if isinstance(item, Mapping) else {"ticker": item}
            for item in (value or ())]


def _fold(value: object) -> str:
    return "".join(character for character in unicodedata.normalize("NFKD", _text(value).casefold())
                   if not unicodedata.combining(character))


def _horizon(position: Mapping[str, object]) -> str | None:
    """Use only a declared horizon; a company name or score is not a plan."""
    value = _fold(position.get("horizon"))
    if position.get("strategy_id") == "mixed" or any(word in value for word in ("distintos", "diferentes", "mixed")):
        return None
    if not value or value in {"no documentado", "sin definir", "pendiente", "no definido"}:
        return None
    if any(word in value for word in ("largo", "long", "anual", "12 meses")) or re.search(r"\b(?:ano|anos|year|years)\b", value):
        return "long_term"
    if any(word in value for word in ("seman", "weeks", "medio plazo", "medium", "meses")):
        return "weeks"
    if any(word in value for word in ("corto", "short")) or re.search(r"\b(?:dia|dias|day|days)\b", value):
        return "days"
    return None


def _snapshot_index(snapshots: object) -> dict[str, list[tuple[dict, dict]]]:
    output: dict[str, list[tuple[dict, dict]]] = {}
    for record in _records(snapshots):
        policy = decode_policy_note(record.get("note"))
        if policy and policy.get("snapshot_id"):
            output.setdefault(str(policy["snapshot_id"]), []).append((record, policy))
    return output


def _matching_snapshot(row: Mapping, index: Mapping) -> tuple[dict | None, dict | None]:
    identity, ticker = row.get("snapshot_id"), _ticker(row.get("ticker"))
    recorded = _stamp(row.get("recorded_at"))
    if not identity or recorded is None:
        return None, None
    matches = [(record, policy) for record, policy in index.get(identity, ())
               if _ticker(record.get("ticker")) == ticker == _ticker(policy.get("ticker"))
               and _day(record.get("analyzed_at")) == _day(policy.get("as_of")) == _day(row.get("session"))
               and _stored_stamp(record.get("created_at")) == recorded]
    if not matches:
        return None, None
    # Same policy identity can legitimately have repeated storage rows. Never
    # choose a technical winner if the external score fields disagree.
    technical = {(_score(record.get("entry_score")), _text(record.get("entry_label")),
                  _text(record.get("price"))) for record, _ in matches}
    return (matches[0][0] if len(technical) == 1 else None), matches[0][1]


def _base(row: Mapping, names: Mapping, held: set[str]) -> dict:
    ticker = _ticker(row.get("ticker"))
    return {"ticker": ticker, "company_name": resolve_company_name(ticker, names.get(ticker), row.get("company_name")),
            "held": ticker in held, "as_of": _text(row.get("session")) or None,
            "source": _text(row.get("source")) or "Fuente no documentada",
            "snapshot_id": row.get("snapshot_id"), "score": None, "score_kind": "Sin nota conjunta",
            "signal": _text(row.get("label")) or "Sin análisis guardado", "action": "Revisar datos",
            "reason": "No hay evidencia suficiente para una decisión nueva.",
            "next_step": "Completar la revisión; no interpretar la falta de datos como una venta.",
            "data_status": "pending", "strategy": "Sin estrategia asignada", "horizon": "Sin plazo definido",
            "horizon_key": None, "evidence": [], "priority": 4}


def _dated(row: Mapping, reference: date | None, report_valid: bool, *,
           clock: pd.Timestamp | None, as_of: date | None) -> bool:
    recorded, session = _stamp(row.get("recorded_at")), _day(row.get("session"))
    if (not report_valid or reference is None or recorded is None or session is None
            or as_of is None or session > as_of or recorded.date() > reference
            or clock is not None and recorded > clock or not _text(row.get("source"))
            or not quote_freshness(session, reference_date=reference).fresh):
        return False
    close = _session_close(_ticker(row.get("ticker")), session.isoformat())
    return close is not None and recorded >= close


def _strategy_kind(position: Mapping) -> str | None:
    identity = position.get("strategy_id")
    if identity == PRIMARY_STRATEGY_ID:
        return "growth"
    if identity == COMPARATIVE_STRATEGY_ID:
        return "quality"
    if identity == "technical" or _fold(position.get("strategy_label")) in {"momento tecnico", "tendencia tecnica", "tecnica"}:
        return "technical"
    return None


def _portfolio_row(base: dict, row: Mapping, policy: Mapping | None, dated: bool,
                   record: Mapping | None = None) -> dict:
    position = policy.get("position", {}) if policy else {}
    position = position if isinstance(position, Mapping) else {}
    output = {**base, "strategy": _text(position.get("strategy_label")) or "Sin estrategia asignada",
              "horizon": _text(position.get("horizon")) or "Sin plazo definido",
              "horizon_key": _horizon(position), "score": _score(row.get("score")),
              "score_kind": "Crecimiento + momentum", "priority": 3}
    output["plan_status"] = ("unverified" if policy is None else
                             "missing" if output["horizon_key"] is None else "documented")
    kind = _strategy_kind(position)
    if kind == "technical":
        output.update(score=_score(record.get("entry_score")) if record else None,
                      score_kind="Momento técnico")
    elif kind != "growth":
        output.update(score=None, score_kind="Sin nota conjunta")
    comparative = policy.get("comparative", {}) if policy else {}
    comparative = comparative if isinstance(comparative, Mapping) else {}
    usable = dated and (
        kind == "growth" and output["score"] is not None and not daily_score_reading(row)["blocked"]
        or kind == "technical" and output["score"] is not None and bool(_text(record.get("entry_label")))
        or kind == "quality" and comparative.get("data_status") == "complete"
    )
    invalidated = position.get("invalidated") is True
    if invalidated:
        output.update(action="Revisar posible salida", priority=0,
            reason=_text(position.get("invalidation_reason")) or "La tesis figura como invalidada; hay que contrastar el hecho.",
            next_step="Confirmar el motivo de invalidación, exposición y costes antes de decidir. No se envía una orden.")
    elif policy is None:
        output.update(action="Comprobar plan", priority=1,
            reason="El informe ligero no incluye el contexto del plan; su horizonte todavía no se ha verificado.",
            next_step="Abrir la cartera y consultar la estrategia y el plan existentes, sin sustituirlos ni deducirlos por la puntuación.")
    elif output["horizon_key"] is None:
        output.update(action="Definir plan", priority=1,
            reason="No consta un plazo definido para esta posición.",
            next_step="Asignar estrategia, horizonte y condición de salida; no deducirlos por la pérdida o por el nombre de la empresa.")
    elif not dated or kind is not None and not usable:
        output.update(action="Actualizar datos", priority=1,
            reason="La posición no tiene una lectura reciente y suficiente de su estrategia en este informe.",
            next_step="Completar la revisión de su estrategia. Los datos ausentes o bloqueados no son una señal de venta ni de ampliación.")
    elif output["horizon_key"] == "long_term":
        if position.get("thesis_status") != "documented":
            output.update(action="Documentar tesis", priority=1,
                reason="El horizonte es de largo plazo, pero no consta la tesis que justifica mantenerla.",
                next_step="Documentar negocio, valoración y hechos que invalidarían la inversión. Una nota técnica baja no decide vender.")
        else:
            output.update(action="Revisar tesis", reason="La tesis está documentada; contrastar sus hechos y la valoración actual.",
                next_step="Revisar cuentas, valoración y concentración; mantener o salir depende de esa tesis, no de una nota de entrada aislada.")
    elif kind == "quality":
        output.update(action="Revisar tesis", reason="Tu estrategia de calidad y valoración requiere contrastar la tesis y sus hechos.",
            next_step="Revisar negocio y valoración. Una señal técnica auxiliar no confirma una salida de esta estrategia.")
    elif position.get("technical_reading") in {"Vender", "Reducir"} and kind == "technical":
        output.update(action="Revisar exposición", priority=0,
            reason="La regla técnica guardada indica revisar el riesgo de la posición.",
            next_step="Contrastar la regla de salida de su estrategia, tamaño y costes; no confundirla con la tesis de largo plazo.")
    elif position.get("technical_reading") in {"Vender", "Reducir"}:
        output.update(action="Contrastar señal técnica", priority=1,
            reason="El motor técnico emite un aviso auxiliar; no es una salida confirmada por la estrategia declarada.",
            next_step="Comprobar los criterios de salida de tu estrategia y la exposición. No vender sólo por una lectura auxiliar.")
    elif (kind == "growth" and not daily_score_reading(row)["blocked"] and row.get("status") in {"entry_strong", "entry_candidate"}
          or kind == "technical" and record is not None and _score(record.get("entry_score")) is not None
          and record.get("entry_label") in _ENTRY_LABELS):
        output.update(action="Revisar ampliación", priority=2,
            reason="La estrategia declarada tiene una candidatura de entrada; una posición ya abierta requiere revisar su peso.",
            next_step="Confirmar concentración, riesgo y coste de ampliar; no aumentar automáticamente por la puntuación.")
    else:
        output.update(action="Seguir plan", reason="No hay una nueva ampliación confirmada por esta lectura.",
            next_step="Revisar la condición de salida de la estrategia. Una mala entrada actual no obliga a vender lo que ya tienes.")
    output["data_status"] = "observed" if usable else "pending"
    if position.get("technical_reading"):
        output["evidence"] = ["Regla técnica guardada: " + _text(position["technical_reading"])]
    return output


def build_investment_workspace(report: Mapping | None, *, holdings: object = (), snapshots: object = None,
                               company_names: Mapping | None = None, reference_date: object = None) -> dict:
    """Deterministic view of observed evidence; caller supplies review date.

    ``holdings`` accepts tickers, mappings or a DataFrame. Snapshot policies are
    used only when their identity, session and recording time match the report.
    Missing reference dates fail closed. No scores or trade rules are changed.
    """
    reference = _day(reference_date)
    reference_clock = _stamp(reference_date)
    names = {_ticker(key): value for key, value in (company_names or {}).items() if _ticker(key)}
    held: set[str] = set()
    for position in _records(holdings):
        ticker = _ticker(position.get("ticker") or position.get("analysis_ticker") or position.get("Ticker"))
        quantity = position.get("quantity")
        if quantity is not None:
            try:
                if isfinite(float(quantity)) and float(quantity) <= 0:
                    continue
            except (TypeError, ValueError, OverflowError):
                pass
        if ticker:
            held.add(ticker)
            names.setdefault(ticker, position.get("asset_name") or position.get("name") or ticker)
    valid = False
    if isinstance(report, Mapping):
        try:
            valid = bool(report.get("report_id")) and seal_daily_score_report(report)["report_id"] == report["report_id"]
        except (ValueError, TypeError, OverflowError):
            valid = False
    as_of = _day(report.get("as_of")) if isinstance(report, Mapping) else None
    valid = bool(valid and as_of and reference and as_of <= reference)
    raw_rows = report.get("rows", []) if valid and isinstance(report.get("rows"), list) else []
    grouped: dict[str, list[Mapping]] = {}
    for row in raw_rows:
        if isinstance(row, Mapping) and (ticker := _ticker(row.get("ticker"))):
            grouped.setdefault(ticker, []).append(row)
            if row.get("held") is True:
                held.add(ticker)
    rows = []
    duplicates = []
    for ticker in sorted(set(grouped) | held):
        entries = grouped.get(ticker, [])
        row = dict(entries[0]) if len(entries) == 1 else {"ticker": ticker, "held": ticker in held,
            "score": None, "status": "review_data", "quality": {"block_entry": True},
            "label": "Sin análisis verificable"}
        if len(entries) > 1:
            duplicates.append(ticker)
        rows.append(row)
    index = _snapshot_index(snapshots)
    horizons = {key: {"title": title, "description": description, "strategy": strategy,
                     "portfolio": [], "opportunities": [], "waiting": [], "research": [], "limitations": []}
                for key, (title, description, strategy) in _HORIZONS.items()}
    horizons["days"]["limitations"] = ["No hay datos intradía ni ejecución en tiempo real. Es una revisión diaria de cierres.",
        "Una configuración técnica no fija por sí sola tamaño, costes, precio de ejecución ni stop de una operación nueva."]
    horizons["weeks"]["limitations"] = ["Se conserva la estrategia original de crecimiento y momentum; las otras notas no se promedian con ella."]
    horizons["long_term"]["limitations"] = ["Calidad, valoración y tesis no equivalen a una entrada técnica.",
        "Sin tesis y horizonte documentados no se confirma una inversión a largo plazo."]
    portfolio = []
    dated_rows = {_ticker(row.get("ticker")): _dated(row, reference, valid, clock=reference_clock, as_of=as_of)
                  for row in rows}
    for row in rows:
        base = _base(row, names, held)
        record, policy = _matching_snapshot(row, index)
        dated = dated_rows[base["ticker"]]
        if base["held"]:
            managed = _portfolio_row(base, row, policy, dated, record)
            portfolio.append(managed)
            if managed["horizon_key"]:
                horizons[managed["horizon_key"]]["portfolio"].append(managed)
            continue
        for key, view in horizons.items():
            item = {**base, "horizon_key": key, "horizon": view["title"], "strategy": view["strategy"]}
            eligible = False
            if key == "days":
                technical_score = _score(record.get("entry_score")) if record else None
                technical_label = _text(record.get("entry_label")) if record else ""
                item.update(score=technical_score, score_kind="Momento técnico", signal=technical_label or "Sin lectura técnica enlazada")
                observed = dated and technical_score is not None and bool(technical_label)
                eligible = observed and technical_label in _ENTRY_LABELS
                item.update(data_status="observed" if observed else "pending",
                    action="Estudiar configuración" if eligible else "Esperar" if observed else "Revisar datos",
                    reason="La regla técnica guardada detecta una configuración de entrada." if eligible else
                        "No hay una entrada técnica confirmada." if observed else "Falta una lectura técnica fechada que coincida con este informe.",
                    next_step="Contrastar precio de la próxima sesión, costes y riesgo antes de proponer una operación. No es una señal intradía.")
            elif key == "weeks":
                reading = daily_score_reading(row)
                item.update(score=_score(row.get("score")), score_kind="Crecimiento + momentum")
                observed = dated and item["score"] is not None
                eligible = observed and not reading["blocked"] and row.get("status") in {"entry_strong", "entry_candidate"}
                item.update(data_status="observed" if observed and not reading["blocked"] else "pending",
                    action="Estudiar entrada" if eligible else "Esperar" if observed and not reading["blocked"] else "Revisar datos",
                    reason=(_text(row.get("chart_comment")) or "La estrategia guardada señala una candidatura de crecimiento y momentum.") if eligible else
                        "Falta una revisión reciente verificable para esta sesión." if not dated else
                        " ".join(reading["reasons"]) if reading["blocked"] else "La estrategia no confirma una entrada nueva.",
                    next_step=_text(row.get("next_step")) or "Confirmar precio, riesgo, concentración y próximo evento antes de comprar.")
            else:
                comparative = policy.get("comparative", {}) if policy else {}
                comparative = comparative if isinstance(comparative, Mapping) else {}
                position = policy.get("position", {}) if policy else {}
                position = position if isinstance(position, Mapping) else {}
                documented = (position.get("thesis_status") == "documented"
                              and _horizon(position) == "long_term" and position.get("invalidated") is not True)
                observed = dated and comparative.get("data_status") == "complete"
                eligible = observed and documented
                invalidated = position.get("invalidated") is True
                reviewed_thesis = observed and _horizon(position) == "long_term" and (
                    position.get("thesis_status") in {"documented", "invalidated"})
                item.update(score=None, score_kind="Sin nota conjunta", signal="Tesis documentada" if documented else "Tesis pendiente",
                    data_status="observed" if reviewed_thesis else "pending", action="Estudiar tesis" if eligible else "Revisar tesis" if invalidated else "Documentar tesis",
                    reason="La tesis figura como invalidada; contrastar el motivo antes de considerarla otra vez." if invalidated else
                        _text(comparative.get("reading")) if observed else "Falta una revisión de calidad y valoración enlazada a esta sesión.",
                    next_step="Contrastar negocio, valoración, balance, diversificación y hechos que invalidarían la tesis. No se confirma una compra automática.")
                item["evidence"] = [_text(comparative.get("reading"))] if comparative.get("reading") else []
                if observed and not eligible and not invalidated:
                    view["research"].append({**item, "action": "Estudiar empresa", "data_status": "research_only",
                        "reason": "Calidad y valoración observadas; la tesis y la adecuación a largo plazo siguen pendientes.",
                        "next_step": "Documentar una tesis propia y sus condiciones de invalidación; esta lista no confirma compras.",
                        "quality_score": _score(comparative.get("quality_score")),
                        "valuation_score": _score(comparative.get("valuation_score")),
                        "risk_score": _score(comparative.get("risk_score"))})
            (view["opportunities"] if eligible else view["waiting"]).append(item)
    portfolio.sort(key=lambda row: (row["priority"], row["company_name"], row["ticker"]))
    coverage = daily_score_coverage(rows)
    coverage["current_with_score"] = sum(dated_rows[_ticker(row.get("ticker"))] and _score(row.get("score")) is not None for row in rows)
    coverage["stale_or_undated"] = sum(not value for value in dated_rows.values())
    status = ("unavailable" if not valid or not coverage["current_with_score"] else
              "partial" if coverage["pending_data"] or coverage["stale_or_undated"] else "available")
    for key, view in horizons.items():
        view["portfolio"].sort(key=lambda row: (row["priority"], row["company_name"]))
        view["opportunities"].sort(key=lambda row: (row["signal"] != "Entrada fuerte", -(row["score"] if row["score"] is not None else -1), row["company_name"]))
        view["waiting"].sort(key=lambda row: (row["data_status"] != "observed", row["company_name"]))
        view["research"].sort(key=lambda row: (row["company_name"], row["ticker"]))
        observed = sum(row["data_status"] == "observed" for row in view["opportunities"] + view["waiting"])
        count = len(view["opportunities"])
        total = len(view["opportunities"]) + len(view["waiting"])
        view["counts"] = {"portfolio": len(view["portfolio"]), "opportunities": count, "waiting": len(view["waiting"]),
                          "observed": observed, "total": total, "research": len(view["research"])}
        view["opportunity_count_label"] = ("N/D" if not observed or observed < total and not count else
                                           f"≥ {count}" if observed < total else str(count))
    limitations = ["No se ejecutan órdenes ni se cambian posiciones o parámetros.",
                  "Una señal observada no demuestra rentabilidad futura. Los resultados deben compararse después de costes y divisa."]
    if not valid:
        limitations.insert(0, "No hay un informe sellado y fechado verificable con una fecha de revisión válida.")
    if duplicates:
        limitations.append("Hay empresas duplicadas en el informe; se dejan pendientes sin elegir la mejor puntuación.")
    return {"version": WORKSPACE_VERSION, "report_id": report.get("report_id") if isinstance(report, Mapping) else None,
            "as_of": as_of.isoformat() if as_of else None, "status": status, "coverage": coverage,
            "limitations": limitations, "horizons": horizons, "portfolio": portfolio,
            "unassigned_portfolio": [row for row in portfolio if row["horizon_key"] is None],
            "unverified_portfolio": [row for row in portfolio if row["plan_status"] == "unverified"], "duplicates": duplicates}
