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
    )

    assert workbook.startswith(b"PK")
    with ZipFile(BytesIO(workbook)) as archive:
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
    assert "Resumen anual" in workbook_xml
    assert "Civislend y Sego" in workbook_xml
