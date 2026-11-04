"""Hot deployment repair must not need providers, storage writes or SMTP."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("scenario", ["current", "sqlite", "remote", "proxy"])
def test_read_adapters_are_repaired_before_binding_without_io(scenario):
    script = '''
import importlib
from unittest.mock import patch
import src.journal as local
import src.supabase_journal as remote
import src.storage as storage
import src.session_journal as proxy
from src.investment_runtime import prepare_investment_runtime
if SCENARIO == "sqlite": del local.TradingJournal.get_latest_daily_score_report
if SCENARIO == "remote": del remote.SupabaseTradingJournal.list_analysis_snapshots_for_sessions
if SCENARIO == "proxy": proxy._READ_METHODS = frozenset()
with patch("requests.sessions.Session.request", side_effect=AssertionError("Network")), \
     patch("sqlite3.connect", side_effect=AssertionError("Storage")), \
     patch("smtplib.SMTP", side_effect=AssertionError("SMTP")):
    changed = prepare_investment_runtime()
    assert bool(changed) == (SCENARIO != "current")
    assert prepare_investment_runtime() == ()
assert storage.TradingJournal is local.TradingJournal
assert storage.SupabaseTradingJournal is remote.SupabaseTradingJournal
assert callable(local.TradingJournal.get_latest_daily_score_report)
assert callable(remote.SupabaseTradingJournal.list_analysis_snapshots_for_sessions)
assert "get_latest_daily_score_report" in proxy._READ_METHODS
'''.replace("SCENARIO", repr(scenario))
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
