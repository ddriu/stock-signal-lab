"""Synthetic UI checks: opening a preview never imports or places an order."""

from streamlit.testing.v1 import AppTest


IMPORT_SCRIPT = '''
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import src.reconciled_portfolio_ui as ui

class Journal:
    def list_operations(self):
        return pd.DataFrame()
    def import_reconciled_portfolio(self, bundle, **kwargs):
        st.session_state['imports_called'] = st.session_state.get('imports_called', 0) + 1
        return dict(inserted_positions=1, inserted_operations=1, pending_conflicts=0)

plan = SimpleNamespace(snapshot_date='2026-10-13',source_sha256='a'*64,
    snapshot=SimpleNamespace(positions=pd.DataFrame([{'value_eur':100}])),
    historical_operations=pd.DataFrame([{}]),post_snapshot_operations=pd.DataFrame(),
    inventory=pd.DataFrame([{'sheet':'Synthetic','role':'snapshot'}]),
    events=pd.DataFrame(),issues=pd.DataFrame())
with patch.object(ui.st,'file_uploader',return_value=SimpleNamespace(getvalue=lambda:b'synthetic')),
     patch.object(ui,'parse_reconciled_workbook_excel',return_value=plan),
     patch.object(ui,'prepare_reconciled_workbook_bundle',return_value={'metadata':{}}):
    ui.render_reconciled_workbook_import(Journal(),key='test')
'''.replace("with patch.object", "with (patch.object").replace("return_value={'metadata':{}}):", "return_value={'metadata':{}})):")


def test_workbook_preview_is_read_only_and_import_requires_confirmation():
    app = AppTest.from_string(IMPORT_SCRIPT, default_timeout=30).run()
    assert not app.exception
    assert 'imports_called' not in app.session_state
    save = next(button for button in app.button if button.label == 'Guardar importación conciliada')
    assert save.disabled
    app.checkbox[0].check().run()
    assert not app.exception
    assert 'imports_called' not in app.session_state
    assert not next(button for button in app.button if button.label == 'Guardar importación conciliada').disabled


def test_confirmed_save_executes_only_the_import_and_rerender_does_not_repeat_it():
    app = AppTest.from_string(IMPORT_SCRIPT, default_timeout=30).run()
    app.checkbox[0].check().run()
    next(button for button in app.button if button.label == 'Guardar importación conciliada').click().run()
    assert not app.exception
    assert app.session_state['imports_called'] == 1
    app.run()
    assert app.session_state['imports_called'] == 1
