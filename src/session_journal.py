"""Bounded, short-lived journal reads in one authenticated UI session.

Wrap the process-cached backend, never cache this proxy with ``cache_resource``.
The caller owns authentication and purges session state on an account change;
private and group journals can coexist in the same session without sharing reads.
Only the exact read methods below are cached. Writes are never deferred and
invalidate both journal reads and derived views, including ambiguous failures.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, MutableMapping
from datetime import date, datetime
from functools import wraps
import hashlib
import inspect
import json
import math
import time

from src.view_cache import copy_view_value, invalidate_view_cache


SESSION_JOURNAL_CACHE_VERSION = "session-journal-reads-v1"
_STATE_KEY = "_ssl_session_journal_reads"
_MAX_ENTRIES = 32
_READ_METHODS = frozenset({
    "list_operations", "open_positions", "portfolio_summary",
    "list_portfolio_imports", "list_private_investments",
    "list_portfolio_accounts", "list_portfolio_snapshot_positions",
    "list_favorites", "list_analysis_snapshots", "get_daily_score_report",
    "get_latest_daily_score_report",
    "list_analysis_snapshots_for_sessions",
    "get_alert_preferences", "list_alert_states", "list_paper_simulations",
    "list_paper_daily_runs",
})


def _owner(journal: object) -> str:
    value = getattr(journal, "owner", "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A journal owner is required for session-scoped reads")
    return value.strip().lower()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _argument(value: object, depth: int = 0) -> object:
    if depth > 12:
        raise ValueError("Journal read arguments are nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (date, datetime)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [_argument(item, depth + 1) for item in value]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return {key: _argument(item, depth + 1) for key, item in value.items()}
    raise TypeError("Unsupported journal read argument; this read is not cached")


class SessionJournal:
    """An owner-bound proxy; session keys contain hashes, not input credentials."""

    def __init__(self, journal: object, state: MutableMapping[str, object], *,
                 ttl_seconds: float = 60) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise ValueError("ttl_seconds must be a positive finite number")
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive finite number")
        self._journal = journal
        self._state = state
        self._owner = _owner(journal)
        self._owner_hash = _hash(self._owner)
        # Distinct connections with an identical owner must not reuse each
        # other's results. No endpoint, path or backend credential is a key.
        self._backend_hash = _hash(
            f"{type(journal).__module__}.{type(journal).__qualname__}:{id(journal)}"
        )
        self._ttl_seconds = float(ttl_seconds)

    @property
    def owner(self) -> str:
        self._check_owner()
        return self._owner

    def _entries(self) -> OrderedDict:
        bucket = self._state.get(_STATE_KEY)
        if (not isinstance(bucket, dict)
                or bucket.get("version") != SESSION_JOURNAL_CACHE_VERSION
                or not isinstance(bucket.get("entries"), OrderedDict)):
            bucket = {"version": SESSION_JOURNAL_CACHE_VERSION, "entries": OrderedDict()}
            self._state[_STATE_KEY] = bucket
        return bucket["entries"]

    def _invalidate(self) -> None:
        entries = self._entries()
        for key in [key for key, entry in entries.items()
                    if entry.get("owner_hash") == self._owner_hash]:
            del entries[key]
        invalidate_view_cache(self._state, owner=self._owner)
        revision = self._state.get("_view_data_revision", 0)
        self._state["_view_data_revision"] = (
            revision + 1 if isinstance(revision, int) and not isinstance(revision, bool) else 1
        )

    def _check_owner(self) -> None:
        try:
            same_owner = _owner(self._journal) == self._owner
        except ValueError:
            same_owner = False
        if not same_owner:
            self._invalidate()
            raise RuntimeError("The journal owner changed; bind a new session journal")

    def _read_key(self, method: str, function: object, args: tuple,
                  kwargs: dict) -> str | None:
        try:
            bound = inspect.signature(function).bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = _argument(dict(bound.arguments))
            payload = json.dumps(
                [SESSION_JOURNAL_CACHE_VERSION, self._owner_hash, self._backend_hash,
                 self._ttl_seconds, method, arguments],
                sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            )
            if len(payload.encode("utf-8")) > 32_768:
                return None
        except (TypeError, ValueError):
            return None
        return _hash(payload)

    def __getattr__(self, name: str):
        self._check_owner()
        attribute = getattr(self._journal, name)
        if not callable(attribute) or name.startswith("_"):
            return attribute

        if name in _READ_METHODS:
            @wraps(attribute)
            def read(*args, **kwargs):
                self._check_owner()
                key = self._read_key(name, attribute, args, kwargs)
                if key is None:
                    return copy_view_value(attribute(*args, **kwargs))
                now = time.monotonic()
                entries = self._entries()
                for expired in [identity for identity, entry in entries.items()
                                if entry["expires_at"] <= now]:
                    del entries[expired]
                entry = entries.get(key)
                if entry is not None:
                    entries.move_to_end(key)
                    return copy_view_value(entry["value"])
                value = copy_view_value(attribute(*args, **kwargs))
                self._check_owner()
                entries[key] = {"owner_hash": self._owner_hash,
                                "expires_at": time.monotonic() + self._ttl_seconds,
                                "value": value}
                while len(entries) > _MAX_ENTRIES:
                    entries.popitem(last=False)
                return copy_view_value(value)

            return read

        @wraps(attribute)
        def uncached(*args, **kwargs):
            self._check_owner()
            try:
                return attribute(*args, **kwargs)
            finally:
                # A failed write may still have committed remotely. Never reuse
                # the previous local view as evidence that nothing changed.
                self._invalidate()

        return uncached


def session_journal(journal: object, state: MutableMapping[str, object], *,
                    ttl_seconds: float = 60) -> SessionJournal:
    """Bind a backend to this session; owners have independent cache entries."""
    return SessionJournal(journal, state, ttl_seconds=ttl_seconds)
