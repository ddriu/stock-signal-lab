"""Arena mail orchestration with synthetic services: never network or SMTP."""

from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import send_arena_digest as arena_mail
from src.email_sender import EmailConfig, EmailDeliveryReceipt


REPORT_DAY = date(2026, 11, 12)
NEXT_SESSION = "2026-11-13"
PRIVATE_MARKER = "private-provider-response-and-recipient@example.invalid"


def response(payload, status=200):
    return SimpleNamespace(
        ok=200 <= status < 300, status_code=status,
        json=lambda: deepcopy(payload), text=PRIVATE_MARKER,
    )


@pytest.fixture
def mail_case(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "ddriu")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    state = SimpleNamespace(
        now=datetime(2026, 11, 13, 2, 27, tzinfo=timezone.utc),
        report={"as_of": REPORT_DAY.isoformat(), "report_id": "a" * 64},
        posts=[], messages=[], journal_owners=[],
        report_verified=True,
        config=EmailConfig(host="smtp.example.invalid", port=465, username="synthetic",
            password="private-password", sender_email="sender@example.invalid"),
        receipt=EmailDeliveryReceipt(status="smtp_accepted", smtp_code=250,
            accepted_at="2026-11-13T02:27:00+00:00", receipt_hash="b" * 64,
            message_hash="c" * 64),
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now.astimezone(tz) if tz else state.now.replace(tzinfo=None)

    monkeypatch.setattr(arena_mail, "datetime", Clock)
    state.journal = SimpleNamespace(url="https://database.example.invalid",
        headers={"apikey": "private-database-key"},
        get_latest_daily_score_report=lambda: deepcopy(state.report))

    def journal_factory(owner):
        state.journal_owners.append(owner)
        return state.journal

    monkeypatch.setattr(arena_mail, "create_journal", journal_factory)
    monkeypatch.setattr(arena_mail, "load_email_config", lambda: state.config)
    monkeypatch.setattr(arena_mail, "arena_report_authorized",
                        lambda report, **kwargs: state.report_verified)
    state.credentials = response({"recipient": "recipient@example.invalid",
        "site_token": "private-site-token", "job_token": "private-job-token"})
    state.digest = {"report_day": REPORT_DAY.isoformat(),
        "open_at": "2026-11-13T14:30:00+00:00", "fee_percent": 0.2,
        "standings": [{"name": "Synthetic model", "market_pnl": 4.0,
            "equity": 1004.0, "cash": 904.0, "day_pnl": None,
            "invested_total": 98.0, "market_value": 100.0,
            "positions": [{"ticker": "SYNTH", "units": 2.0,
                "cost_basis": 98.0, "entry_price": 49.0,
                "value": 100.0, "valuation_date": REPORT_DAY.isoformat()}],
            "plan": {"orders": []}}]}
    state.prepare = response({"status": "ready", "delivery_id": "d" * 64,
        "digest": state.digest})
    state.confirm = response({"status": "accepted"})

    def post(url, **kwargs):
        state.posts.append((url, deepcopy(kwargs)))
        if url.endswith("/rpc/arena_mail_credentials"):
            return state.credentials
        assert url == arena_mail.ARENA + "/api/arena/daily-mail"
        action = kwargs["json"]["action"]
        assert action in {"prepare", "confirm"}
        return getattr(state, action)

    def sender(*args, **kwargs):
        state.messages.append((args, kwargs))
        return state.receipt

    monkeypatch.setattr(arena_mail.requests, "post", post)
    monkeypatch.setattr(arena_mail, "send_email", sender)
    return state


@pytest.mark.parametrize(("owner", "actions"), [("someone-else", "true"), ("ddriu", "")])
def test_non_deployment_run_does_not_read_accounts_or_attempt_mail(monkeypatch, mail_case, owner, actions):
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", owner)
    monkeypatch.setenv("GITHUB_ACTIONS", actions)
    arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert mail_case.journal_owners == mail_case.posts == mail_case.messages == []


@pytest.mark.parametrize("report", [None,
    {"as_of": "2026-11-11", "report_id": "a" * 64}])
def test_absent_or_other_session_report_never_prepares_old_mail(mail_case, report):
    mail_case.report = report
    arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert mail_case.journal_owners == ["ddriu"]
    assert mail_case.posts == mail_case.messages == []


def test_nontrading_report_is_not_a_mail_session(mail_case):
    mail_case.report["as_of"] = "2026-11-14"
    arena_mail.send_arena_digest(expected_session=date(2026, 11, 14))
    assert mail_case.posts == mail_case.messages == []


@pytest.mark.parametrize("clock", ["2026-11-12T20:59:59+00:00",
    "2026-11-13T14:30:00+00:00", "2026-11-13T15:00:00+00:00"])
def test_mail_window_requires_report_close_and_precedes_next_open(mail_case, clock):
    mail_case.now = datetime.fromisoformat(clock)
    arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert mail_case.posts == mail_case.messages == []


def test_ready_mail_is_sent_once_then_confirmed_without_changing_sender_contract(mail_case, capsys):
    arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert len(mail_case.messages) == 1
    args, kwargs = mail_case.messages[0]
    assert args == ("recipient@example.invalid", *arena_mail.render_digest(mail_case.digest))
    assert kwargs == {"config": mail_case.config}
    assert len(mail_case.posts) == 3
    credentials, prepare, confirm = mail_case.posts
    assert credentials[1]["json"] == {}
    assert prepare[1]["json"] == {
        "action": "prepare", "report_day": REPORT_DAY.isoformat(),
        "report_id": "a" * 64, "session": NEXT_SESSION,
        "open_at": "2026-11-13T14:30:00+00:00", "close_at": "2026-11-13T21:00:00+00:00",
    }
    assert confirm[1]["json"] == {"action": "confirm", "session": NEXT_SESSION,
        "delivery_id": "d" * 64}
    for _, request in mail_case.posts:
        assert request["allow_redirects"] is False
    assert "aceptado por Gmail" in capsys.readouterr().out


def test_already_accepted_session_does_not_send_or_confirm_again(mail_case):
    mail_case.prepare = response({"status": "accepted"})
    arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert mail_case.messages == []
    assert [request["json"].get("action") for _, request in mail_case.posts] == [None, "prepare"] * 2


def test_transport_failure_after_known_smtp_acceptance_still_confirms_once(monkeypatch, mail_case):
    def accepted_then_disconnected(*args, **kwargs):
        mail_case.messages.append((args, kwargs))
        failure = RuntimeError(PRIVATE_MARKER)
        failure.smtp_receipt = mail_case.receipt
        raise failure

    monkeypatch.setattr(arena_mail, "send_email", accepted_then_disconnected)
    arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert len(mail_case.messages) == 1
    assert mail_case.posts[-1][1]["json"]["action"] == "confirm"


def assert_private_error(failure, *, phase, code=None, http_status=None):
    error = failure.value
    assert error.phase == phase
    if code is not None:
        assert error.code == code
    assert error.http_status == http_status
    for secret in (PRIVATE_MARKER, "private-password", "private-site-token",
                   "private-job-token", "private-database-key", "recipient@example.invalid"):
        assert secret not in str(error)
        assert secret not in repr(error)


def test_missing_smtp_config_fails_before_credentials_or_mail(mail_case):
    mail_case.config = EmailConfig(host="smtp.example.invalid", port=465,
        username="", password="", sender_email="")
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase="config")
    assert mail_case.posts == mail_case.messages == []


def test_credentials_failure_has_its_own_safe_stage(mail_case):
    mail_case.credentials = response({"error": PRIVATE_MARKER}, status=403)
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase="credentials", code="http_error", http_status=403)
    assert len(mail_case.posts) == 1 and mail_case.messages == []


@pytest.mark.parametrize(("body", "status", "code"), [
    ({"error": "Hay una ronda pendiente con otro informe."}, 409, "report_conflict"),
    ({"error": "Las decisiones guardadas no corresponden al informe actualizado."}, 409, "report_conflict"),
    ({"error": "La Arena está procesando otra ronda."}, 409, "lease_busy"),
    ({"error": "La sesión de ejecución caducó."}, 409, "lease_expired"),
    ({"error": "El informe carece de señales válidas o tiene duplicados."}, 409, "report_no_signals"),
    ({"error": "No se pudieron obtener cotizaciones: " + PRIVATE_MARKER}, 409, "quotes_unavailable"),
    ({"error": "Falta cotización para " + PRIVATE_MARKER}, 409, "quotes_unavailable"),
    ({"error": "La ronda requiere un informe renovado y una sesión futura."}, 409, "report_or_session_not_ready"),
    ({"error": "Todavía no está disponible el informe de Streamlit: " + PRIVATE_MARKER}, 409, "report_not_ready"),
    ({"error": "error de OpenRouter (402): " + PRIVATE_MARKER}, 502, "model_provider_payment_required"),
    ({"error": "error de OpenRouter (429): " + PRIVATE_MARKER}, 502, "model_rate_limited"),
    ({"error": PRIVATE_MARKER}, 409, "http_error"),
    ({"error": {"detail": PRIVATE_MARKER}}, 409, "http_error"),
    ([PRIVATE_MARKER], 409, "http_error"),
])
def test_prepare_conflicts_are_coded_without_logging_private_response(mail_case, capsys, body, status, code):
    mail_case.prepare = response(body, status=status)
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase="prepare", code=code, http_status=status)
    assert len(mail_case.posts) == 2 and mail_case.messages == []
    captured = capsys.readouterr()
    assert PRIVATE_MARKER not in captured.out + captured.err


@pytest.mark.parametrize("status", ["reserved", "uncertain"])
def test_reserved_or_uncertain_session_never_repeats_smtp(mail_case, status):
    mail_case.prepare = response({"status": status})
    for _ in range(2):
        with pytest.raises(arena_mail.ArenaDigestError) as failure:
            arena_mail.send_arena_digest(expected_session=REPORT_DAY)
        assert_private_error(failure, phase="prepare", code="reservation_uncertain")
    assert mail_case.messages == []
    assert [request["json"].get("action") for _, request in mail_case.posts] == [None, "prepare"] * 2


@pytest.mark.parametrize("receipt", [None, SimpleNamespace(status="uncertain"),
    SimpleNamespace(smtp_code=250)])
def test_unconfirmed_smtp_receipt_is_not_confirmed_or_retried(mail_case, receipt):
    mail_case.receipt = receipt
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase="send", code="smtp_unconfirmed")
    assert len(mail_case.messages) == 1
    assert [request["json"].get("action") for _, request in mail_case.posts] == [None, "prepare"]
    # A later run sees the persistent reservation rather than sending again.
    mail_case.prepare = response({"status": "reserved"})
    with pytest.raises(arena_mail.ArenaDigestError) as reserved:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(reserved, phase="prepare", code="reservation_uncertain")
    assert len(mail_case.messages) == 1


def test_smtp_exception_has_safe_send_stage_and_no_immediate_retry(monkeypatch, mail_case):
    def failed_sender(*args, **kwargs):
        mail_case.messages.append((args, kwargs))
        raise RuntimeError(PRIVATE_MARKER)

    monkeypatch.setattr(arena_mail, "send_email", failed_sender)
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase="send")
    assert len(mail_case.messages) == 1
    assert [request["json"].get("action") for _, request in mail_case.posts] == [None, "prepare"]


def test_confirmation_http_failure_does_not_erase_send_or_repeat_smtp(mail_case):
    mail_case.confirm = response({"error": PRIVATE_MARKER}, status=502)
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase="confirm", code="http_error", http_status=502)
    assert len(mail_case.messages) == 1 and len(mail_case.posts) == 3
    mail_case.prepare = response({"status": "accepted"})
    arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert len(mail_case.messages) == 1


def test_unaccepted_confirmation_is_not_reported_as_delivery_success(mail_case, capsys):
    mail_case.confirm = response({"status": "reserved"})
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase="confirm", code="reservation_uncertain")
    assert len(mail_case.messages) == 1
    assert "aceptado por Gmail" not in capsys.readouterr().out


@pytest.mark.parametrize("phase", ["credentials", "prepare", "confirm"])
@pytest.mark.parametrize(("exception_type", "code"), [
    (arena_mail.requests.Timeout, "transport_timeout"),
    (arena_mail.requests.RequestException, "transport_error"),
])
def test_transport_errors_preserve_safe_stage_and_do_not_retry(monkeypatch, mail_case, phase, exception_type, code):
    original_post = arena_mail.requests.post

    def unavailable(url, **kwargs):
        action = kwargs["json"].get("action") or "credentials"
        if action == phase:
            mail_case.posts.append((url, deepcopy(kwargs)))
            raise exception_type(PRIVATE_MARKER)
        return original_post(url, **kwargs)

    monkeypatch.setattr(arena_mail.requests, "post", unavailable)
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase=phase, code=code)
    assert len(mail_case.messages) == (1 if phase == "confirm" else 0)
    assert [request["json"].get("action") or "credentials" for _, request in mail_case.posts].count(phase) == 1


@pytest.mark.parametrize("phase", ["credentials", "prepare", "confirm"])
def test_malformed_response_reports_safe_stage(monkeypatch, mail_case, phase):
    def invalid_json():
        raise ValueError(PRIVATE_MARKER)

    setattr(mail_case, phase, SimpleNamespace(ok=True, status_code=200, json=invalid_json))
    with pytest.raises(arena_mail.ArenaDigestError) as failure:
        arena_mail.send_arena_digest(expected_session=REPORT_DAY)
    assert_private_error(failure, phase=phase, code="invalid_response")
    assert len(mail_case.messages) == (1 if phase == "confirm" else 0)


def test_arena_cli_reports_only_safe_error_fields_without_retrying(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    calls = []

    def fail_once(**kwargs):
        calls.append(kwargs)
        raise arena_mail.ArenaDigestError("prepare", "report_conflict", 409)

    monkeypatch.setattr(arena_mail, "send_arena_digest", fail_once)
    assert arena_mail.main() == 1
    captured = capsys.readouterr()
    assert "fase=prepare" in captured.err
    assert "código=report_conflict" in captured.err and "HTTP=409" in captured.err
    assert "No se reenvían resúmenes de Stock Signal Lab" in captured.err
    assert calls == [{"expected_session": None}]


def test_arena_cli_hides_unclassified_private_exception(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")

    def unavailable(**kwargs):
        raise RuntimeError(PRIVATE_MARKER)

    monkeypatch.setattr(arena_mail, "send_arena_digest", unavailable)
    assert arena_mail.main() == 1
    captured = capsys.readouterr()
    assert PRIVATE_MARKER not in captured.out + captured.err
    assert "no clasificado" in captured.err


def test_arena_cli_skips_after_exchange_open_without_attempting_mail(monkeypatch, capsys, mail_case):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.delenv("ARENA_EXPECTED_SESSION", raising=False)
    monkeypatch.setattr("src.digest_schedule.daily_digest_session_date", lambda: None)
    mail_case.now = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)
    assert arena_mail.main() == 0
    assert mail_case.posts == mail_case.messages == []
    assert "estado=omitted_outside_execution_window" in capsys.readouterr().out


def test_arena_cli_uses_same_target_session_as_daily_report(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr("src.digest_schedule.daily_digest_session_date", lambda: REPORT_DAY)
    calls = []
    monkeypatch.setattr(arena_mail, "send_arena_digest", lambda **kwargs: calls.append(kwargs))
    assert arena_mail.main() == 0
    assert calls == [{"expected_session": REPORT_DAY}]


def test_arena_cli_keeps_upstream_session_after_analysis_crosses_startup_window(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("ARENA_EXPECTED_SESSION", REPORT_DAY.isoformat())
    monkeypatch.setattr("src.digest_schedule.daily_digest_session_date",
                        lambda: pytest.fail("Do not choose a different session in the second step"))
    calls = []
    monkeypatch.setattr(arena_mail, "send_arena_digest", lambda **kwargs: calls.append(kwargs))
    assert arena_mail.main() == 0
    assert calls == [{"expected_session": REPORT_DAY}]


def test_arena_cli_invalid_upstream_session_never_reads_or_sends(monkeypatch):
    monkeypatch.setenv("ARENA_EXPECTED_SESSION", "not-a-session")
    monkeypatch.setattr(arena_mail, "send_arena_digest",
                        lambda **kwargs: pytest.fail("Invalid anchor cannot send"))
    assert arena_mail.main() == 1


def test_arena_cli_recovers_after_stock_window_before_next_open(monkeypatch, mail_case):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.delenv("ARENA_EXPECTED_SESSION", raising=False)
    monkeypatch.setattr("src.digest_schedule.daily_digest_session_date", lambda: None)
    mail_case.now = datetime(2026, 11, 13, 8, 28, tzinfo=timezone.utc)
    assert arena_mail.main() == 0
    assert len(mail_case.messages) == 1
    assert mail_case.posts[-1][1]["json"]["action"] == "confirm"


def test_daily_workflow_does_not_block_arena_when_stock_window_is_closed():
    from pathlib import Path
    workflow = (Path(__file__).parents[1] / ".github/workflows/daily-alerts.yml").read_text()
    assert "if: steps.stock.outputs.status != 'omitted_outside_window'" not in workflow


@pytest.mark.parametrize(("message", "code"), [
    ("Atlas: formato inválido. Las respuestas válidas quedan guardadas. Puedes reintentar.", "model_plan_incomplete"),
    ("Atlas recibió un error de OpenRouter (503). Las respuestas válidas quedan guardadas. Puedes reintentar.", "model_provider_unavailable"),
    ("Atlas recibió un error de OpenRouter (402). Las respuestas válidas quedan guardadas. Puedes reintentar.", "model_provider_payment_required"),
    ("La bolsa ya abrió. La ronda quedó incompleta.", "execution_window_closed"),
    ("La temporada está cerrada.", "season_closed"),
])
def test_additional_prepare_diagnostics_are_safe_and_keep_payment_priority(message, code):
    assert arena_mail._failure_code(response({"error": message + PRIVATE_MARKER}, 409)) == code


def test_cli_partial_models_retry_preparation_once_then_send_once(monkeypatch, mail_case, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.delenv("ARENA_EXPECTED_SESSION", raising=False)
    original_post = arena_mail.requests.post
    prepares = []

    def partial_then_ready(url, **kwargs):
        if kwargs["json"].get("action") == "prepare":
            prepares.append(deepcopy(kwargs["json"]))
            if len(prepares) == 1:
                mail_case.posts.append((url, deepcopy(kwargs)))
                return response({"error": "Atlas inválido. Las respuestas válidas quedan guardadas. Puedes reintentar."}, 409)
        return original_post(url, **kwargs)

    waits = []
    monkeypatch.setattr(arena_mail.requests, "post", partial_then_ready)
    monkeypatch.setattr(arena_mail, "sleep", waits.append)
    assert arena_mail.main() == 0
    assert len(prepares) == 2 and prepares[0] == prepares[1]
    assert waits == [3]
    assert len(mail_case.messages) == 1
    assert mail_case.posts[-1][1]["json"]["action"] == "confirm"
    assert "estado=sent" in capsys.readouterr().out


@pytest.mark.parametrize(("phase", "code"), [
    ("prepare", "model_provider_payment_required"),
    ("prepare", "report_conflict"), ("prepare", "report_no_signals"),
    ("prepare", "reservation_uncertain"), ("prepare", "http_error"),
    ("prepare", "season_closed"), ("prepare", "execution_window_closed"),
    ("send", "smtp_unconfirmed"), ("confirm", "transport_timeout"),
])
def test_cli_does_not_retry_unsafe_or_nonrecoverable_stages(monkeypatch, phase, code):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.delenv("ARENA_EXPECTED_SESSION", raising=False)
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise arena_mail.ArenaDigestError(phase, code, 409)

    monkeypatch.setattr(arena_mail, "send_arena_digest", fail)
    monkeypatch.setattr(arena_mail, "sleep", lambda _: pytest.fail("Must not retry"))
    assert arena_mail.main() == 1
    assert len(calls) == 1


def test_cli_recovery_is_bounded_to_two_attempts(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    calls = []
    monkeypatch.setattr(arena_mail, "sleep", lambda _: None)

    def fail(**kwargs):
        calls.append(kwargs)
        raise arena_mail.ArenaDigestError("prepare", "model_plan_incomplete", 409)

    monkeypatch.setattr(arena_mail, "send_arena_digest", fail)
    assert arena_mail.main() == 1
    assert len(calls) == 2
    assert "estado=pending" in capsys.readouterr().out


def test_prepare_timeout_then_reservation_never_sends_smtp(monkeypatch, mail_case):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr(arena_mail, "sleep", lambda _: None)
    original_post = arena_mail.requests.post
    attempts = []

    def reserved_after_timeout(url, **kwargs):
        if kwargs["json"].get("action") == "prepare":
            attempts.append(1)
            if len(attempts) == 1:
                raise arena_mail.requests.Timeout(PRIVATE_MARKER)
            return response({"status": "reserved"})
        return original_post(url, **kwargs)

    monkeypatch.setattr(arena_mail.requests, "post", reserved_after_timeout)
    assert arena_mail.main() == 1
    assert len(attempts) == 2 and mail_case.messages == []


def test_recovery_rechecks_opening_before_retry(monkeypatch, mail_case, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    mail_case.prepare = response({"error": "La Arena está procesando otra ronda."}, 409)

    def cross_opening(_):
        mail_case.now = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(arena_mail, "sleep", cross_opening)
    assert arena_mail.main() == 0
    assert mail_case.messages == []
    assert len(mail_case.posts) == 2
    assert "estado=omitted_outside_execution_window" in capsys.readouterr().out


def test_recovery_of_already_accepted_mail_is_not_a_second_send(monkeypatch, mail_case, tmp_path):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.delenv("ARENA_EXPECTED_SESSION", raising=False)
    monkeypatch.setattr("src.digest_schedule.daily_digest_session_date", lambda: None)
    mail_case.now = datetime(2026, 11, 13, 10, 35, tzinfo=timezone.utc)
    mail_case.prepare = response({"status": "accepted"})
    output, summary = tmp_path / "output", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert arena_mail.main() == 0
    assert mail_case.messages == []
    assert output.read_text() == "status=already_accepted\n"
    assert "already_accepted" in summary.read_text()


def test_recovery_workflow_is_arena_only_and_shares_delivery_lock():
    from pathlib import Path
    workflow = (Path(__file__).parents[1] / ".github/workflows/arena-mail-recovery.yml").read_text()
    assert 'cron: "35 7 * * 1-5"' in workflow
    assert 'cron: "35 10 * * 1-5"' in workflow
    assert "group: stock-signal-lab-daily-alerts" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "python -m scripts.send_arena_digest" in workflow
    assert "scripts.run_alerts" not in workflow
    assert "MODEL_RESULTS_ENABLED" not in workflow


def test_unverified_report_never_reads_mail_credentials_or_calls_arena(mail_case):
    mail_case.report_verified = False
    assert arena_mail.send_arena_digest(expected_session=REPORT_DAY) == "omitted_unverified_report"
    assert mail_case.posts == mail_case.messages == []


def test_paused_arena_never_sends_smtp_or_confirms(mail_case):
    mail_case.prepare = response({"status": "paused", "message": "Calidad de datos pendiente."})
    assert arena_mail.send_arena_digest(expected_session=REPORT_DAY) == "paused_data_quality"
    assert mail_case.messages == []
    assert [request["json"].get("action") for _, request in mail_case.posts] == [None, "prepare"]


def test_legacy_unsealed_report_cannot_feed_real_quality_gate(monkeypatch, mail_case):
    from src.arena_data_quality import arena_report_authorized
    monkeypatch.setattr(arena_mail, "arena_report_authorized", arena_report_authorized)
    assert arena_mail.send_arena_digest(expected_session=REPORT_DAY) == "omitted_unverified_report"
    assert mail_case.posts == mail_case.messages == []
