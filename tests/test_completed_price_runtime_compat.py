"""Load the published previous tree, then repair the new price-validation API."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIOR_TREE_COMMIT = "03992ce29ee22e161b2eeadf9c329ee5488dd809"


def test_warm_upgrade_rebinds_completed_price_validation_without_resetting_state():
    script = f"previous = {PRIOR_TREE_COMMIT!r}\n" + r'''
import importlib
import importlib.util
import subprocess
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
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


previous_module('src.data_loader')
previous_module('src.runtime_compat')
old = previous_module('app')
loader = sys.modules['src.data_loader']
assert not hasattr(loader,'PRICE_VALIDATION_VERSION')
assert sys.modules['src.runtime_compat'].prepare_analysis_runtime() == ()

raw = pd.DataFrame({'open':[100.,105.],'high':[101.,101.],'low':[99.,99.],
    'close':[100.,100.],'volume':[1000.,1000.]},index=pd.to_datetime(['2026-11-23','2026-11-24']))
raw.attrs['quote_currency'] = 'USD'
clock = '2026-11-24T17:28:00Z'
from src.hourly_market_schedule import validated_completed_daily_prices
validator = lambda symbol,frame: validated_completed_daily_prices(symbol,frame,now=clock,require_currency=True)
state = {'_qa_preserve':{'favorites':['SYNTH'],'history':[{'session':'2026-11-23'}]}}
sentinel = state['_qa_preserve']
real_reload = importlib.reload
reloaded = []


def tracked_reload(module):
    reloaded.append(module.__name__)
    return real_reload(module)


with (
    patch('yfinance.download',side_effect=lambda *args,**kwargs:raw.copy(deep=True)),
    patch('requests.get',return_value=SimpleNamespace(raise_for_status=lambda:None,json=lambda:None,text='No data')),
    patch('requests.sessions.Session.request',side_effect=AssertionError('No network')),
    patch('smtplib.SMTP_SSL',side_effect=AssertionError('No SMTP')),
    patch.object(old.st,'session_state',state),
    patch.object(old.st.cache_data,'clear',side_effect=AssertionError('No cache clearing')),
    patch.object(importlib,'reload',side_effect=tracked_reload),
):
    try:
        old.download_prices('SYNTH','2021-11-25','2026-11-24',validator=validator)
    except loader.DataDownloadError as error:
        assert error.code == 'invalid_ohlc'
    else:
        raise AssertionError('Previous source unexpectedly ignored the open session before OHLC validation')

    current = real_reload(old)
    import src.runtime_compat as compat
    assert compat.RUNTIME_COMPAT_VERSION == 'arena-universe-runtime-v3'
    assert loader.PRICE_VALIDATION_VERSION == 'completed-session-ohlc-order-v1'
    assert reloaded[0] == 'src.runtime_compat'
    assert 'src.data_loader' in reloaded
    assert current.download_prices is loader.download_prices
    result = current.cached_download('SYNTH',date(2021,11,25),date(2026,11,24),True,
        'qa-completed-order',review_clock=clock)
    assert result.index.tolist() == [pd.Timestamp('2026-11-23')]
    assert result.attrs['quote_units_normalized'] is True
    assert result.attrs['display_currency'] == 'USD'
    assert current.st.session_state is state and state['_qa_preserve'] is sentinel
    assert sentinel == {'favorites':['SYNTH'],'history':[{'session':'2026-11-23'}]}

    reloaded.clear()
    real_reload(current)
    assert reloaded == [],reloaded
    assert compat.prepare_analysis_runtime() == ()
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
