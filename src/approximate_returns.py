"""Rentabilidad sencilla basada en liquidaciones reales y estimaciones explícitas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from src.data_loader import resolve_analysis_ticker
from src.data_sources import convert_currency


OPEN_COLUMNS = [
    "Ticker",
    "Cuenta",
    "Cantidad",
    "Moneda",
    "Has pagado",
    "Ahora vale",
    "Coste de vender",
    "Recibirías si vendieras",
    "Ganancia antes de impuestos",
    "Rentabilidad",
    "Impuesto aproximado",
    "Ganancia neta estimada",
    "Capital neto disponible",
    "Origen",
]

CLOSED_COLUMNS = [
    "Ticker",
    "Cuenta",
    "Fecha",
    "Año",
    "Cantidad",
    "Moneda",
    "Has pagado por lo vendido",
    "Recibiste",
    "Ganancia antes de impuestos",
    "Impuesto aproximado",
    "Ganancia neta",
    "Comisión informativa",
    "Origen",
]


@dataclass(frozen=True)
class ApproximateReturnSummary:
    year: int
    purchases_eur: float
    sales_eur: float
    fees_eur: float
    closed_cost_eur: float
    realized_pnl_eur: float
    estimated_realized_tax_eur: float
    realized_after_tax_eur: float
    open_cost_eur: float
    open_value_eur: float
    estimated_exit_costs_eur: float
    unrealized_pnl_eur: float
    estimated_unrealized_tax_eur: float
    unrealized_after_tax_eur: float
    approximate_total_pnl_eur: float
    approximate_return_pct: float | None
    incomplete_operations: int
    unpriced_positions: int


@dataclass(frozen=True)
class ApproximateReturnReport:
    open_positions: pd.DataFrame
    closed_operations: pd.DataFrame
    summary: ApproximateReturnSummary
    missing_currencies: tuple[str, ...] = ()
    missing_prices: tuple[str, ...] = ()


def _number(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _convert_to_eur(
    value: float,
    currency: str,
    rates_per_eur: dict[str, float],
) -> float | None:
    try:
        return float(convert_currency(value, currency, "EUR", rates_per_eur))
    except (TypeError, ValueError):
        return None


def _operation_settlement_eur(
    operation: Any,
    rates_per_eur: dict[str, float],
    *,
    fx_cost_pct: float,
) -> tuple[float | None, str]:
    """Devuelve el neto de la operación sin volver a sumar una comisión incluida."""

    recorded = _number(getattr(operation, "settlement_amount_eur", None))
    if recorded is not None and recorded > 0:
        return recorded, "Liquidación del bróker"

    quantity = _number(getattr(operation, "quantity", None))
    price = _number(getattr(operation, "price", None))
    fee = _number(getattr(operation, "fees", None)) or 0.0
    side = str(getattr(operation, "side", ""))
    currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
    if quantity is None or price is None or quantity <= 0 or price <= 0:
        return None, "Estimación incompleta"
    local = quantity * price + fee if side == "Compra" else quantity * price - fee
    if local <= 0:
        return None, "Estimación incompleta"

    recorded_fx = _number(getattr(operation, "fx_rate_to_eur", None))
    if recorded_fx is not None and recorded_fx > 0:
        return local * recorded_fx, "Estimación con cambio registrado"

    converted = _convert_to_eur(local, currency, rates_per_eur)
    if converted is None:
        return None, "Estimación incompleta"
    if currency != "EUR" and fx_cost_pct > 0:
        factor = (
            1.0 + fx_cost_pct / 100.0
            if side == "Compra"
            else 1.0 - fx_cost_pct / 100.0
        )
        converted *= factor
    return max(0.0, converted), "Estimación con cambio actual"


def _operation_fee_eur(
    operation: Any,
    rates_per_eur: dict[str, float],
) -> float | None:
    recorded = _number(getattr(operation, "fee_eur", None))
    if recorded is not None:
        return recorded
    fee = _number(getattr(operation, "fees", None))
    if fee is None:
        return 0.0
    currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
    recorded_fx = _number(getattr(operation, "fx_rate_to_eur", None))
    if recorded_fx is not None and recorded_fx > 0:
        return fee * recorded_fx
    return _convert_to_eur(fee, currency, rates_per_eur)


def _empty_report(year: int) -> ApproximateReturnReport:
    summary = ApproximateReturnSummary(
        year=year,
        purchases_eur=0.0,
        sales_eur=0.0,
        fees_eur=0.0,
        closed_cost_eur=0.0,
        realized_pnl_eur=0.0,
        estimated_realized_tax_eur=0.0,
        realized_after_tax_eur=0.0,
        open_cost_eur=0.0,
        open_value_eur=0.0,
        estimated_exit_costs_eur=0.0,
        unrealized_pnl_eur=0.0,
        estimated_unrealized_tax_eur=0.0,
        unrealized_after_tax_eur=0.0,
        approximate_total_pnl_eur=0.0,
        approximate_return_pct=None,
        incomplete_operations=0,
        unpriced_positions=0,
    )
    return ApproximateReturnReport(
        pd.DataFrame(columns=OPEN_COLUMNS),
        pd.DataFrame(columns=CLOSED_COLUMNS),
        summary,
    )


def build_approximate_return_report(
    operations: pd.DataFrame,
    latest_prices: dict[str, float],
    rates_per_eur: dict[str, float],
    *,
    tax_rate_pct: float = 20.0,
    sell_fee_eur: float = 1.0,
    spread_pct: float = 0.15,
    fx_cost_pct: float = 0.20,
    year: int | None = None,
) -> ApproximateReturnReport:
    """Calcula resultados realizados y potenciales sin fingir precisión fiscal.

    Los importes liquidados por el bróker tienen prioridad y se usa FIFO para
    asignar el coste de las ventas. Los costes configurados sólo se aplican a
    estimaciones; nunca se añade dos veces una comisión incluida en un total.
    """

    for value, label in (
        (tax_rate_pct, "impuesto"),
        (sell_fee_eur, "comisión de venta"),
        (spread_pct, "spread"),
        (fx_cost_pct, "coste de divisa"),
    ):
        if value < 0:
            raise ValueError(f"El {label} no puede ser negativo.")
    if tax_rate_pct > 100 or spread_pct > 100 or fx_cost_pct > 100:
        raise ValueError("Los porcentajes deben estar entre 0 y 100.")

    selected_year = int(year or date.today().year)
    if operations.empty:
        return _empty_report(selected_year)

    frame = operations.copy()
    if "id" not in frame:
        frame["id"] = range(1, len(frame) + 1)
    frame["executed_at_parsed"] = pd.to_datetime(
        frame.get("executed_at"), errors="coerce"
    )
    frame = frame.sort_values(
        ["executed_at_parsed", "id"], na_position="last", kind="stable"
    )

    states: dict[tuple[str, str, str], dict[str, Any]] = {}
    closed_rows: list[dict[str, object]] = []
    missing_currencies: set[str] = set()
    incomplete = 0
    purchases_year = 0.0
    sales_year = 0.0
    fees_year = 0.0

    for operation in frame.itertuples(index=False):
        ticker = str(getattr(operation, "ticker", "") or "").strip().upper()
        currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
        account = str(getattr(operation, "account_name", "") or "").strip()
        side = str(getattr(operation, "side", ""))
        quantity = _number(getattr(operation, "quantity", None))
        operation_date = pd.to_datetime(
            getattr(operation, "executed_at_parsed", None), errors="coerce"
        )
        operation_year = int(operation_date.year) if pd.notna(operation_date) else None
        settlement, settlement_source = _operation_settlement_eur(
            operation,
            rates_per_eur,
            fx_cost_pct=fx_cost_pct,
        )
        fee_eur = _operation_fee_eur(operation, rates_per_eur)
        if (
            not ticker
            or side not in {"Compra", "Venta"}
            or settlement is None
            or quantity is None
            or quantity <= 0
        ):
            incomplete += 1
            if currency not in rates_per_eur:
                missing_currencies.add(currency)
            continue
        if operation_year is None:
            incomplete += 1
        if operation_year == selected_year:
            if side == "Compra":
                purchases_year += settlement
            else:
                sales_year += settlement
            if fee_eur is not None:
                fees_year += fee_eur

        key = (ticker, currency, account)
        state = states.setdefault(
            key,
            {
                "ticker": ticker,
                "currency": currency,
                "account": account,
                "lots": [],
            },
        )
        lots: list[dict[str, Any]] = state["lots"]
        if side == "Compra":
            lots.append(
                {
                    "quantity": quantity,
                    "cost_eur": settlement,
                    "source": settlement_source,
                }
            )
            continue

        available = sum(float(lot["quantity"]) for lot in lots)
        sold = min(quantity, available)
        if sold <= 0:
            incomplete += 1
            continue
        if quantity > available + 1e-9:
            incomplete += 1
        sale_fraction = sold / quantity
        net_proceeds = settlement * sale_fraction
        remaining = sold
        removed_cost = 0.0
        cost_sources: list[str] = []
        while remaining > 1e-9 and lots:
            lot = lots[0]
            lot_quantity = float(lot["quantity"])
            consumed = min(remaining, lot_quantity)
            unit_cost = float(lot["cost_eur"]) / lot_quantity
            consumed_cost = unit_cost * consumed
            removed_cost += consumed_cost
            cost_sources.append(str(lot["source"]))
            lot["quantity"] = lot_quantity - consumed
            lot["cost_eur"] = max(0.0, float(lot["cost_eur"]) - consumed_cost)
            remaining -= consumed
            if float(lot["quantity"]) <= 1e-9:
                lots.pop(0)

        realized = net_proceeds - removed_cost
        tax = max(0.0, realized) * tax_rate_pct / 100.0
        source = (
            "Liquidaciones del bróker · FIFO"
            if settlement_source == "Liquidación del bróker"
            and cost_sources
            and all(item == "Liquidación del bróker" for item in cost_sources)
            else "FIFO con alguna estimación"
        )
        closed_rows.append(
            {
                "Ticker": ticker,
                "Cuenta": account or "Sin especificar",
                "Fecha": (
                    operation_date.date().isoformat()
                    if pd.notna(operation_date)
                    else None
                ),
                "Año": operation_year,
                "Cantidad": sold,
                "Moneda": currency,
                "Has pagado por lo vendido": removed_cost,
                "Recibiste": net_proceeds,
                "Ganancia antes de impuestos": realized,
                "Impuesto aproximado": tax,
                "Ganancia neta": realized - tax,
                "Comisión informativa": (fee_eur or 0.0) * sale_fraction,
                "Origen": source,
            }
        )

    open_rows: list[dict[str, object]] = []
    missing_prices: set[str] = set()
    for state in states.values():
        lots = state["lots"]
        quantity = sum(float(lot["quantity"]) for lot in lots)
        if quantity <= 1e-9:
            continue
        ticker = str(state["ticker"])
        try:
            analysis_ticker = resolve_analysis_ticker(ticker)
        except ValueError:
            analysis_ticker = ticker
        price = _number(latest_prices.get(analysis_ticker, latest_prices.get(ticker)))
        currency = str(state["currency"])
        if price is None or price <= 0:
            missing_prices.add(ticker)
            continue
        gross_eur = _convert_to_eur(quantity * price, currency, rates_per_eur)
        if gross_eur is None:
            missing_currencies.add(currency)
            continue
        spread_cost = gross_eur * spread_pct / 100.0
        fx_cost = gross_eur * fx_cost_pct / 100.0 if currency != "EUR" else 0.0
        exit_cost = sell_fee_eur + spread_cost + fx_cost
        net_sale = max(0.0, gross_eur - exit_cost)
        cost = sum(float(lot["cost_eur"]) for lot in lots)
        pnl = net_sale - cost
        tax = max(0.0, pnl) * tax_rate_pct / 100.0
        exact_cost = all(
            str(lot["source"]) == "Liquidación del bróker" for lot in lots
        )
        open_rows.append(
            {
                "Ticker": ticker,
                "Cuenta": state["account"] or "Sin especificar",
                "Cantidad": quantity,
                "Moneda": currency,
                "Has pagado": cost,
                "Ahora vale": gross_eur,
                "Coste de vender": exit_cost,
                "Recibirías si vendieras": net_sale,
                "Ganancia antes de impuestos": pnl,
                "Rentabilidad": pnl / cost * 100.0 if cost > 0 else None,
                "Impuesto aproximado": tax,
                "Ganancia neta estimada": pnl - tax,
                "Capital neto disponible": net_sale - tax,
                "Origen": (
                    "Compras liquidadas por el bróker · FIFO"
                    if exact_cost
                    else "Coste de compra con alguna estimación · FIFO"
                ),
            }
        )

    open_positions = pd.DataFrame(open_rows, columns=OPEN_COLUMNS)
    closed_operations = pd.DataFrame(closed_rows, columns=CLOSED_COLUMNS)
    closed_year = (
        closed_operations.loc[closed_operations["Año"] == selected_year]
        if not closed_operations.empty
        else closed_operations
    )
    realized = (
        float(closed_year["Ganancia antes de impuestos"].sum())
        if not closed_year.empty
        else 0.0
    )
    realized_tax = (
        float(closed_year["Impuesto aproximado"].sum())
        if not closed_year.empty
        else 0.0
    )
    closed_cost = (
        float(closed_year["Has pagado por lo vendido"].sum())
        if not closed_year.empty
        else 0.0
    )
    open_value = (
        float(open_positions["Recibirías si vendieras"].sum())
        if not open_positions.empty
        else 0.0
    )
    exit_costs = (
        float(open_positions["Coste de vender"].sum())
        if not open_positions.empty
        else 0.0
    )
    unrealized = (
        float(open_positions["Ganancia antes de impuestos"].sum())
        if not open_positions.empty
        else 0.0
    )
    unrealized_tax = (
        float(open_positions["Impuesto aproximado"].sum())
        if not open_positions.empty
        else 0.0
    )
    open_cost = (
        float(open_positions["Has pagado"].sum())
        if not open_positions.empty
        else 0.0
    )
    total_pnl = realized - realized_tax + unrealized - unrealized_tax
    denominator = closed_cost + open_cost
    summary = ApproximateReturnSummary(
        year=selected_year,
        purchases_eur=purchases_year,
        sales_eur=sales_year,
        fees_eur=fees_year,
        closed_cost_eur=closed_cost,
        realized_pnl_eur=realized,
        estimated_realized_tax_eur=realized_tax,
        realized_after_tax_eur=realized - realized_tax,
        open_cost_eur=open_cost,
        open_value_eur=open_value,
        estimated_exit_costs_eur=exit_costs,
        unrealized_pnl_eur=unrealized,
        estimated_unrealized_tax_eur=unrealized_tax,
        unrealized_after_tax_eur=unrealized - unrealized_tax,
        approximate_total_pnl_eur=total_pnl,
        approximate_return_pct=(
            total_pnl / denominator * 100.0 if denominator > 0 else None
        ),
        incomplete_operations=incomplete,
        unpriced_positions=len(missing_prices),
    )
    return ApproximateReturnReport(
        open_positions=open_positions,
        closed_operations=closed_operations,
        summary=summary,
        missing_currencies=tuple(sorted(missing_currencies)),
        missing_prices=tuple(sorted(missing_prices)),
    )
