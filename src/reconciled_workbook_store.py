"""Validated, additive and idempotent persistence of reconciled import plans.

No source workbook paths or raw cells are copied into metadata. The SQLite
transaction backs up all previous accounting rows privately before adding
anything; conflicts are retained for review, never resolved by overwriting.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import json
import math
import re

import pandas as pd

from src.journal import (
    OPERATION_COLUMNS, PORTFOLIO_SNAPSHOT_COLUMNS,
    normalize_operation, normalize_portfolio_snapshot_position,
)


IMPORT_BATCH_COLUMNS = ["id", "owner", "source_sha256", "snapshot_date", "metadata",
                        "backup", "summary", "recorded_by", "created_at"]
OPERATION_INPUT_FIELDS = ["ticker", "side", "quantity", "price", "fees", "executed_at",
                          "notes", "currency", "account_name", "settlement_amount_eur",
                          "fee_eur", "fx_rate_to_eur", "settlement_amount_local", "source_event_id"]
SNAPSHOT_INPUT_FIELDS = [column for column in PORTFOLIO_SNAPSHOT_COLUMNS
                         if column not in {"id", "recorded_by", "created_at", "updated_at"}]
IMPORT_MARKER = "[reconciled-workbook:v1]"


def json_safe(value: object) -> object:
    if isinstance(value, pd.DataFrame):
        return json_safe(value.to_dict("records"))
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return json_safe(value.item())
    return value


def _private_metadata(value: object) -> object:
    """Keep controlled financial provenance, not filesystem/contact identifiers."""
    value = json_safe(value)
    if isinstance(value, dict):
        return {key: _private_metadata(item) for key, item in value.items()
                if not any(term in key.casefold() for term in ("path", "filename", "email", "phone", "iban", "tax_id", "account_number"))}
    if isinstance(value, list):
        return [_private_metadata(item) for item in value]
    if isinstance(value, str) and re.search(r"(?:/Users/|/home/|[A-Za-z]:\\)", value):
        return "[ruta de origen omitida]"
    return value


def _json(value: object) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalize_reconciled_bundle(bundle: Mapping[str, object]) -> dict[str, object]:
    """Validate before touching storage; timestamps never establish identities."""
    digest = str(bundle.get("source_sha256", "")).lower().strip()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("La importación necesita una huella SHA-256 válida.")
    timestamp = pd.to_datetime(bundle.get("snapshot_date"), errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("La fecha de la fotografía no es válida.")
    target = timestamp.date().isoformat()
    source_positions = bundle.get("positions")
    source_operations = bundle.get("operations", [])
    if not isinstance(source_positions, list) or not source_positions:
        raise ValueError("La importación necesita posiciones documentadas.")
    if not isinstance(source_operations, list):
        raise ValueError("Las operaciones deben ser una lista.")
    positions, operations, position_keys, operation_keys = [], [], set(), set()
    for row in source_positions:
        if not isinstance(row, Mapping):
            raise ValueError("La fotografía contiene una fila inválida.")
        item = normalize_portfolio_snapshot_position(**{key: json_safe(row[key]) for key in SNAPSHOT_INPUT_FIELDS if key in row})
        if not math.isfinite(item["value_eur"]):
            raise ValueError("La valoración de la fotografía debe ser finita.")
        item["source"] = _private_metadata(item["source"])
        if item["snapshot_date"] != target:
            raise ValueError("Las posiciones no pertenecen a la fecha declarada.")
        identity = (item["snapshot_date"], item["platform"], item["asset_name"])
        if identity in position_keys:
            raise ValueError("La fotografía contiene una posición duplicada.")
        position_keys.add(identity)
        positions.append(item)
    for row in source_operations:
        if not isinstance(row, Mapping):
            raise ValueError("El diario contiene una fila inválida.")
        values = {key: json_safe(row[key]) for key in OPERATION_INPUT_FIELDS if key in row}
        item = normalize_operation(**values)
        source_id = item["source_event_id"]
        if not source_id or source_id in operation_keys:
            raise ValueError("Falta una identidad de origen única en las operaciones.")
        operation_keys.add(source_id)
        if not item["account_name"]:
            raise ValueError("Cada operación importada necesita su cuenta de bróker.")
        metadata = {}
        notes = str(row.get("notes", ""))
        if notes.startswith(IMPORT_MARKER):
            try:
                metadata = json.loads(notes.removeprefix(IMPORT_MARKER))
            except (TypeError, ValueError):
                raise ValueError("La trazabilidad de la operación no es válida.") from None
        if not isinstance(metadata, dict):
            raise ValueError("La trazabilidad de la operación no es válida.")
        for key in ("phase", "fee_quality", "fx_quality", "source_sheet", "source_row"):
            if key in row:
                metadata[key] = json_safe(row[key])
        metadata["source_event_id"] = source_id
        item["notes"] = IMPORT_MARKER + _json(_private_metadata(metadata))
        # The numeric fee field is legacy compatibility, never proof of zero.
        if metadata.get("fee_quality") == "unknown":
            item["fee_eur"] = None
        operations.append(item)
    metadata = bundle.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("La metadata de la importación debe ser un objeto.")
    result = {"source_sha256": digest, "snapshot_date": target, "positions": positions,
              "operations": operations, "metadata": _private_metadata(metadata)}
    _json(result)  # Reject unsupported/non-finite values before a transaction.
    return result


def prepare_reconciled_workbook_bundle(plan: object, existing_operations: pd.DataFrame | None = None) -> dict[str, object]:
    """Turn the read-only parser plan into a backend-independent immutable bundle."""
    from src.reconciled_workbook_import import plan_reconciled_operation_reimport

    preview = plan_reconciled_operation_reimport(plan.operations, existing_operations)
    provenance = []
    for row in plan.operations.to_dict("records"):
        record = {key: json_safe(row[key]) for key in (
            "source_event_id", "phase", "after_snapshot", "raw_identifier", "identity_identifier",
            "fee_quality", "fx_quality", "cost_quality", "residual_cost_local", "reference_fx_rate",
            "source_sheet", "source_row") if key in row}
        try:
            record["sources"] = json.loads(str(row.get("provenance_json", "[]")))
        except (TypeError, ValueError):
            raise ValueError("La procedencia de una operación no es válida.") from None
        provenance.append(record)
    bundle = {
        "source_sha256": plan.source_sha256, "snapshot_date": plan.snapshot_date,
        "positions": plan.snapshot.positions.to_dict("records"),
        "operations": plan.operations.to_dict("records"),
        "metadata": {"events": plan.events, "issues": plan.issues,
                     "coverage": plan.coverage, "inventory": plan.inventory,
                     "checks": getattr(plan, "checks", pd.DataFrame()),
                     "fifo_sales": getattr(plan, "fifo_sales", pd.DataFrame()),
                     "sales_references": getattr(plan, "sales_references", pd.DataFrame()),
                     "position_references": getattr(plan, "position_references", pd.DataFrame()),
                     "operation_preview": preview,
                     "provenance": {"source_sha256": plan.source_sha256,
                                    "snapshot_date": plan.snapshot_date,
                                    "schema": "reconciled-workbook:v1", "operations": provenance}},
    }
    return normalize_reconciled_bundle(bundle)


def classify_reconciled_operations(operations: list[dict[str, object]], existing: pd.DataFrame) -> pd.DataFrame:
    """Recheck under the storage transaction, including ambiguous corrections."""
    from src.reconciled_workbook_import import plan_reconciled_operation_reimport

    result = plan_reconciled_operation_reimport(pd.DataFrame(operations), existing)
    existing_by_id = {}
    for saved in existing.to_dict("records"):
        source_id = saved.get("source_event_id")
        if pd.isna(source_id) or not source_id:
            notes = str(saved.get("notes", ""))
            if notes.startswith(IMPORT_MARKER):
                try:
                    source_id = json.loads(notes.removeprefix(IMPORT_MARKER)).get("source_event_id")
                except (TypeError, ValueError, AttributeError):
                    source_id = None
        if source_id:
            existing_by_id[str(source_id)] = saved
    for index, item in enumerate(operations):
        saved = existing_by_id.get(str(item["source_event_id"]))
        if saved is None:
            continue
        changed = any(str(saved.get(field, "")).strip().upper() != str(item.get(field, "")).strip().upper()
                      for field in ("ticker", "account_name", "side", "currency"))
        changed |= any(not math.isclose(float(saved.get(field, 0)), float(item[field]), abs_tol=1e-8)
                       for field in ("quantity", "price"))
        changed |= str(saved.get("executed_at", ""))[:10] != str(item["executed_at"])[:10]
        if changed:
            result.loc[index, ["action", "reason", "existing_id"]] = ["pending", "source_event_identity_conflict", saved.get("id")]
    # Same-day same-size corrections without a source ID cannot safely become a
    # second trade merely because the execution price was changed manually.
    for index, item in enumerate(operations):
        if result.iloc[index]["action"] != "insert":
            continue
        for saved in existing.to_dict("records"):
            fields = ("ticker", "side", "currency", "account_name")
            if all(str(saved.get(key, "")).strip().upper() == str(item.get(key, "")).strip().upper() for key in fields) and str(saved.get("executed_at", ""))[:10] == str(item["executed_at"])[:10] and math.isclose(float(saved.get("quantity", 0)), float(item["quantity"]), abs_tol=1e-8):
                result.loc[index, ["action", "reason", "existing_id"]] = ["pending", "ambiguous_same_day_correction", saved.get("id")]
                break
    return result


def store_reconciled_bundle_sqlite(journal: object, bundle: Mapping[str, object], *, recorded_by: str = "") -> dict[str, object]:
    """Back up and add a complete batch atomically, preserving all manual rows."""
    data = normalize_reconciled_bundle(bundle)
    actor = recorded_by.strip().lower()
    with journal._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute("SELECT id, summary FROM portfolio_import_batches WHERE owner=? AND source_sha256=?", (journal.owner, data["source_sha256"])).fetchone()
        if prior is not None:
            return {**json.loads(prior["summary"]), "batch_id": int(prior["id"]), "already_imported": True}
        backup = {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
                  for table in ("operations", "portfolio_snapshots", "portfolio_accounts", "private_investments")}
        metadata = dict(data["metadata"])
        cursor = connection.execute("INSERT INTO portfolio_import_batches(owner,source_sha256,snapshot_date,metadata,backup,summary,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)", (journal.owner, data["source_sha256"], data["snapshot_date"], _json(metadata), _json(backup), "{}", actor, datetime.now().isoformat(timespec="seconds")))
        batch_id = int(cursor.lastrowid)
        summary = {"batch_id": batch_id, "source_sha256": data["source_sha256"],
                   "inserted_positions": 0, "inserted_operations": 0,
                   "skipped_positions": 0, "skipped_operations": 0,
                   "pending_conflicts": 0, "already_imported": False}
        pending = list(metadata.get("pending_conflicts", []))
        existing_ops = pd.DataFrame(backup["operations"], columns=OPERATION_COLUMNS)
        decisions = classify_reconciled_operations(data["operations"], existing_ops)
        operation_columns = [column for column in OPERATION_COLUMNS if column != "id"]
        for item, decision in zip(data["operations"], decisions.to_dict("records")):
            if decision["action"] == "pending":
                pending.append({"kind": "operation", **decision})
                summary["pending_conflicts"] += 1
            elif decision["action"] == "skip":
                summary["skipped_operations"] += 1
            else:
                row = {**item, "recorded_by": actor}
                connection.execute(f"INSERT INTO operations({','.join(operation_columns)}) VALUES({','.join('?' for _ in operation_columns)})", tuple(row[column] for column in operation_columns))
                summary["inserted_operations"] += 1
        position_columns = [column for column in PORTFOLIO_SNAPSHOT_COLUMNS if column != "id"]
        comparison_columns = [column for column in SNAPSHOT_INPUT_FIELDS if column not in {"comments", "source", "notes"}]
        for item in data["positions"]:
            existing = connection.execute("SELECT * FROM portfolio_snapshots WHERE snapshot_date=? AND platform=? AND asset_name=?", (item["snapshot_date"], item["platform"], item["asset_name"])).fetchone()
            if existing is not None:
                equal = all(_json(existing[column]) == _json(item[column]) for column in comparison_columns)
                if equal:
                    summary["skipped_positions"] += 1
                else:
                    pending.append({"kind": "snapshot", "reason": "existing_snapshot_conflict", "existing_id": existing["id"], "platform": item["platform"], "asset_name": item["asset_name"]})
                    summary["pending_conflicts"] += 1
                continue
            row = {**item, "recorded_by": actor}
            connection.execute(f"INSERT INTO portfolio_snapshots({','.join(position_columns)}) VALUES({','.join('?' for _ in position_columns)})", tuple(row[column] for column in position_columns))
            summary["inserted_positions"] += 1
        metadata["pending_conflicts"] = pending
        metadata["operation_preview"] = decisions.to_dict("records")
        connection.execute("UPDATE portfolio_import_batches SET metadata=?,summary=? WHERE id=? AND owner=?", (_json(metadata), _json(summary), batch_id, journal.owner))
        return summary
