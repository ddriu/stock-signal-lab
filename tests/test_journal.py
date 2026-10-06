import sqlite3

import pandas as pd
import pytest

from src.alerts import AlertState, normalize_alert_preferences
from src.journal import TradingJournal, default_database_path


def test_existing_sqlite_database_is_migrated_without_losing_operations(tmp_path) -> None:
    database = tmp_path / "journal.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                fees REAL NOT NULL,
                executed_at TEXT NOT NULL,
                notes TEXT NOT NULL,
                currency TEXT NOT NULL,
                recorded_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO operations (
                ticker, side, quantity, price, fees, executed_at, notes,
                currency, recorded_by, created_at
            ) VALUES ('ABC', 'Compra', 1, 10, 0, '2026-01-01', '', 'EUR', '', '2026-01-01')
            """
        )
        connection.execute(
            """
            CREATE TABLE email_alert_states (
                owner TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signature TEXT NOT NULL,
                entry_score INTEGER NOT NULL,
                entry_label TEXT NOT NULL DEFAULT '',
                position_label TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL,
                evaluated_at TEXT NOT NULL,
                notified_at TEXT,
                PRIMARY KEY (owner, ticker)
            )
            """
        )

    journal = TradingJournal(database)

    operations = journal.list_operations()
    states = journal.list_alert_states()
    assert operations["ticker"].tolist() == ["ABC"]
    assert operations.iloc[0]["account_name"] == ""
    assert pd.isna(operations.iloc[0]["settlement_amount_eur"])
    assert "opportunity_score" in states.columns


def test_journal_reconstructs_fifo_cost_and_realized_pnl(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_operation("ABC", "Compra", 10, 100, 1, "2025-01-01", currency="EUR")
    journal.add_operation("ABC", "Compra", 10, 120, 1, "2025-02-01", currency="EUR")
    journal.add_operation("ABC", "Venta", 5, 130, 1, "2025-03-01", currency="EUR")

    position = journal.open_positions().iloc[0]
    assert position["quantity"] == 15
    assert position["average_cost"] == pytest.approx(1_701.5 / 15)
    assert position["cost_basis"] == pytest.approx(1_701.5)
    assert position["realized_pnl"] == pytest.approx(148.5)
    assert position["paid_fees"] == 3


def test_journal_native_eur_result_prefers_broker_liquidations(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_operation("ABC", "Compra", 1, 100, 0, "2026-01-01", settlement_amount_eur=120)
    journal.add_operation("ABC", "Venta", 1, 110, 0, "2026-02-01", settlement_amount_eur=100)
    from src.journal import calculate_position_states
    state = calculate_position_states(journal.list_operations()).iloc[0]
    assert state["realized_pnl"] == pytest.approx(-20)
    assert state["realized_pnl_eur"] == pytest.approx(-20)


def test_paper_reference_coverage_is_persisted_and_legacy_unknown(tmp_path) -> None:
    database = tmp_path / "journal.db"
    journal = TradingJournal(database)
    simulation_id = journal.create_paper_simulation(
        name="Cobertura", start_date="2026-10-01", initial_nav_eur=100, initial_positions=[]
    )
    journal.upsert_paper_daily_run(
        simulation_id=simulation_id, market_date="2026-10-01", signal_as_of=None,
        input_hash="run1", positions_after=[], cash_eur=100, net_nav_eur=100,
        hold_nav_eur=100, benchmark_nav_eur=100, coverage_pct=100,
        hold_coverage_pct=80, benchmark_coverage_pct=100,
    )
    row = journal.list_paper_daily_runs().iloc[0]
    assert row["hold_coverage_pct"] == 80
    assert row["benchmark_coverage_pct"] == 100
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE paper_daily_runs DROP COLUMN hold_coverage_pct")
        connection.execute("ALTER TABLE paper_daily_runs DROP COLUMN benchmark_coverage_pct")
    migrated = TradingJournal(database)
    row = migrated.list_paper_daily_runs().iloc[0]
    assert row["net_nav_eur"] == 100
    assert pd.isna(row["hold_coverage_pct"])
    assert pd.isna(row["benchmark_coverage_pct"])


@pytest.mark.parametrize("field", ["hold_coverage_pct", "benchmark_coverage_pct"])
def test_paper_reference_coverage_rejects_invalid_percentages(field) -> None:
    from src.journal import normalize_paper_daily_run
    arguments = dict(simulation_id=1, market_date="2026-10-01", signal_as_of=None,
                     input_hash="run1", positions_after=[], cash_eur=100, net_nav_eur=100,
                     hold_nav_eur=100, benchmark_nav_eur=100)
    with pytest.raises(ValueError):
        normalize_paper_daily_run(**arguments, **{field: 101})


def test_journal_rejects_sale_larger_than_position(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_operation("ABC", "Compra", 2, 100, 1, "2025-01-01", currency="EUR")
    with pytest.raises(ValueError, match="supera"):
        journal.add_operation("ABC", "Venta", 3, 110, 1, "2025-02-01", currency="EUR")


def test_journal_separates_the_same_ticker_by_broker(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_operation(
        "ABC", "Compra", 2, 100, 1, "2026-01-01", currency="USD",
        account_name="Revolut", settlement_amount_eur=185,
    )
    journal.add_operation(
        "ABC", "Compra", 1, 110, 1, "2026-01-02", currency="USD",
        account_name="Trade Republic", settlement_amount_eur=102,
    )

    positions = journal.open_positions()

    assert len(positions) == 2
    assert set(positions["account_name"]) == {"Revolut", "Trade Republic"}
    assert positions.set_index("account_name").loc["Revolut", "cost_basis_eur"] == 185
    with pytest.raises(ValueError, match="supera"):
        journal.add_operation(
            "ABC", "Venta", 2.5, 120, 1, "2026-01-03", currency="USD",
            account_name="Revolut", settlement_amount_eur=275,
        )


def test_journal_preserves_actual_eur_settlement_for_foreign_operations(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_operation(
        "ABC", "Compra", 2, 100, 1, "2026-01-01", currency="USD",
        account_name="Revolut", settlement_amount_eur=190, fee_eur=0.95,
    )
    journal.add_operation(
        "ABC", "Venta", 1, 120, 1, "2026-02-01", currency="USD",
        account_name="Revolut", settlement_amount_eur=110, fee_eur=0.92,
    )

    position = journal.open_positions().iloc[0]

    assert position["quantity"] == 1
    assert position["cost_basis_eur"] == pytest.approx(95)
    assert position["realized_pnl_eur"] == pytest.approx(15)
    assert position["paid_fees_eur"] == pytest.approx(1.87)
    assert bool(position["eur_values_complete"])


def test_journal_records_who_added_an_operation(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_operation(
        "ABC",
        "Compra",
        2,
        50,
        1,
        "2025-01-01",
        currency="EUR",
        recorded_by="Luci",
    )

    assert journal.list_operations().iloc[0]["recorded_by"] == "luci"


def test_historical_closure_is_atomic_idempotent_and_does_not_open_position(
    tmp_path,
) -> None:
    journal = TradingJournal(tmp_path / "journal.db", owner="demo-owner")

    identifiers = journal.add_historical_closure(
        ticker="test",
        account_name="Demo",
        quantity=10,
        cost_basis_eur=100.0,
        net_proceeds_eur=120.0,
        sold_at="2025-01-15",
        sale_fee_eur=1.0,
        quality="Documentado",
        notes="Ticket del bróker",
        recorded_by="demo-owner",
    )

    operations = journal.list_operations().sort_values("executed_at")
    assert len(identifiers) == 2
    assert operations["side"].tolist() == ["Compra", "Venta"]
    assert operations["settlement_amount_eur"].tolist() == pytest.approx(
        [100.0, 120.0]
    )
    assert operations["account_name"].str.endswith("cierre histórico").all()
    assert journal.open_positions().empty
    states = journal.portfolio_summary()
    assert states.empty

    with pytest.raises(ValueError, match="ya está registrado"):
        journal.add_historical_closure(
            ticker="TEST",
            account_name="Demo",
            quantity=10,
            cost_basis_eur=100.0,
            net_proceeds_eur=120.0,
            sold_at="2025-01-15",
            sale_fee_eur=1.0,
            quality="Documentado",
        )


def test_favorites_are_saved_without_duplicates(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")

    first_id = journal.add_favorite(
        "aapl",
        "Apple Inc.",
        "Nasdaq",
        recorded_by="Luci",
    )
    repeated_id = journal.add_favorite("AAPL", "Apple", "Nasdaq")

    favorites = journal.list_favorites()
    assert first_id == repeated_id
    assert len(favorites) == 1
    assert favorites.iloc[0]["ticker"] == "AAPL"
    assert favorites.iloc[0]["name"] == "Apple Inc."
    assert favorites.iloc[0]["recorded_by"] == "luci"

    journal.delete_favorite("aapl")
    assert journal.list_favorites().empty


def test_favorite_tags_can_be_saved_and_updated(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_favorite(
        "tsm",
        "Taiwan Semiconductor",
        "NYSE",
        tags=["Tecnología"],
        recorded_by="ddriu",
    )

    assert journal.list_favorites().iloc[0]["tags"] == "Tecnología"

    journal.update_favorite_tags("TSM", ["Tecnología", "Dividendos"])
    assert journal.list_favorites().iloc[0]["tags"] == "Tecnología, Dividendos"

    with pytest.raises(ValueError, match="no existe"):
        journal.update_favorite_tags("NOPE", ["Otra"])


def test_private_investments_are_saved_updated_and_deleted(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db", owner="ddriu")
    investment_id = journal.add_private_investment(
        platform="Civislend",
        project_name="Promoción Centro",
        invested_amount=2_000,
        current_value=2_050,
        expected_return_pct=9.5,
        start_date="2026-01-10",
        maturity_date="2027-01-10",
        notes="Garantía hipotecaria",
        recorded_by="ddriu",
    )

    investments = journal.list_private_investments()
    assert investments.iloc[0]["id"] == investment_id
    assert investments.iloc[0]["platform"] == "Civislend"
    assert investments.iloc[0]["recorded_by"] == "ddriu"

    journal.update_private_investment(
        investment_id,
        current_value=2_075,
        status="Retrasada",
        notes="Vencimiento ampliado",
    )
    updated = journal.list_private_investments().iloc[0]
    assert updated["current_value"] == 2_075
    assert updated["status"] == "Retrasada"

    journal.delete_private_investment(investment_id)
    assert journal.list_private_investments().empty


def test_private_investment_rejects_invalid_dates(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db", owner="ddriu")
    with pytest.raises(ValueError, match="vencimiento"):
        journal.add_private_investment(
            platform="Segofactoring",
            project_name="Factura 01",
            invested_amount=500,
            current_value=500,
            expected_return_pct=7,
            start_date="2026-06-01",
            maturity_date="2026-05-01",
        )


def test_portfolio_accounts_are_saved_and_updated(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db", owner="ddriu")

    account_id = journal.upsert_portfolio_account(
        account_name="MyInvestor",
        account_type="Bróker",
        investments_value=2_500,
        cash_balance=350,
        status="Pendiente de actualizar",
    )
    repeated_id = journal.upsert_portfolio_account(
        account_name="MyInvestor",
        account_type="Bróker",
        investments_value=2_650,
        cash_balance=200,
        status="Actualizada",
        notes="Resumen del bróker",
    )

    account = journal.list_portfolio_accounts().iloc[0]
    assert repeated_id == account_id
    assert account["account_name"] == "MyInvestor"
    assert account["investments_value"] == 2_650
    assert account["cash_balance"] == 200
    assert account["status"] == "Actualizada"


def test_portfolio_account_rejects_negative_values(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db", owner="ddriu")
    with pytest.raises(ValueError, match="no pueden ser negativos"):
        journal.upsert_portfolio_account(
            account_name="Revolut",
            account_type="Bróker",
            investments_value=-1,
        )


def test_complete_snapshot_replacement_removes_omitted_positions(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db", owner="ddriu")
    original = pd.DataFrame(
        [
            {
                "snapshot_date": "2026-08-13",
                "platform": "Revolut",
                "asset_name": "Moderna",
                "value_eur": 50.0,
            },
            {
                "snapshot_date": "2026-08-13",
                "platform": "Revolut",
                "asset_name": "Oracle",
                "value_eur": 270.0,
            },
        ]
    )
    journal.upsert_portfolio_snapshot_positions(original, recorded_by="ddriu")

    journal.replace_portfolio_snapshot_positions(
        original.loc[original["asset_name"] == "Oracle"],
        snapshot_date="2026-08-13",
        recorded_by="ddriu",
    )

    saved = journal.list_portfolio_snapshot_positions()
    assert saved["asset_name"].tolist() == ["Oracle"]


def test_analysis_snapshots_are_private_lightweight_history(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    snapshot_id = journal.add_analysis_snapshot(
        ticker="tsm",
        analyzed_at="2026-07-29",
        price=151.5,
        opportunity_score=78,
        confidence_pct=84,
        company_score=90,
        entry_score=64,
        valuation_score=55,
        relative_score=82,
        risk_score=61,
        opportunity_label="Candidata",
        entry_label="Vigilancia",
        position_label="Mantener",
        expected_return_pct=6.5,
        positive_rate_pct=62.0,
        expected_price=161.35,
        horizon_days=20,
        sector="Technology",
        explanation="Buena empresa con entrada todavía incompleta.",
        note="Revisar después de resultados.",
    )

    snapshots = journal.list_analysis_snapshots("TSM")
    assert snapshots.iloc[0]["id"] == snapshot_id
    assert snapshots.iloc[0]["ticker"] == "TSM"
    assert snapshots.iloc[0]["entry_score"] == 64
    assert snapshots.iloc[0]["confidence_pct"] == 84
    assert snapshots.iloc[0]["note"] == "Revisar después de resultados."

    journal.delete_analysis_snapshot(snapshot_id)
    assert journal.list_analysis_snapshots().empty


def test_frozen_windows_app_uses_private_local_app_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.journal.sys.platform", "win32")
    monkeypatch.setattr("src.journal.sys.frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("STOCK_SIGNAL_LAB_DATA_DIR", raising=False)
    assert default_database_path() == tmp_path / "StockSignalLab" / "trading_journal.db"


def test_paper_simulation_status_change_is_owner_scoped(tmp_path) -> None:
    database = tmp_path / "journal.db"
    journal = TradingJournal(database, owner="ddriu")
    simulation_id = journal.create_paper_simulation(
        name="Temporada",
        start_date="2026-09-28",
        initial_nav_eur=100,
        initial_positions=[
            {
                "ticker": "AAA",
                "quantity": 1,
                "price_eur": 100,
                "value_eur": 100,
            }
        ],
    )

    journal.set_paper_simulation_status(simulation_id, "archived")

    assert journal.list_paper_simulations(status="active").empty
    archived = journal.list_paper_simulations(status="archived")
    assert archived["id"].tolist() == [simulation_id]
    with pytest.raises(ValueError, match="no existe"):
        TradingJournal(database, owner="otro").set_paper_simulation_status(
            simulation_id,
            "archived",
        )
    with pytest.raises(ValueError, match="estado"):
        journal.set_paper_simulation_status(simulation_id, "deleted")


def test_email_alert_preferences_and_states_are_private(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db", owner="luci")
    preferences = normalize_alert_preferences(
        owner="luci",
        email="luci@example.com",
        enabled=True,
        minimum_buy_score=75,
    )
    journal.save_alert_preferences(preferences)
    journal.upsert_alert_states(
        [
            AlertState(
                owner="luci",
                ticker="TSM",
                signature="entry:Entrada fuerte",
                entry_score=80,
                entry_label="Entrada fuerte",
                position_label="Mantener",
                price=155,
                evaluated_at="2026-07-29T08:00:00+02:00",
                notified_at="2026-07-29T08:00:00+02:00",
                company_name="Taiwan Semiconductor",
                growth_score=82,
                fundamental_score=88,
                opportunity_score=79,
                opportunity_status="Comprable",
            )
        ]
    )

    stored = journal.get_alert_preferences()
    states = journal.list_alert_states()
    assert stored.email == "luci@example.com"
    assert stored.minimum_buy_score == 75
    assert states.iloc[0]["signature"] == "entry:Entrada fuerte"
    assert states.iloc[0]["company_name"] == "Taiwan Semiconductor"
    assert states.iloc[0]["growth_score"] == 82
    assert states.iloc[0]["opportunity_score"] == 79

    with pytest.raises(ValueError, match="otro usuario"):
        journal.save_alert_preferences(
            normalize_alert_preferences(owner="fer", email="fer@example.com")
        )
