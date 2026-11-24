"""Recovery orchestration only: no provider, SMTP, model or real backend."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd

import scripts.run_arena_data as cli


@pytest.fixture
def deployment(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "synthetic-owner")
    monkeypatch.setenv("ARENA_DATA_OWNER", "synthetic-owner")
    outputs = []
    monkeypatch.setattr(cli, "_stage_output", lambda **values: outputs.append(values))
    monkeypatch.setattr(cli, "read_arena_universe_policy", lambda *_a, **_k: None)
    return outputs


@pytest.mark.parametrize(("clock", "target"), [
    ("2026-11-24T06:35:00+00:00", "2026-11-23"),
    ("2026-11-24T09:35:00+00:00", "2026-11-23"),
    ("2026-11-24T14:29:59+00:00", "2026-11-23"),
    ("2026-11-24T14:30:00+00:00", None),
    ("2026-11-24T18:00:00+00:00", None),
    ("2026-11-24T21:30:00+00:00", "2026-11-24"),
    ("2026-11-26T09:35:00+00:00", "2026-11-25"),
    ("2026-11-27T18:00:00+00:00", "2026-11-27"),
])
def test_target_uses_actual_exchange_close_and_next_open_not_email_hours(clock, target):
    assert cli._target_session(datetime.fromisoformat(clock)) == target


def test_recovery_requests_only_owner_analysis_then_checks_actual_archive(deployment, monkeypatch, capsys):
    report = {"as_of": "2026-11-23", "review_mode": "saved_close_archive", "report_id": "a" * 64,
              "arena_quality": {"favorite_tickers": ["SYNTH"]}}
    stored = {"report": None}
    journal = SimpleNamespace(owner="synthetic-owner", get_latest_daily_score_report=lambda: stored["report"],
                              list_favorites=lambda: pd.DataFrame({"ticker": ["SYNTH"]}))
    monkeypatch.setattr(cli, "create_journal", lambda owner: journal)
    calls = []

    def analyze(**kwargs):
        calls.append(kwargs)
        assert kwargs["journal_factory"]("synthetic-owner") is journal
        with pytest.raises(ValueError):
            kwargs["journal_factory"]("another-owner")
        stored["report"] = report
        print("PRIVATE_PROVIDER_RESPONSE", file=cli.sys.stderr)
        return SimpleNamespace(emails_sent=0, alerts_sent=0, errors=())

    monkeypatch.setattr(cli, "run_daily_alerts", analyze)
    checks = []
    monkeypatch.setattr(cli, "arena_report_authorized", lambda value, **kwargs: checks.append((value, kwargs)) or True)
    assert cli.main(now=datetime.fromisoformat("2026-11-24T09:35:00+00:00")) == 0
    assert len(calls) == 1
    assert calls[0]["owner"] == "synthetic-owner"
    assert calls[0]["analysis_only"] is True and calls[0]["require_current_session"] is True
    assert calls[0]["today"].isoformat() == "2026-11-23"
    assert "sender" not in calls[0] and "virtual_tracker" not in calls[0] and "review_receiver" not in calls[0]
    assert checks == [(report, {"expected_session": "2026-11-23", "owner": "synthetic-owner",
                               "universe_policy": None})]
    assert deployment == [{"status": "ready", "report_session": "2026-11-23"}]
    output = capsys.readouterr()
    assert "synthetic-owner" not in output.out + output.err
    assert "PRIVATE_PROVIDER_RESPONSE" not in output.out + output.err


@pytest.mark.parametrize("setting", ["missing_scope", "wrong_owner", "outside_actions"])
def test_no_explicit_deployment_owner_means_no_backend_or_analysis(deployment, monkeypatch, setting):
    if setting == "missing_scope":
        monkeypatch.delenv("ARENA_DATA_OWNER")
    elif setting == "wrong_owner":
        monkeypatch.setenv("ARENA_DATA_OWNER", "another-owner")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS")
    monkeypatch.setattr(cli, "create_journal", lambda *_: pytest.fail("No backend access"))
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: pytest.fail("No analysis"))
    assert cli.main() == 0
    assert deployment == [{"status": "omitted_unscoped"}]


def test_already_open_round_never_loads_owner_data(deployment, monkeypatch):
    monkeypatch.setattr(cli, "create_journal", lambda *_: pytest.fail("No backend access"))
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: pytest.fail("No analysis"))
    assert cli.main(now=datetime.fromisoformat("2026-11-24T15:00:00+00:00")) == 0
    assert deployment == [{"status": "omitted_outside_execution_window"}]


@pytest.mark.parametrize("report", [None,
    {"as_of": "2026-11-20", "review_mode": "saved_close_archive", "report_id": "a" * 64},
    {"as_of": "2026-11-23", "review_mode": "manual_preview", "report_id": "a" * 64},
    {"as_of": "2026-11-23", "review_mode": "saved_close_archive", "report_id": "a" * 64},
])
def test_missing_stale_preview_or_unverified_archive_never_arms_downstream(deployment, monkeypatch, report):
    monkeypatch.setattr(cli, "create_journal", lambda _: SimpleNamespace(owner="synthetic-owner",
        get_latest_daily_score_report=lambda: report,
        list_favorites=lambda: pd.DataFrame({"ticker": ["SYNTH"]})))
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: SimpleNamespace(emails_sent=0, alerts_sent=0))
    monkeypatch.setattr(cli, "arena_report_authorized", lambda *_a, **_k: False)
    assert cli.main(now=datetime.fromisoformat("2026-11-24T09:35:00+00:00")) == 0
    assert deployment == [{"status": "blocked", "report_session": "2026-11-23"}]


def test_provider_exception_is_safe_and_does_not_authorize_an_old_report(deployment, monkeypatch, capsys):
    monkeypatch.setattr(cli, "create_journal", lambda _: SimpleNamespace(owner="synthetic-owner",
        get_latest_daily_score_report=lambda: None))

    def failed(**_kwargs):
        raise RuntimeError("PRIVATE_BACKEND_TOKEN")

    monkeypatch.setattr(cli, "run_daily_alerts", failed)
    assert cli.main(now=datetime.fromisoformat("2026-11-24T09:35:00+00:00")) == 1
    output = capsys.readouterr()
    assert "RuntimeError" in output.err and "PRIVATE_BACKEND_TOKEN" not in output.err
    assert deployment == [{"status": "error"}]


def test_window_is_checked_again_after_slow_collection(deployment, monkeypatch):
    targets = iter(["2026-11-23", None])
    monkeypatch.setattr(cli, "_target_session", lambda *_: next(targets))
    reads = []

    def prior_report():
        reads.append(True)
        assert len(reads) == 1, "Do not arm a round after opening"
        return None

    monkeypatch.setattr(cli, "create_journal", lambda _: SimpleNamespace(owner="synthetic-owner",
        get_latest_daily_score_report=prior_report))
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: SimpleNamespace(emails_sent=0, alerts_sent=0))
    assert cli.main() == 0
    assert deployment == [{"status": "omitted_outside_execution_window"}]


def test_revoked_policy_never_authorizes_an_older_verified_only_archive(deployment, monkeypatch):
    import runpy

    from src.arena_universe import read_arena_universe_policy
    from src.arena_universe_policy import build_arena_universe_policy
    from src.arena_data_quality import arena_report_authorized
    from src.saved_close_report import build_saved_close_report
    import pandas as pd

    fixture = runpy.run_path(str(Path(__file__).with_name("test_saved_close_report.py")))
    clock = "2026-11-24T09:35:00Z"
    approved = build_arena_universe_policy("synthetic-owner", "verified_only",
        approved_at="2026-11-23T22:00:00Z", reference_time=clock)
    report = build_saved_close_report(pd.DataFrame([fixture["observation"]("GOOD")]),
        favorite_tickers=["GOOD", "PENDING"], arena_universe_policy=approved,
        arena_policy_owner="synthetic-owner", reference_time=clock)
    assert arena_report_authorized(report, expected_session="2026-11-23", owner="synthetic-owner")
    revoked = build_arena_universe_policy("synthetic-owner", reference_time=clock)
    journal = SimpleNamespace(owner="synthetic-owner", get_latest_daily_score_report=lambda: report,
        get_arena_universe_policy=lambda: revoked,
        list_favorites=lambda: pd.DataFrame({"ticker": ["GOOD", "PENDING"]}))
    monkeypatch.setattr(cli, "create_journal", lambda _: journal)
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: SimpleNamespace(emails_sent=0, alerts_sent=0))
    monkeypatch.setattr(cli, "read_arena_universe_policy", read_arena_universe_policy)
    assert cli.main(now=datetime.fromisoformat("2026-11-24T09:35:00+00:00")) == 0
    assert deployment == [{"status": "blocked", "report_session": "2026-11-23"}]


def test_second_ready_recovery_reuses_exact_immutable_report_without_analysis(deployment, monkeypatch):
    import runpy
    from src.arena_universe_policy import build_arena_universe_policy
    from src.arena_universe import read_arena_universe_policy
    from src.saved_close_report import build_saved_close_report

    fixture = runpy.run_path(str(Path(__file__).with_name("test_saved_close_report.py")))
    clock = "2026-11-24T09:35:00Z"
    report = build_saved_close_report(pd.DataFrame([fixture["observation"]()]),
        favorite_tickers=["SYNTH"], reference_time=clock)
    policy = build_arena_universe_policy("synthetic-owner", reference_time=clock)
    holder, calls = {"report": None}, []
    journal = SimpleNamespace(owner="synthetic-owner", get_latest_daily_score_report=lambda: holder["report"],
        get_arena_universe_policy=lambda: policy,
        list_favorites=lambda: pd.DataFrame({"ticker": ["SYNTH"]}))

    def collect(**_kwargs):
        calls.append(True)
        holder["report"] = report
        return SimpleNamespace(emails_sent=0, alerts_sent=0)

    monkeypatch.setattr(cli, "create_journal", lambda _: journal)
    monkeypatch.setattr(cli, "run_daily_alerts", collect)
    monkeypatch.setattr(cli, "read_arena_universe_policy", read_arena_universe_policy)
    assert cli.main(now=datetime.fromisoformat("2026-11-24T06:35:00+00:00")) == 0
    original_id = holder["report"]["report_id"]
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: pytest.fail("No second scan of a ready close"))
    assert cli.main(now=datetime.fromisoformat("2026-11-24T09:35:00+00:00")) == 0
    assert calls == [True] and holder["report"]["report_id"] == original_id
    assert deployment == [{"status": "ready", "report_session": "2026-11-23"}] * 2


def test_changed_personal_favorites_prevent_reusing_previous_ready_report(deployment, monkeypatch):
    report = {"as_of": "2026-11-23", "review_mode": "saved_close_archive", "report_id": "a" * 64,
              "arena_quality": {"favorite_tickers": ["SYNTH"]}}
    journal = SimpleNamespace(owner="synthetic-owner", get_latest_daily_score_report=lambda: report,
        list_favorites=lambda: pd.DataFrame({"ticker": ["SYNTH", "NEW"]}))
    monkeypatch.setattr(cli, "create_journal", lambda _: journal)
    calls = []
    monkeypatch.setattr(cli, "run_daily_alerts", lambda **_: calls.append(True)
        or SimpleNamespace(emails_sent=0, alerts_sent=0))
    monkeypatch.setattr(cli, "arena_report_authorized", lambda *_a, **_k: pytest.fail("Wrong universe"))
    assert cli.main(now=datetime.fromisoformat("2026-11-24T09:35:00+00:00")) == 0
    assert calls == [True] and deployment == [{"status": "blocked", "report_session": "2026-11-23"}]


def test_actual_analysis_only_persists_archive_and_verifies_without_notification_state(
    deployment, monkeypatch, tmp_path,
):
    import numpy as np
    import pandas as pd
    from datetime import timezone

    import src.alert_runner as runner
    import src.journal as journal_module
    import src.saved_close_report as archive_module
    from src.arena_universe import read_arena_universe_policy
    from src.journal import TradingJournal
    from src.saved_close_report import archive_saved_close_report
    from src.hourly_market_schedule import completed_daily_prices

    clock = datetime.fromisoformat("2026-11-24T09:35:00+00:00")
    journal = TradingJournal(tmp_path / "real-data-only.sqlite", owner="synthetic-owner")
    journal.add_favorite("GOOD", "Synthetic company")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Analysis-only touched notification, model or external I/O")

    assert journal.get_alert_preferences().enabled is False
    for name in ("list_alert_states", "upsert_alert_states",
                 "claim_daily_digest", "finish_daily_digest"):
        monkeypatch.setattr(journal, name, forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("smtplib.SMTP", forbidden)
    monkeypatch.setattr("smtplib.SMTP_SSL", forbidden)
    monkeypatch.setattr(runner, "_record_model_email", forbidden)

    class RecordedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 11, 23, 22, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(journal_module, "datetime", RecordedClock)
    monkeypatch.setattr(runner, "completed_daily_prices",
        lambda ticker, frame, **_: completed_daily_prices(ticker, frame, now=clock))
    monkeypatch.setattr(archive_module, "archive_saved_close_report",
        lambda value, **_: archive_saved_close_report(value, reference_time=clock))
    index = pd.bdate_range(end="2026-11-23", periods=300)
    close = np.linspace(100, 170, len(index))
    frame = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * .99,
                          "close": close, "volume": 1_000_000}, index=index)
    frame.attrs.update(provider="Synthetic", quote_currency="USD", display_currency="USD",
        quote_units_normalized=True, auto_adjust=True, adjustment_status="provider_confirmed",
        daily_session_contract="provider_date_labels")
    values = {"revenueGrowth": .20, "earningsGrowth": .30, "operatingMargins": .25,
              "returnOnEquity": .20, "freeCashflow": 100_000_000.}
    info = {**values, "symbol": "GOOD", "currency": "USD", "quoteType": "EQUITY",
        "sector": "Technology", "marketCap": 10_000_000_000, "forwardPE": 20,
        "_sources": {key: "SEC EDGAR" for key in values},
        "_metric_periods": {key: {"source": "SEC EDGAR", "periodicity": "quarterly",
            "period_end": "2026-09-30", "filed_at": "2026-11-05", "comparable": True}
            for key in values}}
    calls = []

    def actual_runner(**kwargs):
        calls.append(True)
        return runner.run_daily_alerts(**kwargs, downloader=lambda *_a, **_k: frame.copy(deep=True),
            fundamental_downloader=lambda _: info, sender=forbidden)

    monkeypatch.setattr(cli, "create_journal", lambda _: journal)
    monkeypatch.setattr(cli, "run_daily_alerts", actual_runner)
    monkeypatch.setattr(cli, "read_arena_universe_policy", read_arena_universe_policy)
    assert cli.main(now=clock) == 0
    assert calls == [True] and deployment == [{"status": "ready", "report_session": "2026-11-23"}]
    assert not journal.list_analysis_snapshots().empty
    report = journal.get_latest_daily_score_report()
    assert report["review_mode"] == "saved_close_archive"
    assert report["arena_quality"]["authorized"] is True
    with journal._connect() as connection:
        assert connection.execute("select count(*) from daily_score_reports").fetchone()[0] == 1
        assert connection.execute("select count(*) from daily_digest_deliveries").fetchone()[0] == 0
        assert connection.execute("select count(*) from email_alert_states").fetchone()[0] == 0
        assert connection.execute("select count(*) from operations").fetchone()[0] == 0


def test_workflow_collects_before_existing_arena_step_without_smtp_in_data_environment():
    from pathlib import Path
    text = (Path(__file__).parents[1] / ".github/workflows/arena-mail-recovery.yml").read_text()
    data_start = text.index("id: arena_data")
    data_end = text.index("run: python -m scripts.run_arena_data", data_start)
    data_environment = text[data_start:data_end]
    assert "ARENA_DATA_OWNER: ${{ github.repository_owner }}" in data_environment
    assert "EMAIL_" not in data_environment and "SMTP" not in data_environment
    assert "OPENROUTER" not in data_environment and "MODEL_RESULTS" not in data_environment
    assert "steps.arena_data.outputs.status == 'ready'" in text[data_end:]
    assert "ARENA_EXPECTED_SESSION: ${{ steps.arena_data.outputs.report_session }}" in text[data_end:]
    assert text.index("python -m scripts.run_arena_data") < text.index("python -m scripts.send_arena_digest")
