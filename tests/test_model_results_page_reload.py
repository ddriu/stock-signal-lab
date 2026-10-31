"""The authenticated Results branch refreshes retained old view modules."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("old", ["results", "tracking", "both", "current"])
def test_results_route_rebinds_only_old_view_contracts_without_real_views_or_storage(old):
    script = f"old = {old!r}\n" + '''
import ast
import importlib
from pathlib import Path
from unittest.mock import patch
import src.model_results_ui as results
import src.model_results_tracking_ui as tracking
import src.model_results_tracking as core

source = Path("app.py").read_text()
main = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name=="main")
branch = next(node for node in ast.walk(main) if isinstance(node, ast.If)
    and 'analysis_detail == "Resultados del modelo"' in (ast.get_source_segment(source, node.test) or ""))
function = ast.FunctionDef(name="route", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
    body=branch.body, decorator_list=[])
program = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
scope = {"importlib":importlib, "journal":object()}
exec(compile(program,"<private-results-route>","exec"),scope)
calls, reloaded = [], []
def bind(module):
    if module is results:
        module.render_model_results_page = lambda journal, **kwargs: calls.append("results")
    if module is tracking:
        module.render_model_tracking_page = lambda journal: calls.append("tracking")
bind(results); bind(tracking)
if old in {"results","both"}: results.MODEL_RESULTS_PRESENTATION_VERSION="old"
if old in {"tracking","both"}: tracking.TRACKING_PRESENTATION_VERSION="old"
real_reload=importlib.reload
def reload(module):
    reloaded.append(module.__name__)
    result=real_reload(module)
    bind(result)
    return result
with patch.object(importlib,"reload",side_effect=reload), patch.object(core,"git_revision",return_value="synthetic"):
    scope["route"]()
assert calls==["results","tracking"]
expected=[]
if old in {"results","both"}:expected.append("src.model_results_ui")
if old in {"tracking","both"}:expected.append("src.model_results_tracking_ui")
assert reloaded==expected, (reloaded,expected)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
        timeout=45, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stdout + result.stderr
