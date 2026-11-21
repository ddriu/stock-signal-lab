"""Explicit runner and actual main-route integration with synthetic owners only.

These are in-process Streamlit AppTest tests, not browser/production evidence.
All transport is forbidden except an injected synthetic SMTP acknowledgment.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from scripts import run_external_analysts as runner
from src.email_sender import EmailConfig
from src.external_analyst_service import activate_comparison, advance_comparison
from src.external_analyst_store import ExternalAnalystStore
from src.journal import TradingJournal


ACTIVATION = "2026-11-12T08:00:00+00:00"
OBSERVED = "2026-11-12T09:01:00+00:00"
CUTOFF = "2026-11-13T18:00:00+00:00"
CONFIG = EmailConfig("smtp.example.invalid", 465, "synthetic-sender", "not-a-real-secret", "test@example.invalid")


def fail_transport(*args, **kwargs):
    raise AssertionError("No real market, SMTP, IMAP or HTTP request is permitted")


@pytest.fixture(autouse=True)
def no_real_transports(monkeypatch):
    import imaplib
    import requests
    import smtplib
    import socket
    import yfinance
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fail_transport)
    monkeypatch.setattr(smtplib, "SMTP", fail_transport)
    monkeypatch.setattr(smtplib, "SMTP_SSL", fail_transport)
    monkeypatch.setattr(socket, "create_connection", fail_transport)
    monkeypatch.setattr(requests.sessions.Session, "request", fail_transport)
    monkeypatch.setattr(yfinance, "download", fail_transport)


@pytest.fixture
def store(tmp_path):
    journal = TradingJournal(tmp_path / "synthetic-runner.db", owner="qa-external-runner")
    journal.get_alert_preferences = lambda: SimpleNamespace(email="test@example.invalid")
    return ExternalAnalystStore(journal)


def seed(store, *, enabled=False):
    activate_comparison(store, now=ACTIVATION)
    store.import_message({"message_id": "qa-synthetic", "provider": "perito_inversor",
        "received_at": OBSERVED, "observed_at": OBSERVED, "announced_count": 2,
        "issues": ["Synthetic sample; no original mail"], "calls": [{
            "source_id": "qa-synthetic", "provider": "perito_inversor", "ticker": "DEMO",
            "company_name": "Empresa sintética", "currency": "EUR", "analyst": "QA analyst",
            "action": "buy", "direction": "long", "entry_rule": "limit", "source_status": "pending",
            "entry": 10., "stop": 9., "target": 12., "reference_price": 10.,
            "source_url": "https://www.peritoinversor.com/#synthetic-only"}]})
    store.save_preferences({"enabled": enabled})


def bar(**changes):
    return {"ticker": "DEMO", "currency": "EUR", "open_at": "2026-11-13T09:00:00+00:00",
            "close_at": "2026-11-13T17:00:00+00:00", "available_at": "2026-11-13T17:20:00+00:00",
            "open": 10., "high": 10.5, "low": 9.5, "close": 10.2, **changes}


def market(candidates, **kwargs):
    return {"bars": [bar()], "issues": [], "sources": [{"provider": "synthetic"}],
            "coverage": {"usable_bars": 1}, "as_of": kwargs["as_of"]}


def snapshot(store):
    return deepcopy((store.list_calls(), store.list_messages(), store.list_revisions(),
                     store.list_events(), store.list_simulation_runs(), store.list_deliveries()))


def test_runner_idle_does_not_poll_download_activate_send_or_mutate(store):
    before = snapshot(store)
    outcome = runner.run_one(store.journal, now=CUTOFF, email_config=CONFIG,
                             market_loader=fail_transport, client_factory=fail_transport, smtp_send=fail_transport)
    assert outcome == {"mailbox": "not_requested", "tracking": "not_requested", "delivery": "not_requested"}
    assert snapshot(store) == before


def test_unactivated_runner_never_downloads_or_creates_a_season(store):
    outcome = runner.run_one(store.journal, now=CUTOFF, advance=True, email_config=CONFIG,
                             market_loader=fail_transport, smtp_send=fail_transport)
    assert outcome["tracking"] == "not_activated"
    assert not store.list_simulation_runs()


def test_active_runner_uses_explicit_earliest_activation_and_four_separate_budgets(store):
    seed(store)
    original = store.list_calls()[0]
    for strategy in "ABC":
        evidence = ({"engine_snapshot_id": "synthetic-frozen", "signal_known_at": OBSERVED}
                    if strategy == "C" else {"evidence_url": "https://example.com/synthetic-earnings",
                                             "evidence_at": OBSERVED,
                                             "beneficiary_relation": "Documented synthetic fixture only"})
        candidate = {**original, **evidence, "strategy": strategy}
        store.save_simulation_run("synthetic-candidate-" + strategy,
            {"kind": "candidate", "observed_at": OBSERVED, "candidate": candidate})
    seen = []
    def collect(candidates, **kwargs):
        seen.append((candidates, kwargs))
        return market(candidates, **kwargs)
    outcome = runner.run_one(store.journal, now=CUTOFF, advance=True, email_config=CONFIG, market_loader=collect)
    assert outcome["tracking"] == "recorded"
    assert seen[0][1] == {"as_of": CUTOFF, "activation_at": ACTIVATION}
    comparison = next(row["payload"] for row in store.list_simulation_runs() if row["payload"]["kind"] == "comparison")
    accounts = comparison["result"]["accounts"]
    assert set(accounts) == set("ABCD")
    assert [accounts[key]["initial_cash_eur"] for key in "ABCD"] == [100.] * 4
    assert all(accounts[key]["open_positions"] for key in "ABCD")
    assert all(accounts[key]["nav_eur"] < 103 for key in "ABCD")
    assert all(accounts[key]["cash_eur"] >= 0 for key in "ABCD")
    assert store.list_deliveries() == []


def test_runner_rerun_is_idempotent_for_fills_events_and_budget(store):
    seed(store)
    first = runner.run_one(store.journal, now=CUTOFF, advance=True, email_config=CONFIG, market_loader=market)
    before = snapshot(store)
    second = runner.run_one(store.journal, now=CUTOFF, advance=True, email_config=CONFIG, market_loader=market)
    assert first == second
    assert snapshot(store) == before


def test_partial_market_failure_remains_visible_in_saved_comparison_and_summary_still_works(store):
    seed(store, enabled=True)
    def partial(*args, **kwargs):
        return {"bars": [], "issues": [{"code": "provider_unavailable", "ticker": "DEMO"}],
                "sources": [], "coverage": {"usable_bars": 0, "has_incidents": True}}
    sent = []
    def smtp(*args, **kwargs):
        sent.append(args)
        return SimpleNamespace(status="smtp_accepted")
    outcome = runner.run_one(store.journal, now=CUTOFF, advance=True, send=True,
                             email_config=CONFIG, market_loader=partial, smtp_send=smtp)
    assert outcome["delivery"] == "smtp_accepted" and len(sent) == 1
    latest = [row["payload"] for row in store.list_simulation_runs() if row["payload"]["kind"] == "comparison"][-1]
    assert latest["market"]["issues"] == [{"code": "provider_unavailable", "ticker": "DEMO"}]
    account = latest["result"]["accounts"]["D"]
    assert account["coverage"]["not_verifiable"] == 1 and not account["metrics"]["reliable"]


def test_loader_exception_does_not_cancel_independent_opted_in_summary(store):
    seed(store, enabled=True)
    def unavailable(*args, **kwargs):
        raise TimeoutError("provider error must not leak transport URLs")
    sent = []
    def smtp(*args, **kwargs):
        sent.append(args)
        return SimpleNamespace(status="smtp_accepted")
    outcome = runner.run_one(store.journal, now=CUTOFF, advance=True, send=True,
                             email_config=CONFIG, market_loader=unavailable, smtp_send=smtp)
    assert outcome["tracking"] != "recorded"
    assert outcome["delivery"] == "smtp_accepted" and len(sent) == 1
    assert "provider error" not in str(outcome)


def test_invalid_partial_bar_is_frozen_as_incident_not_a_later_win(store):
    seed(store)
    broken = bar(data_issue="provider_missing_session")
    advance_comparison(store, [broken], now=CUTOFF)
    later = advance_comparison(store, [bar(high=14., close=13.)], now="2026-11-13T19:00:00Z")
    account = later["result"]["accounts"]["D"]
    assert account["trades"] == [] and account["metrics"]["hit_rate_pct"] is None
    assert later["market"]["revision_incidents"]
    assert account["coverage"]["not_verifiable"] == 1


def test_future_bars_are_not_frozen_before_their_availability(store):
    seed(store)
    advance_comparison(store, [bar()], now="2026-11-13T10:00:00Z")
    frozen = [row for row in store.list_simulation_runs() if row["payload"]["kind"] == "market_bars"]
    assert not frozen
    assert not store.list_events()


def test_provider_correction_cannot_rewrite_previous_drawdown_or_winning_fill(store):
    seed(store)
    first = advance_comparison(store, [bar(high=12.5, close=12.)], now=CUTOFF)
    previous = first["result"]["accounts"]["D"]
    assert previous["trades"][0]["status"] == "target_reached"
    updated = advance_comparison(store, [bar(low=8., high=10.5, close=8.5)], now="2026-11-13T19:00:00Z")
    current = updated["result"]["accounts"]["D"]
    assert current["trades"] == previous["trades"]
    assert current["metrics"]["max_drawdown_pct"] == previous["metrics"]["max_drawdown_pct"]
    assert current["nav_eur"] == previous["nav_eur"]
    assert updated["market"]["revision_incidents"][0]["code"] == "provider_revision_frozen_original_retained"


def test_mailbox_configuration_failure_does_not_cancel_stored_independent_summary(store, monkeypatch):
    seed(store, enabled=True)
    def broken_config(*args, **kwargs):
        raise ValueError("Synthetic unsafe mailbox setup")
    monkeypatch.setattr(runner.IMAPConfig, "from_existing_mail", broken_config)
    sent = []
    def smtp(*args, **kwargs):
        sent.append(args)
        return SimpleNamespace(status="smtp_accepted")
    outcome = runner.run_one(store.journal, now=CUTOFF, receive=True, send=True,
                             email_config=CONFIG, smtp_send=smtp)
    assert outcome["mailbox"] == "error" and outcome["delivery"] == "smtp_accepted"
    assert len(sent) == 1
    incidents = [row["payload"] for row in store.list_simulation_runs() if row["payload"]["kind"] == "runner_incident"]
    assert incidents[0]["code"] == "ValueError" and "unsafe" not in str(incidents)


def test_main_global_disable_does_not_open_storage_or_transport(monkeypatch):
    monkeypatch.setenv("EXTERNAL_ANALYSTS_ENABLED", "false")
    monkeypatch.setattr(runner, "create_journal", fail_transport)
    monkeypatch.setattr(runner, "load_supabase_config", fail_transport)
    monkeypatch.setattr(runner, "run_one", fail_transport)
    monkeypatch.setattr("sys.argv", ["run_external_analysts.py", "--receive", "--advance", "--send"])
    assert runner.main() == 0


@pytest.mark.parametrize("owner", ["", "grupo_compartido"])
def test_scheduled_runner_requires_private_explicit_owner(monkeypatch, owner):
    monkeypatch.setenv("EXTERNAL_ANALYSTS_ENABLED", "true")
    monkeypatch.setenv("EXTERNAL_ANALYSTS_MAILBOX_OWNER", owner)
    monkeypatch.setattr(runner, "create_journal", fail_transport)
    monkeypatch.setattr("sys.argv", ["run_external_analysts.py", "--advance"])
    with pytest.raises(SystemExit, match="authorized private"):
        runner.main()


def test_actual_main_analysts_route_returns_before_market_sidebar_or_recompute(store, monkeypatch):
    import app as application
    import src.data_loader as data_loader
    import src.external_analyst_market as collector
    from src.auth import AuthConfig
    seed(store)
    before = snapshot(store)
    monkeypatch.setattr(application, "require_login", lambda: AuthConfig("qa-external-runner", "test-only"))
    monkeypatch.setattr(application, "load_auth_accounts", lambda: {})
    monkeypatch.setattr(application, "cached_journal", lambda owner: store.journal if owner == store.owner
                        else SimpleNamespace(owner=owner))
    monkeypatch.setattr(application, "render_app_header", lambda user: None)
    monkeypatch.setattr(application, "_render_route_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(application, "download_prices", fail_transport)
    monkeypatch.setattr(data_loader, "download_prices", fail_transport)
    monkeypatch.setattr(collector, "load_simulation_bars", fail_transport)
    # main(route=...) is real; only authentication/header/native-navigation shell
    # is replaced. All external screen, owner proxy and journal logic runs.
    page = AppTest.from_string('''
import app
from src.app_routes import ROUTES
app.main(route=ROUTES["analistas"], pages={})
''', default_timeout=30).run()
    assert not page.exception and not page.error
    assert any(header.value == "Analistas externos" for header in page.subheader)
    assert page.metric[0].value == "1"
    assert not any(button.label == "Actualizar datos" for button in page.button)
    page.radio(key="external_filter_scope").set_value("Mis favoritas").run()
    assert not page.exception and not page.error
    assert snapshot(store) == before
    # Same Streamlit session changes identity: no cached first-owner calls,
    # preview or filters may survive into the empty second private account.
    other = TradingJournal(store.path, owner="qa-external-other")
    monkeypatch.setattr(application, "require_login", lambda: AuthConfig("qa-external-other", "test-only"))
    monkeypatch.setattr(application, "cached_journal", lambda owner: other if owner == other.owner
                        else SimpleNamespace(owner=owner))
    page.run()
    assert not page.exception and not page.error
    assert page.metric[0].value == "0"
    assert page.session_state["external_ui_owner"] == "qa-external-other"
    assert snapshot(store) == before
