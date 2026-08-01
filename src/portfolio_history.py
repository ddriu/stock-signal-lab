"""Evolución temporal de una cartera a partir de operaciones y cierres diarios."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.data_sources import convert_currency


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
    """Calcula el beneficio realizado por coste medio en cada año."""

    states: dict[tuple[str, str], dict[str, float]] = {}
    results: dict[int, float] = {}
    ordered = operations.sort_values(["executed_at", "id"])
    for operation in ordered.itertuples(index=False):
        ticker = str(operation.ticker).upper()
        currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
        key = (ticker, currency)
        state = states.setdefault(key, {"quantity": 0.0, "cost_basis": 0.0})
        quantity = float(operation.quantity)
        price = float(operation.price)
        fees = float(operation.fees)
        if str(operation.side) == "Compra":
            state["quantity"] += quantity
            state["cost_basis"] += quantity * price + fees
            continue
        available = state["quantity"]
        sold = min(quantity, available)
        if sold <= 0:
            continue
        average_cost = state["cost_basis"] / available
        sale_fee = fees * sold / quantity
        realized = sold * price - sale_fee - average_cost * sold
        realized_eur = _to_eur(realized, currency, rates_per_eur)
        if realized_eur is not None:
            year = pd.Timestamp(operation.executed_at).year
            results[year] = results.get(year, 0.0) + realized_eur
        state["quantity"] -= sold
        state["cost_basis"] = max(0.0, state["cost_basis"] - average_cost * sold)
    return results


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
        return PortfolioHistoryResult(
            daily=pd.DataFrame(columns=HISTORY_COLUMNS),
            annual=_annual_summary(
                operations,
                pd.DataFrame(columns=HISTORY_COLUMNS),
                rates_per_eur,
            ),
            missing_tickers=tuple(sorted(missing_tickers)),
        )
    start_date = min(operations["executed_at"].min(), min(s.index.min() for s in usable_prices.values()))
    index = pd.date_range(start_date, last_price_date, freq="D")
    market_value = pd.Series(0.0, index=index)
    missing_currencies: set[str] = set()

    for (ticker, currency), ticker_operations in operations.groupby(
        [operations["ticker"].astype(str).str.upper(), operations["currency"].fillna("EUR").astype(str).str.upper()]
    ):
        close = usable_prices.get(str(ticker))
        if close is None:
            continue
        quantity_changes = pd.Series(0.0, index=index)
        for operation in ticker_operations.itertuples(index=False):
            executed = pd.Timestamp(operation.executed_at).normalize()
            if executed not in quantity_changes.index:
                continue
            direction = 1.0 if str(operation.side) == "Compra" else -1.0
            quantity_changes.loc[executed] += direction * float(operation.quantity)
        quantities = quantity_changes.cumsum().clip(lower=0)
        aligned_close = close.reindex(index).ffill().bfill()
        native_values = quantities * aligned_close
        converted = _to_eur(1.0, str(currency), rates_per_eur)
        if converted is None:
            missing_currencies.add(str(currency))
            continue
        market_value = market_value.add(native_values * converted, fill_value=0.0)

    cash_flows = pd.Series(0.0, index=index)
    for operation in operations.itertuples(index=False):
        currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
        gross = float(operation.quantity) * float(operation.price)
        native_flow = (
            gross + float(operation.fees)
            if str(operation.side) == "Compra"
            else -(gross - float(operation.fees))
        )
        flow_eur = _to_eur(native_flow, currency, rates_per_eur)
        if flow_eur is None:
            missing_currencies.add(currency)
            continue
        executed = pd.Timestamp(operation.executed_at).normalize()
        if executed in cash_flows.index:
            cash_flows.loc[executed] += flow_eur

    daily = pd.DataFrame(index=index)
    daily["market_value_eur"] = market_value
    daily["net_contributions_eur"] = cash_flows.cumsum()
    daily["accumulated_result_eur"] = (
        daily["market_value_eur"] - daily["net_contributions_eur"]
    )
    # Evita mostrar el periodo anterior a la primera operación como parte del historial.
    daily = daily.loc[operations["executed_at"].min() :]
    annual = _annual_summary(operations, daily, rates_per_eur)
    return PortfolioHistoryResult(
        daily=daily,
        annual=annual,
        missing_tickers=tuple(sorted(missing_tickers)),
        missing_currencies=tuple(sorted(missing_currencies)),
    )


def _annual_summary(
    operations: pd.DataFrame,
    daily: pd.DataFrame,
    rates_per_eur: dict[str, float],
) -> pd.DataFrame:
    realized_by_year = _realized_result_by_year(operations, rates_per_eur)
    rows: list[dict[str, float | int]] = []
    for year, annual_operations in operations.groupby(operations["executed_at"].dt.year):
        buys = sales = fees = 0.0
        for operation in annual_operations.itertuples(index=False):
            currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
            gross = float(operation.quantity) * float(operation.price)
            converted_gross = _to_eur(gross, currency, rates_per_eur)
            converted_fee = _to_eur(float(operation.fees), currency, rates_per_eur)
            if converted_gross is not None:
                if str(operation.side) == "Compra":
                    buys += converted_gross
                else:
                    sales += converted_gross
            if converted_fee is not None:
                fees += converted_fee
        year_daily = daily.loc[daily.index.year == int(year)] if not daily.empty else daily
        ending_value = (
            float(year_daily["market_value_eur"].iloc[-1]) if not year_daily.empty else 0.0
        )
        accumulated_result = (
            float(year_daily["accumulated_result_eur"].iloc[-1])
            if not year_daily.empty
            else realized_by_year.get(int(year), 0.0)
        )
        total_buys_to_year = sum(
            _to_eur(
                float(row.quantity) * float(row.price),
                str(getattr(row, "currency", "EUR") or "EUR"),
                rates_per_eur,
            )
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
                "Aportación neta EUR": buys - sales + fees,
                "Comisiones EUR": fees,
                "Resultado realizado EUR": realized_by_year.get(int(year), 0.0),
                "Valor al cierre EUR": ending_value,
                "Resultado acumulado EUR": accumulated_result,
                "Resultado acumulado %": (
                    accumulated_result / total_buys_to_year * 100
                    if total_buys_to_year > 0
                    else 0.0
                ),
                "Operaciones": int(len(annual_operations)),
            }
        )
    return pd.DataFrame(rows, columns=ANNUAL_COLUMNS).sort_values("Año", ignore_index=True)
