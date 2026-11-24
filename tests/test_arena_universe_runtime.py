"""Upgrade an actual pre-policy deployment without resetting private state."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_published_process_rebinds_selector_and_owner_journals(tmp_path):
    script = f"data_dir = {str(tmp_path)!r}\n" + r'''
import importlib
import importlib.util
import os
import subprocess
import sys
from unittest.mock import patch

os.environ['STOCK_SIGNAL_LAB_DATA_DIR'] = data_dir
import src

def previous(name):
    spec = importlib.util.find_spec(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    package, _, symbol = name.rpartition('.')
    if package:
        setattr(sys.modules[package], symbol, module)
    code = subprocess.check_output(['git','show','cd69d01:'+name.replace('.','/')+'.py'],text=True)
    exec(compile(code,'<published '+name+'>','exec'),module.__dict__)
    return module

for name in ('src.journal','src.supabase_journal','src.storage','src.session_journal',
             'src.arena_data_quality','src.daily_digest','src.saved_close_report',
             'src.daily_digest_ui','src.alert_runner','src.runtime_compat'):
    previous(name)
old = previous('app')
import src.storage as storage
with patch.object(storage,'load_supabase_config',return_value=None):
    backend = old.cached_journal('synthetic-owner')
    backend.add_favorite('SYNTH','Synthetic','NASDAQ')
assert not hasattr(backend,'get_arena_universe_policy')
assert sys.modules['src.runtime_compat'].prepare_analysis_runtime() == ()
try:
    sys.modules['src.daily_digest'].build_daily_score_report([],arena_universe_policy={})
except TypeError as error:
    assert 'arena_universe_policy' in str(error)
else:
    raise AssertionError('Previous digest unexpectedly accepted the new API')

state = {'_qa_preserve': {'favorites':['SYNTH'],'rounds':[{'day':'2026-11-20'}]}}
sentinel = state['_qa_preserve']
real_reload = importlib.reload
reloaded = []
def tracked(module):
    reloaded.append(module.__name__)
    return real_reload(module)
with (
    patch('requests.sessions.Session.request',side_effect=AssertionError('No providers')),
    patch('smtplib.SMTP_SSL',side_effect=AssertionError('No mail')),
    patch.object(old.st,'session_state',state),
    patch.object(importlib,'reload',side_effect=tracked),
):
    current = real_reload(old)
    import src.runtime_compat as compat
    import src.arena_universe as selector
    import src.daily_digest as digest
    import src.saved_close_report as archive
    import src.alert_runner as alerts
    assert compat.RUNTIME_COMPAT_VERSION == 'arena-universe-runtime-v3'
    assert reloaded.index('src.arena_data_quality') < reloaded.index('src.daily_digest')
    assert digest.resolve_arena_universe is selector.resolve_arena_universe
    assert archive.read_arena_universe_policy is alerts.read_arena_universe_policy is selector.read_arena_universe_policy
    with patch.object(storage,'load_supabase_config',return_value=None):
        journal = current.cached_journal('synthetic-owner')
        assert journal is not backend
        assert journal.list_favorites().ticker.tolist() == ['SYNTH']
        assert journal.get_arena_universe_policy()['mode'] == 'strict'
    assert state['_qa_preserve'] is sentinel
    assert sentinel == {'favorites':['SYNTH'],'rounds':[{'day':'2026-11-20'}]}
    reloaded.clear()
    real_reload(current)
    assert not reloaded,reloaded
    assert compat.prepare_analysis_runtime() == ()
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
