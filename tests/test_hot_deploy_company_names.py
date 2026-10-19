"""Hot reruns must refresh presentation modules before binding their exports."""

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRESENTATION_MODULES = (
    "src.instruments",
    "src.growth_candidates",
    "src.portfolio_rotation",
)


def _run_hot_rerun(legacy_modules: tuple[str, ...]) -> None:
    # Isolate sys.modules and reloaded dataclasses from the rest of the suite.
    # No files, session data, or real investment records are changed.
    script = f"legacy_modules = {legacy_modules!r}\n" + '''
from datetime import date
import importlib
import inspect
import sys
from unittest.mock import patch

import app
import src.instruments as instruments
import src.growth_candidates as growth
import src.portfolio_rotation as rotation

presentation_modules = {
    "src.instruments": (instruments, "COMPANY_NAMES_VERSION"),
    "src.growth_candidates": (growth, "GROWTH_CANDIDATE_PRESENTATION_VERSION"),
    "src.portfolio_rotation": (rotation, "ROTATION_PRESENTATION_VERSION"),
}

def legacy_growth_rows(*args, **kwargs):
    raise AssertionError("The previous growth presentation was not refreshed")

def legacy_rotation_dashboard(live_summary, held_tickers, favorite_tickers):
    raise AssertionError("The previous rotation signature was not refreshed")

for module_name in legacy_modules:
    module, marker = presentation_modules[module_name]
    assert sys.modules[module_name] is module
    if hasattr(module, marker):
        delattr(module, marker)
    if module is instruments:
        delattr(module, "resolve_company_name")
    elif module is growth:
        module.build_growth_candidate_rows = legacy_growth_rows
    elif module is rotation:
        module.build_rotation_dashboard = legacy_rotation_dashboard

if "src.instruments" in legacy_modules:
    # Reproduce the reported import failure before running the repair guard.
    try:
        exec("from src.instruments import resolve_company_name", {})
    except ImportError as error:
        assert "resolve_company_name" in str(error)
    else:
        raise AssertionError("The stale helper import unexpectedly succeeded")

real_reload = importlib.reload
reloaded_names = []

def tracked_reload(module):
    reloaded_names.append(module.__name__)
    return real_reload(module)

with patch.object(importlib, "reload", side_effect=tracked_reload):
    real_reload(app)

expected = [name for name in presentation_modules if name in legacy_modules]
assert [name for name in reloaded_names if name in presentation_modules] == expected
for module, marker in presentation_modules.values():
    assert getattr(module, marker) == "company-names-v1"

assert app.resolve_company_name is instruments.resolve_company_name
assert app.build_growth_candidate_rows is growth.build_growth_candidate_rows
assert app.build_rotation_dashboard is rotation.build_rotation_dashboard
assert "company_names" in inspect.signature(app.build_rotation_dashboard).parameters
assert app.resolve_company_name("NKE", {"longName": "NIKE, Inc."}) == "NIKE, Inc."

rows = app.build_growth_candidate_rows(
    {}, universe=["NKE"], names={"NKE": "NIKE, Inc."},
    reference_date=date(2026, 10, 20),
)
assert [(row["Ticker"], row["Empresa"]) for row in rows] == [
    ("NKE", "NIKE, Inc. · NKE"),
]
dashboard = app.build_rotation_dashboard(
    [], ["NKE"], [], company_names={"NKE": "NIKE, Inc."},
    today=date(2026, 10, 20),
)
priorities = app.build_rotation_priorities(dashboard)
assert [(row["Ticker"], row["Empresa"]) for row in priorities] == [
    ("NKE", "NIKE, Inc."),
]
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "legacy_modules",
    [(name,) for name in PRESENTATION_MODULES] + [PRESENTATION_MODULES],
)
def test_hot_deploy_refreshes_old_company_name_modules(
    legacy_modules: tuple[str, ...],
) -> None:
    _run_hot_rerun(legacy_modules)


def test_current_company_name_modules_are_not_reloaded_on_rerun() -> None:
    _run_hot_rerun(())
