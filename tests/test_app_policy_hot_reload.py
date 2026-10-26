"""The deployed app must tolerate an older policy module in a warm worker."""
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = r'''
import importlib
import inspect
import json
import smtplib
import requests

def forbidden(*args, **kwargs):
    raise AssertionError("The hot-reload regression must not access HTTP or SMTP")

requests.sessions.Session.request = forbidden
smtplib.SMTP = forbidden
smtplib.SMTP_SSL = forbidden

import src.analysis_policy as policy

mode = __MODE__
original_enrich = policy.enrich_analysis_rows
original_build = policy.build_policy_snapshot

def legacy_enrich(rows, *, growth_results=None, fundamental_filters=None,
                  position_contexts=None, held_tickers=(), cycle_contexts=None,
                  config=None):
    return original_enrich(
        rows, growth_results=growth_results, fundamental_filters=fundamental_filters,
        position_contexts=position_contexts, held_tickers=held_tickers,
        cycle_contexts=cycle_contexts, config=config,
    )

def legacy_build(row, *, growth_result=None, fundamental_filter=None,
                 position_context=None, cycle_context=None, held=None, config=None):
    return original_build(
        row, growth_result=growth_result, fundamental_filter=fundamental_filter,
        position_context=position_context, cycle_context=cycle_context,
        held=held, config=config,
    )

if mode in {"enrich", "both"}:
    policy.enrich_analysis_rows = legacy_enrich
if mode in {"build", "both"}:
    policy.build_policy_snapshot = legacy_build

reload_policy_calls = []
original_reload = importlib.reload
def tracked_reload(module):
    if module.__name__ == "src.analysis_policy":
        reload_policy_calls.append(module.__name__)
    return original_reload(module)
importlib.reload = tracked_reload

import app

rows = app._enrich_current_analysis_policy(
    [{"Ticker": "AAPL"}], {}, {"AAPL": {}}, {}, {}, {},
)
assert "fundamental_info" in inspect.signature(app.enrich_analysis_rows).parameters
assert "fundamental_info" in inspect.signature(app.build_policy_snapshot).parameters
assert len(rows) == 1
saved = rows[0]["Política de análisis"]
assert saved["data_quality"]["status"] == "unknown"
assert saved["data_quality"]["not_a_sell_signal"] is True
assert saved["data_quality"]["affects_engine_scores"] is False
assert saved["policy_version"] == policy.POLICY_VERSION
assert len(reload_policy_calls) == (0 if mode == "current" else 1)
print(json.dumps({"mode": mode, "policy_reload_count": len(reload_policy_calls)}))
'''


@pytest.mark.parametrize("mode", ["current", "enrich", "build", "both"])
def test_app_refreshes_only_legacy_policy_apis_before_importing_symbols(mode):
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT.replace("__MODE__", repr(mode))],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert '"policy_reload_count"' in result.stdout
