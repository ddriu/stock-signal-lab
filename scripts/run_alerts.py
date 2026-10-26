"""Punto de entrada para las alertas programadas de GitHub Actions."""

from __future__ import annotations

import os
import sys

from src.alert_runner import run_daily_alerts
from src.digest_schedule import daily_digest_window


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    if not daily_digest_window(event_name=event_name):
        print("Sin envío: fuera de la ventana de las 23:00 de Madrid.")
        return 0
    result = (
        run_daily_alerts(require_current_session=True)
        if event_name == "schedule"
        else run_daily_alerts()
    )
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
