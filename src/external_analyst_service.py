"""Owner-private orchestration; independent of the original stock model.

No network, mailbox reads or SMTP run on import. Explicit commands opt in.
Raw letters, addresses and credentials never enter the external record store.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from io import StringIO
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from src.external_analysts import (EXTERNAL_ANALYST_VERSION, STATE_LABELS,
                                   historical_model_reading, normalize_call, revision_id, stable_id, utc_timestamp)


def runtime_manifest() -> dict:
    root = Path(__file__).resolve().parents[1]
    revision = os.getenv("GITHUB_SHA", "")
    if not revision:
        try:
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                      text=True, check=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            revision = "unavailable"
    paths = ["src/external_analysts.py", "src/external_analyst_service.py", "src/external_analyst_simulation.py",
             "src/external_analyst_market.py", "src/external_analyst_ingest.py"]
    return {"git_revision": revision, "version": EXTERNAL_ANALYST_VERSION,
            "source_hashes": {name: sha256((root/name).read_bytes()).hexdigest()
                              for name in paths if (root/name).is_file()}}


def effective_call(call, *, at):
    """Read-only current eligibility; never record an expiry as a market fill."""
    output = dict(call)
    state = output.get("tracking_status") or output["state"]
    if output.get("expires_at") and datetime.fromisoformat(utc_timestamp(output["expires_at"])) <= datetime.fromisoformat(utc_timestamp(at)):
        if state == "pending":
            state = "expired"
            output["issues"] = [*output["issues"], "Ha vencido el plazo documentado de entrada."]
    output["state"] = state
    if "tracking_status" in output:
        output["tracking_status"] = state
    if state not in {"pending"}:
        output["executable"] = False
    return output


def import_csv_bytes(raw: bytes, *, observed_at) -> dict:
    """Import explicit, UTF-8 structured facts, never infer symbols or dates."""
    if len(raw) > 2_000_000:
        raise ValueError("El CSV supera 2 MB.")
    observed = utc_timestamp(observed_at)
    rows = list(csv.DictReader(StringIO(raw.decode("utf-8-sig"))))
    if len(rows) > 1000:
        raise ValueError("Importa como máximo 1.000 recomendaciones cada vez.")
    calls, issues = [], []
    for index, row in enumerate(rows, 2):
        try:
            facts = {key: value or None for key, value in row.items() if key}
            facts.update(observed_at=observed)
            # received_at is evidence supplied by the user, never defaulted to a
            # historical publication date. observed_at always limits execution.
            facts["evidence"] = {"import": "manual_csv_unverified"}
            calls.append(normalize_call(facts))
        except (ValueError, TypeError) as exc:
            issues.append(f"Fila {index}: {exc}")
    return {"message_id": stable_id("csv", raw.hex()), "provider": "manual_csv",
            "received_at": observed, "observed_at": observed, "announced_count": None,
            "extracted_count": len(calls), "calls": calls, "issues": issues,
            "parser_version": EXTERNAL_ANALYST_VERSION, "authentication": "manual_unverified"}


def activate_comparison(store, *, now, costs=None) -> dict:
    """One immutable start per owner. Repeating the action cannot reset losses."""
    from src.external_analyst_simulation import SimulationCosts
    from dataclasses import asdict
    existing = [row["payload"] for row in store.list_simulation_runs()
                if row["payload"].get("kind") == "activation"]
    if existing:
        return min(existing, key=lambda row: row["observed_at"])
    stamp = utc_timestamp(now)
    payload = {"kind": "activation", "observed_at": stamp,
               "version": EXTERNAL_ANALYST_VERSION, "initial_cash_per_account_eur": 100.0,
               "manifest": runtime_manifest(),
               "costs": asdict(costs or SimulationCosts()),
               "rule": "first_observation_frozen_no_retroactive_changes"}
    # A fixed identity makes concurrent requests conflict instead of creating
    # several seasons. The caller reloads on conflict, never overwrites.
    store.save_simulation_run("external-comparison-activation-v1", payload)
    return payload


def first_observations(store, *, activation_at=None) -> list[dict]:
    first = {}
    revisions = sorted(store.list_revisions(), key=lambda row: (row["observed_at"], row["revision_id"]))
    for row in revisions:
        first.setdefault(row["call_id"], row["call"])
    return [call for call in first.values()
            if activation_at is None or utc_timestamp(call["observed_at"]) >= utc_timestamp(activation_at)]


def save_research_candidate(store, facts: dict, *, strategy: str, now, evidence: dict) -> dict:
    """Prospectively record A/B/C research with explicit provenance, not a score."""
    if strategy not in {"A", "B", "C"}:
        raise ValueError("La investigación independiente requiere A, B o C.")
    allowed = {"evidence_url", "evidence_at", "beneficiary_relation"} if strategy in {"A", "B"} else {
        "engine_snapshot_id", "signal_known_at"}
    if set(evidence) - allowed:
        raise ValueError("La evidencia no puede modificar fechas, precios ni identidad de la señal.")
    stamp = utc_timestamp(now)
    from src.external_analyst_simulation import simulate_four_strategies
    call = normalize_call({**facts, "provider": "independent_research", "received_at": stamp,
                           "observed_at": stamp, "source_status": "pending"})
    candidate = {**call, **evidence, "strategy": strategy, "received_at": stamp, "observed_at": stamp}
    check = simulate_four_strategies([candidate], [], as_of=stamp)
    if check["accounts"][strategy]["evidence_rejected"]:
        raise ValueError("Falta evidencia contemporánea verificable para esa estrategia.")
    # C must cite a real frozen snapshot belonging to this private journal.
    if strategy == "C":
        snapshots = store.journal.list_analysis_snapshots()
        records = snapshots.to_dict("records") if hasattr(snapshots, "to_dict") else snapshots
        matches = [row for row in records
                   if str(row.get("id")) == str(candidate.get("engine_snapshot_id"))
                   and str(row.get("ticker", "")).upper() == call["ticker"]]
        if not matches:
            raise ValueError("El análisis técnico guardado no pertenece a esta empresa y usuario.")
        created = utc_timestamp(matches[0].get("created_at"))
        if created > stamp or utc_timestamp(candidate.get("signal_known_at")) != created:
            raise ValueError("La hora de la señal debe coincidir con el análisis original guardado.")
    store.save_simulation_run(stable_id("candidate", strategy, call["id"]),
                              {"kind": "candidate", "observed_at": stamp, "candidate": candidate})
    return candidate


def advance_comparison(store, bars, *, now, market_metadata=None) -> dict:
    """Replay frozen evidence to a cutoff; record decisions idempotently."""
    from src.external_analyst_simulation import SimulationCosts, simulate_four_strategies
    stamp = utc_timestamp(now)
    runs = store.list_simulation_runs()
    activations = [row["payload"] for row in runs if row["payload"].get("kind") == "activation"]
    if not activations:
        return {"status": "not_activated"}
    activation = min(activations, key=lambda row: row["observed_at"])
    start = utc_timestamp(activation["observed_at"])
    if stamp < start:
        raise ValueError("No se puede simular antes de activar la comparación.")
    manifest = runtime_manifest()
    for path in ("src/external_analyst_simulation.py", "src/external_analyst_market.py"):
        original_hash = activation.get("manifest", {}).get("source_hashes", {}).get(path)
        if original_hash and manifest["source_hashes"].get(path) != original_hash:
            raise ValueError("Ha cambiado la versión del experimento; se conserva la anterior y requiere revisión explícita.")
    calls = first_observations(store, activation_at=start)
    candidates = [{**call, "strategy": "D"} for call in calls]
    candidates.extend(row["payload"]["candidate"] for row in runs
                      if row["payload"].get("kind") == "candidate"
                      and start <= utc_timestamp(row["payload"]["observed_at"]) <= stamp)
    # Freeze completed observations before replay. Provider corrections cannot
    # silently rewrite yesterday's paper fills or select a better outcome.
    frozen = {}
    for run in runs:
        if run["payload"].get("kind") == "market_bars":
            for bar in run["payload"]["bars"]:
                frozen.setdefault((bar["ticker"], bar["open_at"]), bar)
    new, conflicts = [], []
    for bar in bars:
        if datetime.fromisoformat(utc_timestamp(bar["available_at"])) > datetime.fromisoformat(stamp):
            conflicts.append({"ticker": bar["ticker"], "code": "future_bar_not_persisted"})
            continue
        key = (bar["ticker"], bar["open_at"])
        if key in frozen and frozen[key] != bar:
            conflicts.append({"ticker": key[0], "open_at": key[1], "code": "provider_revision_frozen_original_retained"})
        elif key not in frozen:
            frozen[key] = bar
            new.append(bar)
    for offset in range(0, len(new), 30):
        chunk = new[offset:offset+30]
        store.save_simulation_run(stable_id("observed-bars", chunk),
                                  {"kind": "market_bars", "observed_at": stamp, "bars": chunk})
    records = list(frozen.values())
    result = simulate_four_strategies(candidates, records, as_of=stamp,
                                     costs=SimulationCosts(**activation["costs"]))
    payload = {"kind": "comparison", "observed_at": stamp, "activation_at": start,
               "version": EXTERNAL_ANALYST_VERSION, "result": result,
               "manifest": manifest,
               "market": {**(market_metadata or {}), "revision_incidents": conflicts}, "bar_hash": stable_id(records),
               "candidate_hash": stable_id(candidates)}
    store.save_simulation_run(stable_id("comparison", payload), payload)
    for event in result["accounts"]["D"].get("events", []):
        identity = event.get("call_id") or event.get("id")
        if identity not in {call["id"] for call in calls}:
            continue
        event_at = event.get("at") or event.get("occurred_at") or event.get("timestamp")
        # Observation time is never substituted for an unknown execution time.
        if event_at:
            status = {"entry_activated": "open", "expired": "expired"}.get(event.get("event_type"))
            if event.get("event_type") == "exit":
                status = {"stop_executed": "stopped", "target_reached": "target_reached",
                          "ambiguous_conservative_stop": "ambiguous"}.get(event.get("status"))
            store.append_event(identity, stable_id("simulated", event),
                               {"origin": "paper_simulation", **event, **({"status": status} if status else {})}, event_at)
    return payload


def ingest_existing_mailbox(store, *, now, config, client_factory=None) -> dict:
    from src.external_analyst_ingest import poll_imap
    if not config.enabled:
        return {"status": "disabled", "imported": 0}
    if config.owner != store.owner:
        raise ValueError("El buzón no está autorizado para este propietario.")
    seen = {row["message_key"] for row in store.list_messages()}
    for run in store.list_simulation_runs():
        if run["payload"].get("kind") == "mail_ingestion":
            seen.update(run["payload"].get("processed_ids", []))
    polled = poll_imap(config, observed_at=now, existing_ids=seen, client_factory=client_factory)
    processed, imported = [], 0
    for message in polled["messages"]:
        store.import_message(message)
        processed.append(message.get("transport_id") or message["message_id"])
        imported += message["extracted_count"]
    processed.extend(item["message_id"] for item in polled.get("failures", []) if not item.get("retryable"))
    safe = {"kind": "mail_ingestion", "observed_at": utc_timestamp(now),
            "processed_ids": processed, "status": polled["status"],
            "imported": imported, "scanned": polled["scanned"], "remaining": polled["remaining"],
            "failures": polled.get("failures", []), "issues": polled.get("issues", [])}
    store.save_simulation_run(stable_id("mail-ingestion", safe), safe)
    return safe


def build_external_report(store, *, now, favorites=(), held=()) -> dict:
    """Same immutable report for UI/SMTP; no combined model/analyst grade."""
    stamp = utc_timestamp(now)
    previous = {event for row in store.list_deliveries()
                if row["status"] in {"reserved", "sent", "uncertain"}
                for event in row["report"].get("item_ids", [])}
    items = []
    try:
        snapshots = store.journal.list_analysis_snapshots()
        snapshots = snapshots.to_dict("records") if hasattr(snapshots, "to_dict") else snapshots
    except Exception:
        snapshots = []
    for original in store.list_calls():
        call = effective_call(original, at=stamp)
        item_id = stable_id(call["id"], call["revision_id"], call["state"])
        if item_id in previous:
            continue
        items.append({"item_id": item_id, "call_id": call["id"], "company": call["company_name"] or "N/D",
                      "ticker": call["ticker"], "analyst": call["analyst"],
                      "currency": call["currency"], "entry": call["entry"], "stop": call["stop"],
                      "target": call["target"], "source_url": call["source_url"],
                      "received_at": call["received_at"], "observed_at": call["observed_at"],
                      "published_at": call["published_at"],
                      "published_date": call.get("evidence", {}).get("published_date"),
                      "source_status": call["source_status"], "state": call.get("tracking_status", call["state"]),
                      "in_portfolio": call["ticker"] in held, "favorite": call["ticker"] in favorites,
                      "model_at_receipt": historical_model_reading(call, snapshots),
                      "executable": call["executable"], "issues": call["issues"]})
    items.sort(key=lambda row: (not row["in_portfolio"], not row["favorite"], not row["executable"], row["observed_at"], row["item_id"]))
    chosen = items[:5]
    return {"kind": "external_daily_report", "version": EXTERNAL_ANALYST_VERSION,
            "observed_at": stamp, "day": datetime.fromisoformat(stamp).astimezone(ZoneInfo("Europe/Madrid")).date().isoformat(),
            "items": chosen, "item_ids": [row["item_id"] for row in chosen], "waiting": max(0, len(items)-5),
            "limitations": "Fuente externa independiente; no es una puntuación del motor ni una orden. Niveles sin confirmar no se ejecutan. Rentabilidad virtual neta de costes estimados, no garantizada."}


def render_external_report(report) -> tuple[str, str]:
    rows, plain = [], [f"Analistas externos · {report['day']} · máximo cinco cambios"]
    def value(item):
        return "N/D" if item is None or item == "" else str(item)
    for item in report["items"]:
        state = STATE_LABELS.get(item["state"], item["state"])
        if item["state"] == "historical":
            state += " · " + STATE_LABELS.get(item["source_status"], item["source_status"])
        facts = f"Entrada {value(item['entry'])} · Stop {value(item['stop'])} · Objetivo {value(item['target'])} {value(item['currency'])}"
        title = f"{item['company']} ({value(item['ticker'])}) · {state}"
        published = item.get("published_at") or ((item["published_date"]+" (sin hora)") if item.get("published_date") else "N/D")
        next_step = {"historical": "Histórica recibida: no se contabiliza como una ganancia ejecutable.",
                     "open": "Seguir stop y objetivo virtuales; no es una nueva compra.",
                     "target_reached": "Objetivo virtual alcanzado; revisar la operación registrada.",
                     "stopped": "Stop virtual ejecutado; revisar coste y precio de salida.",
                     "expired": "Plazo de entrada vencido; no abrir una posición nueva.",
                     "ambiguous": "Vela ambigua: resultado conservador no incluido en la tasa de aciertos."}.get(item["state"])
        next_step = next_step or ("Revisar niveles antes de considerar una entrada." if item["executable"] else "Solo seguimiento: faltan condiciones de entrada verificadas.")
        detail = (f"Analista {value(item['analyst'])}. Publicada {published}; recibida {item['received_at']}; observada {item['observed_at']}. " + next_step)
        reading = item.get("model_at_receipt", {})
        detail += f" Motor a recepción: técnica {value(reading.get('technical'))}; crecimiento {value(reading.get('growth'))}; radar {value(reading.get('radar'))}."
        plain.extend([title, facts, detail, "Fuente: "+value(item["source_url"])])
        link = f'<a href="{escape(item["source_url"], quote=True)}">Fuente original</a>' if item["source_url"] else "Fuente N/D"
        app_link = f'https://stock-signal-lab-ddriu.streamlit.app/analistas?signal={item["call_id"]}'
        rows.append(f'<tr><td style="padding:10px;border-bottom:1px solid #ddd"><strong>{escape(title)}</strong><br>{escape(facts)}<br><small>{escape(detail)}</small><br>{link} · <a href="{app_link}">Ver ficha privada en Stock Signal Lab</a></td></tr>')
    if not rows:
        plain.append("Sin cambios nuevos verificados; no significa que no existan oportunidades.")
        rows.append("<tr><td>Sin cambios nuevos verificados.</td></tr>")
    plain.append(report["limitations"])
    html = ('<!doctype html><html lang="es"><head><meta charset="utf-8"></head><body style="font:15px Arial,sans-serif;max-width:640px;margin:auto;padding:16px">'
            f'<h2>Analistas externos · {escape(report["day"])}</h2><table style="width:100%;border-collapse:collapse">'
            + "".join(rows)+f'</table><p>{escape(report["limitations"])}</p></body></html>')
    return "\n".join(plain), html


def deliver_external_report(store, report, *, email_config, send=None) -> str:
    """Explicit opt-in, same existing destination, atomic at-most-once SMTP."""
    from src.email_sender import send_email
    if not store.get_preferences().get("enabled"):
        return "disabled"
    recipient = store.journal.get_alert_preferences().email
    if not recipient or not email_config.configured:
        return "not_configured"
    if not report["items"]:
        return "no_changes"
    key = stable_id("external-daily", store.owner, recipient.strip().lower(), report["day"])
    if not store.claim_delivery(key, report):
        return "already_reserved"
    plain, html = render_external_report(report)
    try:
        receipt = (send or send_email)(recipient, f"Analistas externos · {report['day']}", plain, html, config=email_config)
        if getattr(receipt, "status", None) != "smtp_accepted":
            store.finish_delivery(key, "uncertain")
            return "uncertain"
        store.finish_delivery(key, "sent")
        return "smtp_accepted"
    except Exception:
        # A timeout can happen after SMTP DATA acceptance. Never duplicate.
        store.finish_delivery(key, "uncertain")
        return "uncertain"
