from streamlit.testing.v1 import AppTest


def test_benchmark_page_renders_portfolio_and_favorites_in_one_table() -> None:
    script = '''
import numpy as np
import pandas as pd
from app import prepare_data, render_benchmark_outperformance_page
from config import StrategyConfig

index = pd.bdate_range("2022-01-03", periods=900)
stock_close = 100 * np.cumprod(np.full(len(index), 1.0010))
spy_close = 100 * np.cumprod(np.full(len(index), 1.0003))

def prices(close):
    return pd.DataFrame({
        "open": close * .999, "high": close * 1.01,
        "low": close * .99, "close": close,
        "volume": np.full(len(close), 2_000_000),
    }, index=index)

info = {
    "symbol": "LEAD", "shortName": "Leader Inc.", "sector": "Technology",
    "country": "United States", "currency": "USD", "quoteType": "EQUITY",
    "returnOnEquity": .22, "returnOnInvestedCapital": .18,
    "profitMargins": .18, "operatingMargins": .21, "grossMargins": .58,
    "revenueGrowth": .20, "earningsGrowth": .24, "debtToEquity": 25,
    "currentRatio": 1.8, "freeCashflow": 500_000_000,
    "operatingCashflow": 650_000_000, "capitalExpenditures": 80_000_000,
    "marketCap": 8_000_000_000, "totalCash": 1_000_000_000,
    "forwardPE": 22, "trailingPE": 25, "pegRatio": 1.2, "priceToBook": 3,
}
config = StrategyConfig()
raw = prices(stock_close)
spy = prices(spy_close)
(prepared, _, _, valuations, relatives, risks, opportunities) = prepare_data(
    {"LEAD": raw}, {"LEAD": info}, {"SPY": spy}, config
)

class Journal:
    def open_positions(self):
        return pd.DataFrame([{"ticker": "LEAD"}])
    def list_portfolio_snapshot_positions(self):
        return pd.DataFrame()

render_benchmark_outperformance_page(
    prepared, config, {"LEAD": info}, {"SPY": spy}, valuations,
    relatives, risks, opportunities, Journal(), ["LEAD", "PENDING"],
    {"LEAD": "Leader Inc. (LEAD)", "PENDING": "Pending Co. (PENDING)"},
    pd.DataFrame([{"ticker": "LEAD"}, {"ticker": "PENDING"}]),
)
'''
    app = AppTest.from_string(script, default_timeout=30).run()

    assert not app.exception
    assert not app.error
    assert app.dataframe
    assert any("tres preguntas distintas" in item.value for item in app.info)
    assert any("Corto · 1–3 meses" in control.options for control in app.segmented_control)
    refresh = next(button for button in app.button if button.label == "Revisar todo el universo")
    refresh.click().run()
    state = app.session_state.filtered_state
    assert state["_force_all_favorite_refresh"] is True
    assert state["_requested_analysis_navigation"] == "Estrategias"
    assert state["analysis_strategy_navigation"] == "Ventaja relativa"
