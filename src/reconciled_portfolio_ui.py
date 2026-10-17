"""Opt-in private workbook import and read-only accounting drill-downs."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.supabase_journal import JournalStorageError
from src.reconciled_workbook_import import parse_reconciled_workbook_excel
from src.reconciled_workbook_store import prepare_reconciled_workbook_bundle


def render_reconciled_workbook_import(journal: object, *, actor_username: str = "", key: str = "reconciled_import") -> None:
    with st.expander("Importar cartera conciliada e historial (.xlsx)"):
        st.caption(
            "La fotografía no es una compra. El historial se añade por plataforma; "
            "las operaciones posteriores actualizan las cantidades una sola vez. "
            "Antes de guardar se conserva una copia privada de los registros existentes. "
            "Los conflictos quedan pendientes, sin sobrescribir datos manuales."
        )
        uploaded = st.file_uploader("Excel conciliado", type=["xlsx"], key=f"{key}_file")
        if uploaded is None:
            return
        try:
            plan = parse_reconciled_workbook_excel(uploaded.getvalue())
            existing = journal.list_operations()
            bundle = prepare_reconciled_workbook_bundle(plan, existing)
        except (ValueError, ImportError, JournalStorageError) as exc:
            st.error(f"No se pudo preparar la importación: {exc}")
            return
        cols = st.columns(4)
        cols[0].metric("Foto histórica", plan.snapshot_date)
        cols[1].metric("Partidas", len(plan.snapshot.positions))
        cols[2].metric("Operaciones históricas", len(plan.historical_operations))
        cols[3].metric("Operaciones posteriores", len(plan.post_snapshot_operations))
        st.caption(
            f"Valor declarado: {plan.snapshot.positions['value_eur'].sum():,.2f} € · "
            f"{len(plan.events)} movimientos de ingresos documentados. "
            "Las compras no se consideran aportaciones externas."
        )
        st.dataframe(plan.inventory, hide_index=True, width="stretch")
        preview = pd.DataFrame(bundle["metadata"].get("operation_preview", []))
        if not preview.empty:
            st.write("Revisión del diario", preview["action"].value_counts().rename_axis("Acción").to_frame("Filas"))
        if not plan.issues.empty:
            st.warning("Hay límites o discrepancias de origen. Se conservarán como pendientes.")
            st.dataframe(plan.issues, hide_index=True, width="stretch")
        confirmed = st.checkbox(
            "He revisado la fecha, las plataformas y los pendientes; guardar sin inventar ni sustituir operaciones.",
            key=f"{key}_confirm_{plan.source_sha256[:12]}",
        )
        if not callable(getattr(journal, "import_reconciled_portfolio", None)):
            st.error("Este almacenamiento todavía no admite la importación conciliada.")
            return
        if st.button("Guardar importación conciliada", type="primary", disabled=not confirmed, key=f"{key}_save"):
            try:
                result = journal.import_reconciled_portfolio(bundle, recorded_by=actor_username)
            except (ValueError, JournalStorageError) as exc:
                st.error(f"No se guardó la importación: {exc}")
            else:
                if result.get("already_imported"):
                    st.info("Este archivo ya estaba importado. No se han añadido duplicados.")
                else:
                    st.success(
                        f"Guardadas {result['inserted_positions']} partidas y "
                        f"{result['inserted_operations']} operaciones; "
                        f"{result['pending_conflicts']} conflictos pendientes. Copia privada conservada."
                    )
                st.rerun()


def render_reconciled_accounting_details(report: object) -> None:
    with st.expander("Rentabilidad y movimientos documentados por plataforma y acción"):
        st.caption(
            "FIFO separado por plataforma y moneda. Los importes en moneda original no "
            "se convierten con el cambio de hoy para simular un resultado histórico en euros. "
            "N/D significa que falta cobertura; un subtotal conocido no es el resultado completo."
        )
        for label, field in (("Por plataforma", "metrics_by_platform"), ("Por activo", "metrics_by_asset"),
                             ("Ventas y coste FIFO", "realized_trades"), ("Ingresos y flujos", "cash_flows")):
            frame = getattr(report, field, pd.DataFrame())
            if not frame.empty:
                st.markdown(f"**{label}**")
                st.dataframe(frame, hide_index=True, width="stretch")


def render_position_review_table(rows: pd.DataFrame) -> None:
    st.markdown("### Revisión de mis posiciones")
    st.caption(
        "Propuestas para revisar, no órdenes. Un mal momento de entrada no obliga a vender. "
        "Los límites propuestos (15 % por empresa, 30 % por sector) son reglas revisables, "
        "no una cartera óptima. La capacidad se comparte entre brókers y no representa efectivo disponible."
    )
    if rows.empty:
        st.info("No hay posiciones cotizadas identificadas para revisar.")
        return
    labels = {"platform": "Plataforma", "company": "Empresa", "ticker": "Ticker", "strategy": "Estrategia",
              "horizon": "Horizonte", "action": "Qué revisar", "reason": "Motivo", "latest_close": "Último cierre",
              "quote_date": "Fecha cotización", "currency": "Moneda", "company_weight_pct": "Peso empresa %",
              "target_company_weight_pct": "Límite empresa %", "capacity_eur": "Margen concentración EUR",
              "entry_zone": "Zona de entrada", "risk_limitations": "Riesgo y límites"}
    st.dataframe(rows.loc[:, [c for c in labels if c in rows]].rename(columns=labels), hide_index=True, width="stretch")
    with st.expander("Criterios, referencias técnicas y trazabilidad de la revisión"):
        st.dataframe(rows, hide_index=True, width="stretch")
