from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from src.alerts import AlertCandidate, AlertPreferences, normalize_alert_preferences
from src.alert_runner import _snapshot_positions, run_daily_alerts
from src.entry_opportunity import STATUS_BUYABLE, STATUS_WAIT_PRICE
from src.opportunity import evaluate_relative_strength
from src.signal_engine import SignalResult
from src.storage import GROUP_PORTFOLIO_OWNER


def _email_evidence_run(monkeypatch, *, sender, enabled=True, recorder=None,
                        virtual_tracker=None, configure_user=None):
    """Exercise the single reserved digest with synthetic inputs and no SMTP."""
    import src.model_results_tracking as tracking

    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "1" if enabled else "0")
    if recorder is not None:
        monkeypatch.setattr(tracking, "record_model_email_evidence", recorder)
    preference = normalize_alert_preferences(owner="ddriu", email="ddriu@example.com",
        enabled=True, include_group=False)
    user = FakeManualJournal(preference, tickers=("MINE",))
    if configure_user is not None:
        configure_user(user)
    frame = _manual_test_frame(monkeypatch)
    frame["open"], frame["high"], frame["low"], frame["atr_14"] = 99., 101., 98., 2.
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=85, confidence_pct=90, label="Entrada fuerte", growth_score=80, momentum_score=90,
        context_score=80, sector_key="technology", as_of=kwargs["frame"].index[-1],
        positive_factors=(), risk_factors=(), manual_checks=()))
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        opportunity_score=75, confidence_pct=80, status_code=STATUS_BUYABLE,
        status_label="Entrada evaluada", explanation="Análisis sintético completo"))
    candidate = AlertCandidate(ticker="MINE", kind="buy", title="Entrada de prueba",
        entry_score=80, entry_label="Entrada fuerte", position_label="Mantener", price=100,
        as_of="2026-10-14", explanation="Synthetic", signature="test:buy", held=False,
        policy_snapshot={"data_quality": {"block_entry": False}})
    from src.analysis_policy import decode_policy_note
    monkeypatch.setattr("src.alert_runner.filter_changed_candidates", lambda *args, **kwargs: [
        replace(candidate, policy_snapshot=decode_policy_note(user.snapshots[-1]["note"]))])
    result = run_daily_alerts(owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=sender, today=date(2026, 10, 15), virtual_tracker=virtual_tracker)
    return result, user, preference


def test_single_digest_mirrors_receipt_and_preserves_recipient_preferences(monkeypatch) -> None:
    from src.email_sender import EmailDeliveryReceipt

    sent, evidence = [], []
    receipt = EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
        accepted_at="2026-10-15T07:00:00+00:00", receipt_hash="a" * 64, message_hash="b" * 64)

    def sender(*args):
        sent.append(args)
        return receipt

    result, user, preference = _email_evidence_run(monkeypatch, sender=sender,
        recorder=lambda journal, received, identity: evidence.append((journal, received, identity)))
    assert result.emails_sent == 1 and result.alerts_sent == 1
    assert len(sent) == len(evidence) == 1
    assert [message[0] for message in sent] == [preference.email]
    assert [entry[0] for entry in evidence] == [user]
    assert all(entry[1] is receipt for entry in evidence)
    assert len({entry[2] for entry in evidence}) == 1
    assert all(len(entry[2]) == 64 and "@" not in entry[2] for entry in evidence)
    assert user.preference is preference and user.preference_reads == 1


def test_legacy_sender_success_remains_unverified_in_optional_evidence(monkeypatch) -> None:
    evidence = []
    result, _, _ = _email_evidence_run(monkeypatch, sender=lambda *args: None,
        recorder=lambda journal, receipt, identity: evidence.append(receipt))
    assert result.emails_sent == 1
    assert evidence == [None]  # No synthetic smtp_accepted claim.


def test_optional_email_tracking_is_off_by_default_and_never_initializes_ledger(monkeypatch) -> None:
    import src.alert_runner as runner

    monkeypatch.delenv("MODEL_RESULTS_ENABLED", raising=False)
    errors = []
    runner._record_model_email(object(), None, "identity", owner="ddriu", errors=errors)
    assert errors == []
    result, _, _ = _email_evidence_run(monkeypatch, enabled=False, sender=lambda *args: None,
        recorder=lambda *args, **kwargs: pytest.fail("Disabled tracking must not touch a ledger"))
    assert result.emails_sent == 1


def _virtual_summary(**changes):
    value = dict(status="updated", available_at="2026-10-15T21:00:00Z", ready_experiments=1,
        preserved_experiments=0, issues=0, ledger_incidents=0, new_proposals=2, new_fills=1,
        pending_proposals=2, experiments=[dict(experiment_id="a" * 64, source="growth", cohort="verified",
            status="updated", strategy_accounts=8, runs=8, budget_usd=6000)])
    value.update(changes)
    return value


def test_signal_persistence_then_virtual_tracking_then_sealed_report_claim_and_smtp(monkeypatch):
    import src.alert_runner as runner
    from src.daily_digest import seal_daily_score_report
    steps, holder = [], {}

    def configure(user):
        holder["user"] = user
        for name, stage in (("add_analysis_snapshot", "signal_saved"),
                            ("save_daily_score_report", "report_saved"), ("claim_daily_digest", "claim")):
            original = getattr(user, name)
            def wrapped(*args, _original=original, _stage=stage, **kwargs):
                result = _original(*args, **kwargs)
                steps.append(_stage)
                return result
            setattr(user, name, wrapped)

    def tracker(journal, stored):
        assert journal is holder["user"] and len(stored) == len(journal.snapshots) == 1
        assert stored.iloc[0]["note"] == journal.snapshots[0]["note"]
        steps.append("virtual")
        return _virtual_summary()

    def sealed(report):
        assert report["virtual_tracking"]["new_fills"] == 1
        steps.append("seal")
        return seal_daily_score_report(report)

    monkeypatch.setattr(runner, "seal_daily_score_report", sealed)
    result, user, _ = _email_evidence_run(monkeypatch, enabled=False,
        virtual_tracker=tracker, configure_user=configure, sender=lambda *args: steps.append("sender"))
    assert result.emails_sent == 1 and not result.errors
    assert steps == ["signal_saved", "virtual", "seal", "report_saved", "claim", "sender"]
    report = next(iter(user.reports.values()))
    assert report["virtual_tracking"]["experiments"][0]["experiment_id"] == "a" * 64
    assert seal_daily_score_report(report)["report_id"] == report["report_id"]


def test_virtual_collection_failure_is_explicit_without_exposing_details_or_confirming_zero_operations(monkeypatch):
    sent = []
    def tracker(*args):
        raise RuntimeError("private-token secret@example.invalid")
    result, user, _ = _email_evidence_run(monkeypatch, enabled=False,
        virtual_tracker=tracker, sender=lambda *args: sent.append(args))
    assert result.emails_sent == 1 and len(sent) == 1
    virtual = next(iter(user.reports.values()))["virtual_tracking"]
    assert virtual["status"] == "error" and virtual["issues"] == 1
    assert virtual["new_fills"] is None and virtual["new_proposals"] is None
    assert "Error de actualización" in sent[0][2] and "N/D ejecuciones" in sent[0][2]
    assert "private-token" not in repr(sent) + repr(result.errors)
    assert "secret@example.invalid" not in repr(sent) + repr(result.errors)


def test_owner_callback_can_decline_without_changing_other_users_digest(monkeypatch):
    result, user, _ = _email_evidence_run(monkeypatch, enabled=False,
        virtual_tracker=lambda journal, stored: None, sender=lambda *args: None)
    assert result.emails_sent == 1 and not result.errors
    assert "virtual_tracking" not in next(iter(user.reports.values()))


def test_structured_virtual_failure_is_not_hidden_as_an_error_free_run(monkeypatch):
    result, user, _ = _email_evidence_run(monkeypatch, enabled=False,
        virtual_tracker=lambda *args: _virtual_summary(status="partial_failure", issues=1),
        sender=lambda *args: None)
    assert result.emails_sent == 1 and any("seguimiento virtual comunica incidencias" in text for text in result.errors)
    assert next(iter(user.reports.values()))["virtual_tracking"]["status"] == "partial_failure"


def test_repeat_claim_still_blocks_same_session_after_new_virtual_summary(monkeypatch):
    sent, tracked = [], []
    def tracker(journal, stored):
        tracked.append(len(stored))
        value = _virtual_summary()
        value["new_fills"] = len(tracked)
        return value
    first, user, _ = _email_evidence_run(monkeypatch, enabled=False,
        virtual_tracker=tracker, sender=lambda *args: sent.append(args))
    frame = _manual_test_frame(monkeypatch)
    frame["open"], frame["high"], frame["low"], frame["atr_14"] = 99., 101., 98., 2.
    second = run_daily_alerts(owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        virtual_tracker=tracker, sender=lambda *args: sent.append(args), today=date(2026, 10, 15))
    assert first.emails_sent == 1 and second.emails_sent == 0 and second.emails_skipped == 1
    assert len(sent) == 1 and len(tracked) == 2 and len(user.delivery_claims) == 1


@pytest.mark.parametrize("owner,journal_owner", [("other", "other"), ("ddriu", "other"), ("other", "ddriu")])
def test_optional_email_evidence_does_not_touch_a_different_scoped_owner(monkeypatch, owner, journal_owner):
    import src.alert_runner as runner
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "true")
    monkeypatch.setenv("MODEL_RESULTS_OWNER", "ddriu")
    monkeypatch.setattr("src.model_results_tracking.record_model_email_evidence",
        lambda *args: pytest.fail("No ledger write for another scoped owner"))
    errors = []
    runner._record_model_email(SimpleNamespace(owner=journal_owner), None, "a" * 64,
                               owner=owner, errors=errors)
    assert errors == []


def test_virtual_summary_whitelist_never_copies_owners_errors_or_arbitrary_provider_payload():
    from src.alert_runner import _virtual_report_summary
    value = _virtual_summary()
    value.update(owner="private@example.invalid", secret="private-token", raw_error="secret trace")
    value["experiments"][0].update(owner="private@example.invalid", payload={"secret": "private-token"})
    value["operations"] = [dict(event_type="order_proposed", side="buy", ticker="MA", strategy="A",
        mode="budget", intended_open_at="2026-10-16T13:30:00Z", is_real_execution=False,
        owner="private@example.invalid", api_key="private-token")]
    safe = _virtual_report_summary(value)
    assert "private" not in repr(safe) and "raw_error" not in safe
    assert safe["operations"][0]["ticker"] == "MA"
    assert safe["operations"][0]["is_real_execution"] is False


def test_tracking_failure_does_not_retry_or_reclassify_accepted_mail(monkeypatch) -> None:
    sent = []

    def recorder(*args, **kwargs):
        raise RuntimeError("private-token secret@example.invalid")

    result, _, _ = _email_evidence_run(monkeypatch, sender=lambda *args: sent.append(args), recorder=recorder)
    assert len(sent) == result.emails_sent == 1
    assert result.alerts_sent == 1
    evidence_errors = [error for error in result.errors if "evidencia de correo" in error]
    assert len(evidence_errors) == 1
    assert all("RuntimeError" in error for error in evidence_errors)
    assert "private-token" not in repr(result.errors)
    assert "secret@example.invalid" not in repr(result.errors)


def test_send_failure_records_only_failure_category_and_keeps_original_error(monkeypatch) -> None:
    evidence, attempts = [], []

    def sender(*args):
        attempts.append(args)
        raise RuntimeError("synthetic original sending failure")

    result, _, _ = _email_evidence_run(monkeypatch, sender=sender,
        recorder=lambda journal, receipt, identity: evidence.append(receipt))
    assert result.emails_sent == result.alerts_sent == 0
    assert len(attempts) == 1
    assert evidence == [{"status": "failed", "error_type": "RuntimeError"}]
    assert sum("requiere revisión" in error for error in result.errors) == 1
    assert "synthetic original sending failure" not in repr(result.errors)


def test_post_acceptance_transport_error_keeps_receipt_without_changing_old_error_semantics(monkeypatch) -> None:
    from src.email_sender import EmailDeliveryError, EmailDeliveryReceipt

    receipt = EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
        accepted_at="2026-10-15T07:00:00+00:00", receipt_hash="a" * 64, message_hash="b" * 64)
    evidence = []

    def sender(*args):
        error = EmailDeliveryError("synthetic failure after DATA acceptance")
        error.smtp_receipt = receipt
        raise error

    result, _, _ = _email_evidence_run(monkeypatch, sender=sender,
        recorder=lambda journal, received, identity: evidence.append(received))
    assert result.emails_sent == result.alerts_sent == 1
    assert evidence == [receipt]  # Do not erase proven SMTP acceptance.
    assert sum("requiere revisión" in error for error in result.errors) == 1


class FakeJournal:
    def __init__(self, *, tickers: tuple[str, ...] = ()) -> None:
        self.tickers = tickers
        self.saved_states: list[object] = []
        self.snapshots: list[dict[str, object]] = []
        self.reports: dict[str, dict[str, object]] = {}
        self.delivery_claims: dict[str, tuple[str, str]] = {}
        self.delivery_finishes: list[tuple[str, str, str]] = []

    def list_favorites(self) -> pd.DataFrame:
        return pd.DataFrame({"ticker": list(self.tickers)})

    def open_positions(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["ticker", "average_cost"])

    def list_alert_states(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["ticker", "signature"])

    def upsert_alert_states(self, states: list[object]) -> None:
        self.saved_states.extend(states)

    def list_analysis_snapshots(self) -> pd.DataFrame:
        return pd.DataFrame(self.snapshots)

    def add_analysis_snapshot(self, **values: object) -> int:
        values.setdefault("created_at", (pd.Timestamp(values["analyzed_at"]).normalize()
            + pd.Timedelta(hours=22)).isoformat())
        self.snapshots.append(values)
        return len(self.snapshots)

    def save_daily_score_report(self, report: dict[str, object]) -> str:
        self.reports[report["report_id"]] = report
        return report["report_id"]

    def claim_daily_digest(self, delivery_key: str, market_date: str, report_id: str) -> bool:
        assert report_id in self.reports
        if delivery_key in self.delivery_claims:
            return False
        self.delivery_claims[delivery_key] = (market_date, report_id)
        return True

    def finish_daily_digest(self, delivery_key: str, status: str, *, error_type: str = "") -> None:
        self.delivery_finishes.append((delivery_key, status, error_type))


def _verified_growth_info(ticker):
    metrics = dict(revenueGrowth=.2, earningsGrowth=.3, operatingMargins=.2,
                   returnOnEquity=.2, freeCashflow=100.)
    return dict(symbol=ticker, longName=f"Empresa {ticker}", **metrics,
        _metric_periods={name: dict(periodicity="annual", period_end="2025-12-31",
                                   source="SEC EDGAR", comparable=True) for name in metrics})


def test_latest_snapshot_positions_are_included_in_the_daily_scope() -> None:
    class SnapshotJournal(FakeJournal):
        def list_portfolio_snapshot_positions(self) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "snapshot_date": "2026-09-20",
                        "platform": "Broker",
                        "asset_name": "Posición antigua",
                        "asset_type": "Acción",
                        "analysis_ticker": "OLD",
                        "value_eur": 100.0,
                    },
                    {
                        "snapshot_date": "2026-09-25",
                        "platform": "Broker",
                        "asset_name": "AST SpaceMobile",
                        "asset_type": "Acción",
                        "analysis_ticker": "ASTS",
                        "value_eur": 320.0,
                    },
                    {
                        "snapshot_date": "2026-09-25",
                        "platform": "Broker",
                        "asset_name": "Cobre",
                        "asset_type": "ETF",
                        "analysis_ticker": "CEBS",
                        "value_eur": 150.0,
                    },
                ]
            )

    assert _snapshot_positions(SnapshotJournal()) == {
        "ASTS": "AST SpaceMobile",
        "CEBS.DE": "Cobre",
    }


class FakeGroupJournal(FakeJournal):
    def list_enabled_alert_preferences(self) -> list[object]:
        return [
            normalize_alert_preferences(
                owner="ddriu",
                email="ddriu@example.com",
                enabled=True,
            )
        ]


class FakeManualJournal(FakeJournal):
    def __init__(
        self, preference: AlertPreferences, *, tickers: tuple[str, ...] = ()
    ) -> None:
        super().__init__(tickers=tickers)
        self.preference = preference
        self.preference_reads = 0
        self.state_writes = 0
        self.snapshots: list[dict[str, object]] = []

    def get_alert_preferences(self) -> AlertPreferences:
        self.preference_reads += 1
        return self.preference

    def list_enabled_alert_preferences(self) -> list[AlertPreferences]:
        raise AssertionError("Una revisión manual no enumera preferencias ajenas.")

    def save_alert_preferences(self, preference: AlertPreferences) -> None:
        raise AssertionError("Una revisión manual no modifica preferencias.")

    def upsert_alert_states(self, states: list[object]) -> None:
        self.state_writes += 1
        super().upsert_alert_states(states)

    def add_analysis_snapshot(self, **values: object) -> int:
        values.setdefault("created_at", (pd.Timestamp(values["analyzed_at"]).normalize()
            + pd.Timedelta(hours=22)).isoformat())
        self.snapshots.append(values)
        return len(self.snapshots)


def _manual_test_frame(monkeypatch, *, as_of: str = "2026-10-14") -> pd.DataFrame:
    frame = pd.DataFrame({"close": [100.0]}, index=pd.DatetimeIndex([as_of]))
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    monkeypatch.setattr(
        "src.alert_runner.evaluate_latest_signal",
        lambda frame, config, *, ticker, entry_price=None: SignalResult(
            ticker=ticker, as_of=frame.index[-1], score=50, label="Esperar",
            position_label="Mantener", explanation="Análisis de prueba.",
            positive_factors=(), risk_factors=(),
        ),
    )
    return frame


def test_daily_digest_claim_blocks_same_session_even_when_body_changes(monkeypatch):
    preference = normalize_alert_preferences(owner="ddriu", email="ddriu@example.com",
        enabled=True, include_group=False)
    user = FakeManualJournal(preference, tickers=("MINE",))
    frame = _manual_test_frame(monkeypatch)
    sent = []
    kwargs = dict(owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=lambda ticker: {},
        sender=lambda *args: sent.append(args), today=date(2026, 10, 15))
    first = run_daily_alerts(**kwargs)
    user.tickers = ("MINE", "NEW")
    second = run_daily_alerts(**kwargs)
    assert first.emails_sent == 1
    assert second.emails_sent == 0 and second.emails_skipped == 1
    assert len(sent) == 1
    assert len(user.delivery_claims) == 1
    assert user.delivery_finishes[0][1] == "uncertain"  # The legacy fake has no SMTP receipt.


def test_uncertain_smtp_attempt_is_reserved_without_automatic_retry(monkeypatch):
    preference = normalize_alert_preferences(owner="ddriu", email="ddriu@example.com",
        enabled=True, include_group=False)
    user = FakeManualJournal(preference, tickers=("MINE",))
    frame = _manual_test_frame(monkeypatch)
    attempts = []
    def sender(*args):
        attempts.append(args)
        raise RuntimeError("secret smtp transport message")
    kwargs = dict(owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=lambda ticker: {},
        sender=sender, today=date(2026, 10, 15))
    first = run_daily_alerts(**kwargs)
    second = run_daily_alerts(**kwargs)
    assert first.emails_sent == second.emails_sent == 0
    assert second.emails_skipped == 1
    assert len(attempts) == 1
    assert user.delivery_finishes[0][1:] == ("uncertain", "RuntimeError")
    assert "secret smtp" not in repr(first.errors)


@pytest.mark.parametrize("broken", ["list_analysis_snapshots", "save_daily_score_report", "claim_daily_digest"])
def test_digest_fails_closed_before_smtp_if_history_or_claim_unavailable(monkeypatch, broken):
    preference = normalize_alert_preferences(owner="ddriu", email="ddriu@example.com",
        enabled=True, include_group=False)
    user = FakeManualJournal(preference, tickers=("MINE",))
    def fail(*args, **kwargs):
        raise RuntimeError("private backend detail")
    setattr(user, broken, fail)
    frame = _manual_test_frame(monkeypatch)
    summary = run_daily_alerts(owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=lambda ticker: {},
        sender=lambda *args: pytest.fail("No SMTP without persistent history and claim"),
        today=date(2026, 10, 15))
    assert summary.emails_sent == 0 and summary.errors
    assert "private backend" not in repr(summary.errors)


def test_scheduled_holiday_does_not_resend_last_sessions_report(monkeypatch):
    group, user = FakeGroupJournal(), FakeJournal(tickers=("MINE",))
    frame = _manual_test_frame(monkeypatch, as_of="2026-10-23")
    result = run_daily_alerts(journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=lambda ticker: {},
        sender=lambda *args: pytest.fail("No new market session"), today=date(2026, 10, 26),
        require_current_session=True)
    assert result.emails_sent == 0 and result.emails_skipped == 1


def test_scheduled_european_session_is_not_skipped_for_a_us_holiday(monkeypatch):
    group, user = FakeGroupJournal(), FakeJournal(tickers=("IBE.MC",))
    frame = _manual_test_frame(monkeypatch, as_of="2026-02-16")
    frame["open"], frame["high"], frame["low"], frame["atr_14"] = 99., 101., 98., 2.
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        opportunity_score=50, confidence_pct=20, status_code=STATUS_WAIT_PRICE,
        status_label="Esperar", explanation="Análisis sintético de una sesión europea cerrada"))
    sent = []
    result = run_daily_alerts(journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=lambda ticker: {},
        sender=lambda *args: sent.append(args), today=date(2026, 2, 16),
        require_current_session=True)
    assert result.emails_sent == 1 and len(sent) == 1


def test_new_purchase_alert_is_not_promoted_with_unverified_fundamental_dates(monkeypatch):
    preference = normalize_alert_preferences(owner="ddriu", email="ddriu@example.com",
        enabled=True, include_group=False)
    user = FakeManualJournal(preference, tickers=("MINE",))
    frame = _manual_test_frame(monkeypatch)
    candidate = AlertCandidate(ticker="MINE", kind="Compra", title="Entrada fuerte",
        entry_score=88, entry_label="Entrada fuerte", position_label="Mantener", price=100,
        as_of="2026-10-14", explanation="Motor bruto", signature="entry:strong", held=False,
        policy_snapshot={"data_quality": {"block_entry": True}})
    monkeypatch.setattr("src.alert_runner.filter_changed_candidates", lambda *args, **kwargs: [candidate])
    sent = []
    summary = run_daily_alerts(owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=lambda ticker: {},
        sender=lambda *args: sent.append(args), today=date(2026, 10, 15))
    assert summary.emails_sent == 1 and summary.alerts_sent == 0
    report = next(iter(user.reports.values()))
    assert report["alerts"] == []
    assert "Datos pendientes" in sent[0][2]
    assert "Compra no confirmada" in sent[0][2]


def test_delivery_key_is_private_and_scoped_to_owner_recipient_and_session():
    from src.alert_runner import _daily_digest_delivery_key
    key = _daily_digest_delivery_key("Owner", "ME@EXAMPLE.COM", "2026-10-23")
    assert key == _daily_digest_delivery_key("owner", "me@example.com", "2026-10-23")
    assert len(key) == 64 and "@" not in key
    assert key != _daily_digest_delivery_key("other", "me@example.com", "2026-10-23")
    assert key != _daily_digest_delivery_key("owner", "other@example.com", "2026-10-23")
    assert key != _daily_digest_delivery_key("owner", "me@example.com", "2026-10-26")


def test_five_visible_alerts_only_are_counted_and_omitted_changes_remain_pending(monkeypatch):
    class StatefulManual(FakeManualJournal):
        def list_alert_states(self):
            if not self.saved_states:
                return pd.DataFrame(columns=["ticker", "signature", "notified_at"])
            return pd.DataFrame([dict(ticker=item.ticker, signature=item.signature,
                                      notified_at=item.notified_at) for item in self.saved_states
            ]).drop_duplicates("ticker", keep="last")
    preference = normalize_alert_preferences(owner="ddriu", email="ddriu@example.com",
        enabled=True, include_group=False)
    user = StatefulManual(preference, tickers=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"))
    frame = _manual_test_frame(monkeypatch, as_of="2026-10-14")
    frame["open"], frame["high"], frame["low"], frame["atr_14"] = 99., 101., 98., 2.
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        opportunity_score=75, confidence_pct=80, status_code=STATUS_BUYABLE,
        status_label="Entrada evaluada", explanation="Análisis sintético completo"))
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=85, confidence_pct=90, label="Entrada fuerte", growth_score=80, momentum_score=90,
        context_score=80, sector_key="technology", as_of=kwargs["frame"].index[-1],
        positive_factors=("Tendencia confirmada",), risk_factors=(), manual_checks=()))
    sent = []
    kwargs = dict(owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=lambda *args: sent.append(args))
    first = run_daily_alerts(**kwargs, today=date(2026, 10, 14))
    assert first.alerts_sent == 5 and first.emails_sent == 1
    report = next(iter(user.reports.values()))
    assert len(report["alerts"]) == 5
    assert {item.ticker for item in user.saved_states} == {"AAA", "BBB", "CCC", "DDD", "EEE"}
    assert "FFF" in {item["ticker"] for item in user.snapshots}
    frame.index = pd.DatetimeIndex(["2026-10-15"])
    second = run_daily_alerts(**kwargs, today=date(2026, 10, 15))
    assert second.alerts_sent == 1 and second.emails_sent == 1
    report = list(user.reports.values())[-1]
    assert [item["ticker"] for item in report["alerts"]] == ["FFF"]


def test_runner_with_real_sqlite_history_report_and_claim_has_no_duplicate_mail(monkeypatch, tmp_path):
    from src.journal import TradingJournal
    from src.email_sender import EmailDeliveryReceipt
    user = TradingJournal(tmp_path / "digest.sqlite", owner="synthetic")
    user.save_alert_preferences(normalize_alert_preferences(owner="synthetic",
        email="synthetic@example.invalid", enabled=True, include_group=False))
    monkeypatch.setattr(user, "list_favorites", lambda: pd.DataFrame({"ticker": ["MINE"]}))
    frame = _manual_test_frame(monkeypatch)
    frame["open"], frame["high"], frame["low"], frame["atr_14"] = 99., 101., 98., 2.
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        opportunity_score=50, confidence_pct=20, status_code=STATUS_WAIT_PRICE,
        status_label="Esperar", explanation="Análisis sintético para probar persistencia"))
    calls = []
    def sender(*args):
        calls.append(args)
        return EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
            accepted_at="2026-10-15T21:00:00+00:00", receipt_hash="a" * 64, message_hash="b" * 64)
    kwargs = dict(owner="synthetic", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=lambda ticker: {},
        sender=sender, today=date(2026, 10, 15))
    first = run_daily_alerts(**kwargs)
    second = run_daily_alerts(**kwargs)
    assert first.emails_sent == 1 and not first.errors
    assert second.emails_sent == 0 and second.emails_skipped == 1 and not second.errors
    assert len(calls) == 1
    snapshots = user.list_analysis_snapshots()
    assert len(snapshots) == 1
    import re
    identity = re.search(r"digest=([a-f0-9]{64})", calls[0][2]).group(1)
    report = user.get_daily_score_report(identity)
    assert report["report_id"] == identity and report["as_of"] == "2026-10-14"


def _real_closing_digest_case(monkeypatch, tmp_path, *, ticker, session, created_at, clock):
    from src.journal import TradingJournal
    user = TradingJournal(tmp_path / "closing-digest.sqlite", owner="synthetic")
    user.save_alert_preferences(normalize_alert_preferences(owner="synthetic",
        email="synthetic@example.invalid", enabled=True, include_group=False))
    monkeypatch.setattr(user, "list_favorites", lambda: pd.DataFrame({"ticker": [ticker]}))
    original_add = user.add_analysis_snapshot
    def add_at_known_time(**values):
        identity = original_add(**values)
        with user._connect() as connection:
            connection.execute("UPDATE analysis_snapshots SET created_at = ? WHERE id = ?",
                               (created_at, identity))
        return identity
    monkeypatch.setattr(user, "add_analysis_snapshot", add_at_known_time)
    monkeypatch.setattr("src.daily_digest._utc_clock", lambda value=None: pd.Timestamp(clock))
    frame = _manual_test_frame(monkeypatch, as_of=session)
    frame["open"], frame["high"], frame["low"], frame["atr_14"] = 99., 101., 98., 2.
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=85, confidence_pct=90, label="Entrada fuerte", growth_score=80, momentum_score=90,
        context_score=80, sector_key="technology", as_of=kwargs["frame"].index[-1],
        positive_factors=("Tendencia confirmada",), risk_factors=(), manual_checks=()))
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        opportunity_score=75, confidence_pct=80, status_code=STATUS_BUYABLE,
        status_label="Entrada evaluada", explanation="Análisis sintético completo"))
    return user, frame


def test_manual_intraday_policy_is_not_reintroduced_into_a_previous_closing_report(monkeypatch, tmp_path):
    user, frame = _real_closing_digest_case(monkeypatch, tmp_path, ticker="MINE", session="2026-10-26",
        created_at="2026-10-26T14:00:00+00:00", clock="2026-10-26T15:00:00+00:00")
    result = run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=lambda *args: pytest.fail("An open US session is not a closing digest"),
        today=date(2026, 10, 26))
    assert result.emails_sent == result.alerts_sent == 0
    assert result.emails_skipped == 1
    assert any("sesión abierta" in error for error in result.errors)
    assert user.list_alert_states().empty
    with user._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_score_reports").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM daily_digest_deliveries").fetchone()[0] == 0


def test_european_closing_digest_uses_exact_report_session_for_real_sqlite_claim_on_us_holiday(monkeypatch, tmp_path):
    from src.email_sender import EmailDeliveryReceipt
    user, frame = _real_closing_digest_case(monkeypatch, tmp_path, ticker="IBE.MC", session="2026-02-16",
        created_at="2026-02-16T21:00:00+00:00", clock="2026-02-16T22:15:00+00:00")
    calls = []
    def sender(*args):
        calls.append(args)
        return EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
            accepted_at="2026-02-16T22:15:00+00:00", receipt_hash="a" * 64, message_hash="b" * 64)
    result = run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=sender, today=date(2026, 2, 16), require_current_session=True)
    assert result.emails_sent == result.alerts_sent == 1 and not result.errors
    assert len(calls) == 1
    with user._connect() as connection:
        report = connection.execute("SELECT market_date FROM daily_score_reports").fetchone()
        claim = connection.execute("SELECT market_date, status FROM daily_digest_deliveries").fetchone()
    assert report[0] == claim[0] == "2026-02-16"
    assert claim[1] == "sent"


@pytest.mark.parametrize(("session", "clock"), [
    ("2026-10-27", "2026-10-28T00:58:19+00:00"),
    ("2026-10-23", "2026-10-24T00:58:19+00:00"),
])
def test_delayed_closing_digest_keeps_the_target_session_and_actual_recording_time(
    monkeypatch, tmp_path, session, clock,
):
    """A midnight delay is still the prior close, including Friday to Saturday."""
    from src.email_sender import EmailDeliveryReceipt

    user, frame = _real_closing_digest_case(monkeypatch, tmp_path, ticker="MINE", session=session,
        created_at=clock, clock=clock)
    calls, downloads = [], []

    def downloader(ticker, start, end, **kwargs):
        downloads.append((ticker, end))
        return frame

    def sender(*args):
        calls.append(args)
        return EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
            accepted_at=clock, receipt_hash="a" * 64, message_hash="b" * 64)

    result = run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=downloader, fundamental_downloader=_verified_growth_info,
        sender=sender, today=date.fromisoformat(session), require_current_session=True)

    assert result.emails_sent == result.alerts_sent == len(calls) == 1
    assert not result.errors
    assert downloads and all(end == date.fromisoformat(session) for _, end in downloads)
    with user._connect() as connection:
        report = connection.execute("SELECT market_date, report_json FROM daily_score_reports").fetchone()
        claim = connection.execute("SELECT market_date, status FROM daily_digest_deliveries").fetchone()
        observation = connection.execute("SELECT analyzed_at, created_at FROM analysis_snapshots").fetchone()
    assert report[0] == claim[0] == session
    assert claim[1] == "sent"
    assert str(observation[0])[:10] == session
    assert pd.Timestamp(observation[1]) == pd.Timestamp(clock)


def test_delayed_repeat_with_a_changed_report_cannot_send_the_same_session_twice(monkeypatch, tmp_path):
    from src.email_sender import EmailDeliveryReceipt

    session, clock = "2026-10-27", "2026-10-28T00:58:19+00:00"
    user, frame = _real_closing_digest_case(monkeypatch, tmp_path, ticker="MINE", session=session,
        created_at=clock, clock=clock)
    calls = []

    def sender(*args):
        calls.append(args)
        return EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
            accepted_at=clock, receipt_hash="a" * 64, message_hash="b" * 64)

    kwargs = dict(owner="synthetic", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=sender, today=date.fromisoformat(session), require_current_session=True)
    first = run_daily_alerts(**kwargs)
    monkeypatch.setattr("src.daily_digest._utc_clock", lambda value=None:
        pd.Timestamp("2026-10-28T02:30:00+00:00"))
    monkeypatch.setattr(user, "list_favorites", lambda: pd.DataFrame({"ticker": ["MINE", "NEW"]}))
    second = run_daily_alerts(**kwargs)

    assert first.emails_sent == len(calls) == 1
    assert second.emails_sent == 0 and second.emails_skipped == 1
    assert not first.errors and not second.errors
    with user._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_score_reports").fetchone()[0] == 2
        claims = connection.execute("SELECT market_date, status FROM daily_digest_deliveries").fetchall()
    assert [tuple(row) for row in claims] == [(session, "sent")]


def test_delayed_digest_does_not_mislabel_an_older_close_as_its_target(monkeypatch, tmp_path):
    user, frame = _real_closing_digest_case(monkeypatch, tmp_path, ticker="MINE", session="2026-10-26",
        created_at="2026-10-28T00:58:19+00:00", clock="2026-10-28T00:58:19+00:00")
    result = run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=lambda *args: pytest.fail("An older quote is not the requested daily close"),
        today=date(2026, 10, 27), require_current_session=True)
    assert result.emails_sent == result.alerts_sent == 0
    assert result.emails_skipped == 1
    with user._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_digest_deliveries").fetchone()[0] == 0


def test_delayed_digest_never_mixes_next_day_asian_quotes_into_the_target_close(monkeypatch, tmp_path):
    import json
    from src.email_sender import EmailDeliveryReceipt

    session, clock = "2026-10-27", "2026-10-28T00:58:19+00:00"
    user, frame = _real_closing_digest_case(monkeypatch, tmp_path, ticker="MINE", session=session,
        created_at=clock, clock=clock)
    monkeypatch.setattr(user, "list_favorites", lambda:
        pd.DataFrame({"ticker": ["MINE", "ALK.AX"]}))
    next_day_asian_frame = frame.copy()
    next_day_asian_frame.index = pd.DatetimeIndex(["2026-10-28"])
    calls = []

    def downloader(ticker, start, end, **kwargs):
        assert end == date.fromisoformat(session)
        # Even a provider that ignores the requested end date cannot make the
        # already-open Australian next session a signal for yesterday's close.
        return next_day_asian_frame if ticker == "ALK.AX" else frame

    def sender(*args):
        calls.append(args)
        return EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
            accepted_at=clock, receipt_hash="a" * 64, message_hash="b" * 64)

    result = run_daily_alerts(owner="synthetic", journal_factory=lambda owner: user,
        downloader=downloader, fundamental_downloader=_verified_growth_info,
        sender=sender, today=date.fromisoformat(session), require_current_session=True)
    assert result.emails_sent == len(calls) == 1
    with user._connect() as connection:
        saved = connection.execute("SELECT market_date, report_json FROM daily_score_reports").fetchone()
        observations = connection.execute("SELECT ticker, analyzed_at FROM analysis_snapshots").fetchall()
    assert saved[0] == session
    report = json.loads(saved[1])
    assert report["as_of"] == session
    assert all(item.get("as_of", session) <= session for item in report["alerts"])
    assert all(point["session"] <= session for row in report["rows"] for point in row["history"])
    assert not any(ticker == "ALK.AX" and str(observed)[:10] == "2026-10-28"
                   for ticker, observed in observations)


def test_manual_review_reads_only_the_selected_owner_and_uses_the_stored_recipient(monkeypatch) -> None:
    preference = replace(
        normalize_alert_preferences(
            owner="ddriu", email="ddriu@example.com", enabled=True, include_group=False,
        ),
        email="Ddriu@Example.com",
    )
    user = FakeManualJournal(preference, tickers=("MINE",))
    other = FakeManualJournal(
        normalize_alert_preferences(owner="other", email="other@example.com", enabled=True),
        tickers=("OTHER",),
    )
    group = FakeGroupJournal(tickers=("GROUP",))
    group.list_enabled_alert_preferences = lambda: (_ for _ in ()).throw(
        AssertionError("No consultar la lista de usuarios activos.")
    )
    journals = {"ddriu": user, "other": other, GROUP_PORTFOLIO_OWNER: group}
    journal_reads: list[str] = []
    downloaded: list[str] = []
    sent: list[tuple[object, ...]] = []
    frame = _manual_test_frame(monkeypatch)

    def journal_factory(owner: str) -> FakeJournal:
        journal_reads.append(owner)
        return journals[owner]

    def downloader(ticker, *args, **kwargs) -> pd.DataFrame:
        downloaded.append(ticker)
        return frame

    summary = run_daily_alerts(
        owner=" Ddriu ", journal_factory=journal_factory, downloader=downloader,
        fundamental_downloader=lambda ticker: {}, sender=lambda *args: sent.append(args),
        today=date(2026, 10, 15),
    )

    assert summary.users_checked == 1
    assert summary.tickers_checked == 1
    assert summary.tickers_with_fresh_prices == 1
    assert summary.emails_sent == 1
    assert journal_reads == ["ddriu"]
    assert user.preference_reads == 1
    assert other.preference_reads == 0
    assert "MINE" in downloaded
    assert not {"GROUP", "OTHER"} & set(downloaded)
    assert [message[0] for message in sent] == [preference.email]
    assert "MINE" in sent[0][2]
    assert "OTHER" not in sent[0][2]
    assert "GROUP" not in sent[0][2]
    assert user.preference is preference
    assert user.state_writes == 1
    assert other.state_writes == 0


def test_manual_review_includes_group_only_when_the_saved_preference_allows_it(monkeypatch) -> None:
    user = FakeManualJournal(
        normalize_alert_preferences(
            owner="ddriu", email="ddriu@example.com", enabled=True, include_group=True,
        ),
        tickers=("MINE",),
    )
    group = FakeGroupJournal(tickers=("GROUP",))
    group.list_enabled_alert_preferences = lambda: (_ for _ in ()).throw(
        AssertionError("No enumerar preferencias para incluir favoritos compartidos.")
    )
    journal_reads: list[str] = []
    sent: list[tuple[object, ...]] = []
    frame = _manual_test_frame(monkeypatch)

    def journal_factory(owner: str) -> FakeJournal:
        journal_reads.append(owner)
        if owner == "ddriu":
            return user
        assert owner == GROUP_PORTFOLIO_OWNER
        return group

    summary = run_daily_alerts(
        owner="ddriu", journal_factory=journal_factory,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=lambda ticker: {}, sender=lambda *args: sent.append(args),
        today=date(2026, 10, 15),
    )

    assert journal_reads == ["ddriu", GROUP_PORTFOLIO_OWNER]
    assert summary.users_checked == 1
    assert summary.tickers_checked == 2
    assert summary.emails_sent == 1
    assert "MINE" in sent[0][2] and "GROUP" in sent[0][2]
    assert [message[0] for message in sent] == ["ddriu@example.com"]


@pytest.mark.parametrize("owner", ["", "  ", GROUP_PORTFOLIO_OWNER, " GRUPO_COMPARTIDO "])
def test_manual_review_rejects_empty_or_group_owners_before_reading_data(owner) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("Un propietario inválido no permite lecturas ni envíos.")

    with pytest.raises(ValueError, match="usuario privado"):
        run_daily_alerts(
            owner=owner, journal_factory=forbidden, downloader=forbidden,
            fundamental_downloader=forbidden, sender=forbidden,
        )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"owner": "other"}, "no pertenecen"),
        ({"owner": " Ddriu "}, "no pertenecen"),
        ({"enabled": False}, "Activa los avisos"),
        ({"email": ""}, "única dirección"),
        ({"email": "invalid"}, "única dirección"),
        ({"email": "ddriu@example.com,other@example.com"}, "única dirección"),
        ({"email": "Ddriu <ddriu@example.com>"}, "única dirección"),
        ({"email": " ddriu@example.com "}, "única dirección"),
        ({"email": "ddriu@example.com\r\nBcc: other@example.com"}, "única dirección"),
    ],
)
def test_manual_review_rejects_mismatched_disabled_or_invalid_stored_preferences(
    overrides, message,
) -> None:
    preference = replace(
        normalize_alert_preferences(owner="ddriu", email="ddriu@example.com", enabled=True),
        **overrides,
    )
    user = FakeManualJournal(preference, tickers=("MINE",))
    journal_reads: list[str] = []

    def journal_factory(owner: str) -> FakeManualJournal:
        journal_reads.append(owner)
        assert owner == "ddriu"
        return user

    def forbidden(*args, **kwargs):
        raise AssertionError("La validación debe impedir análisis, envíos y cambios.")

    user.list_favorites = forbidden
    with pytest.raises(ValueError, match=message):
        run_daily_alerts(
            owner="ddriu", journal_factory=journal_factory, downloader=forbidden,
            fundamental_downloader=forbidden, sender=forbidden,
        )
    assert journal_reads == ["ddriu"]
    assert user.preference_reads == 1
    assert user.preference is preference
    assert user.state_writes == 0
    assert user.snapshots == []


@pytest.mark.parametrize(
    "unusable", ["stale", "empty", "evaluation_failure", "no_tickers", "nan", "zero", "infinite"],
)
def test_manual_review_aborts_without_delivery_or_state_marking_when_no_symbols_are_usable(
    monkeypatch, unusable,
) -> None:
    user = FakeManualJournal(
        normalize_alert_preferences(
            owner="ddriu", email="ddriu@example.com", enabled=True, include_group=False,
        ),
        tickers=() if unusable == "no_tickers" else ("UNUSABLE",),
    )
    frame = _manual_test_frame(
        monkeypatch, as_of="2026-07-28" if unusable == "stale" else "2026-10-14",
    )
    if unusable == "empty":
        frame = pd.DataFrame()
    if unusable in {"nan", "zero", "infinite"}:
        frame["close"] = {"nan": float("nan"), "zero": 0.0, "infinite": float("inf")}[unusable]
    if unusable == "evaluation_failure":
        def broken_evaluation(*args, **kwargs):
            raise ValueError("No hay suficiente histórico.")

        monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", broken_evaluation)

    def forbidden(*args, **kwargs):
        raise AssertionError("No consultar fundamentales ni enviar sin análisis utilizables.")

    summary = run_daily_alerts(
        owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=forbidden, sender=forbidden, today=date(2026, 10, 15),
    )

    assert summary.users_checked == 1
    assert summary.emails_sent == 0
    assert summary.alerts_sent == 0
    assert user.state_writes == 0
    assert user.saved_states == []
    assert user.snapshots == []
    assert any("no hay análisis frescos utilizables" in error for error in summary.errors)


def test_optional_group_plan_failure_does_not_cancel_private_daily_review() -> None:
    class BrokenGroup(FakeGroupJournal):
        def list_portfolio_snapshot_positions(self):
            raise ValueError("snapshot unavailable")

        def list_enabled_alert_preferences(self):
            return [normalize_alert_preferences(owner="ddriu", email="ddriu@example.com",
                                                enabled=True, include_group=False)]
    group, user = BrokenGroup(), FakeJournal(tickers=("GOOD",))
    sent = []
    result = run_daily_alerts(journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: pd.DataFrame(),
        fundamental_downloader=lambda ticker: {}, sender=lambda *args: sent.append(args), today=date(2026, 7, 29))
    assert result.users_checked == 1
    assert result.tickers_checked == 1
    assert result.emails_sent == 1
    assert result.alerts_sent == 0
    assert any("planes no disponibles" in error for error in result.errors)
    assert len(sent) == 1


def test_primary_candidate_preserves_auxiliary_event_warning_and_its_evidence(monkeypatch) -> None:
    from src.entry_opportunity import STATUS_EVENT
    group, user = FakeGroupJournal(), FakeJournal(tickers=("AAA",))
    frame = pd.DataFrame({"open": [99.0], "high": [101.0], "low": [98.0],
                          "close": [100.0], "atr_14": [2.0]}, index=pd.DatetimeIndex(["2026-07-28"]))
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    signal = SignalResult(ticker="AAA", as_of=pd.Timestamp("2026-07-28"), score=20,
                         label="No entrar", position_label="Mantener", explanation="Técnica auxiliar débil.",
                         positive_factors=(), risk_factors=())
    monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", lambda *args, **kwargs: signal)
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=85, confidence_pct=90, label="Entrada fuerte", growth_score=80, momentum_score=90,
        context_score=80, sector_key="technology", as_of=pd.Timestamp("2026-07-28"),
        positive_factors=("El flujo de caja crece.",), risk_factors=("Valoración exigente.",),
        manual_checks=("Contrastar próximos resultados.",)))
    monkeypatch.setattr("src.alert_runner.evaluate_fundamentals", lambda *args, **kwargs: SimpleNamespace(
        score=75, coverage_pct=80, sector="Technology", country="US"))
    monkeypatch.setattr("src.alert_runner.evaluate_fundamental_filter", lambda *args, **kwargs: SimpleNamespace(
        score=60, coverage_pct=80, label="Calidad mixta"))
    monkeypatch.setattr("src.alert_runner.evaluate_valuation", lambda *args, **kwargs: SimpleNamespace(score=40, coverage_pct=80))
    monkeypatch.setattr("src.alert_runner.evaluate_risk", lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=100))
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        timing=SimpleNamespace(score=30), opportunity_score=45, confidence_pct=80,
        status_code=STATUS_EVENT, status_label="Evento próximo; no entrar",
        zones=SimpleNamespace(preferred_entry=SimpleNamespace(label="85–90")),
        event=SimpleNamespace(label="Resultados mañana")))
    sent = []
    result = run_daily_alerts(journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=_verified_growth_info,
        sender=lambda *args: sent.append(args), today=date(2026, 7, 29))
    assert result.alerts_sent == 1
    assert result.emails_sent == 1
    plain = sent[0][2]
    assert "Evento próximo; no entrar" in plain
    assert "Resultados mañana" in plain
    assert "El flujo de caja crece." in plain
    assert "Valoración exigente." in plain
    assert "dual_strategy_v1" in plain


def test_one_invalid_ticker_does_not_cancel_the_user_digest(monkeypatch) -> None:
    group = FakeGroupJournal()
    user = FakeJournal(tickers=("BAD", "GOOD"))
    sent: list[tuple[object, ...]] = []

    def journal_factory(owner: str) -> FakeJournal:
        return group if owner == GROUP_PORTFOLIO_OWNER else user

    def downloader(
        ticker: str,
        start: date,
        end: date,
        *,
        auto_adjust: bool,
    ) -> pd.DataFrame:
        del ticker, start, end, auto_adjust
        return pd.DataFrame(
            {
                "open": [99.0],
                "high": [101.0],
                "low": [98.0],
                "close": [100.0],
                "atr_14": [2.0],
            },
            index=pd.DatetimeIndex(["2026-07-28"]),
        )

    def evaluate(frame, config, *, ticker: str, entry_price=None) -> SignalResult:
        del frame, config, entry_price
        if ticker == "BAD":
            raise ValueError("No hay suficiente histórico.")
        return SignalResult(
            ticker=ticker,
            as_of=pd.Timestamp("2026-07-28"),
            score=80,
            label="Entrada fuerte",
            position_label="Mantener",
            explanation="Señal de prueba.",
            positive_factors=("tendencia positiva",),
            risk_factors=(),
        )

    monkeypatch.setattr("src.alert_runner.add_indicators", lambda frame, config: frame)
    monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", evaluate)
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=85, confidence_pct=90, label="Entrada fuerte", growth_score=80,
        momentum_score=90, context_score=80, sector_key="technology", as_of=pd.Timestamp("2026-07-28")))
    monkeypatch.setattr(
        "src.alert_runner.evaluate_fundamentals",
        lambda info, ticker: SimpleNamespace(
            score=75, coverage_pct=80, sector="Technology", country="US"
        ),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_valuation",
        lambda info, ticker: SimpleNamespace(score=70, coverage_pct=80),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_risk",
        lambda ticker, frame: SimpleNamespace(score=70, coverage_pct=100),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_entry_opportunity",
        lambda **kwargs: SimpleNamespace(
            timing=SimpleNamespace(score=75),
            opportunity_score=80,
            confidence_pct=80,
            status_code=STATUS_BUYABLE,
            status_label="🟢 COMPRABLE",
            zones=SimpleNamespace(preferred_entry=SimpleNamespace(label="98–100")),
            event=SimpleNamespace(label="Sin evento próximo"),
        ),
    )

    summary = run_daily_alerts(
        journal_factory=journal_factory,
        downloader=downloader,
        fundamental_downloader=_verified_growth_info,
        sender=lambda *args: sent.append(args),
        today=date(2026, 7, 29),
    )

    assert summary.users_checked == 1
    assert summary.tickers_checked == 2
    assert summary.tickers_with_prices == 2
    assert summary.emails_sent == 1
    assert summary.alerts_sent == 1
    assert any("ddriu / BAD" in error for error in summary.errors)
    assert len(sent) == 1
    assert "resumen del cierre" in sent[0][1]
    assert [state.ticker for state in user.saved_states] == ["GOOD"]
    assert user.saved_states[0].signature == "entry:growth_momentum:Entrada fuerte"
    assert user.saved_states[0].company_name == "Empresa GOOD"
    assert user.saved_states[0].opportunity_score == 80


def test_buy_email_waits_when_the_primary_strategy_requires_a_better_price(monkeypatch) -> None:
    group = FakeGroupJournal()
    user = FakeJournal(tickers=("WAIT",))
    sent: list[tuple[object, ...]] = []

    def journal_factory(owner: str) -> FakeJournal:
        return group if owner == GROUP_PORTFOLIO_OWNER else user

    frame = pd.DataFrame(
        {
            "open": [99.0],
            "high": [101.0],
            "low": [98.0],
            "close": [100.0],
            "atr_14": [2.0],
        },
        index=pd.DatetimeIndex(["2026-07-28"]),
    )
    signal = SignalResult(
        ticker="WAIT",
        as_of=pd.Timestamp("2026-07-28"),
        score=82,
        label="Entrada fuerte",
        position_label="Mantener",
        explanation="Señal fuerte, pero el precio debe esperar.",
        positive_factors=(),
        risk_factors=(),
    )
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    monkeypatch.setattr(
        "src.alert_runner.evaluate_latest_signal", lambda *args, **kwargs: signal
    )
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=80, confidence_pct=90, label="Esperar mejor precio", growth_score=80,
        momentum_score=90, context_score=80, sector_key="technology", as_of=pd.Timestamp("2026-07-28")))
    monkeypatch.setattr(
        "src.alert_runner.evaluate_fundamentals",
        lambda *args, **kwargs: SimpleNamespace(
            score=75, coverage_pct=80, sector="Technology", country="US"
        ),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_valuation",
        lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=80),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_risk",
        lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=100),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_entry_opportunity",
        lambda **kwargs: SimpleNamespace(
            timing=SimpleNamespace(score=45),
            opportunity_score=60,
            confidence_pct=80,
            status_code=STATUS_WAIT_PRICE,
            status_label="🟡 ESPERAR PRECIO",
            zones=SimpleNamespace(preferred_entry=SimpleNamespace(label="85–90")),
            event=SimpleNamespace(label="Sin evento próximo"),
        ),
    )

    summary = run_daily_alerts(
        journal_factory=journal_factory,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=lambda ticker: {
            "symbol": ticker,
            "longName": f"Empresa {ticker}",
        },
        sender=lambda *args: sent.append(args),
        today=date(2026, 7, 29),
    )

    assert summary.emails_sent == 1
    assert len(sent) == 1
    assert "resumen del cierre" in sent[0][1]
    assert user.saved_states[0].signature == "entry:growth_momentum:Esperar mejor precio"


def test_daily_snapshots_use_cached_broad_and_sector_relative_strength(
    monkeypatch,
) -> None:
    class SnapshotJournal(FakeJournal):
        def __init__(self, *, tickers: tuple[str, ...] = ()) -> None:
            super().__init__(tickers=tickers)
            self.snapshots: list[dict[str, object]] = []

        def list_analysis_snapshots(self) -> pd.DataFrame:
            return pd.DataFrame(columns=["ticker", "analyzed_at"])

        def add_analysis_snapshot(self, **values: object) -> int:
            self.snapshots.append(values)
            return len(self.snapshots)

    group = FakeGroupJournal()
    user = SnapshotJournal(tickers=("AAA", "BBB"))

    def journal_factory(owner: str) -> FakeJournal:
        return group if owner == GROUP_PORTFOLIO_OWNER else user

    index = pd.date_range(end="2026-07-28", periods=253, freq="B")

    def price_frame(final_price: float) -> pd.DataFrame:
        close = [100.0 + (final_price - 100.0) * step / 252 for step in range(253)]
        return pd.DataFrame(
            {
                "open": close,
                "high": [value * 1.01 for value in close],
                "low": [value * 0.99 for value in close],
                "close": close,
                "volume": [1_000_000.0] * len(close),
                "atr_14": [2.0] * len(close),
            },
            index=index,
        )

    downloaded = {
        "AAA": price_frame(160.0),
        "BBB": price_frame(140.0),
        "SPY": price_frame(105.0),
        "XLK": price_frame(110.0),
    }
    download_calls: list[str] = []

    def downloader(
        ticker: str,
        start: date,
        end: date,
        *,
        auto_adjust: bool,
    ) -> pd.DataFrame:
        del start, end, auto_adjust
        download_calls.append(ticker)
        return downloaded[ticker]

    monkeypatch.setattr("src.alert_runner.add_indicators", lambda frame, config: frame)
    monkeypatch.setattr(
        "src.alert_runner.evaluate_latest_signal",
        lambda frame, config, *, ticker, entry_price=None: SignalResult(
            ticker=ticker,
            as_of=frame.index[-1],
            score=82,
            label="Entrada fuerte",
            position_label="Mantener",
            explanation="Señal de prueba.",
            positive_factors=(),
            risk_factors=(),
        ),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_fundamental_filter",
        lambda *args, **kwargs: SimpleNamespace(score=75, label="Sólida"),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_fundamentals",
        lambda info, ticker: SimpleNamespace(
            score=75, coverage_pct=80, sector="Technology", country="US"
        ),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_valuation",
        lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=80),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_risk",
        lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=100),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_growth_momentum",
        lambda **kwargs: SimpleNamespace(score=75, label="Fuerte"),
    )
    opportunity_inputs: list[tuple[int | None, int]] = []

    def entry_opportunity(**kwargs):
        opportunity_inputs.append(
            (kwargs["relative_score"], kwargs["relative_coverage"])
        )
        return SimpleNamespace(
            timing=SimpleNamespace(score=65),
            opportunity_score=72,
            confidence_pct=80,
            status_code=STATUS_WAIT_PRICE,
            status_label="🟡 ESPERAR PRECIO",
            zones=SimpleNamespace(preferred_entry=SimpleNamespace(label="95–100")),
            event=SimpleNamespace(label="Sin evento próximo"),
            explanation="Análisis automático.",
        )

    monkeypatch.setattr(
        "src.alert_runner.evaluate_entry_opportunity",
        entry_opportunity,
    )

    summary = run_daily_alerts(
        journal_factory=journal_factory,
        downloader=downloader,
        fundamental_downloader=lambda ticker: {
            "symbol": ticker,
            "longName": f"Empresa {ticker}",
            "sector": "Technology",
        },
        sender=lambda *args: None,
        today=date(2026, 7, 29),
    )

    expected_scores = {
        ticker: evaluate_relative_strength(
            ticker,
            downloaded[ticker],
            downloaded["SPY"],
            broad_name="SPY",
            sector=downloaded["XLK"],
            sector_name="XLK",
        ).score
        for ticker in ("AAA", "BBB")
    }
    saved_scores = {
        str(snapshot["ticker"]): snapshot["relative_score"]
        for snapshot in user.snapshots
    }

    assert summary.tickers_checked == 2
    assert summary.tickers_with_prices == 2
    assert download_calls.count("SPY") == 1
    assert download_calls.count("XLK") == 1
    assert saved_scores == expected_scores
    assert opportunity_inputs == [
        (expected_scores["AAA"], 100),
        (expected_scores["BBB"], 100),
    ]


def test_stale_daily_prices_do_not_create_alerts_or_mutate_signal_states(monkeypatch) -> None:
    group = FakeGroupJournal()
    user = FakeJournal(tickers=("OLD",))
    sent: list[tuple[object, ...]] = []
    frame = pd.DataFrame({"close": [100]}, index=pd.DatetimeIndex(["2026-07-28"]))
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", lambda *args, **kwargs: SignalResult(
        ticker="OLD", as_of=frame.index[-1], score=5, label="Esperar",
        position_label="Vender", explanation="Precio antiguo.", positive_factors=(), risk_factors=(),
    ))
    summary = run_daily_alerts(
        journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=lambda ticker: (_ for _ in ()).throw(AssertionError("No consultar datos antiguos")),
        sender=lambda *args: sent.append(args), today=date(2026, 10, 3),
    )
    assert summary.alerts_sent == 0
    assert summary.tickers_with_fresh_prices == 0
    assert user.saved_states == []
    assert len(sent) == 1
    assert "Actualizar datos" in sent[0][2]
    assert "2026-07-28" in sent[0][3]
    assert summary.errors


def test_daily_holding_email_uses_confirmations_instead_of_raw_sell_label(monkeypatch) -> None:
    group = FakeGroupJournal()
    user = FakeJournal(tickers=("MCD",))
    user.open_positions = lambda: pd.DataFrame([{"ticker": "MCD", "average_cost": 100, "quantity": 1}])
    sent: list[tuple[object, ...]] = []
    frame = pd.DataFrame({
        "open": [100], "high": [101], "low": [99], "close": [100], "atr_14": [2],
    }, index=pd.DatetimeIndex(["2026-10-02"]))
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", lambda *args, **kwargs: SignalResult(
        ticker="MCD", as_of=frame.index[-1], score=5, label="Esperar",
        position_label="Vender", explanation="Debilidad de precio aislada.", positive_factors=(), risk_factors=(),
    ))
    monkeypatch.setattr("src.alert_runner.evaluate_fundamentals", lambda *args: SimpleNamespace(score=90, coverage_pct=90, sector="", country="US"))
    monkeypatch.setattr("src.alert_runner.evaluate_valuation", lambda *args: SimpleNamespace(score=80, coverage_pct=90))
    monkeypatch.setattr("src.alert_runner.evaluate_risk", lambda *args: SimpleNamespace(score=90, coverage_pct=100))
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        opportunity_score=80, confidence_pct=90, status_code=STATUS_WAIT_PRICE,
        status_label="ESPERAR PRECIO", explanation="Análisis conjunto.",
    ))
    summary = run_daily_alerts(
        journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=lambda ticker: {"symbol": ticker},
        sender=lambda *args: sent.append(args), today=date(2026, 10, 3),
    )
    assert summary.alerts_sent == 0
    assert len(sent) == 1
    assert "Esperar confirmación" in sent[0][2]
    assert "no equivale" in sent[0][3]
    assert user.saved_states[0].signature == "position:Esperar confirmación"
