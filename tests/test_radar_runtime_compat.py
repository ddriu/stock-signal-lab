"""Real previous-release/current-source hot-deploy regression, offline."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIOR_RELEASE = "2377afc227a980ce7c65793f03d4bffce674138a"


def test_warm_radar_rebinds_catalog_storage_and_cached_backend(tmp_path):
    script = f"previous = {PRIOR_RELEASE!r}\nlocal_data = {str(tmp_path)!r}\n" + r'''
import importlib
import importlib.util
import linecache
import os
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
from config import StrategyConfig

os.environ["STOCK_SIGNAL_LAB_DATA_DIR"] = local_data


def previous_module(name):
    spec = importlib.util.find_spec(name)
    assert spec and spec.origin
    relative_path = name.replace(".", "/") + ".py"
    code = subprocess.check_output(["git", "show", f"{previous}:{relative_path}"], text=True)
    previous_path = f"<previous deployment {relative_path}>"
    linecache.cache[previous_path] = (len(code), None, code.splitlines(keepends=True), previous_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    package, _, base = name.rpartition(".")
    if package:
        setattr(sys.modules[package], base, module)
    exec(compile(code, previous_path, "exec"), module.__dict__)
    return module


def missing_keyword(call, expected):
    try:
        call()
    except TypeError as error:
        assert f"unexpected keyword argument '{expected}'" in str(error), error
    else:
        raise AssertionError(f"Previous release unexpectedly accepted {expected!r}")


for name in (
    "src.journal", "src.supabase_journal", "src.storage", "src.session_journal",
    "src.hourly_market_schedule", "src.market_cache_ui", "src.analysis_snapshots",
    "src.opportunity_catalog", "src.opportunity_status", "src.runtime_compat",
):
    previous_module(name)

old = previous_module("app")
import src.storage as storage
import src.supabase_journal as supabase
import src.reconciled_portfolio_ui as old_consumer

old_error = supabase.JournalStorageError
assert old_consumer.JournalStorageError is old_error

empty = pd.DataFrame()
strategy = StrategyConfig()
missing_keyword(
    lambda: old.build_opportunity_catalog(["NVDA"], empty, [], verified_review={}),
    "verified_review",
)
missing_keyword(
    lambda: old.persist_analysis_batch_snapshots(
        object(), [], {}, {}, {}, {}, {}, {}, strategy, existing_snapshots=empty,
    ),
    "existing_snapshots",
)

remote_config = storage.SupabaseConfig("https://test-project.invalid", "test-key")
with patch.object(storage, "load_supabase_config", return_value=None):
    old_local = old.cached_journal("qa_local")
    assert old.cached_journal("qa_local") is old_local
    old_local.add_favorite("NVDA", "NVIDIA", "NASDAQ")
with patch.object(storage, "load_supabase_config", return_value=remote_config):
    old_remote = old.cached_journal("qa_remote")
    assert old.cached_journal("qa_remote") is old_remote
for backend in (old_local, old_remote):
    assert not hasattr(backend, "get_radar_review_checkpoint")
    missing_keyword(
        lambda: backend.list_analysis_snapshots_for_sessions(["2026-11-06"], tickers=["NVDA"]),
        "tickers",
    )

session_state = {"qa_radar_keep": {"owner": "qa_local", "favorite": "NVDA"}}
real_reload = importlib.reload
reloaded = []


def tracked_reload(module):
    reloaded.append(module.__name__)
    return real_reload(module)


# Reload only the entrypoint, just as Streamlit can after pulling a revision.
# Do not delete sys.modules entries or clear existing resources or session state.
with (
    patch("requests.sessions.Session.request", side_effect=AssertionError("No network")),
    patch("src.email_sender.send_email", side_effect=AssertionError("No email")),
    patch.object(importlib, "reload", side_effect=tracked_reload),
    patch.object(old.st, "session_state", session_state),
):
    current = real_reload(old)
    expected = {
        "src.runtime_compat", "src.journal", "src.supabase_journal", "src.storage",
        "src.session_journal", "src.hourly_market_schedule", "src.market_cache_ui",
        "src.analysis_snapshots", "src.opportunity_catalog", "src.opportunity_status",
    }
    assert set(reloaded) == expected and len(reloaded) == len(expected), reloaded
    assert current.JournalStorageError is old_consumer.JournalStorageError is old_error
    assert current.st.session_state["qa_radar_keep"] == {"owner": "qa_local", "favorite": "NVDA"}

    with patch.object(storage, "load_supabase_config", return_value=None):
        local = current.cached_journal("qa_local")
        other = current.cached_journal("qa_other")
        assert current.cached_journal("qa_local") is local and local is not old_local
        assert local.database_path == old_local.database_path
        assert local.list_favorites().ticker.tolist() == ["NVDA"]
        assert other.list_favorites().empty
    with patch.object(storage, "load_supabase_config", return_value=remote_config):
        remote = current.cached_journal("qa_remote")
        assert current.cached_journal("qa_remote") is remote and remote is not old_remote
        assert isinstance(remote, supabase.SupabaseTradingJournal)
        assert callable(remote.get_radar_review_checkpoint)
        assert callable(remote.save_radar_review_checkpoint)

    assert local.list_analysis_snapshots_for_sessions(["2026-11-06"], tickers=["NVDA"]).empty
    state = {}
    proxy = current.session_journal(local, state)
    assert proxy.get_radar_review_checkpoint() is None
    assert "_view_data_revision" not in state
    review = current.RadarReview.start(["NVDA"], "a" * 64, datetime.now(timezone.utc))
    assert proxy.save_radar_review_checkpoint(review.checkpoint(), expected_revision=None)
    assert proxy.get_radar_review_checkpoint() == review.checkpoint()
    assert other.get_radar_review_checkpoint() is None
    assert current.build_opportunity_catalog(["NVDA"], empty, [], verified_review={})[0]["Ticker"] == "NVDA"
    assert current.persist_analysis_batch_snapshots(
        proxy, [], {}, {}, {}, {}, {}, {}, strategy, existing_snapshots=empty,
    ).saved_count == 0
    assert current.opportunity_coverage([{
        "Ticker": "NVDA", "Fecha": "2026-11-06", "Momento entrada": 80,
        "Comprobación": "Revisado y guardado", "Lectura entrada": "Entrada fuerte",
    }], reference_date="2026-11-09")["available"] == 1

    from src.hourly_market_schedule import completed_daily_prices
    prices = pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["2026-11-06"]))
    assert completed_daily_prices("NVDA", prices, now="2026-11-09T12:00Z").attrs[
        "hourly_market_schedule"
    ]["latest_completed_session"] == "2026-11-06"

    reloaded.clear()
    real_reload(current)
    assert not reloaded, reloaded
    assert current.cached_journal("qa_local") is local

# Exercise the real authenticated Radar branch of the warmed entrypoint.
# Only identity and transport are substituted; do not mock the Radar renderer.
from streamlit.testing.v1 import AppTest

page = AppTest.from_string("""
import streamlit as st
from unittest.mock import patch
import app
from src.auth import AuthConfig

st.session_state["main_navigation"] = "Analizar"
st.session_state["analysis_navigation"] = "Radar"
with (
    patch.object(app, "require_login", return_value=AuthConfig("qa_local", "")),
    patch.object(app, "load_auth_accounts", return_value={}),
    patch("src.storage.load_supabase_config", return_value=None),
    patch("requests.sessions.Session.request", side_effect=AssertionError("No network")),
    patch("src.email_sender.send_email", side_effect=AssertionError("No email")),
):
    app.main()
""", default_timeout=15).run()
assert not page.exception, [item.message for item in page.exception]
assert any(metric.label == "Con lectura reciente" for metric in page.metric)
assert any("NVDA" in str(table.value) for table in page.dataframe)
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
