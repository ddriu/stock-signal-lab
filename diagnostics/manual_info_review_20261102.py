"""One requested private information email; no fresh investment signals/trades."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict
from datetime import date, datetime, timezone
from html import escape
import hashlib
import io
import json
import logging
import os
from zoneinfo import ZoneInfo

import pandas as pd

from src.alerts import AlertPreferences, is_valid_email
from src.analysis_data_quality import assess_analysis_data_quality
from src.daily_digest import seal_daily_score_report, _sessions
from src.email_sender import EmailDeliveryReceipt, send_email
from src.instruments import resolve_analysis_ticker
from src.market_cache import MarketCacheStore
from src.portfolio_snapshot import latest_portfolio_snapshot
from src.storage import create_journal, load_supabase_config

OWNER = "ddriu"
REQUEST_DAY = date(2026, 11, 2)
REQUEST = "explicit-source-fluency-info-20261102-v1"
SOURCE_REPORT_ID = "9b0ddef62c3003811392550c28026cae5e3a714c75d956d022cbb18594381f16"


def validate_clock(clock):
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise RuntimeError("AwareClockRequired")
    if clock.astimezone(ZoneInfo("Europe/Madrid")).date() != REQUEST_DAY:
        raise RuntimeError("ExpiredReviewRequest")
    if os.getenv("GITHUB_RUN_ATTEMPT") != "1":
        raise RuntimeError("RepeatedWorkflowAttempt")
    if os.getenv("MODEL_RESULTS_ENABLED", "false").lower() not in {"0", "false", "no", "off"}:
        raise RuntimeError("UnexpectedVirtualTracking")


def delivery_key():
    return hashlib.sha256(json.dumps([REQUEST, OWNER], separators=(",", ":")).encode()).hexdigest()


def _preference(journal):
    if getattr(journal, "owner", None) != OWNER:
        raise RuntimeError("UnexpectedOwner")
    pref = journal.get_alert_preferences()
    if not isinstance(pref, AlertPreferences) or pref.owner != OWNER or pref.enabled is not True:
        raise RuntimeError("UnexpectedPreferences")
    if (not isinstance(pref.email, str) or pref.email != pref.email.strip()
            or any(c in pref.email for c in "\r\n") or not is_valid_email(pref.email)):
        raise RuntimeError("InvalidRecipient")
    return pref


def gather_context(journal, clock):
    """Read only the requested owner's current transport coverage."""
    _preference(journal)
    store = MarketCacheStore(journal)
    heads = {(row["kind"], row["symbol"]): row for row in store.heads()}
    names, held = {}, set()
    def symbol_of(value):
        if value is None or pd.isna(value) or not str(value).strip():
            return ""
        try:
            return resolve_analysis_ticker(str(value))
        except ValueError:
            return ""
    for row in journal.list_favorites().to_dict("records"):
        symbol = symbol_of(row.get("ticker"))
        if symbol:
            names[symbol] = str(row.get("name") or symbol)
    snapshots, _ = latest_portfolio_snapshot(journal.list_portfolio_snapshot_positions())
    for row in snapshots.to_dict("records"):
        symbol = symbol_of(row.get("analysis_ticker"))
        if symbol:
            held.add(symbol)
            names.setdefault(symbol, str(row.get("asset_name") or symbol))
    for row in journal.open_positions().to_dict("records"):
        symbol = symbol_of(row.get("ticker"))
        if symbol:
            held.add(symbol)
            names.setdefault(symbol, symbol)
    info = {row["symbol"]: row["payload"] for row in store.read_many("fundamentals", sorted(names))}
    issues, periodicities = [], set()
    today = clock.astimezone(ZoneInfo("Europe/Madrid")).date()
    for symbol in sorted(names, key=lambda ticker: (ticker not in held, names[ticker])):
        price, fundamental = heads.get(("prices", symbol)), info.get(symbol)
        reasons = []
        if price is None:
            reasons.append("Sin histórico válido guardado; la causa de transporte no quedó registrada.")
        else:
            if not price["metadata"].get("completed_price_date"):
                reasons.append("Sin cierre diario verificable en el histórico guardado.")
            if not (price["metadata"].get("quote") or {}).get("currency"):
                reasons.append("La cabecera del precio no identifica moneda; no confundir con ausencia de precios.")
        if fundamental is None:
            reasons.append("Sin ficha fundamental guardada.")
        else:
            quality = assess_analysis_data_quality(fundamental, as_of=today)
            if quality.get("block_entry"):
                reasons.extend(str(item) for item in quality.get("reasons", []))
            periods = {} if fundamental.get("quoteType") in {"ETF", "MUTUALFUND"} else fundamental.get("_metric_periods") or {}
            for metric, metadata in periods.items():
                if not isinstance(metadata, dict):
                    continue
                periodicity = str(metadata.get("periodicity") or "unknown")
                periodicities.add(periodicity)
                end = pd.to_datetime(metadata.get("period_end"), errors="coerce")
                if pd.notna(end) and (today - pd.Timestamp(end).date()).days > (450 if periodicity == "annual" else 210):
                    reasons.append(f"Periodo antiguo de {metric}: {pd.Timestamp(end).date().isoformat()}.")
                if metadata.get("comparable") is False:
                    reasons.append(f"{metric}: marcado como no comparable en la fuente guardada.")
        if reasons:
            issues.append({"ticker": symbol, "name": names[symbol], "held": symbol in held,
                           "reasons": list(dict.fromkeys(reasons))})
    return {"generated_at": clock.astimezone(timezone.utc).isoformat(),
            "instruments": len(names), "price_histories": sum(("prices", symbol) in heads for symbol in names),
            "fundamental_rows": len(info), "issues": issues, "periodicities": sorted(periodicities),
            "latest_cache_retrieval": max((row["observed_at"] for row in heads.values()), default=None)}


def render_info_email(report, context):
    """Current technical audit plus a clearly historical report link, no scores."""
    if report.get("report_id") != SOURCE_REPORT_ID or seal_daily_score_report(report)["report_id"] != SOURCE_REPORT_ID:
        raise RuntimeError("UnexpectedOrUnsealedSourceReport")
    local = datetime.fromisoformat(context["generated_at"]).astimezone(ZoneInfo("Europe/Madrid"))
    subject = f"Stock Signal Lab · fuentes y fluidez · {local:%d/%m/%Y}"
    paragraphs = [
        f"Auditoría técnica elaborada el {local:%d/%m/%Y a las %H:%M %Z}.",
        "No se han propuesto ni ejecutado cambios de posiciones por la actualización informática.",
        f"Último análisis guardado: cierre {report['as_of']}. No es un escaneo intradía nuevo; sus señales históricas no son recomendaciones actuales.",
        f"Cobertura privada consultada: {context['price_histories']}/{context['instruments']} instrumentos con histórico y {context['fundamental_rows']} fichas empresariales. Tener una ficha no garantiza datos suficientes.",
        f"Última consulta de la caché: {context.get('latest_cache_retrieval')}. Es hora de consulta, no la hora de cotización.",
        "Datos incompletos no equivalen a una señal de venta. En fondos/ETF/ETC no se exigen ingresos o beneficios empresariales como si fueran acciones.",
        "Periodicidades financieras declaradas en la caché: " + ", ".join(context.get("periodicities", [])) + ". No presentarlas automáticamente como el último trimestre.",
    ]
    plain = [subject, "", *paragraphs, "", "FUENTES QUE REQUIEREN REVISIÓN"]
    rows = []
    for issue in context.get("issues", []):
        label = f"{issue['name']} ({issue['ticker']}) · {'En cartera' if issue['held'] else 'Favorita'}"
        reason = " ".join(issue["reasons"])
        plain.extend([label, reason, ""])
        rows.append(f"<tr><td>{escape(label)}</td><td>{escape(reason)}</td></tr>")
    performance = [
        "FLUIDEZ: QUÉ ESTÁ MEDIDO Y QUÉ FALTA",
        "La cifra de 84 ms sólo correspondía a una ruta sintética sin importaciones conciliadas; no representa tu cartera importada.",
        "Ensayo local sintético con importación (19 partidas y 180 operaciones), 20 repeticiones: primera carga 3,660 s; mediana 1,559 s; p95 1,644 s; intervalo 1,517–1,676 s.",
        "En esa ruta se repite la conciliación contable en cada visita. El motor general y la red no explican esa demora en la prueba; no está resuelta todavía.",
        "No se ha medido aún el clic hasta el pintado dentro de tu sesión privada en producción.",
        "Protocolo: al menos 20 cambios por pantalla, separando primera carga y navegación caliente, mismo dispositivo/conexión, p50 y p95, errores y lecturas/cálculos. Repetir después de la TTL de 60 segundos y tras una actualización horaria.",
        "La web puede hibernar en Streamlit gratuito. El refresco de datos se ejecuta fuera de ella; no se ha contratado otro alojamiento.",
    ]
    link = f"https://stock-signal-lab-ddriu.streamlit.app/?digest={SOURCE_REPORT_ID}"
    plain.extend([*performance, "", "ANÁLISIS HISTÓRICO GUARDADO (NO SEÑAL ACTUAL)", link])
    html = ("<!doctype html><html lang='es'><head><meta charset='utf-8'></head><body style='font-family:Arial,sans-serif;line-height:1.45'>"
            f"<h1>{escape(subject)}</h1>" + "".join(f"<p>{escape(item)}</p>" for item in paragraphs)
            + "<h2>Fuentes que requieren revisión</h2><table border='1' cellpadding='6' cellspacing='0'><tr><th>Empresa</th><th>Qué falta o debe verificarse</th></tr>"
            + "".join(rows) + "</table><h2>Fluidez: qué está medido y qué falta</h2>"
            + "".join(f"<p>{escape(item)}</p>" for item in performance[1:])
            + f"<p><a href='{link}'>Abrir el análisis histórico guardado del {escape(str(report['as_of']))}</a></p></body></html>")
    return subject, "\n".join(plain), html


def _send(journal, report, context, transport=send_email):
    pref = _preference(journal)
    rendered = render_info_email(report, context)
    key = delivery_key()
    if not journal.claim_daily_digest(key, report["as_of"], SOURCE_REPORT_ID):
        return {"status": "already_reserved_or_completed", "emails_sent": 0, "receipts": []}
    receipt, error = None, ""
    try:
        # Guard against a preference change during preparation, without editing it.
        if _preference(journal).email != pref.email:
            raise RuntimeError("RecipientChangedDuringPreparation")
        receipt = transport(pref.email, *rendered)
    except Exception as exc:
        receipt = getattr(exc, "smtp_receipt", None)
        error = type(exc).__name__
    accepted = isinstance(receipt, EmailDeliveryReceipt) and receipt.status == "smtp_accepted" and receipt.smtp_code == 250
    terminal_saved = True
    try:
        journal.finish_daily_digest(key, "sent" if accepted else "uncertain", error_type=error)
    except Exception:
        terminal_saved = False
    return {"status": "smtp_accepted" if accepted else "delivery_unconfirmed", "emails_sent": int(accepted),
            "receipts": [asdict(receipt)] if accepted else [], "terminal_saved": terminal_saved,
            "source_report_id": SOURCE_REPORT_ID, "instruments": context["instruments"],
            "source_issues": len(context.get("issues", []))}


def execute(clock=None):
    clock = clock or datetime.now(timezone.utc)
    validate_clock(clock)
    if load_supabase_config() is None:
        raise RuntimeError("MissingPrivateBackend")
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            journal = create_journal(OWNER)
            _preference(journal)
            report = journal.get_daily_score_report(SOURCE_REPORT_ID)
            if not isinstance(report, dict):
                raise RuntimeError("HistoricalReportUnavailable")
            context = gather_context(journal, clock)
            result = _send(journal, report, context, transport=send_email)
    finally:
        logging.disable(previous)
    print(json.dumps(result, sort_keys=True))
    if result["status"] not in {"smtp_accepted", "already_reserved_or_completed"}:
        raise RuntimeError("DeliveryNotConfirmed")
    return result


if __name__ == "__main__":
    try:
        execute()
    except Exception as exc:
        print(json.dumps({"status": "information_email_requires_attention", "error_type": type(exc).__name__}))
        raise SystemExit(1)
