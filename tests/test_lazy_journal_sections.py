"""A visible account section must not execute the six hidden account panels."""
import pytest
from streamlit.testing.v1 import AppTest


SCRIPT = '''
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import app
from config import StrategyConfig

class Journal:
    backend_name = 'Synthetic'
    def list_operations(self):
        if CHOICE == 'Comprar / vender': raise AssertionError('Hidden operation query')
        return pd.DataFrame()
    def open_positions(self):
        if CHOICE in ('Comprar / vender', 'Historial', 'Resultado real aproximado'):
            raise AssertionError('Hidden position calculation')
        return pd.DataFrame()
    def list_private_investments(self): return pd.DataFrame()
    def list_portfolio_accounts(self): return pd.DataFrame()

def forbidden(*args, **kwargs): raise AssertionError('Hidden journal panel executed')
def visible(*args, **kwargs): st.write('VISIBLE=' + CHOICE)

st.session_state['private_journal_navigation'] = CHOICE
allowed = {
    'Comprar / vender': {'render_operation_form'},
    'Resultado real aproximado': {'render_approximate_returns','render_historical_closure_form'},
    'Mi cartera actual': {'render_private_investments'},
    'Evolución por años': {'render_portfolio_evolution'},
}.get(CHOICE, set())
with ExitStack() as patches:
    for name in ('render_operation_form','render_approximate_returns', 'render_historical_closure_form',
                 'render_private_investments','render_portfolio_evolution', 'historical_forward_return_study'):
        patches.enter_context(patch.object(app,name,side_effect=visible if name in allowed else forbidden))
    patches.enter_context(patch.object(app,'build_position_dashboard',return_value=(pd.DataFrame(),SimpleNamespace())))
    app.render_journal({}, {}, {}, StrategyConfig(), SimpleNamespace(rates_per_eur={'EUR':1.}), Journal(),
        view_key='private', title='Account', description='Synthetic', actor_username='ddriu')
'''


@pytest.mark.parametrize("choice", ["Comprar / vender", "Historial", "Resultado real aproximado",
    "Mi cartera actual", "Evolución por años", "Posiciones analizadas", "Comparar un cambio"])
def test_only_the_selected_journal_panel_runs(choice):
    page = AppTest.from_string(SCRIPT.replace("CHOICE", repr(choice)), default_timeout=30).run()
    assert not page.exception, [item.message for item in page.exception]
    assert not page.error
    if choice in {"Comprar / vender", "Resultado real aproximado", "Mi cartera actual", "Evolución por años"}:
        assert any("VISIBLE=" + choice in str(item.value) for item in page.markdown)
