"""Offline UI checks: explicit, owner-scoped email reviews and safe retries."""

from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app as application
from src.alert_runner import AlertRunSummary
from src.alerts import AlertPreferences
from src.storage import GROUP_PORTFOLIO_OWNER


_SCRIPT = """
import app
app.render_email_alert_settings(app._manual_email_test_journal)
"""
_PREFERENCES = AlertPreferences(
    owner="alice", email="alice@example.test", enabled=True, include_group=False,
)


def _panel(monkeypatch, *, preferences=_PREFERENCES, owner="alice", configured=True,
           summary=None, exception=None):
    # No database, SMTP or market-data service is touched by these tests.
    forbidden = Mock(side_effect=AssertionError("External I/O is forbidden"))
    journal = SimpleNamespace(
        owner=owner,
        get_alert_preferences=Mock(return_value=preferences),
        list_alert_states=Mock(return_value=pd.DataFrame()),
        save_alert_preferences=forbidden,
    )
    runner = Mock(
        return_value=summary or AlertRunSummary(1, 3, 1, 0, (), 3, 3),
        side_effect=exception,
    )
    monkeypatch.setattr(application, "_manual_email_test_journal", journal, raising=False)
    monkeypatch.setattr(application, "run_daily_alerts", runner)
    monkeypatch.setattr(application, "load_email_config", lambda: SimpleNamespace(configured=configured))
    monkeypatch.setattr(application, "send_test_email", forbidden)
    monkeypatch.setattr(application, "create_journal", forbidden)
    monkeypatch.setattr(application, "download_prices", forbidden)
    monkeypatch.setattr("src.email_sender.smtplib.SMTP", forbidden)
    monkeypatch.setattr("src.email_sender.smtplib.SMTP_SSL", forbidden)
    page = AppTest.from_string(_SCRIPT, default_timeout=30).run()
    assert not page.exception
    return page, runner, forbidden


def _text(page):
    return "\n".join(
        element.value
        for kind in ("info", "warning", "error", "success", "caption", "markdown")
        for element in getattr(page, kind)
    )


def _send(page):
    return next(button for button in page.button if button.label == "Revisar mis favoritas y enviarme el resumen")


def _retry(page):
    return next(button for button in page.button if button.label == "Reintentar la revisión y el envío")


def _confirmation(page):
    return next(item for item in page.checkbox if item.label.startswith("Confirmo que quiero reintentar"))


def _assert_no_delivery_success(page):
    assert all("El canal de correo está conectado" in item.value for item in page.success)


def test_opening_and_rerunning_settings_never_sends_or_writes(monkeypatch):
    page, runner, forbidden = _panel(monkeypatch)
    page.run()
    assert not page.exception
    runner.assert_not_called()
    forbidden.assert_not_called()
    assert not _send(page).disabled
    assert "alice@example.test" in _text(page)
    assert "07:15 UTC" in _text(page)
    assert "la cartera del grupo no está incluida" in _text(page)
    assert "no se mezclan las notas ni se exige consenso" in _text(page)


def test_explicit_click_scopes_review_to_current_owner_and_blocks_repeats(monkeypatch):
    page, runner, forbidden = _panel(monkeypatch)
    _send(page).click().run()
    assert not page.exception
    runner.assert_called_once_with(owner="alice")
    assert _send(page).disabled
    assert "SMTP aceptó 1 correo(s) para alice@example.test" in _text(page)
    assert "no confirma su llegada" in _text(page)
    # Even a stale widget event cannot bypass the stored attempt.
    page.run()
    # Simulate an event queued before the browser learned the button was disabled.
    stale_button = _send(page)
    stale_button.proto.disabled = False
    stale_button.click().run()
    runner.assert_called_once_with(owner="alice")
    assert not any(button.label == "Reintentar la revisión y el envío" for button in page.button)
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "preferences,configured",
    [
        (replace(_PREFERENCES, enabled=False), True),
        (replace(_PREFERENCES, email=""), True),
        (_PREFERENCES, False),
    ],
)
def test_disabled_or_missing_saved_channel_prevents_manual_review(monkeypatch, preferences, configured):
    page, runner, forbidden = _panel(monkeypatch, preferences=preferences, configured=configured)
    assert _send(page).disabled
    page.run()
    runner.assert_not_called()
    forbidden.assert_not_called()


@pytest.mark.parametrize("owner", ["bob", "", None, GROUP_PORTFOLIO_OWNER])
def test_invalid_or_mismatched_owner_blocks_all_email_controls(monkeypatch, owner):
    page, runner, forbidden = _panel(monkeypatch, owner=owner)
    assert any("cuenta distinta" in item.value for item in page.error)
    assert not page.button
    assert "alice@example.test" not in _text(page)
    runner.assert_not_called()
    forbidden.assert_not_called()


def test_unsaved_email_or_toggle_changes_do_not_redirect_or_disable_saved_review(monkeypatch):
    page, runner, forbidden = _panel(monkeypatch)
    page.text_input[0].set_value("another@example.test")
    page.toggle[0].set_value(False)
    page.run()
    _send(page).click().run()
    runner.assert_called_once_with(owner="alice")
    assert "SMTP aceptó 1 correo(s) para alice@example.test" in _text(page)
    assert "SMTP aceptó 1 correo(s) para another@example.test" not in _text(page)
    forbidden.assert_not_called()


def test_partial_smtp_acceptance_and_price_coverage_are_explicit_and_never_retried(monkeypatch):
    partial = AlertRunSummary(1, 3, 1, 0, ("Second SMTP attempt failed",), 2, 1)
    page, runner, forbidden = _panel(monkeypatch, summary=partial)
    _send(page).click().run()
    assert not page.exception
    metrics = {item.label: item.value for item in page.metric}
    assert metrics == {
        "Empresas solicitadas": "3", "Con precios": "2",
        "Con precios recientes": "1", "Correos aceptados por SMTP": "1",
    }
    assert any("Revisión parcial: 1 incidencia(s)" in item.value for item in page.warning)
    assert "SMTP aceptó 1" in _text(page)
    assert "no confirma su llegada" in _text(page)
    _assert_no_delivery_success(page)
    assert _send(page).disabled
    assert not any(button.label == "Reintentar la revisión y el envío" for button in page.button)
    page.run()
    runner.assert_called_once_with(owner="alice")
    forbidden.assert_not_called()


def test_zero_acceptances_require_separate_confirmation_before_retry(monkeypatch):
    no_acceptance = AlertRunSummary(1, 0, 0, 0, ())
    page, runner, forbidden = _panel(monkeypatch, summary=no_acceptance)
    _send(page).click().run()
    assert "No consta ningún correo aceptado" in _text(page)
    _assert_no_delivery_success(page)
    assert _send(page).disabled
    assert _retry(page).disabled
    page.run()
    runner.assert_called_once_with(owner="alice")
    _confirmation(page).check().run()
    assert not _retry(page).disabled
    runner.assert_called_once_with(owner="alice")
    _retry(page).click().run()
    assert not page.exception
    assert runner.call_count == 2
    assert all(call.kwargs == {"owner": "alice"} for call in runner.call_args_list)
    # Every retry needs fresh confirmation, not a retained checked widget.
    assert not _confirmation(page).value
    assert _retry(page).disabled
    page.run()
    assert runner.call_count == 2
    forbidden.assert_not_called()


def test_unexpected_failure_keeps_attempt_guard_and_hides_provider_details(monkeypatch):
    page, runner, forbidden = _panel(monkeypatch, exception=RuntimeError("password TOP-SECRET"))
    _send(page).click().run()
    assert not page.exception
    assert "no hay un resultado confirmado" in _text(page)
    assert "TOP-SECRET" not in _text(page)
    assert _send(page).disabled
    assert not any(button.label == "Reintentar la revisión y el envío" for button in page.button)
    page.run()
    runner.assert_called_once_with(owner="alice")
    forbidden.assert_not_called()


def test_zero_count_with_smtp_or_other_errors_is_not_proof_of_nonacceptance(monkeypatch):
    uncertain = AlertRunSummary(1, 3, 0, 0, ("Timeout after SMTP DATA",), 3, 3)
    page, runner, forbidden = _panel(monkeypatch, summary=uncertain)
    _send(page).click().run()
    assert not page.exception
    assert "cero aceptaciones registradas no garantiza" in _text(page)
    assert _send(page).disabled
    assert not any(button.label == "Reintentar la revisión y el envío" for button in page.button)
    _assert_no_delivery_success(page)
    page.run()
    runner.assert_called_once_with(owner="alice")
    forbidden.assert_not_called()


def test_retry_confirmation_is_not_reused_when_switching_accounts(monkeypatch):
    page, runner, forbidden = _panel(monkeypatch, summary=AlertRunSummary(1, 0, 0, 0, ()))
    _send(page).click().run()
    _confirmation(page).check().run()
    alice_confirmation_key = _confirmation(page).key
    assert not _retry(page).disabled

    journal = application._manual_email_test_journal
    journal.owner = "bob"
    journal.get_alert_preferences.return_value = replace(
        _PREFERENCES, owner="bob", email="bob@example.test",
    )
    page.run()
    assert not _send(page).disabled
    _send(page).click().run()
    assert not page.exception
    assert _confirmation(page).key != alice_confirmation_key
    assert not _confirmation(page).value
    assert _retry(page).disabled
    assert [call.kwargs for call in runner.call_args_list] == [{"owner": "alice"}, {"owner": "bob"}]
    forbidden.assert_not_called()


def test_attempt_is_persisted_before_runner_can_perform_external_work(monkeypatch):
    page, runner, forbidden = _panel(monkeypatch)

    def observe_attempt(*, owner):
        key = f"_manual_email_review_{sha256(owner.encode()).hexdigest()}"
        attempt = application.st.session_state[key]
        assert attempt["status"] == "started"
        assert attempt["number"] == 1
        assert attempt["email"] == "alice@example.test"
        return AlertRunSummary(1, 3, 1, 0, (), 3, 3)

    runner.side_effect = observe_attempt
    _send(page).click().run()
    assert not page.exception
    assert "SMTP aceptó 1" in _text(page)
    runner.assert_called_once_with(owner="alice")
    forbidden.assert_not_called()


def test_group_inclusion_is_explicit_and_does_not_change_recipient_scope(monkeypatch):
    page, runner, forbidden = _panel(monkeypatch, preferences=replace(_PREFERENCES, include_group=True))
    assert "más las favoritas y posiciones del grupo" in _text(page)
    _send(page).click().run()
    runner.assert_called_once_with(owner="alice")
    forbidden.assert_not_called()
