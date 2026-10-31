"""Cohort UI imports must not bind incompatible warm-deployment functions."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("old", ["normalizer", "service", "both", "current"])
def test_cohort_controls_refresh_only_incompatible_loaded_apis(old):
    script = f"old = {old!r}\n" + '''
import importlib
import inspect
from unittest.mock import patch
import src.model_results_ui as ui
import src.strategy_audit_data as data
import src.model_results_service as service

def old_normalize(snapshots, source="technical"):
    raise AssertionError("Old normalizer was retained")
def old_build(snapshots, bars, *, source="technical"):
    raise AssertionError("Old report service was retained")
if old in {"normalizer", "both"}:
    data.normalize_audit_signals = old_normalize
if old in {"service", "both"}:
    service.build_model_results = old_build

real_reload = importlib.reload
names = []
def tracked(module):
    names.append(module.__name__)
    return real_reload(module)
with patch.object(importlib, "reload", side_effect=tracked):
    real_reload(ui)
assert "cohort" in inspect.signature(ui.normalize_audit_signals).parameters
assert "cohort" in inspect.signature(ui.build_model_results).parameters
assert ui.normalize_audit_signals is data.normalize_audit_signals
assert ui.build_model_results is service.build_model_results
if old == "current":
    assert names == []
elif old in {"normalizer", "both"}:
    assert names == ["src.strategy_audit_data", "src.model_results_service"]
else:
    assert names == ["src.model_results_service"]
'''
    result = subprocess.run([sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
