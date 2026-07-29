import pytest

from src.alerts import AlertState, normalize_alert_preferences
from src.journal import TradingJournal, default_database_path


def test_journal_reconstructs_average_cost_and_realized_pnl(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_operation("ABC", "Compra", 10, 100, 1, "2025-01-01", currency="EUR")
    journal.add_operation("ABC", "Compra", 10, 120, 1, "2025-02-01", currency="EUR")
    journal.add_operation("ABC", "Venta", 5, 130, 1, "2025-03-01", currency="EUR")

    position = journal.open_positions().iloc[0]
    assert position["quantity"] == 15
    assert position["average_cost"] == pytest.approx(110.1)
    assert position["cost_basis"] == pytest.approx(1_651.5)
    assert position["realized_pnl"] == pytest.approx(98.5)
    assert position["paid_fees"] == 3


def test_journal_rejects_sale_larger_than_position(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    journal.add_operation("ABC", "Compra", 2, 100, 1, "2025-01-01", currency="EUR")
    with pytest.raises(ValueError, match="supera"):
        journal.add_operation("ABC", "Venta", 3, 110, 1, "2025-02-01", currency="EUR")


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


def test_analysis_snapshots_are_private_lightweight_history(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db")
    snapshot_id = journal.add_analysis_snapshot(
        ticker="tsm",
        analyzed_at="2026-07-29",
        price=151.5,
        opportunity_score=78,
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
    assert snapshots.iloc[0]["note"] == "Revisar después de resultados."

    journal.delete_analysis_snapshot(snapshot_id)
    assert journal.list_analysis_snapshots().empty


def test_frozen_windows_app_uses_private_local_app_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.journal.sys.platform", "win32")
    monkeypatch.setattr("src.journal.sys.frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("STOCK_SIGNAL_LAB_DATA_DIR", raising=False)
    assert default_database_path() == tmp_path / "StockSignalLab" / "trading_journal.db"


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
            )
        ]
    )

    stored = journal.get_alert_preferences()
    states = journal.list_alert_states()
    assert stored.email == "luci@example.com"
    assert stored.minimum_buy_score == 75
    assert states.iloc[0]["signature"] == "entry:Entrada fuerte"

    with pytest.raises(ValueError, match="otro usuario"):
        journal.save_alert_preferences(
            normalize_alert_preferences(owner="fer", email="fer@example.com")
        )
