import pytest

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


def test_frozen_windows_app_uses_private_local_app_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.journal.sys.platform", "win32")
    monkeypatch.setattr("src.journal.sys.frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("STOCK_SIGNAL_LAB_DATA_DIR", raising=False)
    assert default_database_path() == tmp_path / "StockSignalLab" / "trading_journal.db"
