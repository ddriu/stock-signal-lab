"""A provenance repair must not retain a cached, undated provider snapshot."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_hot_rerun_refreshes_legacy_data_module_before_binding_version_export():
    script = '''
import importlib
import app
import src.data_loader as loader
del loader.FUNDAMENTAL_SNAPSHOT_VERSION
importlib.reload(app)
assert app.FUNDAMENTAL_SNAPSHOT_VERSION == "dated_statement_fallback_v2"
assert app.download_fundamental_snapshot is loader.download_fundamental_snapshot
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def test_fundamental_cache_key_includes_provenance_method_version_without_network():
    script = '''
from unittest.mock import patch
import app
app.cached_fundamentals.clear()
with patch.object(app, "download_fundamental_snapshot", return_value={"symbol": "TEST"}) as fetch:
    app.cached_fundamentals("TEST", source_version="old-method")
    app.cached_fundamentals("TEST", source_version="old-method")
    assert fetch.call_count == 1
    app.cached_fundamentals("TEST", source_version=app.FUNDAMENTAL_SNAPSHOT_VERSION)
    assert fetch.call_count == 2
app.cached_fundamentals.clear()
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
