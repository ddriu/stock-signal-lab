"""Owner-scoped closing analysis for Arena recovery; never mail or models.

This stage uses the existing recovery schedule. Its only writes are normal
analysis snapshots and an immutable saved-close report. The downstream Arena
stage is separate and may proceed only if the saved report is authorized.
"ready" describes a current verified archive, not a claim that every input was
newly downloaded by this run; the final backend rechecks favorites and consent.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import logging
import os
import re
import sys

import exchange_calendars
import pandas as pd

from src.alert_runner import run_daily_alerts
from src.arena_data_quality import arena_report_authorized
from src.arena_universe import read_arena_universe_policy
from src.arena_universe_policy import validate_arena_policy_owner
from src.instruments import resolve_analysis_ticker
from src.saved_close_report import latest_completed_us_session
from src.storage import create_journal


def _stage_output(**values):
    destination = os.getenv("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")


def _owner_scope():
    """Never enumerate recipients or infer an owner from a default backend."""
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return None
    owner = os.getenv("ARENA_DATA_OWNER", "").strip()
    repository_owner = os.getenv("GITHUB_REPOSITORY_OWNER", "").strip()
    if not owner or owner != repository_owner:
        return None
    try:
        return validate_arena_policy_owner(owner)
    except ValueError:
        return None


def _target_session(now=None):
    """Collect only after a closed US session and before its next opening.

    This is deliberately not the 23:00–06:00 email window. It never changes
    SMTP's schedule, invents a session or prepares an already-open round.
    """
    clock = datetime.now(timezone.utc) if now is None else now
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("The recovery clock must include a timezone")
    clock = pd.Timestamp(clock).tz_convert("UTC")
    session = latest_completed_us_session(reference_time=clock)
    calendar = exchange_calendars.get_calendar("XNYS")
    day = pd.Timestamp(session)
    opening = calendar.session_open(calendar.next_session(day))
    closing = calendar.session_close(day)
    return session if closing <= clock < opening else None


def _quiet_analysis(**kwargs):
    # Providers can log private watchlists or transport bodies in a public CI
    # job. Preserve the structured result, but never publish those raw logs.
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return run_daily_alerts(**kwargs)
    finally:
        logging.disable(previous)


def _authorized_saved_report(journal, session, reference_clock):
    """Reuse only the current immutable close, current favorites and consent.

    Recovery is a retry of the same decision baseline. Recollecting an already
    authorized close can change its report ID and strand partially saved plans.
    The final SQL/Worker still repeat their independent validation under lock.
    """
    report = journal.get_latest_daily_score_report()
    if (not isinstance(report, dict) or report.get("as_of") != session
            or report.get("review_mode") not in (None, "saved_close_archive")
            or not isinstance(report.get("report_id"), str)
            or re.fullmatch(r"[0-9a-f]{64}", report["report_id"]) is None):
        return None
    favorites = journal.list_favorites()
    if (not isinstance(favorites, pd.DataFrame) or "ticker" not in favorites
            or any(not isinstance(value, str) or not value.strip() for value in favorites["ticker"])):
        return None
    if "owner" in favorites and not favorites["owner"].eq(journal.owner).fillna(False).all():
        raise ValueError("Mismatched favorite owner")
    current_favorites = sorted({resolve_analysis_ticker(value.strip()) for value in favorites["ticker"]})
    manifest = report.get("arena_quality")
    if not isinstance(manifest, dict) or manifest.get("favorite_tickers") != current_favorites:
        return None
    policy = read_arena_universe_policy(journal, reference_time=reference_clock)
    return report if arena_report_authorized(report, expected_session=session,
        owner=journal.owner, universe_policy=policy) else None


def main(*, now=None) -> int:
    owner = _owner_scope()
    if owner is None:
        print("Arena datos: omitido_sin_propietario_explicito. Sin acceso a datos.")
        _stage_output(status="omitted_unscoped")
        return 0
    try:
        session = _target_session(now)
    except Exception as exc:
        print(f"::error::Arena datos: reloj o calendario no verificable ({type(exc).__name__}).", file=sys.stderr)
        _stage_output(status="error")
        return 1
    if session is None:
        print("Arena datos: omitido_fuera_de_ventana. La próxima apertura ya no es futura.")
        _stage_output(status="omitted_outside_execution_window")
        return 0

    try:
        journal = create_journal(owner)
        if getattr(journal, "owner", None) != owner:
            raise ValueError("Mismatched owner")
        reference_clock = datetime.now(timezone.utc) if now is None else now
        if _authorized_saved_report(journal, session, reference_clock) is not None:
            print("Arena datos: ready; archivo vigente reutilizado sin recalcular ni cambiar su identidad.")
            _stage_output(status="ready", report_session=session)
            return 0

        def bound_journal(requested_owner):
            if requested_owner != owner:
                raise ValueError("Cross-owner analysis is forbidden")
            return journal

        summary = _quiet_analysis(owner=owner, journal_factory=bound_journal,
            today=datetime.fromisoformat(session).date(), require_current_session=True,
            analysis_only=True)
        if summary.emails_sent != 0 or summary.alerts_sent != 0:
            raise RuntimeError("Analysis-only contract violated")
        reference_clock = datetime.now(timezone.utc) if now is None else now
        if _target_session(reference_clock) != session:
            print("Arena datos: omitido_fuera_de_ventana tras la actualización. El archivo se conserva.")
            _stage_output(status="omitted_outside_execution_window")
            return 0
        ready = _authorized_saved_report(journal, session, reference_clock) is not None
    except Exception as exc:
        # No provider strings, addresses, owner names, report IDs or credentials.
        print(f"::error::Arena datos: revisión incompleta ({type(exc).__name__}). "
              "No se solicita correo, modelo ni operación.", file=sys.stderr)
        _stage_output(status="error")
        return 1

    status = "ready" if ready else "blocked"
    print(f"Arena datos: {status}. Análisis y archivo privados; sin correo, modelos ni operaciones.")
    _stage_output(status=status, report_session=session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
