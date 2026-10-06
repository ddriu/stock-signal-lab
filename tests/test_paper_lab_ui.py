"""Critical home/lab flows against an isolated SQLite journal, without network."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.journal import TradingJournal


def _fixture_journal(path: Path, *, market_date: pd.Timestamp | None = None) -> tuple[TradingJournal, pd.Timestamp]:
    journal = TradingJournal(path, owner="qa")
    # Last completed weekday: do not imply that today's intraday OHLC is a close.
    if market_date is None:
        market_date = pd.bdate_range(
            end=date.today() - timedelta(days=1), periods=1,
        )[0]
    journal.replace_portfolio_snapshot_positions(
        pd.DataFrame(
            [
                {
                    "platform": "Trade Republic", "asset_name": "Company A",
                    "asset_type": "Acción", "analysis_ticker": "AAA",
                    "quantity": 10, "price_currency": "EUR",
                    "value_eur": 1_000, "cost_estimate_eur": 900,
                    "gain_loss_eur": 100,
                },
                {
                    "platform": "Revolut", "asset_name": "Company B",
                    "asset_type": "Acción", "analysis_ticker": "BBB",
                    "quantity": 5, "price_currency": "EUR",
                    "value_eur": 500, "cost_estimate_eur": 450,
                    "gain_loss_eur": 50,
                },
            ]
        ),
        snapshot_date=market_date.date(),
    )
    return journal, market_date


def _home_script(
    path: Path, market_date: pd.Timestamp, *, missing_b: bool = False, session_now: str | None = None,
) -> str:
    return '''
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import app as application
from src.data_sources import FxSnapshot
from src.journal import TradingJournal
from src.market_data_quality import us_daily_session_is_closed

journal = TradingJournal(__PATH__, owner="qa")
empty_group = TradingJournal(__GROUP_PATH__, owner="group-qa")
market_date = pd.Timestamp(__DATE__)
index = pd.bdate_range(end=market_date, periods=5)
def prices(close, currency="EUR"):
    frame = pd.DataFrame({
        "open": [close] * len(index), "high": [close * 1.01] * len(index),
        "low": [close * .99] * len(index), "close": [close] * len(index),
        "volume": [1_000_000] * len(index),
    }, index=index)
    frame.attrs["display_currency"] = currency
    return frame

prepared = {"AAA": prices(100)}
if not __MISSING_B__:
    prepared["BBB"] = prices(100)
reference = {"SPY": prices(500, "USD")}
session_now = __SESSION_NOW__
with ExitStack() as patches:
    patches.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call")))
    if session_now is not None:
        patches.enter_context(patch.object(application, "us_daily_session_is_closed", side_effect=lambda session_date: us_daily_session_is_closed(session_date, now=session_now)))
    application.render_home(
        SimpleNamespace(display_name="QA", username="qa"), journal, empty_group,
        prepared, reference, [],
        FxSnapshot(market_date.date(), {"EUR": 1.0, "USD": 1.0}),
        pd.DataFrame(), pd.DataFrame(),
    )
'''.replace("__PATH__", repr(str(path))).replace(
        "__GROUP_PATH__", repr(str(path.with_name("group-qa.db")))
    ).replace("__DATE__", repr(market_date.isoformat())).replace("__MISSING_B__", repr(missing_b)).replace(
        "__SESSION_NOW__", repr(session_now),
    )


def _assert_ui_ok(app: AppTest) -> None:
    assert not app.exception, [item.message for item in app.exception]
    assert not app.error, [item.value for item in app.error]


def _metric(app: AppTest, label: str) -> str:
    return str(next(item.value for item in app.metric if item.label == label))


def test_real_home_panels_and_actions_are_safe_after_widgets_exist(tmp_path: Path) -> None:
    journal, market_date = _fixture_journal(tmp_path / "qa.db")
    app = AppTest.from_string(_home_script(journal.database_path, market_date), default_timeout=30).run()
    _assert_ui_ok(app)
    assert journal.list_paper_simulations().empty

    for view in ("Decisiones", "Accesos", "Resumen", "Laboratorio", "Resumen", "Decisiones"):
        app.segmented_control(key="home_view_navigation").set_value(view).run()
        _assert_ui_ok(app)
    for horizon in ("Diario", "Semanal", "Mensual", "Anual"):
        app.segmented_control(key="home_rotation_horizon").set_value(horizon).run()
        _assert_ui_ok(app)

    app.button(key="home_complete_review").click().run()
    _assert_ui_ok(app)
    assert app.session_state["_requested_main_navigation"] == "Analizar"
    assert app.session_state["_requested_analysis_navigation"] == "Radar"
    assert app.session_state["_pending_speculative_discovery"] is True
    assert app.session_state["_force_all_favorite_refresh"] is True

    app.segmented_control(key="home_view_navigation").set_value("Accesos").run()
    next(item for item in app.button if item.label == "Abrir mi cartera").click().run()
    _assert_ui_ok(app)
    assert app.session_state["_requested_main_navigation"] == "Carteras"
    assert app.session_state["_requested_portfolio_navigation"] == "Privada"
    assert journal.list_operations().empty
    assert journal.list_paper_simulations().empty


@pytest.mark.parametrize("missing_b", [False, True])
def test_real_lab_start_persists_both_accounts_and_reentry_is_idempotent(
    tmp_path: Path, missing_b: bool,
) -> None:
    journal, market_date = _fixture_journal(tmp_path / "qa.db")
    before = journal.list_portfolio_snapshot_positions()
    app = AppTest.from_string(
        _home_script(journal.database_path, market_date, missing_b=missing_b), default_timeout=30,
    ).run()
    app.segmented_control(key="home_view_navigation").set_value("Laboratorio").run()
    _assert_ui_ok(app)
    app.button(key="paper_start_simulation").click().run()
    _assert_ui_ok(app)

    simulations = journal.list_paper_simulations(status="active")
    assert len(simulations) == 1
    assert simulations.iloc[0]["initial_nav_eur"] == 1_500
    simulation_id = int(simulations.iloc[0]["id"])
    runs = journal.list_paper_daily_runs(simulation_id)
    assert len(runs) == 1
    assert runs.iloc[0]["net_nav_eur"] == 1_500
    if missing_b:
        assert runs.iloc[0]["coverage_pct"] == pytest.approx(2 / 3 * 100)
        assert runs.iloc[0]["hold_coverage_pct"] == pytest.approx(2 / 3 * 100)
        assert runs.iloc[0]["status"] == "partial"
        assert _metric(app, "Frente a mantener") == "N/D"
        assert _metric(app, "Frente a S&P 500") == "N/D"
        assert any("drawdown máximo N/D" in item.value for item in app.caption)
        assert any("comparación está incompleta" in item.value for item in app.warning)
    else:
        assert runs.iloc[0]["coverage_pct"] == 100
        assert runs.iloc[0]["hold_coverage_pct"] == 100
        assert runs.iloc[0]["benchmark_coverage_pct"] == 100
        assert runs.iloc[0]["status"] == "complete"
        assert _metric(app, "Frente a mantener") == "+0.00 pp"

    app.segmented_control(key="home_view_navigation").set_value("Resumen").run()
    app.segmented_control(key="home_view_navigation").set_value("Laboratorio").run()
    _assert_ui_ok(app)
    assert len(journal.list_paper_daily_runs(simulation_id)) == 1
    assert len(journal.list_paper_simulations(status="active")) == 1
    assert journal.list_operations().empty
    pd.testing.assert_frame_equal(before, journal.list_portfolio_snapshot_positions())


def test_legacy_lab_schema_is_migrated_and_unknown_reference_coverage_is_not_a_win(tmp_path: Path) -> None:
    journal, market_date = _fixture_journal(tmp_path / "qa.db")
    simulation_id = journal.create_paper_simulation(
        name="Legacy QA", start_date=market_date.date(), initial_nav_eur=1_500,
        initial_positions=[
            {"ticker": "AAA", "quantity": 10, "price_eur": 100, "cost_basis_eur": 900},
            {"ticker": "BBB", "quantity": 5, "price_eur": 100, "cost_basis_eur": 450},
        ],
        assumptions={"benchmark_initial_price_eur": 500},
        engine_version="paper-v2",
    )
    journal.upsert_paper_daily_run(
        simulation_id=simulation_id, market_date=market_date.date(), signal_as_of=None,
        input_hash="legacy-qa", positions_after=[], cash_eur=1_500,
        net_nav_eur=1_500, hold_nav_eur=1_450, benchmark_nav_eur=1_450, coverage_pct=100,
    )
    with sqlite3.connect(journal.database_path) as connection:
        connection.execute("ALTER TABLE paper_daily_runs DROP COLUMN hold_coverage_pct")
        connection.execute("ALTER TABLE paper_daily_runs DROP COLUMN benchmark_coverage_pct")

    app = AppTest.from_string(_home_script(journal.database_path, market_date), default_timeout=30).run()
    app.segmented_control(key="home_view_navigation").set_value("Laboratorio").run()
    _assert_ui_ok(app)
    assert _metric(app, "Frente a mantener") == "N/D"
    assert _metric(app, "Frente a S&P 500") == "N/D"
    assert any("comparación está incompleta" in item.value for item in app.warning)
    migrated = TradingJournal(journal.database_path, owner="qa").list_paper_daily_runs(simulation_id)
    assert len(migrated) == 1
    assert pd.isna(migrated.iloc[0]["hold_coverage_pct"])
    assert pd.isna(migrated.iloc[0]["benchmark_coverage_pct"])


def test_partial_season_rebuild_includes_both_accounts_and_archives_only_virtual_history(tmp_path: Path) -> None:
    journal, market_date = _fixture_journal(tmp_path / "qa.db")
    before = journal.list_portfolio_snapshot_positions()
    partial_id = journal.create_paper_simulation(
        name="Only one account QA", start_date=market_date.date(), initial_nav_eur=1_000,
        initial_positions=[
            {"ticker": "AAA", "quantity": 10, "price_eur": 100, "cost_basis_eur": 900},
        ],
        assumptions={"benchmark_initial_price_eur": 500}, engine_version="paper-v2",
    )
    app = AppTest.from_string(_home_script(journal.database_path, market_date), default_timeout=30).run()
    app.segmented_control(key="home_view_navigation").set_value("Laboratorio").run()
    assert not app.exception
    assert any("temporada activa es parcial" in item.value for item in app.error)
    assert not app.button(key="paper_rebuild_complete_portfolio").disabled

    app.button(key="paper_rebuild_complete_portfolio").click().run()
    _assert_ui_ok(app)
    simulations = journal.list_paper_simulations()
    assert len(simulations) == 2
    assert simulations.loc[simulations["id"] == partial_id, "status"].iloc[0] == "archived"
    active = simulations.loc[simulations["status"] == "active"]
    assert len(active) == 1
    assert active.iloc[0]["initial_nav_eur"] == 1_500
    assert len(journal.list_paper_daily_runs(int(active.iloc[0]["id"]))) == 1
    assert journal.list_operations().empty
    pd.testing.assert_frame_equal(before, journal.list_portfolio_snapshot_positions())


def test_intraday_bar_cannot_start_a_virtual_season_or_change_real_positions(tmp_path: Path) -> None:
    market_date = pd.bdate_range(end=date.today(), periods=1)[0]
    journal, market_date = _fixture_journal(tmp_path / "qa.db", market_date=market_date)
    before = journal.list_portfolio_snapshot_positions()
    clock = pd.Timestamp(f"{market_date.date().isoformat()}T10:00:00", tz="America/New_York")
    app = AppTest.from_string(
        _home_script(journal.database_path, market_date, session_now=clock.isoformat()), default_timeout=30,
    ).run()
    app.segmented_control(key="home_view_navigation").set_value("Laboratorio").run()
    _assert_ui_ok(app)

    assert any("16:30" in item.value for item in app.warning)
    assert not any(item.key == "paper_start_simulation" for item in app.button)
    assert journal.list_paper_simulations().empty
    assert journal.list_operations().empty
    pd.testing.assert_frame_equal(before, journal.list_portfolio_snapshot_positions())

    closed_clock = pd.Timestamp(f"{market_date.date().isoformat()}T16:30:00", tz="America/New_York")
    after_close = AppTest.from_string(
        _home_script(journal.database_path, market_date, session_now=closed_clock.isoformat()), default_timeout=30,
    ).run()
    after_close.segmented_control(key="home_view_navigation").set_value("Laboratorio").run()
    _assert_ui_ok(after_close)
    assert after_close.button(key="paper_start_simulation")


def test_intraday_bar_preserves_existing_lab_history_without_recording_a_session(tmp_path: Path) -> None:
    market_date = pd.bdate_range(end=date.today(), periods=1)[0]
    previous_date = pd.bdate_range(end=market_date.date() - timedelta(days=1), periods=1)[0]
    journal, market_date = _fixture_journal(tmp_path / "qa.db", market_date=market_date)
    simulation_id = journal.create_paper_simulation(
        name="Existing closed session QA", start_date=previous_date.date(), initial_nav_eur=1_500,
        initial_positions=[
            {"ticker": "AAA", "quantity": 10, "price_eur": 100, "cost_basis_eur": 900},
            {"ticker": "BBB", "quantity": 5, "price_eur": 100, "cost_basis_eur": 450},
        ],
        assumptions={"benchmark_initial_price_eur": 500}, engine_version="paper-v2",
    )
    journal.upsert_paper_daily_run(
        simulation_id=simulation_id, market_date=previous_date.date(), signal_as_of=None,
        input_hash="closed-qa", positions_after=[], cash_eur=1_500,
        net_nav_eur=1_500, hold_nav_eur=1_500, benchmark_nav_eur=1_500,
        coverage_pct=100, hold_coverage_pct=100, benchmark_coverage_pct=100,
    )
    before = journal.list_paper_daily_runs(simulation_id)
    clock = pd.Timestamp(f"{market_date.date().isoformat()}T16:29:59", tz="America/New_York")
    app = AppTest.from_string(
        _home_script(journal.database_path, market_date, session_now=clock.isoformat()), default_timeout=30,
    ).run()
    app.segmented_control(key="home_view_navigation").set_value("Laboratorio").run()
    _assert_ui_ok(app)

    assert any("16:30" in item.value for item in app.warning)
    assert any(previous_date.date().isoformat() in item.value for item in app.caption)
    pd.testing.assert_frame_equal(before, journal.list_paper_daily_runs(simulation_id))
    assert journal.list_operations().empty


def test_record_guard_rejects_unclosed_session_before_any_trade_or_storage_call() -> None:
    script = '''
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import streamlit as st
import app as application

def forbidden(*args, **kwargs):
    raise AssertionError("The intraday guard must run before state, trades or writes")

with ExitStack() as patches:
    patches.enter_context(patch.object(application, "us_daily_session_is_closed", return_value=False))
    for name in ("_paper_assumptions_from_row", "_paper_state_from_history", "fill_pending_orders"):
        patches.enter_context(patch.object(application, name, side_effect=forbidden))
    try:
        application._record_paper_session(
            SimpleNamespace(upsert_paper_daily_run=forbidden), {}, pd.DataFrame(), [], [],
            {}, {}, {}, 500, pd.Timestamp("2026-10-05").date(),
        )
    except ValueError as exc:
        st.success(str(exc))
    else:
        raise AssertionError("An unclosed session was not rejected")
'''
    app = AppTest.from_string(script, default_timeout=30).run()
    _assert_ui_ok(app)
    assert any("16:30" in item.value for item in app.success)
