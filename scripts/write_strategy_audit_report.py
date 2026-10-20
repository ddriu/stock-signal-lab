"""Turn a completed PRIVATE CLI run into a readable report, without new data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from scripts.run_strategy_audit import load_bundle
from src.strategy_audit_service import BENCHMARKS, matched_benchmark


def _fmt(value, decimals=2):
    return "N/D" if value is None or pd.isna(value) else f"{float(value):.{decimals}f}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if repo == output or repo in output.parents:
        raise ValueError("Keep the private report outside the public repository")
    manifest = json.loads((args.results / "manifest.json").read_text(encoding="utf-8"))
    comparison = pd.read_csv(args.results / "comparison.csv")
    bars, prices_manifest = load_bundle(args.cache)
    coverage = manifest["coverage"]
    lines = ["# Stock Signal Lab — auditoría histórica, fase 1", "",
        "Evaluación de lectura. No cambia inversiones, señales actuales ni correos. "
        "Resultados teóricos brutos en USD, sin costes ni impuestos.", "",
        "## Cobertura", "",
        f"- Fuente: `{manifest['source']}`. Ventana de observación: {' → '.join(manifest['period'])}.",
        f"- Historial completo de entrada: {coverage['input_rows']} registros, {coverage['historical_tickers']} activos históricos.",
        f"- Observaciones canónicas: {coverage['accepted_signals']}; registros apartados/revisiones: {coverage['rejected_rows']}.",
        "- La disponibilidad usa created_at, no la fecha de la vela. Una señal intradía sigue siendo parcial.",
        "- Los precios son reconstruidos; la configuración de salidas está congelada, no certificada como la vigente en todas las fechas antiguas.",
        "- No existe evidencia suficiente para reconstruir todas las entregas históricas de correo.", "",
        "## Resultados parciales verificables", "",
        "Cada fila identifica su último cierre completamente valorable para los lotes que sí pudieron ejecutarse. "
        "No representa automáticamente el resultado final solicitado ni las recomendaciones sin ejecución. "
        "No se ha eliminado ninguna sesión negativa.", "",
        "| Variante | Lotes ejecutados | Lotes pendientes | Último corte valorable | Aportado al corte USD | Patrimonio al corte USD | ROI al corte |",
        "|---|---:|---:|---|---:|---:|---:|"]
    for row in comparison.to_dict("records"):
        lines.append(f"| {row['variant']} | {row['n_lots']} | {row['unfilled_entries']} | "
                     f"{row.get('last_complete_valuation_date') if pd.notna(row.get('last_complete_valuation_date')) else 'N/D'} | "
                     f"{_fmt(row.get('last_complete_contributions_usd'))} | {_fmt(row.get('last_complete_value_usd'))} | "
                     f"{_fmt(row.get('last_complete_roi_pct'))}% |")
    lines += ["", "A1/B1: reducir o vender; A2/B2: vender; A3/B3: mantener. "
        "C80/85/90 conserva la etiqueta Entrada fuerte y exige la nota mínima. "
        "D fija usa 100 USD; D escalonada 100/200/300; D factores sólo factores archivados. "
        "Las salidas con señal conservan el stop del evaluador al cierre; los stops intradía se comparan aparte.", "",
        "## Índices con aportaciones equivalentes — cortes parciales", "",
        "Referencias de precios sin dividendos en efectivo. No son un desembolso inicial único. "
        "Se usan exclusivamente las contribuciones de los lotes ya ejecutados en cada corte.", "",
        "| Variante | Corte | Referencia | Aportado USD | Valor USD | ROI | Completo para esa cohorte |",
        "|---|---|---|---:|---:|---:|---|"]
    references = []
    for name in ("A1", "B1", "C85_reduce"):
        match = comparison.loc[comparison.variant.eq(name)]
        if match.empty or pd.isna(match.iloc[0].last_complete_valuation_date):
            continue
        cutoff = str(match.iloc[0].last_complete_valuation_date)
        lots = pd.read_csv(args.results / name / "lots.csv", parse_dates=["entry_at"])
        lots = lots.loc[lots.entry_at < pd.Timestamp(cutoff, tz="UTC") + pd.Timedelta(days=1)]
        for label, ticker in BENCHMARKS.items():
            result = matched_benchmark(lots, bars.get(ticker, pd.DataFrame()), cutoff)
            references.append(dict(variant=name, cutoff=cutoff, benchmark=label, **result))
            lines.append(f"| {name} | {cutoff} | {label} | {_fmt(result['contributions_usd'])} | "
                         f"{_fmt(result['value_usd'])} | {_fmt(result['roi_pct'])}% | {result['data_complete']} |")
    lines += ["", "## Qué se puede concluir", "",
        "Una rentabilidad negativa en este tramo no prueba que una estrategia siempre pierda; "
        "una pérdida menor tampoco acredita una regla ganadora. Comparar sectores, cantidad de "
        "lotes, fechas y cobertura antes de atribuir la diferencia a un umbral o a un stop. "
        "Las variantes fueron predeclaradas; no se ha cambiado el radar para ajustar resultados.", "",
        "Las cifras exploratorias de 211 compras y −3,05% no se fuerzan: cambian la deduplicación, "
        "la primera apertura posterior, el horizonte realmente ejecutable, las unidades y la "
        "reconstrucción de salidas con el precio de adquisición propio de cada lote virtual.", "",
        "## Pendiente antes de usar dinero real", "",
        "- Completar precios/cierres faltantes y revisar todas las recomendaciones sin ejecución.",
        "- Validar fuera de muestra con suficientes observaciones futuras; no elegir ganador con octubre solamente.",
        "- Añadir costes, spread, impuestos y restricciones reales en una segunda prueba, separada de esta hipótesis bruta.",
        "- Activar registro prospectivo inmutable de decisiones y entregas de correo; no se ha activado ni enviado correo en esta fase.",
        "- XIRR, sensibilidad por fecha y TWR exacto no disponibles en esta primera fase. Los proxies diarios están identificados.", "",
        "## Reproducibilidad", "",
        f"Hash del historial congelado: `{manifest['raw_history_sha256']}`.",
        f"Descarga/procedencia de precios: `{prices_manifest['downloaded_at']}`.",
        "Conservar juntos los JSON originales, la caché de precios y los manifiestos. "
        "Este informe y los CSV son privados y no se suben a GitHub.", ""]
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text("\n".join(lines), encoding="utf-8")
    pd.DataFrame(references).to_csv(output.with_suffix(".benchmarks.csv"), index=False)
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
