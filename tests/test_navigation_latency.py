"""Populated, offline UI probes; timings are diagnostic, not trading evidence.

Unlike empty-panel routing tests, these execute the real renderers, indicators,
growth policy and session read cache. Only storage transport is synthetic.
No network, SMTP, real journal or production account is available.
"""
from statistics import median
from time import perf_counter

from streamlit.testing.v1 import AppTest


ROUTES = ("Inicio", "Favoritos", "Posiciones", "Resultado real", "Historial", "Evolución", "Momentum")
SCRIPT = r'''
from collections import Counter
from contextlib import ExitStack
from datetime import date
from time import perf_counter, sleep
from unittest.mock import patch
import numpy as np
import pandas as pd
import streamlit as st
import app as application
from config import StrategyConfig
from src.auth import AuthConfig
from src.journal import OPERATION_COLUMNS, FAVORITE_COLUMNS, calculate_open_positions
from src.session_journal import session_journal

OWNER = 'qa_latency'
ROUTES = ('Inicio', 'Favoritos', 'Posiciones', 'Resultado real', 'Historial', 'Evolución', 'Momentum')

def forbidden(*args, **kwargs):
    raise AssertionError('Network, SMTP or persistent mutation is forbidden in the populated probe')

if 'qa_data' not in st.session_state:
    tickers = [f'ZQA{i:02}' for i in range(30)]
    index = pd.bdate_range(end='2026-10-30', periods=520)
    x = np.arange(len(index))
    def prices(number):
        close = (70. + number) * np.exp(.0006*x + .03*np.sin(x/12. + number))
        return pd.DataFrame(dict(open=close*.998, high=close*1.01, low=close*.99,
            close=close, volume=np.full(len(index), 2_000_000.)), index=index)
    market = {ticker: prices(number) for number, ticker in enumerate(tickers)}
    info = {ticker: dict(symbol=ticker, shortName='Synthetic Company ' + ticker,
        sector='Technology', country='United States', currency='USD', quoteType='EQUITY',
        returnOnEquity=.22, returnOnInvestedCapital=.18, profitMargins=.18,
        operatingMargins=.21, grossMargins=.58, revenueGrowth=.20, earningsGrowth=.24,
        debtToEquity=25., currentRatio=1.8, freeCashflow=500_000_000.,
        operatingCashflow=650_000_000., marketCap=8_000_000_000., totalCash=1_000_000_000.,
        forwardPE=22., trailingPE=25., pegRatio=1.2, priceToBook=3.) for ticker in tickers}
    op_rows = []
    for number, day in enumerate(pd.bdate_range('2026-01-05', periods=180)):
        row = {column: None for column in OPERATION_COLUMNS}
        row.update(id=number+1, ticker=tickers[number % 18], account_name='Synthetic broker',
            side='Compra', quantity=1., price=80., fees=1., executed_at=day.isoformat(),
            currency='USD', recorded_by=OWNER, notes='Synthetic only', created_at=day.isoformat())
        op_rows.append(row)
    operations = pd.DataFrame(op_rows, columns=OPERATION_COLUMNS)
    favorites = pd.DataFrame([dict(id=number+1, ticker=tickers[number] if number < 30 else f'ZQB{number:03}',
        name='Synthetic Company ' + str(number), exchange='NASDAQ', tags='', recorded_by=OWNER,
        created_at='2026-10-01') for number in range(150)], columns=FAVORITE_COLUMNS)
    st.session_state['qa_data'] = (market, info, {'SPY': prices(1), 'QQQ': prices(2), 'XLK': prices(3)}, operations, favorites)
    st.session_state['qa_reads'] = Counter()
    st.session_state['qa_computations'] = Counter()
    st.session_state['qa_compute_seconds'] = Counter()
    st.session_state['qa_route'] = '__INITIAL_ROUTE__'

market, info, references, operations, favorites = st.session_state['qa_data']

class Journal:
    backend_name = 'Synthetic with injected latency'
    def __init__(self, owner): self.owner = owner
    def read(self, name, value):
        st.session_state['qa_reads'][self.owner + ':' + name] += 1
        sleep(__LATENCY__)
        return value
    def list_operations(self):
        return self.read('operations', operations if self.owner == OWNER else operations.iloc[:0])
    def open_positions(self):
        value = calculate_open_positions(operations if self.owner == OWNER else operations.iloc[:0])
        return self.read('positions', value)
    def list_favorites(self):
        return self.read('favorites', favorites if self.owner == OWNER else favorites.iloc[:30])
    def list_portfolio_snapshot_positions(self): return self.read('snapshots', pd.DataFrame())
    def list_portfolio_imports(self): return self.read('imports', pd.DataFrame())
    def list_private_investments(self): return self.read('private', pd.DataFrame())
    def list_portfolio_accounts(self): return self.read('accounts', pd.DataFrame())
    def list_analysis_snapshots(self): return self.read('analysis_snapshots', pd.DataFrame())
    def __getattr__(self, name):
        if name.startswith(('add_', 'save_', 'delete_', 'upsert_', 'replace_', 'update_')):
            return forbidden
        raise AttributeError(name)

if 'qa_journals' not in st.session_state:
    st.session_state['qa_journals'] = {owner: Journal(owner) for owner in (OWNER, 'qa_group')}
private = session_journal(st.session_state['qa_journals'][OWNER], st.session_state, ttl_seconds=600)
group = session_journal(st.session_state['qa_journals']['qa_group'], st.session_state, ttl_seconds=600)
route = st.radio('Probe route', ROUTES, key='qa_route', horizontal=True)
strategy = StrategyConfig()
fx = application.FxSnapshot(as_of=date(2026,10,30), rates_per_eur={'EUR':1., 'USD':1.1})
prepare_original = application.prepare_data
enrich_original = application._enrich_current_analysis_policy
signal_original = application.evaluate_latest_signal
growth_original = application.evaluate_growth_momentum
history_original = application.historical_forward_return_study
portfolio_original = application.build_position_dashboard

def counted(name, function):
    def wrapper(*args, **kwargs):
        st.session_state['qa_computations'][name] += 1
        started = perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            st.session_state['qa_compute_seconds'][name] += perf_counter() - started
    return wrapper

with ExitStack() as patches:
    patches.enter_context(patch('requests.sessions.Session.request', side_effect=forbidden))
    patches.enter_context(patch('smtplib.SMTP', side_effect=forbidden))
    patches.enter_context(patch('smtplib.SMTP_SSL', side_effect=forbidden))
    for name in ('cached_download', 'cached_fundamentals', 'load_market_data', 'run_daily_alerts', 'persist_analysis_batch_snapshots'):
        patches.enter_context(patch.object(application, name, side_effect=forbidden))
    for name, function, label in (
        ('prepare_data', prepare_original, 'prepare'),
        ('_enrich_current_analysis_policy', enrich_original, 'enrich'),
        ('evaluate_latest_signal', signal_original, 'signals'),
        ('evaluate_growth_momentum', growth_original, 'growth'),
        ('historical_forward_return_study', history_original, 'forward_study'),
        ('build_position_dashboard', portfolio_original, 'dashboard')):
        patches.enter_context(patch.object(application, name, side_effect=counted(label, function)))
    private_favorites, group_favorites = private.list_favorites(), group.list_favorites()
    if route == 'Favoritos':
        application.render_favorites_manager(private, group, private_favorites, group_favorites,
            actor_username=OWNER, is_admin=False, favorite_view='Mis listas')
    else:
        prepared, summary, fundamentals, valuations, relatives, risks, opportunities = application._analysis_view_context(
            market, info, references, strategy, private)
        if route == 'Inicio':
            user = AuthConfig(username=OWNER, password_hash='synthetic', display_name='Synthetic')
            application.render_home(user, private, group, prepared, references, summary, fx,
                private_favorites, group_favorites)
        elif route == 'Momentum':
            application.render_growth_momentum_page(prepared, info, references, relatives, risks, fx,
                private, private_favorites, group_favorites, OWNER, comparative_summary=summary)
        else:
            sections = {'Posiciones':'Posiciones analizadas', 'Resultado real':'Resultado real aproximado',
                'Historial':'Historial', 'Evolución':'Evolución por años'}
            st.session_state['probe_journal_navigation'] = sections[route]
            application.render_journal(prepared, fundamentals, opportunities, strategy, fx, private,
                view_key='probe', title='Synthetic populated account', description='Offline only', actor_username=OWNER)
'''


def populated_page(route="Inicio", *, storage_latency=0.003):
    script = SCRIPT.replace("__INITIAL_ROUTE__", route).replace("__LATENCY__", repr(float(storage_latency)))
    return AppTest.from_string(script, default_timeout=45)


def assert_ok(page):
    assert not page.exception, [item.message for item in page.exception]
    assert not page.error, [item.value for item in page.error]


def benchmark_routes(*, repeats=3, storage_latency=0.05, routes=ROUTES):
    """Return measured cold/warm server render times; never hit a real backend."""
    page = populated_page(storage_latency=storage_latency)
    started = perf_counter()
    page.run()
    assert_ok(page)
    initial = perf_counter() - started
    output = {"initial_seconds": initial, "storage_latency_seconds": storage_latency, "routes": {}}
    for route in routes:
        durations = []
        before_reads = dict(page.session_state["qa_reads"])
        before_compute = dict(page.session_state["qa_computations"])
        before_seconds = dict(page.session_state["qa_compute_seconds"])
        for _ in range(repeats):
            started = perf_counter()
            page.radio(key="qa_route").set_value(route).run()
            assert_ok(page)
            durations.append(perf_counter() - started)
        after_reads = dict(page.session_state["qa_reads"])
        after_compute = dict(page.session_state["qa_computations"])
        after_seconds = dict(page.session_state["qa_compute_seconds"])
        output["routes"][route] = dict(seconds=durations, p50_seconds=median(durations),
            storage_calls=sum(after_reads.values()) - sum(before_reads.values()),
            calculation_seconds={key: after_seconds.get(key, 0) - before_seconds.get(key, 0)
                for key in set(before_seconds) | set(after_seconds)},
            calculations={key: after_compute.get(key, 0) - before_compute.get(key, 0)
                for key in set(before_compute) | set(after_compute)})
    return output


def test_populated_navigation_reuses_analysis_and_storage_without_network_or_writes():
    page = populated_page().run()
    assert_ok(page)
    assert page.session_state["qa_computations"]["prepare"] == 1
    initial_reads = dict(page.session_state["qa_reads"])
    page.run()
    assert_ok(page)
    assert dict(page.session_state["qa_reads"]) == initial_reads
    page.radio(key="qa_route").set_value("Favoritos").run()
    assert_ok(page)
    page.radio(key="qa_route").set_value("Inicio").run()
    assert_ok(page)
    counts = page.session_state["qa_computations"]
    assert counts["prepare"] == counts["enrich"] == 1
    assert page.session_state["_analysis_view_cache_hit"] is True


def test_populated_account_sections_are_lazy_and_do_not_repeat_base_analysis():
    page = populated_page(route="Historial").run()
    assert_ok(page)
    before = dict(page.session_state["qa_computations"])
    assert before.get("forward_study", 0) == before.get("dashboard", 0) == 0
    page.radio(key="qa_route").set_value("Posiciones").run()
    assert_ok(page)
    positions = dict(page.session_state["qa_computations"])
    assert positions["prepare"] == positions["enrich"] == 1
    assert positions["dashboard"] == 1 and positions["forward_study"] == 1
    assert positions["signals"] - before["signals"] == 18
    page.radio(key="qa_route").set_value("Historial").run()
    assert_ok(page)
    assert dict(page.session_state["qa_computations"]) == positions


def test_other_populated_real_renderers_reuse_the_same_base_context():
    page = populated_page().run()
    assert_ok(page)
    for route in ("Resultado real", "Evolución", "Momentum"):
        page.radio(key="qa_route").set_value(route).run()
        assert_ok(page)
    counts = page.session_state["qa_computations"]
    assert counts["prepare"] == counts["enrich"] == 1
    assert len(page.session_state["qa_data"][0]) == 30
    assert len(page.session_state["qa_data"][3]) == 180
