"""Punto de entrada para las alertas programadas de GitHub Actions."""

from __future__ import annotations

import os
import sys

from src.alert_runner import run_daily_alerts
from src.digest_schedule import daily_digest_session_date


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


def main() -> int:
    kwargs = {}
    tracker = configured_virtual_tracker()
    if tracker is not None:
        kwargs["virtual_tracker"] = tracker
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    if event_name == "schedule":
        session = daily_digest_session_date()
        if session is None:
            print("::error::No se analizó ni envió el resumen: arranque fuera de "
                  "la ventana 23:00–06:00 de Europe/Madrid. Revisa la programación.",
                  file=sys.stderr)
            return 1
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
