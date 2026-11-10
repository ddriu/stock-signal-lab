"""CLI and Streamlit through the real digest, SQLite and MIME; no external I/O."""

from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from scripts import run_alerts
from src import email_sender
from src.alert_runner import run_daily_alerts
from src.alerts import normalize_alert_preferences
from src.daily_digest import seal_daily_score_report
from src.data_loader import DataDownloadError
from src.digest_schedule import daily_digest_session_date
from src.journal import TradingJournal
from src.storage import GROUP_PORTFOLIO_OWNER


@pytest.fixture
def email_sandbox(tmp_path, monkeypatch):
    owner = "nightly-test-owner"
    recipient = "nightly@example.invalid"
    db = tmp_path / "nightly.sqlite3"
    group_db = tmp_path / "group.sqlite3"
    user = TradingJournal(db, owner=owner)
    user.add_favorite("QATEST", "Compañía sintética")
    user.save_alert_preferences(normalize_alert_preferences(
        owner=owner, email=recipient, enabled=True, include_group=False,
    ))

    class LocalRegistry(TradingJournal):
        """Use the real SQLite journal; only substitute account discovery."""

        def list_enabled_alert_preferences(self):
            return [TradingJournal(db, owner=owner).get_alert_preferences()]

    def journal_factory(account):
        # Construct fresh instances just as independent scheduled processes do.
        if account == GROUP_PORTFOLIO_OWNER:
            return LocalRegistry(group_db, owner=account)
        assert account == owner
        return TradingJournal(db, owner=account)

    session = date(2025, 11, 3)  # Monday; historical even on machines with earlier clocks.
    state = SimpleNamespace(
        collection_time=datetime(2025, 11, 3, 23, 0, tzinfo=ZoneInfo("Europe/Madrid")),
        prices_available=True, market_calls=[], messages=[],
    )

    class JournalClock(datetime):
        @classmethod
        def now(cls, tz=None):
            # The SQLite snapshot stores a naïve timestamp interpreted as UTC
            # by the report; make the historical fixture independent of TZ.
            if tz is None:
                return state.collection_time.astimezone(timezone.utc).replace(tzinfo=None)
            return state.collection_time.astimezone(tz)

    monkeypatch.setattr("src.journal.datetime", JournalClock)
    index = pd.bdate_range(end=session, periods=300)
    price = np.linspace(85.0, 122.0, len(index))
    price_frame = pd.DataFrame(
        {"open": price - 0.30, "high": price + 0.60, "low": price - 0.80,
         "close": price, "volume": np.full(len(index), 2_000_000.0)}, index=index,
    )

    def prices(ticker, _start, end, *, auto_adjust):
        assert end == session and auto_adjust
        state.market_calls.append(ticker)
        if not state.prices_available:
            raise DataDownloadError("Cierre sintético aún no disponible")
        return price_frame.copy(deep=True)

    metrics = dict(revenueGrowth=.2, earningsGrowth=.3, operatingMargins=.2,
                   returnOnEquity=.2, freeCashflow=1_000_000_000)
    fundamentals = dict(longName="Compañía sintética", sector="Technology", currency="USD",
                        **metrics, _metric_periods={
                            name: {"periodicity": "annual", "period_end": "2024-12-31",
                                   "source": "Datos sintéticos", "comparable": True}
                            for name in metrics
                        })

    class LocalSMTP:
        def __init__(self, host, port, **_kwargs):
            assert (host, port) == ("smtp.example.invalid", 465)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def login(self, username, password):
            assert (username, password) == ("synthetic", "test-only")

        def send_message(self, message):
            state.messages.append(message.as_bytes())
            return {}  # The same successful DATA outcome smtplib returns.

    smtp_config = email_sender.EmailConfig(
        host="smtp.example.invalid", port=465, username="synthetic", password="test-only",
        sender_email="sender@example.invalid",
    )
    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", LocalSMTP)
    monkeypatch.setattr(email_sender.smtplib, "SMTP",
                        lambda *a, **k: pytest.fail("Unexpected SMTP transport"))
    monkeypatch.setenv("MODEL_RESULTS_ENABLED", "false")

    def run(*, today=session, **kwargs):
        return run_daily_alerts(
            today=today, **kwargs, journal_factory=journal_factory, downloader=prices,
            fundamental_downloader=lambda _symbol: fundamentals,
            sender=lambda *args: email_sender.send_email(*args, config=smtp_config),
        )

    def deliveries():
        with sqlite3.connect(db) as connection:
            return connection.execute(
                "SELECT market_date, report_id, status FROM daily_digest_deliveries WHERE owner = ?",
                (owner,),
            ).fetchall()

    state.owner, state.user, state.recipient, state.session = owner, user, recipient, session
    state.run, state.deliveries, state.smtp_config = run, deliveries, smtp_config
    return state


@pytest.mark.parametrize("initial_prices_available", [True, False], ids=["already-sent", "recovery-sends"])
def test_nightly_and_recovery_send_at_most_one_sealed_digest(
    email_sandbox, monkeypatch, capsys, initial_prices_available,
):
    qa = email_sandbox
    madrid = ZoneInfo("Europe/Madrid")
    clocks = iter([
        qa.collection_time,
        datetime(2025, 11, 4, 1, 17, tzinfo=madrid),
        datetime(2025, 11, 4, 4, 57, tzinfo=madrid),
        datetime(2025, 11, 4, 8, 28, tzinfo=madrid),
    ])
    def target_session():
        qa.collection_time = next(clocks)
        return daily_digest_session_date(now=qa.collection_time)

    qa.prices_available = initial_prices_available
    runner_results = []
    def run(**kwargs):
        assert kwargs == {"today": qa.session, "require_current_session": True}
        result = qa.run(**kwargs)
        runner_results.append(result)
        return result

    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr(run_alerts, "daily_digest_session_date", target_session)
    monkeypatch.setattr(run_alerts, "run_daily_alerts", run)

    assert run_alerts.main() == (0 if initial_prices_available else 1)
    assert runner_results[-1].emails_sent == int(initial_prices_available)
    assert len(qa.messages) == len(qa.deliveries()) == int(initial_prices_available)

    qa.prices_available = True
    assert run_alerts.main() == 0
    assert runner_results[-1].emails_sent == int(not initial_prices_available)
    assert runner_results[-1].emails_skipped == int(initial_prices_available)
    [(market_date, report_id, status)] = qa.deliveries()
    assert market_date == qa.session.isoformat() and status == "sent"
    assert len(qa.messages) == 1
    original = qa.user.get_daily_score_report(report_id)
    assert seal_daily_score_report(original)["report_id"] == report_id
    assert original["as_of"] == market_date
    assert original["rows"][0]["ticker"] == "QATEST"
    assert "virtual_tracking" not in original

    message = BytesParser(policy=policy.default).parsebytes(qa.messages[0])
    assert str(message["To"]) == qa.recipient
    assert "resumen del cierre" in str(message["Subject"])
    assert [part.get_content_type() for part in message.iter_parts()] == ["text/plain", "text/html"]
    assert all(part.get_content_charset() == "utf-8" for part in message.iter_parts())
    assert all(f"?digest={report_id}" in part.get_content() for part in message.iter_parts())

    # A further delayed invocation still cannot duplicate the successful one.
    assert run_alerts.main() == 0
    assert runner_results[-1].emails_sent == 0 and runner_results[-1].emails_skipped == 1
    assert qa.deliveries() == [(market_date, report_id, "sent")]
    assert len(qa.messages) == 1
    assert qa.user.get_daily_score_report(report_id) == original

    # After the safety window, even the journal and market provider are skipped.
    market_before = list(qa.market_calls)
    assert run_alerts.main() == 0
    assert len(runner_results) == 3
    assert qa.market_calls == market_before
    assert qa.deliveries() == [(market_date, report_id, "sent")]
    assert len(qa.messages) == 1
    assert "omitido_fuera_de_horario" in capsys.readouterr().out


def test_streamlit_buttons_send_distinct_messages_only_to_the_saved_owner(email_sandbox, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import app

    qa = email_sandbox
    monkeypatch.setattr(app, "_qa_email_journal", qa.user, raising=False)
    monkeypatch.setattr(app, "load_email_config", lambda: qa.smtp_config)
    monkeypatch.setattr(email_sender, "load_email_config", lambda: qa.smtp_config)
    monkeypatch.setattr(app, "run_daily_alerts", qa.run)
    script = "import app\napp.render_email_alert_settings(app._qa_email_journal)"
    page = AppTest.from_string(script).run()
    assert not page.exception and not qa.messages and not qa.market_calls

    def button(label):
        return next(item for item in page.button if item.label == label)

    button("Enviarme un correo de prueba").click().run()
    assert not page.exception and len(qa.messages) == 1 and not qa.market_calls
    assert any("SMTP aceptó el correo de prueba" in item.value for item in page.success)
    assert qa.deliveries() == []

    button("Revisar mis favoritas y enviarme el resumen").click().run()
    assert not page.exception and len(qa.messages) == 2 and "QATEST" in qa.market_calls
    assert button("Revisar mis favoritas y enviarme el resumen").disabled
    [(market_date, report_id, status)] = qa.deliveries()
    assert market_date == qa.session.isoformat() and status == "sent"

    parser = BytesParser(policy=policy.default)
    test_message, digest = map(parser.parsebytes, qa.messages)
    assert all(str(message["To"]) == qa.recipient for message in (test_message, digest))
    assert "correo de prueba" in str(test_message["Subject"])
    assert "resumen del cierre" in str(digest["Subject"])
    assert all(part.get_content_charset() == "utf-8" for part in test_message.iter_parts())
    assert all(f"?digest={report_id}" in part.get_content() for part in digest.iter_parts())
    page.run()
    assert not page.exception and len(qa.messages) == 2

    # A new Streamlit session still uses the same persistent delivery claim.
    page = AppTest.from_string(script).run()
    button("Revisar mis favoritas y enviarme el resumen").click().run()
    assert not page.exception and len(qa.messages) == 2
    assert any("Ya hay un envío registrado o reservado" in item.value for item in page.info)
    assert all(item.label != "Reintentar la revisión y el envío" for item in page.button)
