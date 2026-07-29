"""Punto de entrada para las alertas programadas de GitHub Actions."""

from __future__ import annotations

import sys

from src.alert_runner import run_daily_alerts


def main() -> int:
    result = run_daily_alerts()
    print(
        "Alertas terminadas: "
        f"{result.users_checked} usuarios, "
        f"{result.tickers_checked} empresas, "
        f"{result.emails_sent} correos y "
        f"{result.alerts_sent} avisos."
    )
    for error in result.errors:
        print(f"AVISO: {error}", file=sys.stderr)
    # Los fallos parciales quedan visibles, pero no provocan reintentos que
    # pudieran duplicar correos ya entregados a otros usuarios.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

