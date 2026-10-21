"""Owner-scoped, append-only results for the original four-model experiment.

USD is never written into a column named EUR. The authoritative USD header and
snapshots live in the model tables. Existing paper tables are optional EUR
projections, created only from explicitly supplied, observed EUR amounts.

The remote adapter uses one transactional RPC per write; it intentionally has
no REST/upsert fallback. No method sends mail or touches real holdings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

from src.journal import normalize_paper_daily_run, normalize_paper_simulation


NAMESPACE = "model-original-v1"
GENESIS_HASH = "0" * 64
EVENT_TYPES = frozenset({
    "observation", "decision", "order_proposed", "order_filled", "price_mark",
    "incident", "email_delivery",
})
_SENSITIVE_KEYS = frozenset({
    "password", "secret", "secret_key", "service_role_key", "api_key",
    "access_token", "refresh_token", "authorization", "smtp_password",
    "token", "api_token", "client_secret", "private_key", "credentials",
    "credential", "smtp_user", "smtp_username", "smtp_token", "bearer_token",
    "recipient", "recipients", "email", "email_address", "to_address",
})


class ModelResultsConflict(ValueError):
    """An immutable identity was reused with different contents, or stale CAS."""


def canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("Model results must contain finite, plain JSON values.") from exc


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def deterministic_event_id(*parts: object) -> str:
    """Build a stable identity from semantic keys, never from a wall-clock retry."""
    return content_hash(list(parts))


def _owner(value: object) -> str:
    result = str(value or "").strip()
    if result.lower() in {"", "anon", "anonymous", "public", "none", "null"}:
        raise ValueError("A non-anonymous owner is required for model results.")
    return result


def _identifier(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > 256 or any(ord(char) < 32 for char in result):
        raise ValueError(f"Invalid {label}.")
    return result


def _aware(value: object, label: str) -> str:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} requires a timezone-aware timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} requires a timezone-aware timestamp.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _market_date(value: object) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError("market_date requires an ISO date, not a timestamp.") from exc


def _safe_json(value: object) -> Any:
    """Do not put credentials or email addresses into the experiment ledger."""
    parsed = json.loads(canonical_json(value))

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key.lower().replace("-", "_") in _SENSITIVE_KEYS:
                    raise ValueError("Credentials and recipient PII cannot be stored in model results.")
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and re.search(r"[^\s@]+@[^\s@]+", item):
            raise ValueError("Email addresses cannot be stored in model results.")

    visit(parsed)
    return parsed


def _event(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Each ledger event must be an object.")
    kind = str(value.get("event_type", ""))
    if kind not in EVENT_TYPES:
        raise ValueError("Unsupported model ledger event type.")
    occurred = _aware(value.get("occurred_at"), "occurred_at")
    available = _aware(value.get("available_at"), "available_at")
    if occurred > available:
        raise ValueError("An event cannot become available before it occurs.")
    payload = _safe_json(value.get("payload", {}))
    if not isinstance(payload, dict):
        raise ValueError("Event payload must be an object.")
    if kind == "email_delivery":
        status = payload.get("status")
        if status not in {"planned", "attempted", "failed", "smtp_accepted"}:
            raise ValueError("Email status must distinguish SMTP acceptance from delivery.")
        _identifier(payload.get("delivery_id"), "delivery_id")
        if status == "smtp_accepted":
            if payload.get("smtp_code") != 250 or not re.fullmatch(r"[a-f0-9]{64}", str(payload.get("receipt_hash", ""))):
                raise ValueError("SMTP acceptance requires code 250 and a hashed acceptance receipt.")
            accepted_at = _aware(payload.get("accepted_at"), "accepted_at")
            if accepted_at > available:
                raise ValueError("SMTP acceptance cannot follow event availability.")
            payload["accepted_at"] = accepted_at
    result = {
        "event_id": _identifier(value.get("event_id"), "event_id"),
        "event_type": kind, "occurred_at": occurred,
        "available_at": available, "payload": payload,
    }
    return {**result, "event_hash": content_hash(result), "event_canonical": canonical_json(result)}


def _observed_fx(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("An observed FX record is required, not just an FX identifier.")
    result = dict(value)
    try:
        rate = float(result["rate_usd_to_eur"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The FX observation requires a finite USD/EUR rate.") from exc
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("The FX observation requires a positive finite rate.")
    result["rate_usd_to_eur"] = rate
    result["available_at"] = _aware(result.get("available_at"), "FX available_at")
    result["observation_id"] = _identifier(result.get("observation_id"), "FX observation_id")
    result["source"] = _identifier(result.get("source"), "FX source")
    return result


def _assert_conversion(native: object, eur: object, rate: float) -> None:
    try:
        correct = math.isclose(float(eur), float(native) * rate, rel_tol=1e-8, abs_tol=1e-6)
    except (ValueError, TypeError) as exc:
        raise ValueError("An EUR projection requires observed native amounts.") from exc
    if not correct:
        raise ValueError("The EUR projection does not match its observed USD/EUR rate.")


def _season_request(owner: str, manifest: Mapping[str, object], season_id: str | None) -> dict[str, object]:
    parsed = _safe_json(dict(manifest))
    strategy = str(parsed.get("strategy_key", "")).upper()
    mode = str(parsed.get("mode", "")).lower()
    if strategy not in {"A", "B", "C", "D"} or mode not in {"theoretical", "budget"}:
        raise ValueError("A season requires strategy A/B/C/D and theoretical/budget mode.")
    parsed["strategy_key"], parsed["mode"] = strategy, mode
    parsed["season_key"] = _identifier(parsed.get("season_key"), "season_key")
    parsed["start_date"] = _market_date(parsed.get("start_date"))
    currency = str(parsed.get("base_currency", "USD")).upper()
    if currency not in {"USD", "EUR"}:
        raise ValueError("Model seasons currently support USD or EUR.")
    parsed["base_currency"] = currency
    nav_key = "initial_nav_usd" if currency == "USD" else "initial_nav_eur"
    try:
        nav = float(parsed[nav_key])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"The manifest requires {nav_key}.") from exc
    zero_funded_theory = (currency == "USD" and mode == "theoretical"
                          and parsed.get("funding_rule") == "external_100_per_entry")
    if not math.isfinite(nav) or nav < 0 or (nav == 0 and not zero_funded_theory):
        raise ValueError("Initial NAV must be positive, except zero-funded USD external-entry theory.")
    if nav == 0:
        if parsed.get("initial_cash_usd", 0) != 0 or parsed.get("initial_positions") or parsed.get("initial_positions_usd"):
            raise ValueError("A zero-NAV theoretical season must start without cash or positions.")
    parsed[nav_key] = nav
    paper_header = None
    if "paper_header" in parsed:
        supplied = dict(parsed["paper_header"])
        # No implicit FX, and no conversion of USD amounts into EUR columns.
        if currency == "USD":
            reference = supplied.pop("fx_observation_id", None)
            if not reference:
                raise ValueError("A USD season EUR projection requires an observed FX reference.")
            fx = _observed_fx(parsed.get("fx_observation"))
            if fx["observation_id"] != reference:
                raise ValueError("The initial EUR projection references a different FX observation.")
            parsed["fx_observation"] = fx
            _assert_conversion(nav, supplied.get("initial_nav_eur"), fx["rate_usd_to_eur"])
        paper_header = normalize_paper_simulation(
            name=str(supplied.pop("name", f"{NAMESPACE}/{strategy}/{mode}/{parsed['season_key']}")),
            start_date=parsed["start_date"], initial_positions=supplied.pop("initial_positions", []),
            assumptions={"model_namespace": NAMESPACE, "model_manifest_hash": content_hash(parsed)},
            base_currency="EUR", strategy_key=f"{NAMESPACE}:{strategy}:{mode}",
            engine_version=str(parsed.get("engine_version", NAMESPACE)), **supplied,
        )
        paper_header["recorded_by"] = owner
        if currency == "USD" and fx["available_at"] > _aware(paper_header["created_at"], "header created_at"):
            raise ValueError("An initial EUR projection cannot use a future FX observation.")
    identifier = season_id or content_hash([owner, NAMESPACE, parsed["season_key"], strategy, mode])
    return {"owner": owner, "namespace": NAMESPACE,
            "season_id": _identifier(identifier, "season_id"), "manifest": parsed,
            "manifest_hash": content_hash(parsed), "manifest_canonical": canonical_json(parsed),
            "paper_header": paper_header}


def _batch_request(owner: str, season_id: str, *, run_id: str, market_date: str,
                   events: Sequence[Mapping[str, object]], snapshot: Mapping[str, object] | None = None,
                   paper_snapshot: Mapping[str, object] | None = None,
                   expected_previous_hash: str | None = None, batch_id: str | None = None) -> dict[str, object]:
    normalized = [_event(event) for event in events]
    if not normalized and snapshot is None:
        raise ValueError("An append batch requires events or a snapshot.")
    snapshot_record = None
    if snapshot is not None:
        payload = _safe_json(dict(snapshot))
        available = _aware(payload.get("available_at"), "snapshot.available_at")
        payload["available_at"] = available
        snapshot_record = {"payload": payload, "available_at": available,
                           "snapshot_hash": content_hash(payload), "snapshot_canonical": canonical_json(payload)}
    paper = _safe_json(dict(paper_snapshot)) if paper_snapshot is not None else None
    if paper is not None and snapshot_record is None:
        raise ValueError("A paper projection requires an authoritative model snapshot.")
    if expected_previous_hash is not None and not re.fullmatch(r"[a-f0-9]{64}", expected_previous_hash):
        raise ValueError("expected_previous_hash must be SHA256 hex.")
    content = {"owner": owner, "namespace": NAMESPACE,
               "season_id": _identifier(season_id, "season_id"),
               "run_id": _identifier(run_id, "run_id"), "market_date": _market_date(market_date),
               "events": normalized, "snapshot": snapshot_record, "paper_snapshot": paper,
               "paper_snapshot_canonical": canonical_json(paper) if paper is not None else None}
    digest = content_hash(content)
    return {**content, "content_hash": digest, "content_canonical": canonical_json(content),
            "batch_id": _identifier(batch_id or digest, "batch_id"),
            "expected_previous_hash": expected_previous_hash}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_result_seasons (
 owner TEXT NOT NULL, namespace TEXT NOT NULL, season_id TEXT NOT NULL,
 strategy_key TEXT NOT NULL, mode TEXT NOT NULL, manifest_json TEXT NOT NULL,
 manifest_hash TEXT NOT NULL, paper_simulation_id INTEGER, created_at TEXT NOT NULL,
 PRIMARY KEY (owner,namespace,season_id),
 FOREIGN KEY(owner,paper_simulation_id) REFERENCES paper_simulations(owner,id)
);
CREATE TABLE IF NOT EXISTS model_result_batches (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, namespace TEXT NOT NULL,
 season_id TEXT NOT NULL, run_id TEXT NOT NULL, batch_id TEXT NOT NULL, market_date TEXT NOT NULL,
 content_hash TEXT NOT NULL, previous_hash TEXT NOT NULL, head_hash TEXT NOT NULL,
 request_json TEXT NOT NULL,
 last_available_at TEXT NOT NULL, paper_daily_run_id INTEGER, created_at TEXT NOT NULL,
 UNIQUE(owner,namespace,season_id,batch_id),
 FOREIGN KEY(owner,namespace,season_id) REFERENCES model_result_seasons(owner,namespace,season_id)
);
CREATE TABLE IF NOT EXISTS model_result_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, namespace TEXT NOT NULL,
 season_id TEXT NOT NULL, run_id TEXT NOT NULL, batch_id TEXT NOT NULL, event_id TEXT NOT NULL,
 event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, available_at TEXT NOT NULL,
 payload_json TEXT NOT NULL, event_hash TEXT NOT NULL,
 UNIQUE(owner,namespace,season_id,event_id),
 FOREIGN KEY(owner,namespace,season_id,batch_id)
 REFERENCES model_result_batches(owner,namespace,season_id,batch_id)
);
CREATE TABLE IF NOT EXISTS model_result_snapshots (
 owner TEXT NOT NULL, namespace TEXT NOT NULL, season_id TEXT NOT NULL, run_id TEXT NOT NULL,
 market_date TEXT NOT NULL, batch_id TEXT NOT NULL, available_at TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL, paper_snapshot_hash TEXT,
 PRIMARY KEY(owner,namespace,season_id,run_id),
 FOREIGN KEY(owner,namespace,season_id,batch_id)
 REFERENCES model_result_batches(owner,namespace,season_id,batch_id)
);
"""


class ModelResultsStore:
    """SQLite immutable ledger using the existing journal's database, not APIs."""

    def __init__(self, journal: object, *, owner: str | None = None) -> None:
        self.owner = _owner(owner if owner is not None else getattr(journal, "owner", None))
        if owner is not None and getattr(journal, "owner", self.owner) != self.owner:
            raise ValueError("The model ledger owner must match the journal owner.")
        database_path = getattr(journal, "database_path", getattr(journal, "path", None))
        if database_path is None:
            raise ValueError("SQLite model results require an initialized local journal.")
        self.database_path = Path(database_path)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            for table in ("model_result_seasons", "model_result_batches", "model_result_events", "model_result_snapshots"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}_immutable
                      BEFORE {operation} ON {table} BEGIN
                      SELECT RAISE(ABORT, 'model results are append-only'); END""")
            # Guard only model-owned rows; historical laboratory runs retain their API.
            for table, clause in (
                ("paper_simulations", "s.paper_simulation_id=OLD.id AND s.owner=OLD.owner"),
                ("paper_daily_runs", "s.paper_simulation_id=OLD.simulation_id AND s.owner=OLD.owner"),
            ):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS model_{table}_{operation.lower()}_immutable
                      BEFORE {operation} ON {table}
                      WHEN EXISTS(SELECT 1 FROM model_result_seasons s WHERE {clause})
                      BEGIN SELECT RAISE(ABORT, 'model paper projections are append-only'); END""")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _insert(connection: sqlite3.Connection, table: str, values: Mapping[str, object]) -> int:
        columns = list(values)
        cursor = connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
        return int(cursor.lastrowid)

    def create_season(self, config_manifest: Mapping[str, object], *, season_id: str | None = None) -> dict[str, object]:
        request = _season_request(self.owner, config_manifest, season_id)
        key = (self.owner, NAMESPACE, request["season_id"])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM model_result_seasons WHERE owner=? AND namespace=? AND season_id=?", key).fetchone()
            if existing is not None:
                if existing["manifest_hash"] != request["manifest_hash"]:
                    raise ModelResultsConflict("The immutable season manifest differs.")
                return self._decode(existing)
            paper_id = None
            if request["paper_header"] is not None:
                paper_id = self._insert(connection, "paper_simulations", {"owner": self.owner, **request["paper_header"]})
            self._insert(connection, "model_result_seasons", {
                "owner": self.owner, "namespace": NAMESPACE, "season_id": request["season_id"],
                "strategy_key": request["manifest"]["strategy_key"], "mode": request["manifest"]["mode"],
                "manifest_json": canonical_json(request["manifest"]), "manifest_hash": request["manifest_hash"],
                "paper_simulation_id": paper_id, "created_at": datetime.now(timezone.utc).isoformat(),
            })
            row = connection.execute("SELECT * FROM model_result_seasons WHERE owner=? AND namespace=? AND season_id=?", key).fetchone()
            return self._decode(row)

    def append_batch(self, season_id: str, **kwargs: Any) -> dict[str, object]:
        request = _batch_request(self.owner, season_id, **kwargs)
        key = (self.owner, NAMESPACE, request["season_id"])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            season = connection.execute("SELECT * FROM model_result_seasons WHERE owner=? AND namespace=? AND season_id=?", key).fetchone()
            if season is None:
                raise ValueError("The model season does not exist for this owner.")
            prior = connection.execute("SELECT * FROM model_result_batches WHERE owner=? AND namespace=? AND season_id=? AND batch_id=?", (*key, request["batch_id"])).fetchone()
            if prior is not None:
                if prior["content_hash"] != request["content_hash"]:
                    raise ModelResultsConflict("The immutable batch identity has different contents.")
                return self._result(prior, inserted=0, no_op=True)
            previous = connection.execute("SELECT * FROM model_result_batches WHERE owner=? AND namespace=? AND season_id=? ORDER BY sequence DESC LIMIT 1", key).fetchone()
            head = previous["head_hash"] if previous else GENESIS_HASH
            watermark = previous["last_available_at"] if previous else ""
            dates = connection.execute("SELECT DISTINCT market_date FROM model_result_batches WHERE owner=? AND namespace=? AND season_id=? AND run_id=?", (*key, request["run_id"])).fetchall()
            if any(row["market_date"] != request["market_date"] for row in dates):
                raise ModelResultsConflict("A run identity cannot move to a different market date.")
            fresh = []
            seen: dict[str, str] = {}
            for event in request["events"]:
                old = connection.execute("SELECT event_hash FROM model_result_events WHERE owner=? AND namespace=? AND season_id=? AND event_id=?", (*key, event["event_id"])).fetchone()
                known_hash = old["event_hash"] if old else seen.get(event["event_id"])
                if known_hash is not None:
                    if known_hash != event["event_hash"]:
                        raise ModelResultsConflict("An immutable event identity has different contents.")
                    continue
                seen[event["event_id"]] = event["event_hash"]
                if event["available_at"] < watermark:
                    raise ModelResultsConflict("Availability clocks cannot move backwards.")
                watermark = event["available_at"]
                fresh.append(event)
            snapshot = request["snapshot"]
            paper_hash = content_hash(request["paper_snapshot"]) if request["paper_snapshot"] is not None else None
            old_snapshot = connection.execute("SELECT * FROM model_result_snapshots WHERE owner=? AND namespace=? AND season_id=? AND run_id=?", (*key, request["run_id"])).fetchone()
            new_snapshot = snapshot is not None and old_snapshot is None
            if snapshot is not None and old_snapshot is not None:
                if (snapshot["snapshot_hash"] != old_snapshot["snapshot_hash"] or paper_hash != old_snapshot["paper_snapshot_hash"] or request["market_date"] != old_snapshot["market_date"]):
                    raise ModelResultsConflict("The daily snapshot is immutable.")
            if new_snapshot:
                if snapshot["available_at"] < watermark:
                    raise ModelResultsConflict("Snapshot availability cannot precede committed facts.")
                watermark = snapshot["available_at"]
            if not fresh and not new_snapshot:
                return {"run_id": request["run_id"], "batch_id": request["batch_id"],
                        "head_hash": head, "previous_hash": head, "inserted_events": 0,
                        "no_op": True, "paper_daily_run_id": None}
            if request["expected_previous_hash"] is not None and request["expected_previous_hash"] != head:
                raise ModelResultsConflict("Stale model ledger head; reload before appending.")
            paper_run_id = None
            if request["paper_snapshot"] is not None and new_snapshot:
                if season["paper_simulation_id"] is None:
                    raise ValueError("USD results need an explicit observed-EUR header before a paper projection.")
                paper_values = dict(request["paper_snapshot"])
                if json.loads(season["manifest_json"])["base_currency"] == "USD":
                    reference = paper_values.pop("fx_observation_id", None)
                    if not reference:
                        raise ValueError("An EUR paper snapshot requires an observed FX reference.")
                    fx_event = next((item for item in fresh if item["event_id"] == reference), None)
                    if fx_event is None:
                        row = connection.execute("SELECT * FROM model_result_events WHERE owner=? AND namespace=? AND season_id=? AND event_id=?",
                                                 (*key, reference)).fetchone()
                        fx_event = self._decode(row) if row else None
                    if fx_event is None or fx_event["event_type"] not in {"observation", "price_mark"}:
                        raise ValueError("The EUR snapshot FX reference is not an observed ledger event.")
                    fx = _observed_fx({**fx_event["payload"], "observation_id": reference,
                                       "available_at": fx_event["available_at"]})
                    if fx["available_at"] > snapshot["available_at"]:
                        raise ValueError("The EUR snapshot cannot use future FX observations.")
                    for metric in ("cash", "net_nav", "hold_nav", "benchmark_nav", "realized_pnl", "unrealized_pnl", "cumulative_costs", "tax_reserve"):
                        if f"{metric}_eur" in paper_values:
                            _assert_conversion(snapshot["payload"].get(f"{metric}_usd"),
                                               paper_values[f"{metric}_eur"], fx["rate_usd_to_eur"])
                signal = paper_values.get("signal_as_of")
                if signal is not None:
                    paper_values["signal_as_of"] = _aware(signal, "paper signal_as_of")
                paper_values = normalize_paper_daily_run(simulation_id=season["paper_simulation_id"], market_date=request["market_date"], **paper_values)
                paper_run_id = self._insert(connection, "paper_daily_runs", {"owner": self.owner, **paper_values})
            batch_values = {
                "owner": self.owner, "namespace": NAMESPACE, "season_id": season_id,
                "run_id": request["run_id"], "batch_id": request["batch_id"], "market_date": request["market_date"],
                "content_hash": request["content_hash"], "previous_hash": head,
                "head_hash": hashlib.sha256(f"{head}:{request['content_hash']}".encode()).hexdigest(),
                "request_json": canonical_json({key: value for key, value in request.items() if key != "expected_previous_hash"}),
                "last_available_at": watermark, "paper_daily_run_id": paper_run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._insert(connection, "model_result_batches", batch_values)
            for event in fresh:
                self._insert(connection, "model_result_events", {
                    "owner": self.owner, "namespace": NAMESPACE, "season_id": season_id,
                    "run_id": request["run_id"], "batch_id": request["batch_id"],
                    **{key: event[key] for key in ("event_id", "event_type", "occurred_at", "available_at", "event_hash")},
                    "payload_json": canonical_json(event["payload"]),
                })
            if new_snapshot:
                self._insert(connection, "model_result_snapshots", {
                    "owner": self.owner, "namespace": NAMESPACE, "season_id": season_id,
                    "run_id": request["run_id"], "market_date": request["market_date"], "batch_id": request["batch_id"],
                    "available_at": snapshot["available_at"], "snapshot_json": canonical_json(snapshot["payload"]),
                    "snapshot_hash": snapshot["snapshot_hash"], "paper_snapshot_hash": paper_hash,
                })
            return self._result(batch_values, inserted=len(fresh), no_op=False)

    append_run = append_batch

    @staticmethod
    def _result(row: Mapping[str, object], *, inserted: int, no_op: bool) -> dict[str, object]:
        return {**{key: row[key] for key in ("run_id", "batch_id", "head_hash", "previous_hash", "paper_daily_run_id")},
                "inserted_events": inserted, "no_op": no_op}

    @staticmethod
    def _decode(row: Mapping[str, object]) -> dict[str, object]:
        result = dict(row)
        for column, key in (("manifest_json", "manifest"), ("payload_json", "payload"), ("snapshot_json", "snapshot"), ("request_json", "request")):
            if column in result:
                result[key] = json.loads(result.pop(column))
        return result

    def select(self, kind: str, *, season_id: str | None = None, run_id: str | None = None) -> list[dict[str, object]]:
        tables = {"seasons": "model_result_seasons", "batches": "model_result_batches",
                  "events": "model_result_events", "snapshots": "model_result_snapshots"}
        if kind not in tables:
            raise ValueError("Unknown model result selection.")
        prefix = "s." if kind == "snapshots" else ""
        where, parameters = f"{prefix}owner=? AND {prefix}namespace=?", [self.owner, NAMESPACE]
        if season_id is not None:
            where += f" AND {prefix}season_id=?"
            parameters.append(season_id)
        if run_id is not None:
            if kind == "seasons":
                raise ValueError("Season selection does not take a run_id.")
            where += f" AND {prefix}run_id=?"
            parameters.append(run_id)
        order = "sequence" if kind in {"events", "batches"} else "season_id" if kind == "seasons" else "market_date"
        with self._connect() as connection:
            if kind == "snapshots":
                return [self._decode(row) for row in connection.execute(
                    "SELECT s.*,b.sequence AS commit_sequence FROM model_result_snapshots s "
                    "JOIN model_result_batches b ON b.owner=s.owner AND b.namespace=s.namespace "
                    "AND b.season_id=s.season_id AND b.batch_id=s.batch_id "
                    f"WHERE {where} ORDER BY s.market_date,b.sequence", parameters)]
            return [self._decode(row) for row in connection.execute(f"SELECT * FROM {tables[kind]} WHERE {where} ORDER BY {order}", parameters)]

    def list_seasons(self) -> list[dict[str, object]]:
        return self.select("seasons")

    def list_events(self, season_id: str, *, run_id: str | None = None) -> list[dict[str, object]]:
        return self.select("events", season_id=season_id, run_id=run_id)

    def list_snapshots(self, season_id: str) -> list[dict[str, object]]:
        return self.select("snapshots", season_id=season_id)


class SupabaseModelResultsStore:
    """Thin adapter to the service-side atomic RPCs, with no network at import."""

    def __init__(self, journal: object) -> None:
        self.owner = _owner(getattr(journal, "owner", None))
        if not callable(getattr(journal, "_request", None)) or not getattr(journal, "url", None):
            raise ValueError("The remote ledger requires a configured server-side journal.")
        self._journal = journal

    def _rpc(self, name: str, request: dict[str, object]) -> Any:
        response = self._journal._request("POST", endpoint=f"{self._journal.url}/rest/v1/rpc/{name}", json={"p_request": request})
        result = response.json()
        if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict) and name != "model_results_select":
            return result[0]
        return result

    def create_season(self, config_manifest: Mapping[str, object], *, season_id: str | None = None) -> dict[str, object]:
        return self._rpc("model_results_create_season", _season_request(self.owner, config_manifest, season_id))

    def append_batch(self, season_id: str, **kwargs: Any) -> dict[str, object]:
        return self._rpc("model_results_append_batch", _batch_request(self.owner, season_id, **kwargs))

    append_run = append_batch

    def select(self, kind: str, *, season_id: str | None = None, run_id: str | None = None) -> list[dict[str, object]]:
        if kind not in {"seasons", "batches", "events", "snapshots"}:
            raise ValueError("Unknown model result selection.")
        return self._rpc("model_results_select", {"owner": self.owner, "namespace": NAMESPACE, "kind": kind, "season_id": season_id, "run_id": run_id})

    def list_seasons(self) -> list[dict[str, object]]:
        return self.select("seasons")

    def list_events(self, season_id: str, *, run_id: str | None = None) -> list[dict[str, object]]:
        return self.select("events", season_id=season_id, run_id=run_id)

    def list_snapshots(self, season_id: str) -> list[dict[str, object]]:
        return self.select("snapshots", season_id=season_id)


def create_model_results_store(journal: object) -> ModelResultsStore | SupabaseModelResultsStore:
    if getattr(journal, "database_path", getattr(journal, "path", None)) is not None:
        return ModelResultsStore(journal)
    return SupabaseModelResultsStore(journal)
