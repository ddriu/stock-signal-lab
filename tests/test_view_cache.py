"""Offline session-view reuse never crosses owners or performs I/O."""
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pandas as pd
import pytest

from src.view_cache import get_or_build_view, invalidate_view_cache, view_cache_key


def get(state, *, owner="owner-a", view="analysis", dependencies="revision-1", builder=None, **kwargs):
    return get_or_build_view(state, owner=owner, view=view, dependencies=dependencies,
        builder=builder or (lambda: {"score": 90}), **kwargs)


def test_same_owner_view_revision_builds_once_and_wrapper_is_frozen():
    state, builder = {}, Mock(return_value={"score": 90})
    first = get(state, builder=builder)
    second = get(state, builder=builder)
    assert not first.cache_hit and second.cache_hit and first.cache_key == second.cache_key
    assert first.value == second.value == {"score": 90}
    builder.assert_called_once_with()
    with pytest.raises(FrozenInstanceError):
        second.cache_hit = False


def test_mutating_returned_frames_attrs_and_containers_never_mutates_cached_view():
    source = pd.DataFrame({"score": [90]})
    source.attrs["provenance"] = {"revision": "v1"}
    state, builder = {}, Mock(return_value={"frame": source, "notes": ["original"]})
    first = get(state, builder=builder)
    first.value["frame"].loc[0, "score"] = 0
    first.value["frame"].attrs["provenance"]["revision"] = "changed"
    first.value["notes"].append("not cached")
    second = get(state, builder=builder)
    assert second.value["frame"].loc[0, "score"] == 90
    assert second.value["frame"].attrs["provenance"] == {"revision": "v1"}
    assert second.value["notes"] == ["original"]
    second.value["notes"].clear()
    assert get(state, builder=builder).value["notes"] == ["original"]
    builder.assert_called_once_with()


@pytest.mark.parametrize("different", [dict(owner="owner-b"), dict(view="portfolio"),
    dict(dependencies="revision-2"), dict(version="v2")])
def test_key_includes_owner_view_data_and_version(different):
    state, builder = {}, Mock(return_value=[1])
    first = get(state, builder=builder)
    second = get(state, builder=builder, **different)
    assert not second.cache_hit and first.cache_key != second.cache_key
    assert builder.call_count == 2


def test_account_switch_purges_old_values_and_switching_back_rebuilds():
    state, builder = {}, Mock(side_effect=["private-a", "private-b", "fresh-a"])
    get(state, owner="owner-a", builder=builder)
    second = get(state, owner="owner-b", builder=builder)
    assert second.value == "private-b" and "private-a" not in str(state)
    third = get(state, owner="owner-a", builder=builder)
    assert not third.cache_hit and third.value == "fresh-a"
    assert "private-b" not in str(state)


def test_independent_session_mappings_never_share_entries():
    builder = Mock(side_effect=["session-one", "session-two"])
    one, two = {}, {}
    assert get(one, builder=builder).value == "session-one"
    assert get(two, builder=builder).value == "session-two"
    assert builder.call_count == 2


def test_lru_is_bounded_and_a_hit_refreshes_recency():
    state, builder = {}, Mock(return_value=90)
    for revision in ("one", "two", "three"):
        get(state, dependencies=revision, builder=builder)
    assert get(state, dependencies="one", builder=builder).cache_hit
    get(state, dependencies="four", builder=builder)
    assert get(state, dependencies="three", builder=builder).cache_hit
    assert not get(state, dependencies="two", builder=builder).cache_hit
    assert builder.call_count == 5


def test_lowering_capacity_on_hit_evicts_entries_too():
    state = {}
    for revision in (1, 2, 3):
        get(state, dependencies=revision)
    assert get(state, dependencies=1, max_entries=1).cache_hit
    assert not get(state, dependencies=3, max_entries=1).cache_hit


def test_explicit_view_owner_and_full_invalidation_preserves_unrelated_state():
    state = {"unrelated-widget": "unchanged"}
    get(state, view="analysis")
    get(state, view="analysis", dependencies="v2")
    get(state, view="portfolio")
    assert invalidate_view_cache(state, owner="other-owner") == 0
    assert invalidate_view_cache(state, owner="owner-a", view="analysis") == 2
    assert get(state, view="portfolio").cache_hit
    assert not get(state, view="analysis").cache_hit
    assert invalidate_view_cache(state) == 2
    assert state == {"unrelated-widget": "unchanged"}
    assert invalidate_view_cache(state) == 0


def test_builder_failure_is_not_cached_and_does_not_evict_valid_entries():
    state = {}
    get(state, dependencies="valid", max_entries=1)
    failing = Mock(side_effect=ValueError("synthetic"))
    with pytest.raises(ValueError, match="synthetic"):
        get(state, dependencies="failed", builder=failing, max_entries=1)
    assert get(state, dependencies="valid").cache_hit
    assert not get(state, dependencies="failed").cache_hit


def test_dependency_canonicalization_does_not_depend_on_mapping_order():
    first = view_cache_key(owner="owner-a", view="analysis", dependencies={"revision": 1, "flags": [True, None]})
    second = view_cache_key(owner="owner-a", view="analysis", dependencies={"flags": (True, None), "revision": 1})
    assert first == second and len(first) == 64


@pytest.mark.parametrize("dependency,exception", [(float("nan"), ValueError),
    (float("inf"), ValueError), ({1: "not a string key"}, TypeError),
    (pd.DataFrame({"close": [1]}), TypeError), (object(), TypeError),
    ("x" * 40_000, ValueError)])
def test_large_or_unsafe_dependency_values_fail_before_builder_runs(dependency, exception):
    state, builder = {}, Mock(side_effect=AssertionError("must not build"))
    with pytest.raises(exception):
        get(state, dependencies=dependency, builder=builder)
    assert state == {}
    builder.assert_not_called()


@pytest.mark.parametrize("owner", [None, "", "   ", "bad\nowner"])
def test_invalid_owner_never_falls_back_to_a_shared_cache(owner):
    with pytest.raises(ValueError):
        get({}, owner=owner)


@pytest.mark.parametrize("capacity", [True, 0, -1, 1.5])
def test_invalid_capacity_is_rejected(capacity):
    with pytest.raises(ValueError):
        get({}, max_entries=capacity)


def test_legacy_or_corrupt_session_bucket_is_replaced_without_touching_widgets():
    state = {"_ssl_read_only_view_cache": {"version": "old", "private": "stale"}, "widget": 42}
    assert not get(state).cache_hit
    assert "stale" not in str(state) and state["widget"] == 42


def test_cached_frames_preserve_object_cells_none_and_exact_integer_ids():
    identifier = 2 ** 60 + 1
    frame = pd.DataFrame({'id': pd.Series([identifier, None], dtype=object),
                          'tags': [['saved'], ['other']]})
    state = {}
    get(state, builder=lambda: {'frame': frame})
    result = get(state, builder=lambda: pytest.fail('must reuse')).value['frame']
    assert result.id.dtype == object
    assert result.id.iloc[0] == identifier and isinstance(result.id.iloc[0], int)
    assert result.id.iloc[1] is None
    result.tags.iloc[0].append('changed')
    assert get(state).value['frame'].tags.iloc[0] == ['saved']
    assert frame.tags.iloc[0] == ['saved']
