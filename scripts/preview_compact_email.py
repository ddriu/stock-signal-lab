"""Generate a local synthetic preview; no accounts, downloads or email sends."""
from pathlib import Path
import argparse

from src.daily_digest import render_daily_score_email, seal_daily_score_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    samples = [("EJEMPLO-A", "Empresa de ejemplo A", True, 87, 3, False),
               ("EJEMPLO-B", "Empresa de ejemplo B", True, 78, -4, False),
               ("EJEMPLO-C", "Empresa de ejemplo C", True, None, None, True),
               ("NUEVA-A", "Candidata de ejemplo A", False, 89, 5, False),
               ("NUEVA-B", "Candidata de ejemplo B", False, 86, 3, False)]
    rows = [dict(ticker=ticker, company_name=name, held=held, score=score, change=change,
        label="Sin dato" if score is None else "Entrada fuerte" if score >= 82 else "Esperar",
        status="review_data" if blocked else "entry_strong" if score >= 82 else "wait",
        comparison_status="missing_previous" if change is None else "changed",
        quality=dict(block_entry=blocked, reasons=["Falta verificar el último trimestre."] if blocked else []),
        change_reason="La fuerza del precio ha cambiado desde el cierre anterior.",
        next_step="Confirmar precio, próximo evento y tamaño antes de decidir.",
        source="DATOS FICTICIOS · vista previa", session="2026-11-06", policy_version="demo")
        for ticker, name, held, score, change, blocked in samples]
    report = seal_daily_score_report(dict(as_of="2026-11-06 · DEMOSTRACIÓN", rows=rows,
        alerts=[dict(ticker="EJEMPLO-B", company_name="Empresa de ejemplo B", held=True,
            title="Revisar exposición", change_reason="La tendencia ha perdido fuerza.",
            next_step="Esperar confirmación; no vender sólo por una nota baja.",
            strategy_label="Crecimiento + momentum", horizon="Semanas", as_of="2026-11-06",
            data_source="DATOS FICTICIOS", data_limitations="Ejemplo de formato, no recomendación.")]))
    subject, plain, rendered = render_daily_score_email(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    args.output.with_suffix(".txt").write_text(subject+"\n\n"+plain, encoding="utf-8")
    print("Vista previa sintética generada; no se han enviado correos.")


if __name__ == "__main__":
    main()
