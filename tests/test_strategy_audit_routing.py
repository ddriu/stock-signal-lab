"""Exercise the real application router, not only the isolated audit renderer."""

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_audit_route_does_not_enter_live_refresh_or_write_analysis(monkeypatch, tmp_path):
    import src.data_loader
    import src.journal

    def forbidden(*args, **kwargs):
        raise AssertionError("Opening the audit must not refresh market data or write snapshots")

    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_SIGNAL_LAB_USERNAME", "ddriu")
    monkeypatch.setenv("STOCK_SIGNAL_LAB_PASSWORD_HASH", "test-only-hash")
    monkeypatch.setattr(src.data_loader, "download_prices", forbidden)
    monkeypatch.setattr(src.journal.TradingJournal, "add_analysis_snapshot", forbidden)
    monkeypatch.setattr(src.journal.TradingJournal, "list_analysis_snapshots", forbidden)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30)
    app.session_state["_authenticated_user"] = "ddriu"
    app.session_state["main_navigation"] = "Analizar"
    app.session_state["analysis_navigation"] = "Validar"
    app.session_state["analysis_validation_navigation"] = "Auditoría de estrategias"
    app.run()
    assert not app.exception
    assert not app.error
    assert any(button.label == "Cargar historial guardado" for button in app.button)
    assert not any(button.label == "Descargar precios para esta auditoría" for button in app.button)
