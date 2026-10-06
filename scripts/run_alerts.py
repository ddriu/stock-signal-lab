"""Punto de entrada para las alertas programadas de GitHub Actions."""

from __future__ import annotations

import sys

from src.alert_runner import run_daily_alerts


def main() -> int:
    result = run_daily_alerts()
    print(
        "Alertas terminadas: "
        f"{result.users_checked} usuarios, "
        f"{result.tickers_with_prices}/{result.tickers_checked} empresas con precios, "
        f"{result.tickers_with_fresh_prices} con fecha utilizable, "
        f"{result.emails_sent} correos y "
        f"{result.alerts_sent} avisos."
    )
    for error in result.errors:
        print(f"AVISO: {error}", file=sys.stderr)
    # Un fallo total debe ser visible en Actions. No se vuelve a enviar ningún
    # correo aquí: los destinatarios ya entregados no se duplican.
    if result.tickers_checked and result.tickers_with_fresh_prices == 0:
        return 1
    if result.users_checked and result.tickers_checked and result.emails_sent == 0:
        return 1
    if result.errors and result.users_checked == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
