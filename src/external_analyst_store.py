"""Private external recommendations, append-only revisions and delivery claims.

This adapter never stores a raw email or credentials, changes a real portfolio,
or sends mail. The existing server-side journal supplies the owner and transport.
Remote writes use transactional RPCs, never a REST fallback.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3


class ExternalAnalystConflict(ValueError):
    """An immutable external observation identity has different contents."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _aware(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("La observación requiere hora y zona horaria.")
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,256}", value):
        raise ValueError("Identificador privado no válido.")
    return value


def _private_json(value):
    result = json.loads(_json(value))
    if len(_json(result).encode()) > 1_000_000:
        raise ValueError("La observación externa supera el límite permitido.")
    denied = {"password", "secret", "token", "access_token", "refresh_token", "authorization",
              "credentials", "recipient", "recipients", "email", "raw_body", "raw_email", "html_body"}
    def visit(item, depth=0):
        if depth > 24:
            raise ValueError("La observación externa está demasiado anidada.")
        if isinstance(item, dict):
            for key, nested in item.items():
                if key.lower().replace("-", "_") in denied:
                    raise ValueError("No se almacenan credenciales, destinatarios ni correos íntegros.")
                visit(nested, depth + 1)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, depth + 1)
        elif isinstance(item, str) and re.search(r"[^\s@]+@[^\s@]+", item):
            raise ValueError("No se almacenan direcciones de correo en las observaciones externas.")
    visit(result)
    return result


_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_analyst_messages (
 owner TEXT NOT NULL,message_key TEXT NOT NULL,provider TEXT NOT NULL,
 received_at TEXT NOT NULL,observed_at TEXT NOT NULL,announced_count INTEGER,
 extracted_count INTEGER NOT NULL,issues_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
 PRIMARY KEY(owner,message_key));
CREATE TABLE IF NOT EXISTS external_analyst_calls (
 owner TEXT NOT NULL,call_id TEXT NOT NULL,provider TEXT NOT NULL,source_id TEXT NOT NULL,
 first_observed_at TEXT NOT NULL,original_json TEXT NOT NULL,
 PRIMARY KEY(owner,call_id),UNIQUE(owner,provider,source_id));
CREATE TABLE IF NOT EXISTS external_analyst_revisions (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,call_id TEXT NOT NULL,
 revision_id TEXT NOT NULL,message_key TEXT NOT NULL,observed_at TEXT NOT NULL,call_json TEXT NOT NULL,
 UNIQUE(owner,call_id,revision_id),
 FOREIGN KEY(owner,call_id) REFERENCES external_analyst_calls(owner,call_id),
 FOREIGN KEY(owner,message_key) REFERENCES external_analyst_messages(owner,message_key));
CREATE TABLE IF NOT EXISTS external_analyst_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,call_id TEXT NOT NULL,
 event_key TEXT NOT NULL,occurred_at TEXT NOT NULL,payload_json TEXT NOT NULL,
 UNIQUE(owner,call_id,event_key),FOREIGN KEY(owner,call_id) REFERENCES external_analyst_calls(owner,call_id));
CREATE TABLE IF NOT EXISTS external_analyst_settings (
 owner TEXT PRIMARY KEY,prefs_json TEXT NOT NULL,cursor_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS external_analyst_email_deliveries (
 owner TEXT NOT NULL,delivery_key TEXT NOT NULL,status TEXT NOT NULL,report_json TEXT NOT NULL,
 created_at TEXT NOT NULL,finished_at TEXT,PRIMARY KEY(owner,delivery_key));
CREATE TABLE IF NOT EXISTS external_analyst_simulation_runs (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,run_id TEXT NOT NULL,
 observed_at TEXT NOT NULL,payload_json TEXT NOT NULL,UNIQUE(owner,run_id));
"""


class ExternalAnalystStore:
    def __init__(self, journal):
        self.owner = getattr(journal, "owner", None)
        if (not isinstance(self.owner, str) or not re.fullmatch(r"[A-Za-z0-9_@.+:\-]{1,128}", self.owner)
                or self.owner.casefold() in {"anon", "anonymous", "public", "none", "null", "grupo_compartido"}):
            raise ValueError("Se requiere el diario de un usuario privado.")
        self.journal = journal
        self.path = getattr(journal, "database_path", getattr(journal, "path", None))
        self.remote = self.path is None
        if self.remote:
            if not getattr(journal, "url", None) or not callable(getattr(journal, "_request", None)):
                raise ValueError("El almacenamiento externo no está configurado.")
        else:
            self.path = Path(self.path)
            with self._connect() as db:
                db.executescript(_SCHEMA)
                for table in ("messages", "calls", "revisions", "events", "simulation_runs"):
                    full = "external_analyst_" + table
                    for operation in ("UPDATE", "DELETE"):
                        db.execute(f"CREATE TRIGGER IF NOT EXISTS {full}_{operation.lower()}_immutable "
                                   f"BEFORE {operation} ON {full} BEGIN "
                                   "SELECT RAISE(ABORT,'external observations are append-only'); END")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _rpc(self, name, payload):
        result = self.journal._request("POST", endpoint=f"{self.journal.url}/rest/v1/rpc/{name}",
            json={"p_owner": self.owner, "p_payload": payload}).json()
        return result

    def _rpc_bool(self, name, payload):
        result = self._rpc(name, payload)
        if type(result) is not bool:
            raise ValueError("No se pudo confirmar la operación externa.")
        return result

    def _select(self, kind):
        if self.remote:
            rows = self._rpc("external_analyst_select", {"kind": kind})
            if not isinstance(rows, list):
                raise ValueError("La lectura externa no tiene formato válido.")
            if any(not isinstance(row, dict) or row.get("owner") != self.owner for row in rows):
                raise ValueError("La lectura no corresponde al usuario.")
            return rows
        table = {"calls": "external_analyst_calls", "revisions": "external_analyst_revisions",
                 "events": "external_analyst_events", "messages": "external_analyst_messages",
                 "settings": "external_analyst_settings", "deliveries": "external_analyst_email_deliveries",
                 "simulations": "external_analyst_simulation_runs"}[kind]
        order = "sequence" if kind in {"revisions", "events", "simulations"} else "rowid"
        with self._connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE owner=? ORDER BY {order}", (self.owner,))]

    @staticmethod
    def _decode(value):
        return json.loads(value) if isinstance(value, str) else value

    def import_message(self, parsed):
        from src.external_analysts import normalize_call, revision_id
        if not isinstance(parsed, Mapping):
            raise ValueError("El mensaje debe ser una observación estructurada.")
        received, observed = _aware(parsed["received_at"]), _aware(parsed["observed_at"])
        if observed < received:
            raise ValueError("La detección no puede preceder a la recepción.")
        calls = [normalize_call({**call, "received_at": call.get("received_at") or received, "observed_at": observed})
                 for call in parsed.get("calls", [])]
        if len(calls) > 500 or len({call["id"] for call in calls}) != len(calls):
            raise ValueError("El mensaje contiene demasiadas recomendaciones o duplicados internos.")
        provider = str(parsed.get("provider") or "perito_inversor")
        if not re.fullmatch(r"[a-z0-9_\-]{1,64}", provider):
            raise ValueError("Proveedor del mensaje no válido.")
        key = _hash(str(parsed["message_id"]))
        issues = [str(issue)[:200] for issue in parsed.get("issues", [])][:50]
        announced = parsed.get("announced_count")
        if announced is not None and (type(announced) is not int or announced < 0):
            raise ValueError("El recuento anunciado no es válido.")
        normalized = [_private_json(call) for call in calls]
        fingerprint = _hash({"provider": provider, "calls": [revision_id(c) for c in normalized], "announced": announced})
        request = {"message_key": key, "provider": provider, "received_at": received,
                   "observed_at": observed, "announced_count": announced, "extracted_count": len(calls),
                   "issues": _private_json(issues), "fingerprint": fingerprint,
                   "calls": [{"call": c, "revision_id": revision_id(c)} for c in normalized]}
        if self.remote:
            result = self._rpc("external_analyst_import", request)
            counts = ("inserted", "revised", "duplicates")
            if (not isinstance(result, dict) or type(result.get("message_duplicate")) is not bool
                    or any(type(result.get(key)) is not int or result[key] < 0 for key in counts)
                    or sum(result[key] for key in counts) != len(calls)
                    or result["message_duplicate"] and (result["inserted"] or result["revised"])):
                raise ValueError("El almacenamiento no confirmó la importación externa.")
            return result
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint FROM external_analyst_messages WHERE owner=? AND message_key=?", (self.owner, key)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ExternalAnalystConflict("Un mismo mensaje tiene contenido estructurado distinto.")
                return {"inserted": 0, "revised": 0, "duplicates": len(calls), "message_duplicate": True}
            db.execute("INSERT INTO external_analyst_messages VALUES (?,?,?,?,?,?,?,?,?)",
                (self.owner, key, provider, received, observed, announced, len(calls), _json(issues), fingerprint))
            inserted = revised = duplicates = 0
            for item in request["calls"]:
                call, revision = item["call"], item["revision_id"]
                prior = db.execute("SELECT call_id FROM external_analyst_calls WHERE owner=? AND provider=? AND source_id=?",
                    (self.owner, call["provider"], call["source_id"])).fetchone()
                if prior and prior[0] != call["id"]:
                    raise ExternalAnalystConflict("La identidad de la recomendación ha cambiado.")
                if not prior:
                    db.execute("INSERT INTO external_analyst_calls VALUES (?,?,?,?,?,?)",
                        (self.owner, call["id"], call["provider"], call["source_id"], observed, _json(call)))
                    inserted += 1
                exists = db.execute("SELECT 1 FROM external_analyst_revisions WHERE owner=? AND call_id=? AND revision_id=?",
                    (self.owner, call["id"], revision)).fetchone()
                if exists:
                    duplicates += 1
                else:
                    db.execute("INSERT INTO external_analyst_revisions(owner,call_id,revision_id,message_key,observed_at,call_json) VALUES (?,?,?,?,?,?)",
                        (self.owner, call["id"], revision, key, observed, _json(call)))
                    if prior:
                        revised += 1
            return {"inserted": inserted, "revised": revised, "duplicates": duplicates, "message_duplicate": False}

    def list_calls(self):
        latest = {}
        revisions = sorted(self._select("revisions"), key=lambda row: (_aware(row["observed_at"]), row["sequence"]))
        for row in revisions:
            call = dict(self._decode(row["call_json"]))
            call["revision_id"] = row["revision_id"]
            latest[row["call_id"]] = call
        for row in self._select("calls"):
            if row["call_id"] in latest:
                latest[row["call_id"]]["first_observed_at"] = row["first_observed_at"]
        events = sorted(self.list_events(), key=lambda row: (_aware(row["occurred_at"]), row["sequence"]))
        for event in events:
            call = latest.get(event["call_id"])
            if call is not None:
                call["latest_event"] = event
                if event["payload"].get("status"):
                    call["tracking_status"] = event["payload"]["status"]
        return list(latest.values())

    def list_messages(self):
        return [{**row, "issues": self._decode(row["issues_json"])} for row in self._select("messages")]

    def list_revisions(self, call_id=None):
        if call_id is not None:
            call_id = _identifier(call_id)
        return [{**row, "call": self._decode(row["call_json"])}
                for row in self._select("revisions") if call_id is None or row["call_id"] == call_id]

    def list_events(self):
        return [{**row, "payload": self._decode(row["payload_json"])} for row in self._select("events")]

    def append_event(self, call_id, event_key, payload, occurred_at):
        call_id, event_key = _identifier(call_id), _identifier(event_key)
        timestamp, payload = _aware(occurred_at), _private_json(payload)
        request = {"call_id": call_id, "event_key": event_key, "occurred_at": timestamp, "payload": payload}
        if self.remote:
            return self._rpc_bool("external_analyst_append_event", request)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            call = db.execute("SELECT first_observed_at FROM external_analyst_calls WHERE owner=? AND call_id=?",
                              (self.owner, call_id)).fetchone()
            if call is None or timestamp < call[0]:
                raise ValueError("El evento no puede preceder a la recomendación conocida.")
            existing = db.execute("SELECT occurred_at,payload_json FROM external_analyst_events WHERE owner=? AND call_id=? AND event_key=?", (self.owner, call_id, event_key)).fetchone()
            if existing:
                if tuple(existing) != (timestamp, _json(payload)):
                    raise ExternalAnalystConflict("El evento ya existe con un contenido distinto.")
                return False
            db.execute("INSERT INTO external_analyst_events(owner,call_id,event_key,occurred_at,payload_json) VALUES (?,?,?,?,?)", (self.owner, call_id, event_key, timestamp, _json(payload)))
            return True

    def get_preferences(self):
        rows = self._select("settings")
        return {"enabled": False, **(self._decode(rows[0]["prefs_json"]) if rows else {})}

    def _settings(self, kind, value):
        value = _private_json(value)
        if self.remote:
            return self._rpc_bool("external_analyst_settings_write", {"kind": kind, "value": value})
        with self._connect() as db:
            db.execute("INSERT INTO external_analyst_settings VALUES (?,?,?) ON CONFLICT(owner) DO NOTHING", (self.owner, "{}", "{}"))
            column = {"preferences": "prefs_json", "cursor": "cursor_json"}[kind]
            db.execute(f"UPDATE external_analyst_settings SET {column}=? WHERE owner=?", (_json(value), self.owner))

    def save_preferences(self, values):
        allowed = {"enabled", "commission_eur", "spread_bps", "slippage_bps"}
        if set(values) - allowed or type(values.get("enabled", False)) is not bool:
            raise ValueError("Preferencias de analistas externos no válidas.")
        for key, number in values.items():
            if key != "enabled" and (isinstance(number, bool) or not isinstance(number, (int, float)) or not 0 <= number <= 1000):
                raise ValueError("Costes simulados no válidos.")
        self._settings("preferences", dict(values))

    def get_mail_cursor(self):
        rows = self._select("settings")
        return self._decode(rows[0]["cursor_json"]) if rows else {}

    def set_mail_cursor(self, values):
        if set(values) - {"uidvalidity", "last_uid", "mailbox_hash"}:
            raise ValueError("Cursor de correo no válido.")
        for key in ("uidvalidity", "last_uid"):
            if key in values and (isinstance(values[key], bool) or not str(values[key]).isdigit()
                                  or not 0 <= int(values[key]) <= 4294967295):
                raise ValueError("UID del cursor no válido.")
        if "mailbox_hash" in values and not re.fullmatch(r"[a-f0-9]{64}", str(values["mailbox_hash"])):
            raise ValueError("La cuenta del cursor debe estar anonimizada.")
        self._settings("cursor", dict(values))

    def claim_delivery(self, key, report):
        key, report = _identifier(key), _private_json(report)
        request = {"delivery_key": key, "report": report}
        if self.remote:
            return self._rpc_bool("external_analyst_claim_delivery", request)
        with self._connect() as db:
            cursor = db.execute("INSERT INTO external_analyst_email_deliveries VALUES (?,?, 'reserved',?,?,NULL) ON CONFLICT(owner,delivery_key) DO NOTHING",
                (self.owner, key, _json(report), datetime.now(timezone.utc).isoformat()))
            return cursor.rowcount == 1

    def finish_delivery(self, key, status):
        key = _identifier(key)
        if status not in {"sent", "failed", "uncertain"}:
            raise ValueError("Estado de envío no válido.")
        if self.remote:
            self._rpc_bool("external_analyst_finish_delivery", {"delivery_key": key, "status": status})
            return None
        with self._connect() as db:
            row = db.execute("SELECT status FROM external_analyst_email_deliveries WHERE owner=? AND delivery_key=?", (self.owner, key)).fetchone()
            if row is None or row[0] not in {"reserved", status}:
                raise ExternalAnalystConflict("La reserva de correo no admite ese cambio.")
            db.execute("UPDATE external_analyst_email_deliveries SET status=?,finished_at=? WHERE owner=? AND delivery_key=? AND status='reserved'",
                (status, datetime.now(timezone.utc).isoformat(), self.owner, key))

    def list_deliveries(self):
        return [{**row, "report": self._decode(row["report_json"])} for row in self._select("deliveries")]

    def save_simulation_run(self, run_id, payload):
        run_id, payload = _identifier(run_id), _private_json(payload)
        observed_at = _aware(payload.get("observed_at") or payload.get("available_at") or datetime.now(timezone.utc))
        if self.remote:
            return self._rpc_bool("external_analyst_save_simulation", {"run_id": run_id, "observed_at": observed_at, "payload": payload})
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_json FROM external_analyst_simulation_runs WHERE owner=? AND run_id=?", (self.owner, run_id)).fetchone()
            if row:
                if row[0] != _json(payload):
                    raise ExternalAnalystConflict("La simulación guardada es inmutable.")
                return False
            db.execute("INSERT INTO external_analyst_simulation_runs(owner,run_id,observed_at,payload_json) VALUES (?,?,?,?)", (self.owner, run_id, observed_at, _json(payload)))
            return True

    def list_simulation_runs(self):
        return [{**row, "payload": self._decode(row["payload_json"])} for row in self._select("simulations")]

    def get_simulation_state(self):
        rows = self.list_simulation_runs()
        return rows[-1]["payload"] if rows else None
