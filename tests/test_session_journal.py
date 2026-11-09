"""Session journal caching uses synthetic backends, never a live database."""
from collections import Counter
from unittest.mock import patch

import pandas as pd
import pytest

from src.session_journal import session_journal
from src.view_cache import get_or_build_view


class Journal:
    backend_name = "Synthetic"

    def __init__(self, owner="alice"):
        self.owner = owner
        self.calls = Counter()
        self.rows = pd.DataFrame([{"ticker": "TEST", "notes": ["original"]}])
        self.fail_reads = False

    def list_operations(self):
        self.calls["list_operations"] += 1
        if self.fail_reads:
            raise RuntimeError("read failed")
        return self.rows

    def list_analysis_snapshots(self, ticker=None):
        self.calls["list_analysis_snapshots"] += 1
        return {"ticker": ticker, "items": [{"score": 88}]}

    def get_daily_score_report(self, report_id):
        self.calls["get_daily_score_report"] += 1
        return {"report_id": report_id}

    def get_radar_review_checkpoint(self):
        self.calls["get_radar_review_checkpoint"] += 1
        if self.fail_reads:
            raise RuntimeError("checkpoint read failed")
        return {"revision": self.calls["get_radar_review_checkpoint"]}

    def save_radar_review_checkpoint(self, checkpoint, *, expected_revision):
        self.calls["save_radar_review_checkpoint"] += 1
        return True

    def list_enabled_alert_preferences(self):
        self.calls["list_enabled_alert_preferences"] += 1
        return []

    def get_unlisted_secret(self):
        self.calls["get_unlisted_secret"] += 1
        return "not cached"

    def add_operation(self, ticker, *, fail=False):
        self.calls["add_operation"] += 1
        self.rows.loc[0, "ticker"] = ticker
        if fail:
            raise RuntimeError("ambiguous write")
        return 42

    def _request(self):
        self.calls["_request"] += 1
        return "private pass-through"


def test_reuses_reads_across_proxy_bindings_in_one_session():
    journal, state = Journal(), {}
    session_journal(journal, state).list_operations()
    session_journal(journal, state).list_operations()
    assert journal.calls["list_operations"] == 1
    assert session_journal(journal, state).owner == "alice"
    assert session_journal(journal, state).backend_name == "Synthetic"


def test_radar_checkpoint_reads_are_uncached_without_invalidating_other_reads():
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    proxy.list_operations()
    assert proxy.get_radar_review_checkpoint() == {"revision": 1}
    assert proxy.get_radar_review_checkpoint() == {"revision": 2}
    journal.fail_reads = True
    with pytest.raises(RuntimeError, match="checkpoint read failed"):
        proxy.get_radar_review_checkpoint()
    assert proxy.list_operations().iloc[0]["ticker"] == "TEST"
    assert journal.calls["list_operations"] == 1
    assert "_view_data_revision" not in state
    journal.fail_reads = False
    assert proxy.save_radar_review_checkpoint({"revision": 3}, expected_revision=2)
    proxy.list_operations()
    assert journal.calls["list_operations"] == 2
    assert state["_view_data_revision"] == 1


def test_private_and_group_owners_coexist_but_never_share_reads():
    first, group, state = Journal("alice"), Journal("group"), {}
    a, b = session_journal(first, state), session_journal(group, state)
    a.list_operations()
    b.list_operations()
    a.list_operations()
    b.list_operations()
    assert first.calls["list_operations"] == group.calls["list_operations"] == 1
    a.add_operation("NEW")
    b.list_operations()
    a.list_operations()
    assert first.calls["list_operations"] == 2
    assert group.calls["list_operations"] == 1


def test_distinct_backend_with_same_owner_and_distinct_sessions_are_isolated():
    first, second, state = Journal(), Journal(), {}
    second.rows.loc[0, "ticker"] = "OTHER"
    assert session_journal(first, state).list_operations().iloc[0]["ticker"] == "TEST"
    assert session_journal(second, state).list_operations().iloc[0]["ticker"] == "OTHER"
    session_journal(first, {}).list_operations()
    assert first.calls["list_operations"] == 2


def test_dataframe_and_nested_results_are_independent_from_backend_and_cache():
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    first = proxy.list_operations()
    first.loc[0, "ticker"] = "MUTATED"
    first.loc[0, "notes"].append("MUTATED")
    # DataFrame deepcopy does not recursively copy Python objects in object
    # columns. The proxy must isolate ordinary mutable results as well.
    nested = proxy.list_analysis_snapshots("TEST")
    nested["items"][0]["score"] = 0
    assert journal.rows.iloc[0]["ticker"] == "TEST"
    assert proxy.list_operations().iloc[0]["ticker"] == "TEST"
    assert proxy.list_operations().iloc[0]["notes"] == ["original"]
    assert journal.rows.iloc[0]["notes"] == ["original"]
    assert proxy.list_analysis_snapshots("TEST")["items"][0]["score"] == 88


def test_arguments_and_defaults_are_in_cache_identity():
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    assert proxy.list_analysis_snapshots("ONE")["ticker"] == "ONE"
    assert proxy.list_analysis_snapshots(ticker="ONE")["ticker"] == "ONE"
    assert proxy.list_analysis_snapshots("TWO")["ticker"] == "TWO"
    proxy.list_analysis_snapshots()
    proxy.list_analysis_snapshots(ticker=None)
    assert journal.calls["list_analysis_snapshots"] == 3


def test_reads_expire_without_downloads_or_writes_and_boundary_is_expired():
    journal, state = Journal(), {}
    proxy = session_journal(journal, state, ttl_seconds=60)
    with patch("src.session_journal.time.monotonic", return_value=100):
        proxy.list_operations()
    with patch("src.session_journal.time.monotonic", return_value=159.99):
        proxy.list_operations()
    assert journal.calls["list_operations"] == 1
    with patch("src.session_journal.time.monotonic", return_value=160):
        proxy.list_operations()
    assert journal.calls["list_operations"] == 2
    assert journal.calls["add_operation"] == 0


@pytest.mark.parametrize("fail", [False, True])
def test_writes_are_immediate_and_always_invalidate_reads_views_and_revision(fail):
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    proxy.list_operations()
    get_or_build_view(state, owner="alice", view="home", dependencies={}, builder=lambda: {"old": True})
    if fail:
        with pytest.raises(RuntimeError, match="ambiguous write"):
            proxy.add_operation("NEW", fail=True)
    else:
        assert proxy.add_operation("NEW") == 42
    assert journal.calls["add_operation"] == 1
    assert proxy.list_operations().iloc[0]["ticker"] == "NEW"
    assert journal.calls["list_operations"] == 2
    assert state["_view_data_revision"] == 1
    result = get_or_build_view(state, owner="alice", view="home", dependencies={}, builder=lambda: {"new": True})
    assert result.cache_hit is False


def test_failed_read_does_not_cache_exception_or_bump_write_revision():
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    journal.fail_reads = True
    with pytest.raises(RuntimeError, match="read failed"):
        proxy.list_operations()
    journal.fail_reads = False
    proxy.list_operations()
    assert journal.calls["list_operations"] == 2
    assert "_view_data_revision" not in state


@pytest.mark.parametrize("method", ["get_unlisted_secret", "list_enabled_alert_preferences"])
def test_prefix_named_methods_are_not_automatically_cached(method):
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    getattr(proxy, method)()
    getattr(proxy, method)()
    assert journal.calls[method] == 2


def test_private_method_is_uncached_pass_through_without_interception():
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    assert proxy._request() == proxy._request() == "private pass-through"
    assert journal.calls["_request"] == 2
    assert "_view_data_revision" not in state


def test_owner_drift_rejects_previously_bound_read_and_purges_old_reads():
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    cached_read = proxy.list_operations
    cached_read()
    journal.owner = "bob"
    with pytest.raises(RuntimeError, match="owner changed"):
        cached_read()
    assert journal.calls["list_operations"] == 1
    assert not state["_ssl_session_journal_reads"]["entries"]
    session_journal(journal, state).list_operations()
    assert journal.calls["list_operations"] == 2


def test_cache_is_bounded_and_keys_do_not_contain_owner_or_arguments():
    journal, state = Journal("private-owner@example.test"), {}
    proxy = session_journal(journal, state)
    for value in range(40):
        proxy.get_daily_score_report(f"sensitive-report-{value}")
    entries = state["_ssl_session_journal_reads"]["entries"]
    assert len(entries) == 32
    assert all(len(key) == 64 for key in entries)
    assert "private-owner" not in repr(list(entries))
    assert "sensitive-report" not in repr(list(entries))
    proxy.get_daily_score_report("sensitive-report-39")
    assert journal.calls["get_daily_score_report"] == 40
    proxy.get_daily_score_report("sensitive-report-0")
    assert journal.calls["get_daily_score_report"] == 41


def test_unsupported_input_bypasses_cache_without_using_repr_as_a_key():
    journal, state = Journal(), {}
    proxy = session_journal(journal, state)
    data = pd.DataFrame({"score": [88]})
    proxy.list_analysis_snapshots(data)
    proxy.list_analysis_snapshots(data)
    assert journal.calls["list_analysis_snapshots"] == 2
    assert "_ssl_session_journal_reads" not in state


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True, "60"])
def test_invalid_ttl_rejected(ttl):
    with pytest.raises(ValueError, match="ttl_seconds"):
        session_journal(Journal(), {}, ttl_seconds=ttl)


@pytest.mark.parametrize("owner", ["", " ", None, 1])
def test_missing_owner_rejected(owner):
    with pytest.raises(ValueError, match="owner"):
        session_journal(Journal(owner), {})
