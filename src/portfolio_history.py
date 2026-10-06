"""Evolución temporal de una cartera a partir de operaciones y cierres diarios."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.data_sources import convert_currency
from src.approximate_returns import build_approximate_return_report
from src.instruments import resolve_analysis_ticker
from src.journal import calculate_position_states, operation_fee_eur, operation_settlement_eur


HISTORY_COLUMNS = [
    "market_value_eur",
    "net_contributions_eur",
    "accumulated_result_eur",
]
ANNUAL_COLUMNS = [
    "Año",
    "Compras EUR",
    "Ventas EUR",
    "Aportación neta EUR",
    "Comisiones EUR",
    "Resultado realizado EUR",
    "Resultado del año EUR",
    "Valor al cierre EUR",
    "Resultado acumulado EUR",
    "Resultado acumulado %",
    "Operaciones",
]


@dataclass(frozen=True)
class PortfolioHistoryResult:
    daily: pd.DataFrame
    annual: pd.DataFrame
    missing_tickers: tuple[str, ...] = ()
    missing_currencies: tuple[str, ...] = ()
    incomplete_operations: int = 0


def _to_eur(
    value: float,
    currency: str,
    rates_per_eur: dict[str, float],
) -> float | None:
    try:
        return float(convert_currency(value, currency, "EUR", rates_per_eur))
    except ValueError:
        return None


def _empty_result() -> PortfolioHistoryResult:
    return PortfolioHistoryResult(
        daily=pd.DataFrame(columns=HISTORY_COLUMNS),
        annual=pd.DataFrame(columns=ANNUAL_COLUMNS),
    )


def _realized_result_by_year(
    operations: pd.DataFrame,
    rates_per_eur: dict[str, float],
) -> dict[int, float]:
    """Utiliza el mismo FIFO y las liquidaciones EUR del resultado aproximado."""

    report = build_approximate_return_report(
        operations, {}, rates_per_eur, tax_rate_pct=0,
        sell_fee_eur=0, spread_pct=0, fx_cost_pct=0,
    )
    if report.closed_operations.empty:
        return {}
    return {
        int(year): float(value)
        for year, value in report.closed_operations.groupby("Año")["Ganancia antes de impuestos"].sum().items()
    }


def build_portfolio_history(
    operations: pd.DataFrame,
    price_history: dict[str, pd.DataFrame],
    rates_per_eur: dict[str, float],
) -> PortfolioHistoryResult:
    """Valora posiciones día a día y resume los movimientos por año.

    Los tipos de cambio son los actuales del BCE. Es una aproximación útil para
    seguimiento, no una contabilidad fiscal ni una valoración histórica exacta.
    """

    if operations.empty:
        return _empty_result()
    operations = operations.copy()
    accounting_report = build_approximate_return_report(
        operations, {}, rates_per_eur, tax_rate_pct=0,
        sell_fee_eur=0, spread_pct=0, fx_cost_pct=0,
    )
    incomplete_operations = accounting_report.summary.incomplete_operations
    operations["executed_at"] = pd.to_datetime(
        operations["executed_at"], errors="coerce"
    ).dt.tz_localize(None).dt.normalize()
    operations = operations.dropna(subset=["executed_at"])
    if operations.empty:
        return _empty_result()

    usable_prices: dict[str, pd.Series] = {}
    missing_tickers: set[str] = set()
    for ticker in operations["ticker"].astype(str).str.upper().unique():
        frame = price_history.get(ticker)
        if frame is None:
            frame = price_history.get(resolve_analysis_ticker(ticker))
        if frame is None or frame.empty or "close" not in frame:
            missing_tickers.add(ticker)
            continue
        close = pd.to_numeric(frame["close"], errors="coerce").dropna()
        if close.empty:
            missing_tickers.add(ticker)
            continue
        close.index = pd.to_datetime(close.index, errors="coerce").tz_localize(None).normalize()
        close = close.loc[~close.index.duplicated(keep="last")].sort_index()
        usable_prices[ticker] = close

    last_price_date = max((series.index.max() for series in usable_prices.values()), default=None)
    if last_price_date is None:
        annual = _annual_summary(
            operations, pd.DataFrame(columns=HISTORY_COLUMNS), rates_per_eur
        )
        if incomplete_operations:
            annual[["Resultado realizado EUR", "Resultado del año EUR", "Resultado acumulado EUR", "Resultado acumulado %"]] = float("nan")
        return PortfolioHistoryResult(
            daily=pd.DataFrame(columns=HISTORY_COLUMNS),
            annual=annual,
            missing_tickers=tuple(sorted(missing_tickers)),
            missing_currencies=accounting_report.missing_currencies,
            incomplete_operations=incomplete_operations,
        )
    start_date = min(operations["executed_at"].min(), min(s.index.min() for s in usable_prices.values()))
    last_price_date = max(last_price_date, operations["executed_at"].max())
    index = pd.date_range(start_date, last_price_date, freq="D")
    market_value = pd.Series(0.0, index=index)
    missing_currencies: set[str] = set()

    for (ticker, currency), ticker_operations in operations.groupby(
        [operations["ticker"].astype(str).str.upper(), operations["currency"].fillna("EUR").astype(str).str.upper()]
    ):
        close = usable_prices.get(str(ticker))
        quantity_changes = pd.Series(0.0, index=index)
        for operation in ticker_operations.itertuples(index=False):
            executed = pd.Timestamp(operation.executed_at).normalize()
            if executed not in quantity_changes.index:
                continue
            direction = 1.0 if str(operation.side) == "Compra" else -1.0
            quantity_changes.loc[executed] += direction * float(operation.quantity)
        quantities = quantity_changes.cumsum().clip(lower=0)
        # Una cotización futura no puede rellenar los días anteriores a su
        # primera observación. Un activo sin precio tampoco equivale a valor cero.
        aligned_close = close.reindex(index).ffill() if close is not None else pd.Series(float("nan"), index=index)
        native_values = quantities * aligned_close
        native_values.loc[quantities <= 1e-9] = 0.0
        converted = _to_eur(1.0, str(currency), rates_per_eur)
        if converted is None:
            missing_currencies.add(str(currency))
            market_value.loc[quantities > 1e-9] = float("nan")
            continue
        market_value = market_value.add(native_values * converted)

    cash_flows = pd.Series(0.0, index=index)
    for operation in operations.itertuples(index=False):
        currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
        flow_eur, _ = operation_settlement_eur(operation, rates_per_eur)
        if flow_eur is None:
            missing_currencies.add(currency)
            executed = pd.Timestamp(operation.executed_at).normalize()
            cash_flows.loc[cash_flows.index >= executed] = float("nan")
            continue
        if str(operation.side) == "Venta":
            flow_eur = -flow_eur
        executed = pd.Timestamp(operation.executed_at).normalize()
        if executed in cash_flows.index:
            cash_flows.loc[executed] += flow_eur

    daily = pd.DataFrame(index=index)
    daily["market_value_eur"] = market_value
    daily["net_contributions_eur"] = cash_flows.cumsum(skipna=False)
    daily["accumulated_result_eur"] = (
        daily["market_value_eur"] - daily["net_contributions_eur"]
    )
    if incomplete_operations:
        daily["accumulated_result_eur"] = float("nan")
    # Evita mostrar el periodo anterior a la primera operación como parte del historial.
    daily = daily.loc[operations["executed_at"].min() :]
    annual = _annual_summary(operations, daily, rates_per_eur)
    if incomplete_operations:
        annual["Resultado realizado EUR"] = float("nan")
    return PortfolioHistoryResult(
        daily=daily,
        annual=annual,
        missing_tickers=tuple(sorted(missing_tickers)),
        missing_currencies=tuple(sorted(missing_currencies)),
        incomplete_operations=incomplete_operations,
    )


def _annual_summary(
    operations: pd.DataFrame,
    daily: pd.DataFrame,
    rates_per_eur: dict[str, float],
) -> pd.DataFrame:
    realized_by_year = _realized_result_by_year(operations, rates_per_eur)
    rows: list[dict[str, float | int]] = []
    first_year = int(operations["executed_at"].dt.year.min())
    last_year = int(operations["executed_at"].dt.year.max())
    if not daily.empty:
        last_year = max(last_year, int(daily.index.year.max()))
    previous_result = 0.0
    for year in range(first_year, last_year + 1):
        annual_operations = operations.loc[operations["executed_at"].dt.year == year]
        buys = sales = fees = 0.0
        for operation in annual_operations.itertuples(index=False):
            converted_gross, _ = operation_settlement_eur(operation, rates_per_eur)
            converted_fee = operation_fee_eur(operation, rates_per_eur)
            if converted_gross is not None:
                if str(operation.side) == "Compra":
                    buys += converted_gross
                else:
                    sales += converted_gross
            elif str(operation.side) == "Compra":
                buys = float("nan")
            else:
                sales = float("nan")
            if converted_fee is not None:
                fees += converted_fee
            else:
                fees = float("nan")
        year_daily = daily.loc[daily.index.year == int(year)] if not daily.empty else daily
        ending_value = (
            float(year_daily["market_value_eur"].iloc[-1]) if not year_daily.empty else 0.0
        )
        accumulated_result = (
            float(year_daily["accumulated_result_eur"].iloc[-1])
            if not year_daily.empty
            else sum(result for operation_year, result in realized_by_year.items() if operation_year <= year)
        )
        if year_daily.empty:
            states = calculate_position_states(
                operations.loc[operations["executed_at"].dt.year <= year],
                include_closed=False, rates_per_eur=rates_per_eur,
            )
            if not states.empty:
                ending_value = accumulated_result = float("nan")
        total_buys_to_year = sum(
            operation_settlement_eur(row, rates_per_eur)[0]
            or 0.0
            for row in operations.loc[
                (operations["executed_at"].dt.year <= int(year))
                & (operations["side"] == "Compra")
            ].itertuples(index=False)
        )
        rows.append(
            {
                "Año": int(year),
                "Compras EUR": buys,
                "Ventas EUR": sales,
                "Aportación neta EUR": buys - sales,
                "Comisiones EUR": fees,
                "Resultado realizado EUR": realized_by_year.get(int(year), 0.0),
                "Resultado del año EUR": accumulated_result - previous_result,
                "Valor al cierre EUR": ending_value,
                "Resultado acumulado EUR": accumulated_result,
                "Resultado acumulado %": (
                    accumulated_result / total_buys_to_year * 100
                    if total_buys_to_year > 0 and pd.notna(accumulated_result)
                    else float("nan")
                ),
                "Operaciones": int(len(annual_operations)),
            }
        )
        previous_result = accumulated_result
    return pd.DataFrame(rows, columns=ANNUAL_COLUMNS).sort_values("Año", ignore_index=True)
