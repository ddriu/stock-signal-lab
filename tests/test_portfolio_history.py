from zipfile import ZipFile
from io import BytesIO

import pandas as pd
import pytest

from src.portfolio_export import build_portfolio_excel
from src.portfolio_history import build_portfolio_history


def sample_operations() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": 1,
                "ticker": "ABC",
                "side": "Compra",
                "quantity": 10,
                "price": 100,
                "fees": 1,
                "executed_at": "2024-01-02",
                "currency": "EUR",
            },
            {
                "id": 2,
                "ticker": "ABC",
                "side": "Venta",
                "quantity": 5,
                "price": 130,
                "fees": 1,
                "executed_at": "2025-01-02",
                "currency": "EUR",
            },
        ]
    )


def test_portfolio_history_separates_value_contributions_and_result() -> None:
    prices = pd.DataFrame(
        {"close": [100, 110, 130, 140]},
        index=pd.to_datetime(["2024-01-02", "2024-12-31", "2025-01-02", "2025-12-31"]),
    )
    result = build_portfolio_history(sample_operations(), {"ABC": prices}, {"EUR": 1})

    assert result.daily.loc["2024-12-31", "market_value_eur"] == 1_100
    assert result.daily.loc["2025-12-31", "market_value_eur"] == 700
    assert result.daily.loc["2025-12-31", "net_contributions_eur"] == 352
    assert result.daily.loc["2025-12-31", "accumulated_result_eur"] == 348
    assert result.annual.loc[result.annual["Año"] == 2025, "Resultado realizado EUR"].iloc[0] == pytest.approx(148.5)


def test_portfolio_excel_contains_readable_sheets() -> None:
    prices = pd.DataFrame(
        {"close": [100, 140]},
        index=pd.to_datetime(["2024-01-02", "2025-12-31"]),
    )
    history = build_portfolio_history(sample_operations(), {"ABC": prices}, {"EUR": 1})
    workbook = build_portfolio_excel(
        operations=sample_operations(),
        positions=pd.DataFrame([{"ticker": "ABC", "quantity": 5}]),
        annual=history.annual,
        daily=history.daily,
        private_investments=pd.DataFrame(
            [{"platform": "Civislend", "invested_amount": 1_000}]
        ),
        portfolio_accounts=pd.DataFrame(
            [{"account_name": "MyInvestor", "investments_value": 2_500}]
        ),
    )

    assert workbook.startswith(b"PK")
    with ZipFile(BytesIO(workbook)) as archive:
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
    assert "Resumen anual" in workbook_xml
    assert "Civislend y Sego" in workbook_xml
    assert "Cuentas y plataformas" in workbook_xml


def test_history_respects_liquidations_instead_of_recomputing_execution_amounts() -> None:
    operations = sample_operations()
    operations["quantity"] = 1
    operations["fees"] = 0
    operations["settlement_amount_eur"] = [120, 100]
    prices = pd.DataFrame({"close": [100, 130]}, index=pd.to_datetime(["2024-01-02", "2025-01-02"]))
    result = build_portfolio_history(operations, {"ABC": prices}, {"EUR": 1})
    last = result.annual.iloc[-1]
    assert last["Resultado realizado EUR"] == pytest.approx(-20)
    assert last["Resultado acumulado EUR"] == pytest.approx(-20)
    assert last["Ventas EUR"] == pytest.approx(100)


def test_history_preserves_year_without_operations() -> None:
    operations = sample_operations().iloc[:1].copy()
    prices = pd.DataFrame({"close": [100, 110, 120]}, index=pd.to_datetime(["2024-01-02", "2024-12-31", "2025-12-31"]))
    result = build_portfolio_history(operations, {"ABC": prices}, {"EUR": 1})
    last = result.annual.iloc[-1]
    assert last["Año"] == 2025
    assert last["Operaciones"] == 0
    assert last["Resultado acumulado EUR"] == pytest.approx(199)
    assert last["Resultado del año EUR"] == pytest.approx(100)


def test_missing_prices_do_not_turn_unknown_holdings_into_losses() -> None:
    operations = sample_operations().iloc[:1].copy()
    extra = operations.copy()
    extra["id"] = 2
    extra["ticker"] = "MISSING"
    operations = pd.concat([operations, extra], ignore_index=True)
    prices = pd.DataFrame({"close": [100]}, index=pd.to_datetime(["2024-01-02"]))
    result = build_portfolio_history(operations, {"ABC": prices}, {"EUR": 1})
    assert result.missing_tickers == ("MISSING",)
    assert pd.isna(result.annual.iloc[0]["Resultado acumulado EUR"])
    assert pd.isna(result.annual.iloc[0]["Resultado acumulado %"])


def test_no_prices_at_all_keep_open_value_unknown() -> None:
    result = build_portfolio_history(sample_operations(), {}, {"EUR": 1})
    assert result.annual["Valor al cierre EUR"].isna().all()
    assert result.annual["Resultado acumulado %"].isna().all()


def test_history_uses_fifo_and_separates_broker_costs() -> None:
    operations = pd.DataFrame([
        {"id": index, "ticker": "ABC", "account_name": account, "currency": "EUR",
         "side": side, "quantity": 1, "price": price, "fees": 0,
         "executed_at": f"2026-01-0{index}"}
        for index, account, side, price in [(1, "Revolut", "Compra", 100), (2, "Trade Republic", "Compra", 200), (3, "Revolut", "Venta", 150)]
    ])
    prices = pd.DataFrame({"close": [100, 200, 150]}, index=pd.date_range("2026-01-01", periods=3))
    result = build_portfolio_history(operations, {"ABC": prices}, {"EUR": 1})
    assert result.annual.iloc[0]["Resultado realizado EUR"] == pytest.approx(50)


def test_sale_without_recorded_purchase_cannot_appear_as_free_profit() -> None:
    operations = sample_operations().iloc[1:].copy()
    prices = pd.DataFrame({"close": [130]}, index=pd.to_datetime(["2025-01-02"]))
    result = build_portfolio_history(operations, {"ABC": prices}, {"EUR": 1})
    assert result.incomplete_operations == 1
    assert result.annual["Resultado realizado EUR"].isna().all()
    assert result.annual["Resultado acumulado %"].isna().all()
