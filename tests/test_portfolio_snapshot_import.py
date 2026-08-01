import pandas as pd
import pytest

from src.journal import TradingJournal
from src.portfolio_snapshot_import import (
    account_summaries_from_positions,
    import_portfolio_workbook_snapshot,
    normalize_portfolio_snapshot_frame,
    PortfolioWorkbookSnapshot,
)


def sample_snapshot_source() -> pd.DataFrame:
    common = {
        "Fecha": "17/07/2026",
        "Cantidad": None,
        "Precio actual": None,
        "Moneda": "EUR",
        "Rentabilidad": None,
        "Ganancia/Pérdida (€)": 0,
        "Comentarios": "",
        "Fuente": "Prueba",
    }
    return pd.DataFrame(
        [
            {
                **common,
                "Plataforma": "MyInvestor",
                "Activo": "Efectivo MyInvestor",
                "Ticker / ISIN": "",
                "Tipo": "Efectivo",
                "Bloque": "Liquidez",
                "Valor actual (€)": 10,
                "Coste estimado (€)": 10,
            },
            {
                **common,
                "Plataforma": "MyInvestor",
                "Activo": "Sego Factoring",
                "Ticker / ISIN": "",
                "Tipo": "Factoring",
                "Bloque": "Alternativo",
                "Valor actual (€)": 1_300,
                "Coste estimado (€)": 1_300,
            },
            {
                **common,
                "Plataforma": "MyInvestor",
                "Activo": "Fondo tecnológico",
                "Ticker / ISIN": "IE00BM95B621",
                "Tipo": "Fondo",
                "Bloque": "Satélite",
                "Valor actual (€)": 270,
                "Rentabilidad": -0.10,
                "Coste estimado (€)": 300,
                "Ganancia/Pérdida (€)": -30,
            },
            {
                **common,
                "Plataforma": "Civislend",
                "Activo": "Proyecto 1",
                "Ticker / ISIN": "",
                "Tipo": "Crowdlending inmobiliario",
                "Bloque": "Alternativo",
                "Valor actual (€)": 500,
                "Coste estimado (€)": 500,
            },
            {
                **common,
                "Plataforma": "Trade Republic",
                "Activo": "Nintendo",
                "Ticker / ISIN": "7974 / NTDOY",
                "Tipo": "Acción",
                "Bloque": "Satélite",
                "Valor actual (€)": 420,
                "Rentabilidad": 0.05,
                "Coste estimado (€)": 400,
                "Ganancia/Pérdida (€)": 20,
            },
        ]
    )


def test_snapshot_normalization_keeps_estimates_separate_from_operations() -> None:
    positions = normalize_portfolio_snapshot_frame(sample_snapshot_source())

    assert len(positions) == 5
    assert positions.loc[positions["asset_name"] == "Sego Factoring", "platform"].iloc[0] == "Segofactoring"
    assert positions.loc[positions["asset_name"] == "Nintendo", "analysis_ticker"].iloc[0] == "NTDOY"
    assert positions.loc[positions["asset_name"] == "Fondo tecnológico", "analysis_ticker"].iloc[0] == ""
    assert positions.loc[positions["asset_name"] == "Fondo tecnológico", "return_pct"].iloc[0] == pytest.approx(-10.0)


def test_account_summary_does_not_double_count_segofactoring() -> None:
    positions = normalize_portfolio_snapshot_frame(sample_snapshot_source())
    accounts = account_summaries_from_positions(positions).set_index("account_name")

    assert accounts.loc["MyInvestor", "investments_value"] == 270
    assert accounts.loc["MyInvestor", "cash_balance"] == 10
    assert accounts.loc["Segofactoring", "investments_value"] == 1_300
    assert accounts[["investments_value", "cash_balance"]].to_numpy().sum() == 2_500


def test_snapshot_import_is_idempotent_and_creates_civislend_detail(tmp_path) -> None:
    positions = normalize_portfolio_snapshot_frame(sample_snapshot_source())
    snapshot = PortfolioWorkbookSnapshot(
        positions=positions,
        accounts=account_summaries_from_positions(positions),
        snapshot_date="2026-07-17",
    )
    journal = TradingJournal(tmp_path / "journal.db", owner="ddriu")

    first = import_portfolio_workbook_snapshot(journal, snapshot, recorded_by="ddriu")
    second = import_portfolio_workbook_snapshot(journal, snapshot, recorded_by="ddriu")

    assert first.positions_saved == 5
    assert first.civislend_created == 1
    assert second.civislend_created == 0
    assert second.civislend_updated == 1
    assert len(journal.list_portfolio_snapshot_positions()) == 5
    assert len(journal.list_private_investments()) == 1


def test_snapshot_rejects_multiple_valuation_dates() -> None:
    source = sample_snapshot_source()
    source.loc[1, "Fecha"] = "18/07/2026"

    with pytest.raises(ValueError, match="una sola fecha"):
        normalize_portfolio_snapshot_frame(source)

