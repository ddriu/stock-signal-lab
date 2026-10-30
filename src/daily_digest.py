"""One observed, versioned five-session growth report for web and email.

No prices, signals, orders, scores or email are generated here. Saved policy
outputs retain their session, recording time and data-quality limitations.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from html import escape
import hashlib
import json
import math
from urllib.parse import urlsplit

import pandas as pd

from src.analysis_policy import PRIMARY_STRATEGY_ID, decode_policy_note
from src.analysis_data_quality import GROWTH_METRICS
from src.instruments import resolve_analysis_ticker, resolve_company_name

DIGEST_VERSION = "daily-score-report-v2"
APP_URL = "https://stock-signal-lab-ddriu.streamlit.app/"
_SUFFIX_CALENDARS = {
    ".MC": "XMAD", ".PA": "XPAR", ".AS": "XAMS", ".DE": "XETR",
    ".F": "XFRA", ".MI": "XMIL", ".HK": "XHKG", ".L": "XLON",
    ".IL": "XLON", ".T": "XTKS", ".TO": "XTSE", ".V": "XTSX",
    ".AX": "XASX", ".SW": "XSWX", ".CO": "XCSE", ".ST": "XSTO",
    ".HE": "XHEL", ".OL": "XOSL", ".IR": "XDUB", ".VI": "XWBO",
    ".WA": "XWAR", ".LS": "XLIS", ".BR": "XBRU", ".SI": "XSES",
    ".KS": "XKRX", ".KQ": "XKRX", ".TW": "XTAI", ".SS": "XSHG",
    ".SA": "BVMF", ".MX": "XMEX", ".NZ": "XNZE", ".TA": "XTAE",
    ".JO": "XJSE", ".IS": "XIST", ".US": "XNYS",
}
_US_SHARE_CLASSES = {"BRK.A", "BRK.B", "BF.A", "BF.B"}


def _text(value: object) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.lower() in {"nan", "none", "<na>", "nat"} else value


def _symbol(value: object) -> str:
    try:
        return resolve_analysis_ticker(_text(value))
    except ValueError:
        return ""


def _score(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not 0 <= number <= 100:
        return None
    return int(number) if number.is_integer() else number


def _day(value: object) -> str | None:
    try:
        result = pd.Timestamp(value)
        return None if pd.isna(result) else result.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def seal_daily_score_report(report: Mapping[str, object]) -> dict[str, object]:
    """Identity covers the exact payload, including any selected alerts."""
    result = {key: value for key, value in report.items() if key != "report_id"}
    result["report_id"] = hashlib.sha256(_canonical(result).encode("utf-8")).hexdigest()
    return result


def _calendar_name(ticker: str) -> str:
    symbol = ticker.upper()
    for suffix, calendar in _SUFFIX_CALENDARS.items():
        if symbol.endswith(suffix):
            return calendar
    # Never invent US sessions for a foreign exchange suffix we do not know.
    if "." in symbol and symbol not in _US_SHARE_CLASSES:
        return "no_verificado"
    return "XNYS"


def _utc_clock(value: object | None = None) -> pd.Timestamp:
    clock = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    if pd.isna(clock) or clock.tzinfo is None:
        raise ValueError("La hora de referencia debe incluir zona horaria.")
    return clock.tz_convert("UTC")


def _session_close(ticker: str, session: str) -> pd.Timestamp | None:
    name = _calendar_name(ticker)
    if name == "no_verificado":
        return None
    try:
        import exchange_calendars as calendars
        close = pd.Timestamp(calendars.get_calendar(name).session_close(pd.Timestamp(session)))
        return close.tz_localize("UTC") if close.tzinfo is None else close.tz_convert("UTC")
    except (ImportError, ValueError, KeyError, TypeError):
        return None


def _sessions(ticker: str, anchor: str, reference_time: object | None = None) -> tuple[list[str], str]:
    name = _calendar_name(ticker)
    if name == "no_verificado":
        return [], name
    try:
        import exchange_calendars as calendars
        end, clock = pd.Timestamp(anchor), _utc_clock(reference_time)
        calendar = calendars.get_calendar(name)
        sessions = calendar.sessions_in_range(end - pd.Timedelta(days=40), end)
        completed = []
        for session in sessions:
            close = _session_close(ticker, session.date().isoformat())
            if close is not None and close <= clock:
                completed.append(session.date().isoformat())
        return completed[-5:][::-1], name
    except (ImportError, ValueError, KeyError, TypeError):
        return [], name


def _quality(policy: Mapping[str, object] | None) -> dict[str, object]:
    quality = policy.get("data_quality") if policy else None
    if not isinstance(quality, Mapping):
        return {"status": "unknown", "block_entry": True,
                "reasons": ["El análisis guardado no acredita la fecha de los fundamentales; revisar los datos antes de comprar."]}
    reasons = quality.get("reasons", [])
    result = {"status": _text(quality.get("status")) or "unknown",
            "block_entry": quality.get("block_entry") is not False,
            "reasons": [_text(item) for item in reasons if _text(item)] if isinstance(reasons, (list, tuple)) else [],
            "comparability_key": _text(quality.get("comparability_key")),
            "input_version": _text(quality.get("input_version"))[:80]}
    # Only the public, whitelisted provenance assessed by the policy belongs
    # in this report. Never copy the provider's whole info/account payload.
    coverage = quality.get("coverage")
    if isinstance(coverage, Mapping):
        result["coverage"] = {key: coverage.get(key) for key in (
            "applicable", "numeric_metrics", "dated_metrics", "undated_metrics", "missing_metrics")}
    periods = quality.get("metric_periods")
    if isinstance(periods, Mapping):
        result["metric_periods"] = {key: {field: metadata.get(field) for field in (
            "source", "periodicity", "period_end", "filed_at", "comparable")}
            for key, metadata in periods.items() if key in GROWTH_METRICS and isinstance(metadata, Mapping)}
    return result


def daily_score_reading(row: Mapping[str, object]) -> dict[str, object]:
    """Separate engine output from data sufficiency, including saved v1 reports.

    A high raw label is evidence of what the engine calculated, not permission
    to buy. Rendering never upgrades a legacy/pending operational status.
    """
    quality = row.get("quality")
    quality = quality if isinstance(quality, Mapping) else {}
    blocked = (quality.get("block_entry") is not False or _score(row.get("score")) is None
               or _text(row.get("status")) == "review_data")
    raw_reasons = quality.get("reasons")
    reasons = [_text(reason) for reason in raw_reasons if _text(reason)] if isinstance(raw_reasons, (list, tuple)) else []
    if blocked and not reasons:
        reasons = ["Falta una revisión completa con datos fechados; no se confirma una entrada nueva."]
    status = _text(row.get("status"))
    decision = ("Compra no confirmada" if blocked else
                "Revisar antes de ampliar" if row.get("held") and status in {"entry_strong", "entry_candidate"} else
                "Estudiar entrada" if status in {"entry_strong", "entry_candidate"} else
                "Sin entrada nueva")
    return {"signal": _text(row.get("label")) or "Sin señal guardada",
            "verification": "Datos pendientes" if blocked else "Datos suficientes",
            "decision": decision, "blocked": blocked, "reasons": reasons}


def daily_score_sort_key(row: Mapping[str, object]) -> tuple:
    reading = daily_score_reading(row)
    status, raw = _text(row.get("status")), _text(row.get("label"))
    score = _score(row.get("score"))
    priority = (0 if not reading["blocked"] and status == "entry_strong" else
                1 if reading["blocked"] and raw == "Entrada fuerte" and score is not None else
                2 if not reading["blocked"] and status == "entry_candidate" else 3)
    return (not bool(row.get("held")), priority, -(score if score is not None else -1), _text(row.get("ticker")))


def daily_score_coverage(rows: Iterable[Mapping[str, object]]) -> dict[str, int]:
    """Counts of the exact report, not a separate/recomputed recommendation."""
    counts = dict(total=0, with_score=0, missing_score=0, sufficient_data=0, pending_data=0,
                  strong_verified=0, strong_pending=0, candidate_verified=0)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        counts["total"] += 1
        counts["with_score" if _score(row.get("score")) is not None else "missing_score"] += 1
        reading = daily_score_reading(row)
        counts["pending_data" if reading["blocked"] else "sufficient_data"] += 1
        if reading["blocked"] and row.get("label") == "Entrada fuerte" and _score(row.get("score")) is not None:
            counts["strong_pending"] += 1
        elif not reading["blocked"] and row.get("status") == "entry_strong":
            counts["strong_verified"] += 1
        elif not reading["blocked"] and row.get("status") == "entry_candidate":
            counts["candidate_verified"] += 1
    return counts


def _identity(policy: Mapping[str, object]) -> str:
    primary, quality = policy["primary"], policy.get("data_quality") or {}
    return _canonical({"version": policy.get("policy_version"), "method": policy.get("snapshot_method"),
        "strategy": primary.get("strategy_id") or primary.get("id"), "profile": primary.get("sector_key"),
        "config": policy.get("config"), "source": policy.get("source"), "currency": policy.get("currency"),
        "fundamentals": quality.get("comparability_key") if isinstance(quality, Mapping) else None})


def _plain_reason(primary: Mapping[str, object]) -> str:
    factors = []
    for key in ("risk_factors", "positive_factors"):
        value = primary.get(key)
        if isinstance(value, (tuple, list)):
            factors.extend(value)
    translations = (
        ("por debajo de la media intermedia", "El precio sigue por debajo de la media de 50 sesiones."),
        ("debajo de la media larga", "El precio está por debajo de la media de 200 sesiones."),
        ("conviene no perseguirla", "El precio se ha alejado de su media corta: esperar una entrada menos exigente."),
        ("por encima de la media intermedia", "El precio mantiene la media de 50 sesiones."),
    )
    for needle, translated in translations:
        if any(needle in str(factor).lower() for factor in factors):
            return translated
    return "La nota resume crecimiento, fuerza del precio y contexto; no mide una probabilidad de ganar."


def _comparison(current: Mapping[str, object], previous: Mapping[str, object] | None) -> tuple[float | int | None, str, str]:
    if previous is None:
        return None, "Falta la revisión de la sesión anterior; no se calcula un cambio diario.", "missing_previous"
    if _identity(current) != _identity(previous):
        return None, "Análisis actualizado: cambió el perfil, la fuente, los datos o las reglas; las notas no son directamente comparables.", "not_comparable"
    now, before = _score(current["primary"].get("score")), _score(previous["primary"].get("score"))
    if now is None or before is None:
        return None, "Faltan notas verificables para comparar estas dos sesiones.", "missing_previous"
    delta = now - before
    if abs(delta) <= 2:
        return delta, "Sin cambios importantes en la puntuación desde la sesión anterior.", "stable"
    changes = []
    for key, label in (("momentum_score", "la fuerza del precio"), ("growth_score", "el crecimiento empresarial"), ("context_score", "el contexto de mercado y riesgo")):
        new, old = _score(current["primary"].get(key)), _score(previous["primary"].get(key))
        if new is not None and old is not None and new != old:
            changes.append((abs(new - old), new - old, f"{'Sube' if new > old else 'Baja'} {label} de {old:g} a {new:g}."))
    relevant = [item for item in changes if (item[1] > 0) == (delta > 0)] or changes
    sentence = max(relevant, default=(0, 0, "Cambió la combinación de factores observados."))[2]
    return delta, sentence, "changed"


def build_daily_score_report(
    snapshots: pd.DataFrame, *, tickers: Iterable[str] | None = None,
    held_tickers: Iterable[str] = (), company_names: Mapping[str, object] | None = None,
    as_of: date | str | None = None, reference_time: object | None = None,
) -> dict[str, object]:
    """Dated, deterministic observed history; never backfill missing scores."""
    held = {_symbol(value) for value in held_tickers if _symbol(value)}
    names = {_symbol(key): value for key, value in (company_names or {}).items() if _symbol(key)}
    universe = {_symbol(value) for value in tickers if _symbol(value)} if tickers is not None else set()
    source = snapshots.to_dict("records") if isinstance(snapshots, pd.DataFrame) else []
    clock = _utc_clock(reference_time)
    available_dates = [day for row in source if (day := _day(row.get("analyzed_at"))) and day <= clock.date().isoformat()]
    requested_anchor = _day(as_of) if as_of is not None else max(available_dates, default=None)
    if tickers is None:
        universe.update(_symbol(row.get("ticker")) for row in source
            if _symbol(row.get("ticker")) and (day := _day(row.get("analyzed_at")))
            and requested_anchor and day <= requested_anchor)
    universe |= held
    # A US holiday must not hide a completed European session. Conversely an
    # open US session must not be certified just because Europe has closed.
    # Each participant contributes only its own already-completed sessions.
    calendars = {ticker: _sessions(ticker, requested_anchor, clock) for ticker in universe} if requested_anchor else {}
    reference_dates = sorted({day for dates, _ in calendars.values() for day in dates}, reverse=True)[:5]
    anchor = reference_dates[0] if reference_dates else None
    seen: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in source:
        ticker, day = _symbol(row.get("ticker")), _day(row.get("analyzed_at"))
        if not ticker or not day or anchor and day > anchor:
            continue
        if ticker not in universe:
            continue
        policy = decode_policy_note(row.get("note"))
        if policy and (_symbol(policy.get("ticker")) != ticker or _day(policy.get("as_of")) != day):
            policy = None
        if policy and (policy["primary"].get("strategy_id") or policy["primary"].get("id")) != PRIMARY_STRATEGY_ID:
            policy = None
        timestamp = pd.to_datetime(row.get("created_at"), errors="coerce", utc=True)
        close = _session_close(ticker, day)
        if pd.isna(timestamp) or close is None or timestamp < close or timestamp > clock:
            policy = None
        created = timestamp.isoformat() if pd.notna(timestamp) else ""
        seen.setdefault((ticker, day), []).append({"policy": policy, "created": created,
            "id": _text(row.get("id")), "snapshot_id": _text((policy or {}).get("snapshot_id"))})
    rows, calendar_status = [], "complete"
    for ticker in sorted(universe):
        dates, calendar = calendars.get(ticker, ([], _calendar_name(ticker)))
        if len(dates) != 5:
            calendar_status = "incomplete"
        history, policies, revisions = [], [], 0
        for day in dates:
            observations = seen.get((ticker, day), [])
            observations.sort(key=lambda item: (item["created"], int(item["id"]) if item["id"].isdigit() else -1, item["snapshot_id"]))
            valid = [item for item in observations if item["policy"]]
            observation = valid[-1] if valid else None
            policy = observation["policy"] if observation else None
            revised = len({item["snapshot_id"] for item in valid}) > 1
            revisions += int(revised)
            history.append({"session": day, "score": _score(policy["primary"].get("score")) if policy else None,
                "snapshot_id": observation["snapshot_id"] if observation else None,
                "recorded_at": (observation["created"] or None) if observation else None, "revised": revised})
            policies.append(policy)
        current = policies[0] if policies else None
        primary, quality = current["primary"] if current else {}, _quality(current)
        score = _score(primary.get("score"))
        label = _text(primary.get("status")) or "Sin dato de esta sesión"
        delta, reason, comparison = (_comparison(current, policies[1] if len(policies) > 1 else None) if current else
            (None, "No hay una revisión de crecimiento y momentum guardada para esta sesión.", "no_data"))
        is_held = ticker in held
        blocked = quality["block_entry"] or score is None or primary.get("data_status") != "complete"
        status = "review_data" if blocked else "entry_strong" if label == "Entrada fuerte" else "entry_candidate" if label == "Entrada candidata" else "wait"
        if blocked:
            next_step = "Actualizar y revisar los datos; no interpretar la ausencia de información como una venta."
        elif is_held:
            next_step = ("Revisar la tesis y la exposición: existe una invalidación declarada; contrastar el motivo antes de decidir."
                if bool(current["position"].get("invalidated")) else
                "Revisar precio, peso y riesgo antes de ampliar; una buena entrada no autoriza una compra automática."
                if status.startswith("entry_") else
                "No ampliar por esta señal; revisar la estrategia y el riesgo de la posición, sin vender solo por la nota.")
        elif status.startswith("entry_"):
            next_step = "Candidata a estudiar: confirmar valoración, precio de entrada, próximo evento y tamaño."
        else:
            next_step = "Esperar para una compra nueva; la tendencia todavía no cumple esta estrategia."
        rows.append({"ticker": ticker, "company_name": resolve_company_name(ticker, names.get(ticker), current or {}),
            "held": is_held, "score": score, "label": label, "history": history, "change": delta,
            "change_reason": reason, "comparison_status": comparison, "next_step": next_step, "status": status,
            "quality": quality, "chart_comment": _plain_reason(primary) if current else "Sin lectura técnica guardada.",
            "momentum_score": _score(primary.get("momentum_score")), "growth_score": _score(primary.get("growth_score")),
            "context_score": _score(primary.get("context_score")), "session": dates[0] if dates else None,
            "calendar": calendar, "recorded_at": history[0]["recorded_at"] if history else None,
            "revision_count": revisions, "source": _text((current or {}).get("source")),
            "policy_version": _text((current or {}).get("policy_version")), "snapshot_id": (current or {}).get("snapshot_id")})
    rows.sort(key=daily_score_sort_key)
    return seal_daily_score_report({"version": DIGEST_VERSION, "as_of": anchor,
        "session_dates": reference_dates, "calendar_status": calendar_status,
        "rows": rows, "limitations": ["Las notas son salidas observadas del modelo, no probabilidades de beneficio.",
            "No se rellenan sesiones sin análisis ni se mezclan estrategias.",
            "La comparación entre empresas no incorpora comisiones, divisa ni valoración de una operación concreta."]})


def render_daily_score_email(report: Mapping[str, object], *, app_url: str = APP_URL) -> tuple[str, str, str]:
    """Compact, UTF-8 escaped rendering; no recalculation or transport."""
    url = app_url if urlsplit(app_url).scheme in {"http", "https"} else APP_URL
    date_label = _text(report.get("as_of")) or "sin sesión verificada"
    subject = f"Stock Signal Lab · resumen del cierre {date_label}"
    lines = [subject, "Crecimiento + momentum · las notas no son probabilidades de ganar.", ""]
    blocks = []
    all_rows = [row for row in report.get("rows") or [] if isinstance(row, Mapping)]
    coverage = daily_score_coverage(all_rows)
    overview = (f"{coverage['strong_verified']} entradas fuertes con datos suficientes · "
                f"{coverage['strong_pending']} señales fuertes del motor pendientes de verificar · "
                f"{coverage['missing_score']} sin nota de la sesión.")
    lines.extend([overview, "Datos suficientes no equivale a una compra autorizada.", ""])
    blocks.append(f"<p><strong>{escape(overview)}</strong><br>Datos suficientes no equivale a una compra autorizada.</p>")
    pending_strong = sorted((row for row in all_rows
        if daily_score_reading(row)["blocked"] and row.get("label") == "Entrada fuerte"
        and _score(row.get("score")) is not None), key=lambda row: (-float(row["score"]), _text(row.get("ticker"))))
    if pending_strong:
        pending_names = " · ".join(f"{_text(row.get('company_name')) or _text(row.get('ticker'))} ({_text(row.get('ticker'))}): {_score(row.get('score')):g}/100" for row in pending_strong[:3])
        pending_message = "Señales fuertes pendientes: " + pending_names + ". Compra no confirmada; revisar la procedencia y las fechas de los datos."
        lines.extend([pending_message, ""])
        blocks.append(f"<p>{escape(pending_message)}</p>")
    incidents = {item.get("ticker"): item for item in report.get("incidents", []) if isinstance(item, Mapping)}
    positions = {item.get("ticker"): item for item in report.get("positions", []) if isinstance(item, Mapping)}
    alerts, alert_tickers = [], set()
    for alert in report.get("alerts") or []:
        if not isinstance(alert, Mapping):
            continue
        ticker = _symbol(alert.get("ticker"))
        if ticker and ticker not in alert_tickers:
            alerts.append(alert)
            alert_tickers.add(ticker)
        if len(alerts) == 5:
            break
    if alerts:
        lines.append("CAMBIOS QUE REVISAR")
        alert_html = []
        for alert in alerts:
            name = resolve_company_name(_symbol(alert.get("ticker")) or "SIN-DATO", alert.get("company_name"))
            title = _text(alert.get("title")) or "Revisar posición"
            reason = _text(alert.get("change_reason"))
            step = _text(alert.get("next_step")) or "Revisar la evidencia; no se ejecuta ninguna orden."
            context = " · ".join(filter(None, [_text(alert.get("strategy_label")), _text(alert.get("horizon")),
                _text(alert.get("as_of")), _text(alert.get("data_source")), _text(alert.get("policy_version"))]))
            auxiliary = " · ".join(filter(None, [_text(alert.get("opportunity_status")), _text(alert.get("event_label"))]))
            factors = []
            for key in ("positive_factors", "risk_factors"):
                values = alert.get(key)
                if isinstance(values, (list, tuple)) and values:
                    factors.append(_text(values[0]))
            position, incident = positions.get(alert.get("ticker"), {}), incidents.get(alert.get("ticker"), {})
            extra = " ".join(filter(None, [auxiliary, *factors, _text(position.get("reading")),
                _text(position.get("reason")), _text(position.get("next_step")), _text(incident.get("reason")),
                _text(incident.get("as_of")), _text(incident.get("next_step"))]))
            limitation = _text(alert.get("data_limitations"))
            held = bool(alert.get("held")) or any(row.get("ticker") == alert.get("ticker") and row.get("held") for row in report.get("rows", []))
            scope = "En cartera" if held else "Favorita / posible entrada nueva"
            lines.extend([f"{name} ({_text(alert.get('ticker'))}): {title}. {reason} {step}", scope, context, extra, limitation])
            alert_html.append(f"<li><strong>{escape(name)} ({escape(_text(alert.get('ticker')))})</strong> · {scope}: {escape(title)}. {escape(reason)} {escape(step)}<br>{escape(extra)}<br><small>{escape(context)} {escape(limitation)}</small></li>")
        blocks.append("<h2>Cambios que revisar</h2><ul>" + "".join(alert_html) + "</ul>")
    eligible = [row for row in all_rows if row.get("ticker") not in alert_tickers]
    changed = [row for row in eligible if row.get("comparison_status") in {"changed", "not_comparable"} or row.get("status") in {"entry_strong", "review_data"}]
    displayed = (changed or eligible)[:max(0, 5 - len(alerts))]
    cells = []
    lines.append("PUNTUACIONES Y CAMBIOS")
    for row in displayed:
        name = f"{_text(row.get('company_name'))} ({_text(row.get('ticker'))})"
        score = _score(row.get("score"))
        note = f"{score:g}/100" if isinstance(score, (float, int)) else "sin dato"
        history = " · ".join(f"{_text(item.get('session'))}: {_score(item.get('score')):g}"
            if _score(item.get("score")) is not None else f"{_text(item.get('session'))}: —"
            for item in row.get("history", []) if isinstance(item, Mapping))
        reading = daily_score_reading(row)
        quality_text = " ".join(reading["reasons"])
        interpretation = f"Motor: {reading['signal']} · {reading['verification']} · {reading['decision']}"
        incident, position = incidents.get(row.get("ticker"), {}), positions.get(row.get("ticker"), {})
        extra = " ".join(filter(None, [_text(incident.get("reason")), _text(incident.get("as_of")),
            _text(incident.get("next_step")), _text(position.get("reading")), _text(position.get("reason")), _text(position.get("next_step"))]))
        source = " · ".join(filter(None, [_text(row.get("source")), _text(row.get("session")), _text(row.get("policy_version"))]))
        lines.extend([f"{name} · {note} · {'En cartera' if row.get('held') else 'Favorita'} · {interpretation}",
            history, _text(row.get("change_reason")), _text(row.get("chart_comment")), _text(row.get("next_step")), extra, quality_text, source, ""])
        cells.append(f"<tr><td><strong>{escape(name)}</strong><br>{'En cartera' if row.get('held') else 'Favorita'}</td><td>{escape(note)}<br>Motor: {escape(reading['signal'])}<br>{escape(reading['verification'])}<br>{escape(reading['decision'])}</td><td>{escape(history)}<br>{escape(_text(row.get('change_reason')))}<br>{escape(_text(row.get('chart_comment')))}<br>{escape(_text(row.get('next_step')))}<br>{escape(extra)}<br><small>{escape(quality_text)} {escape(source)}</small></td></tr>")
    if not displayed and not alerts:
        lines.append("No hay puntuaciones guardadas para esta sesión. No se interpreta como una venta.")
    blocks.append("<h2>Puntuaciones y cambios</h2><table border='1' cellpadding='8' cellspacing='0'><thead><tr><th>Empresa</th><th>Nota del modelo</th><th>Últimas cinco sesiones y siguiente paso</th></tr></thead><tbody>" + "".join(cells) + "</tbody></table>")
    used = alert_tickers | {row.get("ticker") for row in displayed}
    pending = [item for ticker, item in incidents.items() if ticker not in used][:max(0, 5 - len(used))]
    if pending:
        lines.append("DATOS PENDIENTES")
        incident_html = []
        for item in pending:
            message = " · ".join(filter(None, [_text(item.get("ticker")), _text(item.get("as_of")), _text(item.get("reason")), _text(item.get("next_step"))]))
            lines.append(message)
            incident_html.append(f"<li>{escape(message)}</li>")
        blocks.append("<h2>Datos pendientes</h2><ul>" + "".join(incident_html) + "</ul>")
    identifier = _text(report.get("report_id"))
    lines.extend([f"Historial completo de {len(all_rows)} empresas: {url}",
        "Una entrada nueva no equivale a mantener o vender una posición existente. No se ejecutan órdenes.",
        f"Informe de puntuaciones: {identifier}"])
    html = "<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head><body style='font-family:Arial,sans-serif;color:#17253c'>" + f"<h1>{escape(subject)}</h1><p>Crecimiento + momentum. Las notas no son probabilidades de ganar.</p>" + "".join(blocks) + f"<p><a href='{escape(url, quote=True)}'>Ver el historial completo de {len(all_rows)} empresas</a></p><p>Una entrada nueva no equivale a mantener o vender una posición existente. No se ejecutan órdenes.</p><small>Informe de puntuaciones: {escape(identifier)}</small></body></html>"
    return subject, "\n".join(lines), html
