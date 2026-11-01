"""Reconciliation memoization preserves the real accounting result and owners."""
from copy import deepcopy
from datetime import date
from unittest.mock import Mock

import pandas as pd
import pytest

import src.portfolio_view_context as context
from src.portfolio_snapshot import reconcile_current_portfolio
from src.view_cache import invalidate_view_cache


@pytest.fixture
def inputs():
    latest = pd.DataFrame([dict(snapshot_date="2026-10-19", platform="Broker", asset_name="Synthetic",
        analysis_ticker="AAA", asset_type="Acción", quantity=1., currency="USD", value_eur=90.,
        cost_estimate_eur=80., gain_loss_eur=10., comments="", source="Synthetic", notes="")])
    market = latest.copy(deep=True)
    market.loc[0, "value_eur"] = 110.
    market["market_as_of"] = "2026-10-23"
    operations = pd.DataFrame([dict(id=1, ticker="AAA", account_name="Broker", side="Compra",
        quantity=2., price=80., fees=1., currency="USD", executed_at="2026-10-20T14:30:00Z",
        created_at="2026-10-20T14:31:00Z", notes="")])
    dashboard = pd.DataFrame([dict(ticker="AAA", account_name="Broker", currency="USD", quantity=3.,
        latest_price=121., gross_value_eur=330., cost_basis_eur=240.,
        net_pnl_eur=89., net_return_pct=37.083, average_cost=80., cost_basis=240.,
        cost_basis_source="Synthetic")])
    return dict(latest_snapshot=latest, market_snapshot=market, operations=operations,
                positions_dashboard=dashboard)


def call(state, inputs, *, owner="alice", reconcile=reconcile_current_portfolio):
    return context.reconcile_home_views(state, owner=owner, reconcile=reconcile, **inputs)


def test_real_reconciler_outputs_equal_uncached_results_and_sources_are_unchanged(inputs):
    originals = deepcopy(inputs)
    expected = tuple(reconcile_current_portfolio(inputs[name], inputs["operations"], inputs["positions_dashboard"])
                     for name in ("latest_snapshot", "market_snapshot"))
    state = {}
    actual = call(state, inputs)
    repeated = call(state, inputs)
    for first, second, reference in zip(actual, repeated, expected):
        pd.testing.assert_frame_equal(first, reference)
        pd.testing.assert_frame_equal(second, reference)
    for name, original in originals.items():
        pd.testing.assert_frame_equal(inputs[name], original)


def test_identical_inputs_reconcile_twice_once_not_on_every_render(inputs):
    state, reconcile = {}, Mock(side_effect=reconcile_current_portfolio)
    call(state, inputs, reconcile=reconcile)
    call(state, inputs, reconcile=reconcile)
    assert reconcile.call_count == 2


def test_returned_mutable_frames_are_not_aliases_to_inputs_or_stored_views(inputs):
    state, reconcile = {}, Mock(side_effect=lambda snapshot, *unused: snapshot)
    inputs["latest_snapshot"]["mutable"] = [["original"]]
    inputs["latest_snapshot"].attrs["source"] = {"version": 1}
    first = call(state, inputs, reconcile=reconcile)
    first[0].loc[0, "value_eur"] = -1.
    first[0].loc[0, "mutable"].append("mutated")
    first[0].attrs["source"]["version"] = 2
    repeated = call(state, inputs, reconcile=reconcile)
    assert repeated[0].loc[0, "value_eur"] == inputs["latest_snapshot"].loc[0, "value_eur"] == 90.
    assert repeated[0].loc[0, "mutable"] == inputs["latest_snapshot"].loc[0, "mutable"] == ["original"]
    assert repeated[0].attrs["source"] == {"version": 1}
    assert reconcile.call_count == 2


@pytest.mark.parametrize("name,column,value", [
    ("latest_snapshot", "quantity", 4.), ("market_snapshot", "value_eur", 111.),
    ("operations", "quantity", 3.), ("positions_dashboard", "gross_value_eur", 331.),
])
def test_in_place_source_changes_invalidate_even_when_frame_identity_is_unchanged(inputs, name, column, value):
    state, reconcile = {}, Mock(side_effect=reconcile_current_portfolio)
    call(state, inputs, reconcile=reconcile)
    inputs[name].loc[0, column] = value
    call(state, inputs, reconcile=reconcile)
    assert reconcile.call_count == 4


def test_frame_attributes_and_schema_are_part_of_the_cache_identity(inputs):
    state, reconcile = {}, Mock(side_effect=lambda snapshot, *unused: snapshot)
    call(state, inputs, reconcile=reconcile)
    inputs["latest_snapshot"].attrs["source"] = "changed"
    assert call(state, inputs, reconcile=reconcile)[0].attrs["source"] == "changed"
    inputs["latest_snapshot"]["quantity"] = inputs["latest_snapshot"].quantity.astype(object)
    call(state, inputs, reconcile=reconcile)
    assert reconcile.call_count == 6


def test_owner_boundaries_never_reuse_another_account_and_switch_back_rebuilds(inputs):
    state, reconcile = {}, Mock(side_effect=reconcile_current_portfolio)
    for owner in ("alice", "bob", "alice"):
        call(state, inputs, owner=owner, reconcile=reconcile)
    assert reconcile.call_count == 6


def test_explicit_mutation_revision_and_invalidation_rebuild_without_any_storage_write(inputs):
    state, reconcile = {}, Mock(side_effect=reconcile_current_portfolio)
    call(state, inputs, reconcile=reconcile)
    state["_view_data_revision"] = 1
    call(state, inputs, reconcile=reconcile)
    assert invalidate_view_cache(state, owner="alice", view="home_reconciliation") == 2
    call(state, inputs, reconcile=reconcile)
    assert reconcile.call_count == 6


def test_day_and_explicit_view_version_invalidate_existing_fallback_dates(inputs, monkeypatch):
    state, reconcile = {}, Mock(side_effect=reconcile_current_portfolio)
    class NextDay(date):
        @classmethod
        def today(cls): return date(2099, 1, 2)
    call(state, inputs, reconcile=reconcile)
    monkeypatch.setattr(context, "date", NextDay)
    call(state, inputs, reconcile=reconcile)
    monkeypatch.setattr(context, "HOME_PORTFOLIO_VIEW_VERSION", "new-view-version")
    call(state, inputs, reconcile=reconcile)
    assert reconcile.call_count == 6


def test_reconciliation_failure_is_not_cached_as_partial_success(inputs):
    state, reconcile = {}, Mock(side_effect=[pd.DataFrame(), ValueError("synthetic failure")])
    with pytest.raises(ValueError, match="synthetic failure"):
        call(state, inputs, reconcile=reconcile)
    reconcile.side_effect = reconcile_current_portfolio
    latest, market = call(state, inputs, reconcile=reconcile)
    assert not latest.empty and not market.empty and reconcile.call_count == 4
