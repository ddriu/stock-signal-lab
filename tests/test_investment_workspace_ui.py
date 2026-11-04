"""Synthetic read-only screens: opening them never reviews, trades or emails."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from src.daily_digest import seal_daily_score_report
from src.analysis_policy import build_policy_snapshot, encode_policy_note
from src.investment_workspace_ui import _note


DAY = date(2026, 11, 5)


def report(*, portfolio=1, favorites=1, waiting=0):
    def row(ticker, held=False, candidate=True):
        return dict(ticker=ticker, company_name="Compañía " + ticker, held=held,
            score=88 if candidate else 35, label="Entrada fuerte" if candidate else "Esperar",
            status="entry_strong" if candidate else "wait", session="2026-11-04",
            recorded_at="2026-11-04T22:00:00+00:00", source="Fuente sintética",
            quality={"status":"ok", "block_entry":False, "reasons":[]},
            chart_comment="Mantiene la tendencia observada.", next_step="Comprobar riesgo y costes.")
    return seal_daily_score_report(dict(as_of="2026-11-04", calendar_status="complete",
        rows=[row("HELD" + str(i), True) for i in range(portfolio)]
             + [row("NEW" + str(i)) for i in range(favorites)]
             + [row("WAIT" + str(i), candidate=False) for i in range(waiting)]))


def screen(contents, *, home=False, read=False, failure=False, snapshots=None):
    function = "render_investment_home" if home else "render_investment_workspace"
    script = '''
from datetime import date
from copy import deepcopy
from unittest.mock import patch
import streamlit as st
from src.investment_workspace_ui import FUNCTION
REPORT = CONTENTS
class Journal:
    def get_latest_daily_score_report(self):
        st.session_state['reads'] = st.session_state.get('reads', 0) + 1
        if FAILURE:
            raise RuntimeError('private credential must never be displayed')
        return deepcopy(REPORT)
    def __getattr__(self, name):
        raise AssertionError('Forbidden journal access: '+name)
st.session_state.setdefault('actions', [])
def on_action(name, ticker):
    st.session_state['actions'].append((name,ticker))
with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')), \\
     patch('src.email_sender.send_email', side_effect=AssertionError('No email')), \\
     patch('src.signal_engine.evaluate_latest_signal', side_effect=AssertionError('No new signals')), \\
     patch('src.growth_momentum.evaluate_growth_momentum', side_effect=AssertionError('No new growth')):
    FUNCTION(Journal(), report=None if READ else REPORT, on_action=on_action,
        snapshots=SNAPSHOTS, reference_date=date(2026,11,5))
'''.replace("FUNCTION", function).replace("CONTENTS", repr(contents)).replace("FAILURE", repr(failure)).replace("READ", repr(read)).replace("SNAPSHOTS", repr(snapshots))
    return AppTest.from_string(script, default_timeout=25).run()


def test_workspace_uses_one_saved_report_without_any_other_journal_reads_or_side_effects():
    contents = report()
    before = deepcopy(contents)
    page = screen(contents)
    assert not page.exception and not page.error
    assert page.session_state["actions"] == []
    assert contents == before
    headings = [item.value for item in page.markdown]
    assert headings.index("### Mi cartera en este plazo") < headings.index("### Posibles entradas")
    assert any("Planes por verificar o definir" in item for item in headings)
    assert any("Compañía HELD0" in item.value for item in page.text)
    assert any("Compañía NEW0" in item.value for item in page.text)
    captions = " ".join(item.value for item in page.caption)
    assert "Fuente sintética" in captions and "2026-11-04" in captions
    assert "Crecimiento + momentum" in captions


def test_home_has_three_priorities_and_three_horizon_entrances_not_the_whole_workspace():
    page = screen(report(portfolio=20, favorites=20, waiting=100), home=True)
    assert not page.exception and not page.error
    assert not page.text  # One concise plan notice, not three identical warnings.
    assert any("20 posiciones" in item.value for item in page.info)
    assert not page.radio and not page.dataframe
    labels = [button.label for button in page.button]
    assert all(label in labels for label in ["Abrir días", "Abrir semanas", "Abrir largo plazo"])
    assert not any("Posibles entradas" in item.value for item in page.markdown)
    assert page.session_state["actions"] == []


def test_expanded_large_groups_render_only_one_selected_extra_company():
    page = screen(report(portfolio=20, favorites=20, waiting=150))
    assert not page.exception and not page.error
    # Expanders execute eagerly in Streamlit. Bound the rendered cards anyway.
    assert len(page.text) <= 13
    assert len(page.selectbox) <= 3
    assert any("En espera o pendientes" in item.label for item in page.expander)


def test_callbacks_only_request_navigation_and_preserve_the_selected_company():
    page = screen(report())
    page.button(key="investment_weeks_unassigned_0").click().run()
    assert not page.exception
    assert page.session_state["actions"] == [("company","HELD0")]
    page.button(key="investment_workspace_results").click().run()
    assert page.session_state["actions"][-1] == ("results",None)


def test_home_horizon_callback_carries_the_horizon_key_only():
    page = screen(report(), home=True)
    page.button(key="investment_home_days").click().run()
    assert not page.exception
    assert page.session_state["actions"] == [("horizon","days")]


def test_unverified_holding_can_request_its_plan_without_saving_anything():
    page = screen(report())
    page.button(key="investment_weeks_unassigned_0_plan").click().run()
    assert not page.exception
    assert page.session_state["actions"] == [("plan","HELD0")]


def test_home_can_request_all_unverified_plans_without_saving_anything():
    page = screen(report(portfolio=20), home=True)
    page.button(key="investment_home_plans").click().run()
    assert not page.exception
    assert page.session_state["actions"] == [("plan", None)]


def test_long_term_research_is_separate_from_buy_candidates_and_has_no_combined_score():
    contents = report(portfolio=0)
    row = contents["rows"][0]
    policy = build_policy_snapshot({"Ticker": row["ticker"], "Fecha": row["session"],
        "Fuente": "Fuente sintética", "Precio": 100, "Moneda": "USD",
        "Calidad empresa": 72, "Valoración": 61, "Riesgo controlado": 70},
        growth_result=SimpleNamespace(score=88, label="Entrada fuerte", growth_score=80,
            momentum_score=90, context_score=80, confidence_pct=90, sector_key="technology",
            as_of=row["session"], positive_factors=(), risk_factors=(), manual_checks=()),
        position_context={"horizon": "No documentado", "strategy_id": "growth_momentum"}, held=False)
    row["snapshot_id"] = policy["snapshot_id"]
    contents = seal_daily_score_report(contents)
    snapshots = [{"ticker": row["ticker"], "analyzed_at": row["session"],
        "created_at": row["recorded_at"], "price": 100, "entry_score": 88,
        "entry_label": "Entrada fuerte", "note": encode_policy_note("", policy)}]
    page = screen(contents, snapshots=snapshots)
    page.radio(key="investment_horizon").set_value("long_term").run()
    assert not page.exception and not page.error
    assert any("Negocios para estudiar · no son compras confirmadas" in item.value for item in page.markdown)
    assert any(item.value == "Estudiar empresa" for item in page.markdown)
    captions = " ".join(item.value for item in page.caption)
    assert "Sin nota conjunta" in captions
    assert "Calidad 72/100" in captions and "Valoración 61/100" in captions
    assert "Riesgo controlado 70/100" in captions
    assert not any("Estudiar entrada" == item.value for item in page.markdown)
    assert len(page.text) == 1  # Not repeated again inside the pending section.


def test_reader_fetches_only_the_latest_archived_report_once():
    page = screen(report(), read=True, home=True)
    assert not page.exception and not page.error
    assert page.session_state["reads"] == 1


def test_missing_report_is_unavailable_instead_of_zero_opportunities():
    page = screen({}, home=True)
    assert not page.exception and not page.error
    assert any("N/D" in warning.value and "no cero" in warning.value for warning in page.warning)
    assert sum("Posibilidades que revisar: N/D" in caption.value for caption in page.caption) == 3


def test_failed_saved_read_does_not_show_credentials_or_reconstruct_another_report():
    page = screen(report(), read=True, failure=True)
    assert not page.exception and page.error
    assert "private credential" not in " ".join(item.value for item in page.error)
    assert page.session_state["actions"] == []


def test_days_are_explicitly_not_intraday_and_long_term_has_no_automatic_momentum_buy():
    page = screen(report())
    page.radio(key="investment_horizon").set_value("days").run()
    assert not page.exception
    assert any("No hay datos intradía" in item.value for item in page.markdown)
    page.radio(key="investment_horizon").set_value("long_term").run()
    assert not page.exception
    assert not any("Estudiar entrada" == item.value for item in page.markdown)
    assert any("Una mala entrada actual no obliga a vender" in item.value for item in page.caption)


@pytest.mark.parametrize("value,expected",[(None,"N/D"),(True,"N/D"),(float("nan"),"N/D"),
    (float("inf"),"N/D"),(0,"0/100"),(88,"88/100")])
def test_unknown_and_valid_zero_display_differ(value, expected):
    assert _note(value) == expected
