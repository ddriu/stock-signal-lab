"""Owner-bound consent contract; no storage, providers, orders or mail."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re

ARENA_UNIVERSE_POLICY_VERSION = "arena-universe-policy-v1"
_OWNER = re.compile(r"[A-Za-z0-9_@.+:\-]{1,128}\Z")
_RESERVED = frozenset({"anon", "anonymous", "public", "group", "grupo_compartido"})
_FIELDS = frozenset({"owner", "mode", "approved_at", "version", "policy_id"})


def validate_arena_policy_owner(owner: object) -> str:
    if (not isinstance(owner, str) or not _OWNER.fullmatch(owner)
            or owner.lower() in _RESERVED):
        raise ValueError("La política de Arena requiere un propietario privado explícito.")
    return owner


def _timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (str, datetime)):
        raise ValueError("La aprobación de Arena requiere una fecha con zona horaria.")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("Missing timezone")
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("La aprobación de Arena requiere una fecha con zona horaria.") from exc


def build_arena_universe_policy(
    owner: str, mode: str = "strict", *, approved_at: object | None = None,
    reference_time: object | None = None,
) -> dict[str, object]:
    """Produce stable, canonical consent metadata, never authorize a write."""
    owner = validate_arena_policy_owner(owner)
    if not isinstance(mode, str) or mode not in {"strict", "verified_only"}:
        raise ValueError("El modo del universo de Arena no es válido.")
    clock = datetime.now(timezone.utc) if reference_time is None else _timestamp(reference_time)
    approval = _timestamp(approved_at) if approved_at is not None else None
    if mode == "verified_only" and approval is None:
        raise ValueError("El modo sólo verificadas necesita una aprobación explícita fechada.")
    if approval is not None and approval > clock:
        raise ValueError("La aprobación del universo de Arena no puede estar en el futuro.")
    payload = {"owner": owner, "mode": mode,
        "approved_at": approval.isoformat(timespec="microseconds") if approval is not None else None,
        "version": ARENA_UNIVERSE_POLICY_VERSION}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {**payload, "policy_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def normalize_arena_universe_policy(
    policy: Mapping[str, object] | None, *, owner: str | None = None,
    reference_time: object | None = None,
) -> dict[str, object]:
    """Missing means strict; illegible, cross-owner or tampered never does."""
    if policy is None:
        return build_arena_universe_policy(owner, reference_time=reference_time)
    if not isinstance(policy, Mapping) or set(policy) != _FIELDS:
        raise ValueError("La política guardada de Arena no tiene un formato verificable.")
    actual_owner = validate_arena_policy_owner(policy.get("owner"))
    if owner is not None and actual_owner != validate_arena_policy_owner(owner):
        raise ValueError("La política de Arena no corresponde al propietario solicitado.")
    if policy.get("version") != ARENA_UNIVERSE_POLICY_VERSION:
        raise ValueError("La versión de la política de Arena no es compatible.")
    expected = build_arena_universe_policy(actual_owner, policy.get("mode"),
        approved_at=policy.get("approved_at"), reference_time=reference_time)
    if policy.get("policy_id") != expected["policy_id"]:
        raise ValueError("La identidad de la política de Arena no coincide con su contenido.")
    return expected


def prepare_arena_universe_policy_change(
    owner: str, mode: str, *, owner_consent: bool = False,
    approved_at: object | None = None, reference_time: object | None = None,
) -> dict[str, object]:
    """A backend setter must receive explicit consent, never infer it from data."""
    if mode == "verified_only" and owner_consent is not True:
        raise ValueError("El modo sólo verificadas requiere el consentimiento explícito del propietario.")
    clock = datetime.now(timezone.utc) if reference_time is None else _timestamp(reference_time)
    return build_arena_universe_policy(owner, mode,
        approved_at=clock if approved_at is None else approved_at, reference_time=clock)
