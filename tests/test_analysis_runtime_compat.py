"""Offline hot-deployment regressions; no providers, journal or email runner."""
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run(scenario: str) -> None:
    script = f"scenario = {scenario!r}\n" + '''
import importlib
import sys
from unittest.mock import patch

import app
from src.runtime_compat import ANALYSIS_MODULE_ORDER, prepare_analysis_runtime
for name in ANALYSIS_MODULE_ORDER:
    importlib.import_module(name)
import src.data_sources as sources
import src.data_loader as loader
import src.analysis_data_quality as quality
import src.analysis_policy as policy
import src.growth_candidates as growth
import src.daily_digest as digest
import src.daily_digest_ui as ui
import src.alert_runner as alerts

app.st.session_state["runtime_compat_sentinel"] = {"quantity": 3}
if scenario == "missing_digest_helpers":
    for key in ("daily_score_reading", "daily_score_coverage", "daily_score_sort_key"):
        delattr(digest, key)
    # Reproduce the old-loaded-digest/new-UI import failure, before repair.
    sys.modules.pop("src.daily_digest_ui")
    try:
        importlib.import_module("src.daily_digest_ui")
    except ImportError:
        pass
    else:
        raise AssertionError("The old digest unexpectedly supported the new UI")
elif scenario == "old_provider":
    sources.FUNDAMENTAL_PROVENANCE_VERSION = "previous-provider-method"
elif scenario == "old_quality":
    del quality.QUALITY_PRESENTATION_VERSION
elif scenario == "stale_consumer_binding":
    alerts.render_daily_score_email = lambda *args, **kwargs: None
elif scenario != "current":
    raise AssertionError(scenario)

real_reload = importlib.reload
reloaded = []
def tracked_reload(module):
    reloaded.append(module.__name__)
    return real_reload(module)

# Network and transport must not be needed by a compatibility import repair.
with patch("requests.sessions.Session.request", side_effect=AssertionError("No network")), \\
     patch("src.email_sender.send_email", side_effect=AssertionError("No email")), \\
     patch.object(importlib, "reload", side_effect=tracked_reload):
    real_reload(app)

expected = [] if scenario == "current" else [name for name in ANALYSIS_MODULE_ORDER
    if not (scenario == "missing_digest_helpers" and name == "src.daily_digest_ui")]
assert reloaded == expected, (reloaded, expected)
ui = importlib.import_module("src.daily_digest_ui")
assert callable(ui.render_daily_score_page)
assert sources.FUNDAMENTAL_PROVENANCE_VERSION == "fundamental_provenance_v2"
assert quality.QUALITY_PRESENTATION_VERSION == "dated_quality_coverage_v2"
assert loader.download_sec_fundamental_snapshot is sources.download_sec_fundamental_snapshot
assert policy.assess_analysis_data_quality is quality.assess_analysis_data_quality
assert growth.assess_analysis_data_quality is quality.assess_analysis_data_quality
assert ui.daily_score_reading is digest.daily_score_reading
assert app.run_daily_alerts is alerts.run_daily_alerts
assert alerts.render_daily_score_email is digest.render_daily_score_email
assert alerts.download_fundamental_snapshot is loader.download_fundamental_snapshot
assert app.download_fundamental_snapshot is loader.download_fundamental_snapshot
assert app.st.session_state["runtime_compat_sentinel"] == {"quantity": 3}
with patch.object(importlib, "reload", side_effect=AssertionError("No repeated reload")):
    assert prepare_analysis_runtime() == ()
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", ["missing_digest_helpers", "old_provider", "old_quality",
                                     "stale_consumer_binding", "current"])
def test_ordered_runtime_repair_preserves_bindings_and_never_repeats_for_current_modules(scenario):
    _run(scenario)
