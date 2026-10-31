"""Small, owner-scoped cache for read-only calculations in one UI session.

This module does not import Streamlit, fetch data, write holdings or use a
process-global cache. Pass ``st.session_state`` (or any mutable mapping), an
already known revision key, and a builder that only calculates a view. Do not
use builders with writes or notifications: navigation and cache misses are not
authorization to perform those actions.

Dependencies are deliberately small JSON-compatible revision/fingerprint
values. DataFrames and provider objects are rejected as keys; callers need not
rehash hundreds of price frames whenever a user changes pages. Cache entries
hold independent copies, and each returned value is independent of that entry.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Generic, TypeVar

import pandas as pd


VIEW_CACHE_VERSION = "owner-session-view-v1"
_STATE_KEY = "_ssl_read_only_view_cache"
_MAX_DEPENDENCY_BYTES = 32_768
T = TypeVar("T")


def copy_view_value(value, memo=None):
    """Also isolate mutable Python cells, which pandas deep copies preserve."""
    memo = {} if memo is None else memo
    if id(value) in memo:
        return memo[id(value)]
    if isinstance(value, pd.DataFrame):
        result = value.copy(deep=True)
        memo[id(value)] = result
        for position, dtype in enumerate(value.dtypes):
            if dtype == object:
                # Do not infer float/datetime: None and large integer IDs must
                # survive a read-only copy with their original object dtype.
                cells = [copy_view_value(cell, memo) for cell in value.iloc[:, position]]
                result.isetitem(position, pd.Series(cells, index=value.index, dtype=object))
        result.attrs = copy_view_value(value.attrs, memo)
        return result
    if isinstance(value, dict):
        result = {}
        memo[id(value)] = result
        result.update((key, copy_view_value(item, memo)) for key, item in value.items())
        return result
    if isinstance(value, list):
        result = []
        memo[id(value)] = result
        result.extend(copy_view_value(item, memo) for item in value)
        return result
    if isinstance(value, tuple):
        result = tuple(copy_view_value(item, memo) for item in value)
        memo[id(value)] = result
        return result
    return deepcopy(value, memo)


@dataclass(frozen=True)
class ViewCacheResult(Generic[T]):
    value: T
    cache_hit: bool
    cache_key: str


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"A nonempty {name} of at most 256 characters is required")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"Invalid {name}")
    return value.strip()


def _owner_hash(owner: object) -> str:
    return hashlib.sha256(_label(owner, "owner").encode("utf-8")).hexdigest()


def _dependency(value: object, depth: int = 0) -> object:
    if depth > 16:
        raise ValueError("View dependencies are nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("View dependencies must be finite")
        return value
    if isinstance(value, (tuple, list)):
        return [_dependency(item, depth + 1) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("View dependency mapping keys must be strings")
        return {key: _dependency(item, depth + 1) for key, item in value.items()}
    raise TypeError("Use a small JSON revision key, not a DataFrame or provider object")


def view_cache_key(*, owner: str, view: str, dependencies: object, version: str = "v1") -> str:
    """Pure stable identity; no arbitrary-object repr, pickle or frame hashing."""
    payload = dict(cache_version=VIEW_CACHE_VERSION, owner=_owner_hash(owner),
                   view=_label(view, "view"), version=_label(version, "view version"),
                   dependencies=_dependency(dependencies))
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_DEPENDENCY_BYTES:
        raise ValueError("Use a small revision key instead of embedding the source data")
    return hashlib.sha256(encoded).hexdigest()


def _bucket(state: MutableMapping[str, object], owner_hash: str) -> dict:
    existing = state.get(_STATE_KEY)
    if (not isinstance(existing, dict) or existing.get("version") != VIEW_CACHE_VERSION
            or existing.get("owner_hash") != owner_hash
            or not isinstance(existing.get("entries"), OrderedDict)):
        # An account change or incompatible hot-reload drops the previous
        # owner's values rather than leaving them accessible in this session.
        existing = dict(version=VIEW_CACHE_VERSION, owner_hash=owner_hash, entries=OrderedDict())
        state[_STATE_KEY] = existing
    return existing


def get_or_build_view(state: MutableMapping[str, object], *, owner: str, view: str,
                      dependencies: object, builder: Callable[[], T], version: str = "v1",
                      max_entries: int = 3) -> ViewCacheResult[T]:
    """Memoize a read-only calculation with bounded LRU storage in this session.

    Explicitly invalidate after relevant mutations, or change ``dependencies``
    when data/configuration revisions change. A new view version always misses.
    Exceptions are never cached. Mutating a returned DataFrame/container cannot
    mutate the stored entry; this is view reuse, not shared writable state.
    """
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
        raise ValueError("max_entries must be a positive integer")
    if not callable(builder):
        raise TypeError("A read-only view builder is required")
    identity = view_cache_key(owner=owner, view=view, dependencies=dependencies, version=version)
    entries = _bucket(state, _owner_hash(owner))["entries"]
    if identity in entries:
        value = copy_view_value(entries[identity]["value"])
        entries.move_to_end(identity)
        hit = True
    else:
        value = builder()
        # Copy before committing or evicting anything: a failed calculation or
        # uncopyable result cannot replace a previously valid cached value.
        stored = copy_view_value(value)
        entries[identity] = dict(view=_label(view, "view"), value=stored)
        hit = False
    while len(entries) > max_entries:
        entries.popitem(last=False)
    return ViewCacheResult(value=value, cache_hit=hit, cache_key=identity)


def invalidate_view_cache(state: MutableMapping[str, object], *, owner: str | None = None,
                          view: str | None = None) -> int:
    """Remove views explicitly after mutations; no other session keys change.

    Without owner/view, clears this session's full cache (for example on
    logout). An explicit nonmatching owner cannot invalidate another owner's
    views. Returns the number of removed entries, never cached account data.
    """
    owner_hash = _owner_hash(owner) if owner is not None else None
    requested_view = _label(view, "view") if view is not None else None
    existing = state.get(_STATE_KEY)
    if not isinstance(existing, dict):
        return 0
    if owner_hash is not None and existing.get("owner_hash") != owner_hash:
        return 0
    entries = existing.get("entries")
    if not isinstance(entries, OrderedDict):
        state.pop(_STATE_KEY, None)
        return 0
    if requested_view is None:
        count = len(entries)
        state.pop(_STATE_KEY, None)
        return count
    removed = [key for key, entry in entries.items() if entry.get("view") == requested_view]
    for key in removed:
        del entries[key]
    return len(removed)
