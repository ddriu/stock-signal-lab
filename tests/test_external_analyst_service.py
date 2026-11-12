from types import SimpleNamespace

import pytest

from src.email_sender import EmailConfig
from src.external_analyst_service import (activate_comparison, advance_comparison,
    build_external_report, deliver_external_report, first_observations, import_csv_bytes,
    ingest_existing_mailbox, render_external_report, save_research_candidate, effective_call)
from src.external_analyst_store import ExternalAnalystStore
from src.journal import TradingJournal

NOW = "2026-11-12T09:01:00+00:00"


@pytest.fixture
def store(tmp_path):
    journal = TradingJournal(tmp_path/"synthetic.db", owner="synthetic-user")
    journal.get_alert_preferences = lambda: SimpleNamespace(email="test@example.invalid")
    return ExternalAnalystStore(journal)


def message(name="original", **overrides):
    return {"message_id": name, "provider": "perito_inversor", "received_at": NOW,
            "observed_at": NOW, "announced_count": 71, "issues": [], "calls": [{
                "source_id": "demo-1", "company_name": "Empresa sintética", "ticker": "DEMO",
                "currency": "EUR", "analyst": "Analista de pruebas", "action": "buy", "direction": "long",
                "entry_rule": "limit", "entry": 10, "stop": 9, "target": 12,
                "source_status": "pending", "source_url": "https://www.peritoinversor.com/#ins-demo",
                **overrides}]}


def bar(**extra):
    return {"ticker": "DEMO", "currency": "EUR", "open_at": "2026-11-13T09:00:00+00:00",
            "close_at": "2026-11-13T17:00:00+00:00", "available_at": "2026-11-13T17:20:00+00:00",
            "open": 10, "high": 10.5, "low": 9.5, "close": 10.3, **extra}


def test_activation_explicit_once_does_not_reset_losing_accounts(store):
    assert advance_comparison(store, [], now=NOW) == {"status": "not_activated"}
    first = activate_comparison(store, now=NOW)
    assert activate_comparison(store, now="2026-11-13T09:00:00Z") == first
    assert len(store.list_simulation_runs()) == 1
    assert first["initial_cash_per_account_eur"] == 100
    assert first["manifest"]["git_revision"]


def test_original_levels_not_retroactively_replaced(store):
    store.import_message(message())
    revised = message("revised", target=15)
    revised["observed_at"] = revised["received_at"] = "2026-11-12T10:00:00Z"
    store.import_message(revised)
    assert first_observations(store)[0]["target"] == 12
    assert store.list_calls()[0]["target"] == 15


def test_frozen_bars_replay_identical_fills_and_no_duplicate_events(store):
    activate_comparison(store, now=NOW)
    store.import_message(message())
    later = "2026-11-13T18:00:00Z"
    a = advance_comparison(store, [bar()], now=later)
    count = len(store.list_events())
    b = advance_comparison(store, [bar()], now=later)
    assert a == b and count == len(store.list_events()) and count > 0
    c = advance_comparison(store, [bar(high=15, close=12)], now="2026-11-13T19:00:00Z")
    assert c["result"]["accounts"]["D"]["nav_eur"] == a["result"]["accounts"]["D"]["nav_eur"]
    assert c["market"]["revision_incidents"]
    assert store.list_calls()[0]["tracking_status"] == "open"


def test_old_initial_observation_is_not_reactivated_by_new_mail(store):
    store.import_message(message())
    activate_comparison(store, now="2026-11-12T10:00:00Z")
    changed = message("later", target=15)
    changed.update(received_at="2026-11-12T11:00:00Z", observed_at="2026-11-12T11:01:00Z")
    store.import_message(changed)
    result = advance_comparison(store, [bar()], now="2026-11-13T18:00:00Z")
    assert result["result"]["accounts"]["D"]["status"] == "awaiting_evidence"


def test_71_announced_not_71_extracted_and_historical_not_a_trade(store):
    activate_comparison(store, now=NOW)
    store.import_message(message(source_status="target_reached"))
    result = advance_comparison(store, [bar()], now="2026-11-13T18:00:00Z")
    assert result["result"]["accounts"]["D"]["trades"] == []
    assert store.list_messages()[0]["announced_count"] == 71
    assert store.list_messages()[0]["extracted_count"] == 1


def test_pending_expiry_disables_entry_without_claiming_market_execution(store):
    store.import_message(message(expires_at="2026-11-12T10:00:00Z"))
    call = store.list_calls()[0]
    current = effective_call(call, at="2026-11-12T11:00:00Z")
    assert current["state"] == "expired" and not current["executable"]
    assert store.list_calls()[0]["state"] == "pending"
    assert store.list_events() == []


def test_expiry_updates_read_only_tracking_projection_and_report(store):
    store.import_message(message(expires_at="2026-11-12T10:00:00Z"))
    original = {**store.list_calls()[0], "tracking_status": "pending"}
    projected = effective_call(original, at="2026-11-12T11:00:00Z")
    assert projected["state"] == projected["tracking_status"] == "expired"
    assert original["tracking_status"] == "pending"
    report = build_external_report(store, now="2026-11-12T11:00:00Z")
    assert report["items"][0]["state"] == "expired"
    assert not report["items"][0]["executable"]


def test_manual_csv_preserves_evidence_not_observed_clock():
    data = ("source_id,ticker,currency,received_at,observed_at,entry,stop,target\n"
            "one,DEMO,EUR,2026-11-01T09:00:00Z,2020-01-01T00:00:00Z,10,9,12\n").encode()
    parsed = import_csv_bytes(data, observed_at=NOW)
    assert parsed["extracted_count"] == 1
    assert parsed["calls"][0]["observed_at"] == NOW
    assert not parsed["calls"][0]["executable"]


def test_research_needs_source_and_real_owned_technical_snapshot(store):
    facts = message()["calls"][0]
    with pytest.raises(ValueError):
        save_research_candidate(store, facts, strategy="B", now=NOW, evidence={})
    with pytest.raises(ValueError):
        save_research_candidate(store, facts, strategy="C", now=NOW,
            evidence={"engine_snapshot_id": "not-present", "signal_known_at": NOW})
    item = save_research_candidate(store, facts, strategy="A", now=NOW,
        evidence={"evidence_url": "https://example.com/earnings", "evidence_at": NOW})
    assert item["observed_at"] == NOW and item["strategy"] == "A"


def test_provenance_cannot_backdate_or_override_normalized_candidate(store):
    for forged in ("observed_at", "received_at", "provider", "id", "entry", "ticker", "executable"):
        with pytest.raises(ValueError, match="evidencia no puede"):
            save_research_candidate(store, message()["calls"][0], strategy="A", now=NOW,
                evidence={"evidence_url": "https://example.com/earnings", "evidence_at": NOW,
                          forged: "2020-01-01T00:00:00Z"})


def test_external_report_independent_opt_in_and_smtp_mock_only(store):
    store.import_message(message())
    report = build_external_report(store, now=NOW, favorites={"DEMO"})
    config = EmailConfig("smtp.example.invalid", 465, "sender", "not-a-real-secret", "test@example.invalid")
    sent = []
    def smtp(recipient, subject, plain_body, html_body, *, config):
        sent.append((recipient, subject, plain_body, html_body))
        return SimpleNamespace(status="smtp_accepted")
    assert deliver_external_report(store, report, email_config=config, send=smtp) == "disabled"
    assert sent == []
    store.save_preferences({"enabled": True})
    assert deliver_external_report(store, report, email_config=config, send=smtp) == "smtp_accepted"
    assert deliver_external_report(store, report, email_config=config, send=smtp) == "already_reserved"
    assert len(sent) == 1
    assert "Analistas externos" in sent[0][1] and 'charset="utf-8"' in sent[0][3]
    assert "@" not in str(store.list_deliveries())
    tomorrow = build_external_report(store, now="2026-11-13T09:00:00Z")
    assert tomorrow["items"] == []


def test_smtp_uncertainty_never_retries_or_claims_arrival(store):
    store.import_message(message())
    store.save_preferences({"enabled": True})
    report = build_external_report(store, now=NOW)
    config = EmailConfig("smtp.example.invalid", 465, "sender", "not-a-real-secret", "test@example.invalid")
    def timeout(*args, **kwargs):
        raise TimeoutError("may have been accepted")
    assert deliver_external_report(store, report, email_config=config, send=timeout) == "uncertain"
    assert deliver_external_report(store, report, email_config=config, send=timeout) == "already_reserved"


def test_report_no_html_injection_and_max_five(store):
    for n in range(7):
        value = message(str(n), source_id=f"demo-{n}", company_name="<script>alert(1)</script>")
        store.import_message(value)
    report = build_external_report(store, now=NOW)
    assert len(report["items"]) == 5 and report["waiting"] == 2
    _, html = render_external_report(report)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "/analistas?signal=" in html


def test_mailbox_disabled_no_transport_or_other_owner_reads(store):
    from src.external_analyst_ingest import IMAPConfig
    def forbidden(*args, **kwargs):
        raise AssertionError("No mailbox access allowed")
    assert ingest_existing_mailbox(store, now=NOW, config=IMAPConfig(), client_factory=forbidden)["status"] == "disabled"
    with pytest.raises(ValueError):
        ingest_existing_mailbox(store, now=NOW, config=IMAPConfig(enabled=True, owner="someone-else"), client_factory=forbidden)
