"""Reuse owner-scoped home reconciliation without changing accounting rules."""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from datetime import date
import hashlib
import json

import pandas as pd

from src.view_cache import copy_view_value, get_or_build_view


HOME_PORTFOLIO_VIEW_VERSION = "home-reconciliation-v1"


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    """Fingerprint the small account tables, never the whole OHLC universe."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("Home reconciliation inputs must be DataFrames")
    options = dict(date_format="iso", date_unit="ns", double_precision=15)
    data = frame.to_json(orient="split", **options)
    attributes = pd.Series(frame.attrs, dtype=object).to_json(**options)
    schema = json.dumps(dict(dtypes=[str(dtype) for dtype in frame.dtypes],
        index_type=type(frame.index).__name__, index_dtype=str(frame.index.dtype),
        index_names=list(frame.index.names), column_names=list(frame.columns.names)),
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(json.dumps([data, attributes, schema],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def reconcile_home_views(state: MutableMapping[str, object], *, owner: str,
        latest_snapshot: pd.DataFrame, market_snapshot: pd.DataFrame,
        operations: pd.DataFrame, positions_dashboard: pd.DataFrame,
        reconcile: Callable[[pd.DataFrame, pd.DataFrame, pd.DataFrame], pd.DataFrame],
        ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return the same two reconciliations, cached only within this owner/session.

    Source content, prices/FX already reflected in the market/dashboard tables,
    mutation revision and local day all invalidate the view. The day matters
    because the existing reconciler uses today's date for a missing baseline.
    No signal, financial rule, timestamp parsing or stored record is changed.
    """
    if not callable(reconcile):
        raise TypeError("A read-only reconciliation function is required")
    dependencies = dict(
        latest_snapshot=_frame_fingerprint(latest_snapshot),
        market_snapshot=_frame_fingerprint(market_snapshot),
        operations=_frame_fingerprint(operations),
        positions_dashboard=_frame_fingerprint(positions_dashboard),
        journal_revision=state.get("_view_data_revision", 0),
        day=date.today().isoformat(),
        calculation_identity=id(reconcile),
    )

    def build():
        # Keep arbitrary read-only callback implementations from retaining a
        # writable alias to source or cached frames. The production callback's
        # ordinary copy/parse/calculation semantics are otherwise unchanged.
        latest = reconcile(copy_view_value(latest_snapshot),
            copy_view_value(operations), copy_view_value(positions_dashboard))
        market = reconcile(copy_view_value(market_snapshot),
            copy_view_value(operations), copy_view_value(positions_dashboard))
        return latest, market

    return get_or_build_view(state, owner=owner, view="home_reconciliation",
        dependencies=dependencies, builder=build,
        version=HOME_PORTFOLIO_VIEW_VERSION, max_entries=4).value
