"""Previous d70c modules/new entrypoint hot upgrade, with transport prohibited."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIOR_RELEASE = "d70c230"


def test_previous_release_isin_imports_are_repaired_before_new_consumers():
    script = f"previous = {PRIOR_RELEASE!r}\n" + r'''
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import src


def previous_module(name):
    spec = importlib.util.find_spec(name)
    assert spec and spec.origin
    relative = name.replace('.', '/') + '.py'
    code = subprocess.check_output(['git','show',f'{previous}:{relative}'],text=True)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    package, _, base = name.rpartition('.')
    if package:
        setattr(sys.modules[package],base,module)
    exec(compile(code,f'<previous deployment {relative}>','exec'),module.__dict__)
    return module


for name in ('src.instruments','src.arena_data_quality','src.daily_digest',
             'src.daily_digest_ui','src.alert_runner','src.runtime_compat'):
    previous_module(name)
old = previous_module('app')
old_instruments = sys.modules['src.instruments']
old_digest = sys.modules['src.daily_digest']
old_guard = sys.modules['src.runtime_compat']
assert old_guard.prepare_analysis_runtime() == ()
assert not hasattr(old_instruments,'is_isin_identifier')
assert old_digest._calendar_name('IE00BM95B621') == 'XNYS'

# The previous guard considered this process compatible, but importing the new
# consumer immediately failed. Reproduce before repairing, without modifying
# sys.modules or falling back to a less strict quality check.
try:
    exec(compile(Path('src/arena_data_quality.py').read_text(),'<new consumer>','exec'),
         {'__name__':'_new_consumer_probe'})
except ImportError as error:
    assert 'is_isin_identifier' in str(error),error
else:
    raise AssertionError('Previous instruments unexpectedly supported the new consumer')

sentinel = {'favorites':['SYNTH','IE00BM95B621'],'history':[{'day':'2026-11-23','score':80}]}
state = {'_qa_preserve_user_data':sentinel}
real_reload = importlib.reload
reloaded = []


def tracked_reload(module):
    reloaded.append(module.__name__)
    return real_reload(module)


with (
    patch('requests.sessions.Session.request',side_effect=AssertionError('No network')),
    patch('smtplib.SMTP',side_effect=AssertionError('No SMTP')),
    patch('smtplib.SMTP_SSL',side_effect=AssertionError('No SMTP')),
    patch.object(old.st,'session_state',state),
    patch.object(importlib,'reload',side_effect=tracked_reload),
):
    current = real_reload(old)
    import src.runtime_compat as compat
    import src.instruments as instruments
    import src.arena_data_quality as quality
    import src.daily_digest as digest
    import src.saved_close_report as bridge

    assert compat.RUNTIME_COMPAT_VERSION == 'arena-universe-runtime-v3'
    assert reloaded[0] == 'src.runtime_compat',reloaded
    assert reloaded.index('src.instruments') < reloaded.index('src.arena_data_quality') < reloaded.index('src.daily_digest')
    assert instruments is old_instruments and digest is old_digest
    assert instruments.is_isin_identifier('IE00BM95B621') is True
    assert digest._calendar_name('IE00BM95B621') == 'no_verificado'
    assert quality.is_isin_identifier is digest.is_isin_identifier is instruments.is_isin_identifier
    assert digest.build_arena_quality_manifest is quality.build_arena_quality_manifest
    assert bridge.SAVED_CLOSE_REPORT_VERSION == 'saved-close-report-v1'
    assert bridge.build_daily_score_report is digest.build_daily_score_report
    assert bridge.seal_daily_score_report is digest.seal_daily_score_report
    assert current.archive_saved_close_report is bridge.archive_saved_close_report
    assert current.st.session_state is state
    assert state['_qa_preserve_user_data'] is sentinel
    assert sentinel == {'favorites':['SYNTH','IE00BM95B621'],'history':[{'day':'2026-11-23','score':80}]}

    reloaded.clear()
    real_reload(current)
    assert not reloaded,reloaded
    assert compat.prepare_analysis_runtime() == ()
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_each_loaded_scope_contract_repairs_in_dependency_order_once():
    script = r'''
import importlib
from unittest.mock import patch

import src.runtime_compat as compat

with (
    patch('requests.sessions.Session.request',side_effect=AssertionError('No network')),
    patch('smtplib.SMTP',side_effect=AssertionError('No SMTP')),
    patch('smtplib.SMTP_SSL',side_effect=AssertionError('No SMTP')),
):
    for name in compat.ANALYSIS_MODULE_ORDER:
        importlib.import_module(name)
    assert compat.prepare_analysis_runtime() == ()
    for name,marker in (
        ('src.instruments','INSTRUMENT_SCOPE_VERSION'),
        ('src.arena_data_quality','ARENA_SCOPE_VERSION'),
        ('src.daily_digest','DIGEST_SCOPE_VERSION'),
        ('src.saved_close_report','SAVED_CLOSE_REPORT_VERSION'),
    ):
        module = importlib.import_module(name)
        setattr(module,marker,'previous-contract')
        assert compat.prepare_analysis_runtime() == compat.ANALYSIS_MODULE_ORDER
        assert compat.prepare_analysis_runtime() == ()
    import src.instruments as instruments
    import src.arena_data_quality as quality
    import src.daily_digest as digest
    import src.saved_close_report as bridge
    assert quality.is_isin_identifier is digest.is_isin_identifier is instruments.is_isin_identifier
    assert digest.build_arena_quality_manifest is quality.build_arena_quality_manifest
    assert bridge.build_daily_score_report is digest.build_daily_score_report
    assert bridge.seal_daily_score_report is digest.seal_daily_score_report
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
