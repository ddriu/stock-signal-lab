"""Exportación legible de la cartera a un libro Excel."""

from __future__ import annotations

from io import BytesIO

import pandas as pd


def build_portfolio_excel(
    *,
    operations: pd.DataFrame,
    positions: pd.DataFrame,
    annual: pd.DataFrame,
    daily: pd.DataFrame,
    private_investments: pd.DataFrame | None = None,
) -> bytes:
    """Crea un XLSX con hojas, filtros, formatos y gráficos editables."""

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="dd/mm/yyyy") as writer:
        workbook = writer.book
        title_format = workbook.add_format(
            {"bold": True, "font_color": "#FFFFFF", "bg_color": "#173B57", "border": 0}
        )
        money_format = workbook.add_format({"num_format": '#,##0.00 [$€-es-ES]'})
        sheets = {
            "Resumen anual": annual,
            "Operaciones": operations,
            "Posiciones": positions,
            "Evolución diaria": daily.reset_index(names="Fecha"),
        }
        if private_investments is not None and not private_investments.empty:
            sheets["Civislend y Sego"] = private_investments

        for sheet_name, frame in sheets.items():
            safe = frame.copy()
            safe.to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]
            worksheet.freeze_panes(1, 0)
            if len(safe.columns):
                worksheet.autofilter(0, 0, max(len(safe), 1), len(safe.columns) - 1)
            for column_index, column in enumerate(safe.columns):
                values = safe[column].fillna("").astype(str)
                width = min(42, max(len(str(column)) + 2, values.map(len).max() + 2 if len(values) else 12))
                worksheet.set_column(column_index, column_index, width)
                worksheet.write(0, column_index, str(column), title_format)
                lower = str(column).lower()
                if " eur" in lower or lower in {
                    "price", "fees", "cost_basis", "average_cost", "current_price",
                    "invested_amount", "current_value",
                }:
                    worksheet.set_column(column_index, column_index, width, money_format)
                elif lower.endswith("_pct") or lower.endswith(" %"):
                    # En la app los porcentajes se guardan como 0-100, no como 0-1.
                    worksheet.set_column(column_index, column_index, width)

        if not annual.empty:
            worksheet = writer.sheets["Resumen anual"]
            chart = workbook.add_chart({"type": "column"})
            for column_name, color in (("Compras EUR", "#3A86FF"), ("Ventas EUR", "#20C997")):
                column = annual.columns.get_loc(column_name)
                chart.add_series(
                    {
                        "name": ["Resumen anual", 0, column],
                        "categories": ["Resumen anual", 1, 0, len(annual), 0],
                        "values": ["Resumen anual", 1, column, len(annual), column],
                        "fill": {"color": color},
                    }
                )
            chart.set_title({"name": "Compras y ventas por año"})
            chart.set_y_axis({"name": "Euros"})
            chart.set_legend({"position": "bottom"})
            worksheet.insert_chart("L2", chart, {"x_scale": 1.25, "y_scale": 1.15})

        if not daily.empty:
            worksheet = writer.sheets["Evolución diaria"]
            chart = workbook.add_chart({"type": "line"})
            for column_name, color in (
                ("market_value_eur", "#20C997"),
                ("net_contributions_eur", "#3A86FF"),
            ):
                column = daily.reset_index(names="Fecha").columns.get_loc(column_name)
                chart.add_series(
                    {
                        "name": ["Evolución diaria", 0, column],
                        "categories": ["Evolución diaria", 1, 0, len(daily), 0],
                        "values": ["Evolución diaria", 1, column, len(daily), column],
                        "line": {"color": color, "width": 2},
                    }
                )
            chart.set_title({"name": "Evolución de la cartera"})
            chart.set_y_axis({"name": "Euros"})
            chart.set_legend({"position": "bottom"})
            worksheet.insert_chart("F2", chart, {"x_scale": 1.35, "y_scale": 1.2})

    return output.getvalue()
