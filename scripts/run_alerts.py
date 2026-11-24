"""Punto de entrada para las alertas programadas de GitHub Actions."""

from __future__ import annotations

import os
import sys

from src.alert_runner import run_daily_alerts
from src.digest_schedule import daily_digest_session_date
from src.saved_close_report import archive_saved_close_report
from src.storage import create_journal


def _stage_output(**values):
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")


def configured_virtual_tracker():
    """Only advance the explicitly scoped deployment owner, never activate.

    An absent scope fails closed even if the deployment switch is on. Other
    recipients retain their existing report and delivery preferences.
    """
    from scripts.run_model_tracking import enabled
    scope = os.getenv("MODEL_RESULTS_OWNER", "").strip()
    if not enabled() or not scope:
        return None

    def track(journal, snapshots):
        if getattr(journal, "owner", None) != scope:
            return None
        from scripts.run_model_tracking import collect_owner_models
        return collect_owner_models(journal, snapshots)

    return track


def _archive_delayed_report() -> bool:
    """Recover only the configured repository owner's saved close, not mail."""
    owner = os.getenv("REPORT_OWNER", "").strip()
    repository_owner = os.getenv("GITHUB_REPOSITORY_OWNER", "").strip()
    if not owner or not repository_owner or owner != repository_owner:
        print("Archivo de cierre: omitido_sin_propietario_explicito; sin acceso a datos.")
        return True
    try:
        journal = create_journal(owner)
        if getattr(journal, "owner", None) != owner:
            raise ValueError("Mismatched report owner")
        result = archive_saved_close_report(journal)
    except Exception as exc:
        # Public Actions logs must never expose owners, tickers or source bodies.
        print(f"::warning::No se pudo archivar el cierre guardado ({type(exc).__name__}). "
              "No se intentó correo ni se activó Arena.", file=sys.stderr)
        print("Archivo de cierre: archivo=error; sin envío.")
        _stage_output(report_archive_status="error")
        return False
    # Per-owner coverage and incidents belong in the private archived report,
    # not in this public repository's Actions output.
    print(f"Archivo de cierre: archivo={result.status}. "
          "Sólo datos guardados; sin envío ni activación de Arena.")
    _stage_output(report_archive_status=result.status, report_archive_session=result.session)
    return True


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    session = None
    if event_name == "schedule":
        session = daily_digest_session_date()
        if session is None:
            # GitHub can start the recovery job hours after its scheduled slot.
            # The delivery window remains strict. Saved report archival is a
            # separate owner-scoped operation and requires no provider or SMTP.
            print("::warning::Ejecución programada omitida: arranque fuera de "
                  "la ventana 23:00–06:00 de Europe/Madrid. "
                  "No se ha analizado ni intentado enviar correo.",
                  file=sys.stderr)
            print("Estado: omitido_fuera_de_horario. "
                  "Sin revisión ni intento de envío; no se amplía la ventana de seguridad.")
            _stage_output(status="omitted_outside_window")
            return 0 if _archive_delayed_report() else 1

    _stage_output(market_session=session.isoformat() if session is not None else "")

    kwargs = {}
    # Model collection belongs to model-results.yml. Never put simulations or
    # their additional quote downloads on the critical path of email delivery.
    if event_name == "schedule":
        print(f"Sesión objetivo del resumen: {session.isoformat()} (Europe/Madrid).")
        result = run_daily_alerts(today=session, require_current_session=True, **kwargs)
    else:
        result = run_daily_alerts(**kwargs)
    print(
        "Alertas terminadas: "
        f"{result.users_checked} usuarios, "
        f"{result.tickers_with_prices}/{result.tickers_checked} empresas con precios, "
        f"{result.tickers_with_fresh_prices} con fecha utilizable, "
        f"{result.emails_sent} correos y "
        f"{result.alerts_sent} avisos."
    )
    if result.errors:
        # This workflow belongs to a public repository. Errors may contain an
        # owner address, backend response or transport detail. Keep the full
        # structured result private and never publish those strings in CI logs.
        print(f"AVISO: {len(result.errors)} incidencias de actualización o envío. "
              "Los detalles privados no se muestran en este registro.", file=sys.stderr)
    # Arena is a separate workflow step with its own safe diagnostics. Never
    # rerun successful Stock Signal Lab mail to recover a downstream failure.
    # Un fallo total debe ser visible en Actions. No se vuelve a enviar ningún
    # correo aquí: los destinatarios ya entregados no se duplican.
    if result.tickers_checked and result.tickers_with_fresh_prices == 0:
        return 1
    if (result.users_checked and result.tickers_checked and result.emails_sent == 0
            and (result.emails_skipped == 0 or result.errors)):
        return 1
    if result.errors and result.users_checked == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
