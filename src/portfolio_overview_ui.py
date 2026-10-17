"""Presentación de una cartera reconciliada, sin lecturas ni escrituras externas.

El informe recibido es la única fuente contable. Una cifra desconocida no se
convierte en cero ni en una rentabilidad, y las operaciones nunca se dibujan
como si fueran valoraciones históricas del patrimonio.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Protocol

import pandas as pd
import streamlit as st


class PortfolioOverviewReport(Protocol):
    positions: pd.DataFrame
    metrics_global: Mapping[str, object]
    monthly: pd.DataFrame
    annual: pd.DataFrame
    issues: tuple[str, ...]


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _money(value: object, *, signed: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "N/D"
    return f"{number:+,.2f} €" if signed else f"{number:,.2f} €"


def _text_column(frame: pd.DataFrame, *names: str, default: str = "") -> pd.Series:
    result = pd.Series("", index=frame.index, dtype=str)
    for name in names:
        if name in frame:
            values = frame[name].fillna("").astype(str).str.strip()
            result = result.mask(result.eq(""), values)
    return result.mask(result.eq(""), default)


def _normalized_positions(positions: pd.DataFrame) -> pd.DataFrame:
    frame = positions.copy().reset_index(drop=True)
    frame["_platform"] = _text_column(frame, "platform", "account_name", default="Sin plataforma")
    frame["_ticker"] = _text_column(frame, "analysis_ticker", "ticker").str.upper()
    frame["_name"] = _text_column(frame, "asset_name", "analysis_ticker", "ticker", default="Sin identificar")
    frame["_company"] = frame["_ticker"].where(frame["_ticker"].ne(""), frame["_name"])
    frame["_sector"] = _text_column(frame, "sector_label", "sector", "sector_name", default="Sin sector informado")
    frame["_liquidity"] = _text_column(frame, "liquidity_class", default="unknown")
    for target, source in (("_value", "value_eur"), ("_pnl", "unrealized_pnl_eur")):
        values = frame.get(source, pd.Series(index=frame.index, dtype=float))
        frame[target] = pd.to_numeric(values, errors="coerce").map(_number)
    if "value_complete" in frame:
        frame.loc[~frame["value_complete"].fillna(False).astype(bool), ["_value", "_pnl"]] = float("nan")
    if "cost_basis_complete" in frame:
        frame.loc[~frame["cost_basis_complete"].fillna(False).astype(bool), "_pnl"] = float("nan")
    return frame


def _complete_sum(values: pd.Series) -> float | None:
    if values.empty or values.isna().any():
        return None
    return _number(values.sum())


def build_overview_allocations(positions: pd.DataFrame, dimension: str, *, total_complete: bool = True) -> pd.DataFrame:
    """Distribución completa; no publica pesos sobre un denominador parcial.

    Plataforma incluye todo el patrimonio. Empresa y sector se refieren sólo a
    los activos líquidos, sin diluir la concentración con efectivo o ilíquidos.
    """

    keys = {"platform": "_platform", "company": "_company", "sector": "_sector"}
    if dimension not in keys:
        raise ValueError("La distribución debe ser platform, company o sector.")
    frame = _normalized_positions(positions)
    if dimension != "platform":
        frame = frame.loc[frame["_liquidity"].eq("liquid")]
    columns = ["Grupo", "Valor EUR", "Peso %"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    total = _complete_sum(frame["_value"]) if total_complete else None
    rows = []
    for group, part in frame.groupby(keys[dimension], sort=False):
        value = _complete_sum(part["_value"])
        rows.append({
            "Grupo": group,
            "Valor EUR": value,
            "Peso %": value / total * 100.0 if value is not None and total is not None and total > 0 else None,
        })
    return pd.DataFrame(rows, columns=columns).sort_values("Valor EUR", ascending=False, na_position="last").reset_index(drop=True)


def build_overview_movers(positions: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cinco ganancias/pérdidas latentes conocidas, agrupadas por empresa.

    Una empresa con alguna partida de coste desconocido se excluye entera: no se
    presenta el resultado de una sola cuenta como si fuera el de toda la empresa.
    """

    frame = _normalized_positions(positions)
    frame = frame.loc[frame["_liquidity"].eq("liquid")]
    columns = ["Empresa", "Latente EUR"]
    rows = []
    for company, part in frame.groupby("_company", sort=False):
        pnl = _complete_sum(part["_pnl"])
        if pnl is not None:
            rows.append({"Empresa": company, "Latente EUR": pnl})
    result = pd.DataFrame(rows, columns=columns)
    result["Latente EUR"] = pd.to_numeric(result["Latente EUR"], errors="coerce")
    gains = result.loc[result["Latente EUR"] > 0].nlargest(5, "Latente EUR")
    losses = result.loc[result["Latente EUR"] < 0].nsmallest(5, "Latente EUR")
    return gains.reset_index(drop=True), losses.reset_index(drop=True)


def build_overview_position_rows(positions: pd.DataFrame, *, portfolio_value_complete: bool = True, incomplete_platforms: set[str] | None = None) -> pd.DataFrame:
    """Mantiene cada plataforma separada y señala los denominadores de sus pesos."""

    frame = _normalized_positions(positions)
    total = _complete_sum(frame["_value"]) if portfolio_value_complete else None
    platform_totals = {
        name: _complete_sum(part["_value"]) if name not in (incomplete_platforms or set()) else None
        for name, part in frame.groupby("_platform", sort=False)
    }
    result = pd.DataFrame(index=frame.index)
    result["Empresa"] = frame["_name"]
    result["Ticker"] = frame["_ticker"]
    result["Plataforma"] = frame["_platform"]
    result["Liquidez"] = frame["_liquidity"].map({"liquid": "Líquido", "cash": "Efectivo", "illiquid": "Ilíquido"}).fillna("Sin clasificar")
    result["Valor EUR"] = frame["_value"]
    result["Peso patrimonio %"] = frame["_value"] / total * 100.0 if total is not None and total > 0 else float("nan")
    result["Peso plataforma %"] = [
        value / denominator * 100.0 if _number(value) is not None and denominator is not None and denominator > 0 else None
        for value, denominator in zip(frame["_value"], frame["_platform"].map(platform_totals))
    ]
    result["Latente EUR"] = frame["_pnl"]
    result["Cotización al"] = _text_column(frame, "market_as_of", default="N/D")
    result["Valoración"] = _text_column(frame, "valuation_status", default="Dato declarado")
    return result


def _render_table(frame: pd.DataFrame, *, height: int | None = None) -> None:
    config = {
        column: st.column_config.NumberColumn(format="%+.2f €" if "Latente" in column else "%.2f €")
        for column in frame if column.endswith("EUR")
    }
    config.update({column: st.column_config.NumberColumn(format="%.2f%%") for column in frame if "%" in column})
    options = {"height": height} if height is not None else {}
    st.dataframe(frame, hide_index=True, width="stretch", column_config=config, **options)


def _render_metric(container: object, metrics: Mapping[str, object], label: str, field: str, *, signed: bool = False, help_text: str | None = None) -> None:
    container.metric(label, _money(metrics.get(field), signed=signed), help=help_text)
    if _number(metrics.get(field)) is None:
        known = _number(metrics.get(field.removesuffix("_eur") + "_known_eur"))
        if known is not None:
            container.caption(f"Parcial identificado: {_money(known, signed=signed)}; total pendiente.")


def _history_table(frame: pd.DataFrame) -> pd.DataFrame:
    labels = {
        "period": "Periodo", "ending_value_eur": "Último patrimonio documentado EUR",
        "valuation_date": "Fecha de valoración", "valuation_is_period_end": "Cierre de periodo",
        "contributed_capital_eur": "Aportaciones EUR", "withdrawn_capital_eur": "Retiradas EUR",
        "net_contributions_eur": "Aportación neta EUR", "realized_pnl_eur": "Realizado EUR",
        "unrealized_pnl_eur": "Latente EUR", "total_pnl_eur": "Resultado total EUR",
        "dividends_eur": "Dividendos EUR", "interest_eur": "Intereses EUR",
        "fees_eur": "Comisiones EUR", "tax_eur": "Impuestos EUR", "fx_cost_eur": "Coste FX EUR",
        "total_return_pct": "Rentabilidad %",
    }
    return frame.loc[:, [column for column in labels if column in frame]].rename(columns=labels)


def render_portfolio_overview(
    report: PortfolioOverviewReport,
    *,
    source_label: str = "Fotografía y movimientos reconciliados",
    snapshot_date: object | None = None,
    valuation_date: object | None = None,
    key: str = "portfolio_overview",
) -> None:
    """Vista compacta de lectura; no ejecuta motores, órdenes ni persistencia."""

    st.subheader("Patrimonio y resultado")
    dates = [f"Fuente: {source_label}"]
    if snapshot_date is not None and str(snapshot_date).strip():
        dates.append(f"composición de {snapshot_date}")
    if valuation_date is not None and str(valuation_date).strip():
        dates.append(f"valoración/cotizaciones hasta {valuation_date}")
    st.caption(" · ".join(dates))
    metrics = report.metrics_global
    global_complete = bool(metrics.get("value_complete", _number(metrics.get("value_eur")) is not None))
    platforms = getattr(report, "metrics_by_platform", pd.DataFrame())
    incomplete_platforms = {
        str(row.get("platform")) for row in platforms.to_dict("records")
        if not row.get("value_complete", _number(row.get("value_eur")) is not None)
    }
    if not global_complete and platforms.empty:
        incomplete_platforms = set(_normalized_positions(report.positions)["_platform"])
    for container, label, field in zip(st.columns(4), ("Patrimonio financiero", "Cotizada y otros líquidos", "Efectivo", "Alternativos ilíquidos"), ("value_eur", "liquid_value_eur", "cash_value_eur", "illiquid_value_eur")):
        _render_metric(container, metrics, label, field)
    for container, label, field in zip(st.columns(3), ("Resultado latente", "Resultado realizado", "Resultado total"), ("unrealized_pnl_eur", "realized_pnl_eur", "total_pnl_eur")):
        _render_metric(container, metrics, label, field, signed=True)
    total_return = _number(metrics.get("total_return_pct"))
    st.caption(
        f"Rentabilidad total validada: {total_return:+.2f}%." if total_return is not None else
        "Rentabilidad total: N/D hasta validar costes, aportaciones, retiradas e ingresos. Una compra o una venta no equivale a una aportación o retirada externa."
    )
    st.caption("Civislend y Segofactoring cuentan en el patrimonio como ilíquidos, pero no como liquidez para ampliar acciones.")
    if report.issues:
        st.warning(f"Hay {len(report.issues)} comprobaciones de datos pendientes. Los importes no verificables se muestran como N/D; detalle en Trazabilidad.")

    st.markdown("#### Evolución financiera")
    frequency = st.segmented_control("Periodo de evolución", ["Mes", "Año"], default="Mes", key=f"{key}_frequency") or "Mes"
    history = report.monthly if frequency == "Mes" else report.annual
    values = pd.to_numeric(history.get("ending_value_eur", pd.Series(dtype=float)), errors="coerce")
    if "period" in history and values.notna().sum() >= 2:
        labels = history["valuation_date"].astype(str) if "valuation_date" in history else history["period"].astype(str)
        chart = pd.DataFrame({"Fecha de valoración": labels, "Patrimonio EUR": values})
        st.line_chart(chart.set_index("Fecha de valoración"), height=240)
    else:
        st.info("Aún no hay dos valoraciones históricas comprobables para dibujar la evolución del patrimonio. El registro de compras y ventas no sustituye ese historial.")
    with st.expander("Detalle del periodo: patrimonio, flujos y resultados"):
        if history.empty:
            st.caption("Sin periodos documentados. Los datos ausentes no se interpretan como cero.")
        else:
            _render_table(_history_table(history))
        st.caption("Se conserva la fecha real de cada valoración: una fotografía intermedia no es un cierre mensual o anual. La rentabilidad queda N/D si falta cobertura; no es TWR/XIRR ni una rentabilidad anualizada.")

    companies = build_overview_allocations(report.positions, "company")
    known_weights = pd.to_numeric(companies["Peso %"], errors="coerce").dropna()
    if not known_weights.empty:
        st.caption(f"Concentración de activos líquidos: mayor empresa/activo {known_weights.max():.1f}% · cinco primeras {known_weights.nlargest(5).sum():.1f}%. Efectivo e ilíquidos no diluyen estos pesos.")
    else:
        st.caption("Concentración de activos líquidos: N/D hasta completar su valoración.")
    with st.expander("Distribución por plataforma, sector y empresa"):
        choice = st.radio("Distribución", ["Plataforma", "Sector", "Empresa"], horizontal=True, key=f"{key}_distribution")
        dimension = {"Plataforma": "platform", "Sector": "sector", "Empresa": "company"}[choice]
        allocations = build_overview_allocations(report.positions, dimension, total_complete=global_complete if dimension == "platform" else True)
        if dimension == "platform" and not allocations.empty:
            allocations.loc[allocations["Grupo"].isin(incomplete_platforms), "Valor EUR"] = float("nan")
        st.caption("Peso sobre todo el patrimonio, incluido efectivo e ilíquidos." if dimension == "platform" else "Peso dentro de los activos líquidos. El sector no informado se mantiene explícito, sin inferirlo.")
        if allocations.empty:
            st.caption("Sin posiciones valoradas para esta distribución.")
        else:
            _render_table(allocations)

    with st.expander("Top 5 ganancias y pérdidas latentes por empresa"):
        gains, losses = build_overview_movers(report.positions)
        for container, label, table in zip(st.columns(2), ("Ganancias latentes", "Pérdidas latentes"), (gains, losses)):
            with container:
                st.markdown(f"**{label}**")
                if table.empty:
                    st.caption("Sin resultados conocidos de este signo.")
                else:
                    _render_table(table)
        st.caption("Activos líquidos con valor y coste completos; se agregan sus plataformas sin mezclar las bases FIFO. Las partidas incompletas no entran en el ranking.")

    with st.expander("Trazabilidad: posiciones, pesos y datos pendientes"):
        st.caption("Peso patrimonio usa todo el patrimonio; peso plataforma usa sólo el total de esa plataforma, incluido su efectivo. Cada cuenta conserva su coste y divisa.")
        if report.positions.empty:
            st.caption("Sin posiciones reconciliadas disponibles.")
        else:
            _render_table(build_overview_position_rows(report.positions, portfolio_value_complete=global_complete, incomplete_platforms=incomplete_platforms))
        for issue in report.issues:
            st.write(str(issue))
        st.caption("Vista informativa: no envía órdenes, modifica posiciones ni reasigna estrategias.")
