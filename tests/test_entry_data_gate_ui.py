"""No network: old and failed quotes must never look like today's entries."""
from streamlit.testing.v1 import AppTest


HISTORICAL_SCRIPT = '''
import numpy as np
import pandas as pd
from app import prepare_data, render_entry_opportunities_page
from config import StrategyConfig
index = pd.date_range("2020-01-02", periods=280, freq="B")
close = np.linspace(80, 120, len(index)) + np.sin(np.linspace(0, 12, len(index)))
raw = pd.DataFrame({"open": close * .998, "high": close * 1.01,
    "low": close * .99, "close": close, "volume": 1_000_000}, index=index)
info = {"symbol": "TEST", "longName": "Test Company", "sector": "Technology",
    "country": "United States", "currency": "USD", "returnOnEquity": .18,
    "profitMargins": .15, "operatingMargins": .17, "revenueGrowth": .12,
    "earningsGrowth": .15, "debtToEquity": 45, "currentRatio": 1.6,
    "freeCashflow": 100, "marketCap": 1_000, "forwardPE": 18, "pegRatio": 1.2}
config = StrategyConfig()
(prepared, _, fundamentals, valuations, relatives, risks, _) = prepare_data(
    {"TEST": raw}, {"TEST": info}, {}, config)
class Journal:
    def open_positions(self):
        return pd.DataFrame(columns=["ticker"])
render_entry_opportunities_page(prepared, config, fundamentals, valuations,
    relatives, risks, {"TEST": info}, Journal(), ["TEST"], {})
'''


def test_old_prices_remain_consultable_but_not_current_or_buyable():
    page = AppTest.from_string(HISTORICAL_SCRIPT, default_timeout=25).run()
    assert not page.exception
    assert not any(metric.label in {"Comprables ahora", "Precio actual", "Entrada hoy"}
                   for metric in page.metric)
    assert not any("lectura actual" in caption.value for caption in page.caption)
    assert page.dataframe
    assert any("no disponible" in item.value.lower() for item in page.warning)


def test_progress_distinguishes_valid_prices_from_complete_analysis():
    script = '''
from datetime import datetime, timezone
from app import render_automatic_review_status
from src.radar_review import RadarReview, RadarOutcome
now = datetime(2026, 11, 21, 13, tzinfo=timezone.utc)
review = RadarReview.start(["AAPL", "MSFT", "NVDA"], "a" * 64, now)
worker = "b" * 32
review = review.claim(worker, now).release({
    "AAPL": RadarOutcome("complete", "complete", "2026-11-20", "c" * 64, price_verified=True),
    "MSFT": RadarOutcome("partial", "partial", "2026-11-20", "d" * 64, price_verified=True),
    "NVDA": RadarOutcome("no_data"),
})
render_automatic_review_status(review, compatible=True, now=now)
'''
    page = AppTest.from_string(script, default_timeout=20).run()
    assert not page.exception
    text = " ".join(item.value for item in [*page.markdown, *page.caption])
    assert "Precios validados (fecha/unidades): 2" in text
    assert "Análisis completos: 1" in text
    assert "Análisis parciales: 1" in text


def test_partial_refresh_counts_only_the_exact_confirmed_reading():
    script = HISTORICAL_SCRIPT[:HISTORICAL_SCRIPT.index("render_entry_opportunities_page(prepared")]
    script = script.replace('index = pd.date_range("2020-01-02", periods=280, freq="B")',
        'import exchange_calendars\nindex = exchange_calendars.get_calendar("XNYS").sessions_in_range("2025-01-02", "2026-11-20")[-280:]')
    script = script.replace('    {"TEST": raw}, {"TEST": info}, {}, config)',
        '    {x: raw for x in ("AAPL", "MSFT", "NVDA", "AMD")}, {x: info for x in ("AAPL", "MSFT", "NVDA", "AMD")}, {}, config)')
    script += '''
from dataclasses import replace
from datetime import datetime, timezone
from src.radar_review import RadarReview, RadarOutcome
now = datetime(2026, 11, 21, 13, tzinfo=timezone.utc)
identity = "a" * 64
for frame in prepared.values():
    frame.attrs.update(quote_units_normalized=True, display_currency="USD")
review = RadarReview.start(["AAPL", "MSFT", "NVDA", "AMD"], "b" * 64, now)
review = replace(review, outcomes={
    "AAPL": RadarOutcome("complete", "complete", "2026-11-20", identity, price_verified=True),
    "MSFT": RadarOutcome("partial", "partial", "2026-11-20", identity, price_verified=True),
    "NVDA": RadarOutcome("no_data"),
})
policy = {"snapshot_id": identity, "ticker": "AAPL", "as_of": "2026-11-20", "currency": "USD",
    "price": float(prepared["AAPL"]["close"].iloc[-1]),
    "primary": {"data_status": "complete"}, "comparative": {"data_status": "complete"},
    "data_quality": {"block_entry": False}}
render_entry_opportunities_page(prepared, config, fundamentals, valuations,
    relatives, risks, {x: info for x in prepared}, Journal(), list(prepared), {},
    radar_review=review, policy_snapshots={"AAPL": policy}, reference_time=now)
'''
    page = AppTest.from_string(script, default_timeout=25).run()
    assert not page.exception
    counts = {metric.label: metric.value for metric in page.metric
              if metric.label in {"Comprables ahora", "Esperar precio", "Demasiado extendidas", "Evento próximo"}}
    assert sorted(counts.values()) == ["N/D", "N/D", "N/D", "≥ 1"]
    assert any("1/4" in warning.value for warning in page.warning)
    assert any("3 lecturas no operativas" in item.label for item in page.expander)
    assert not any("lectura actual" in caption.value for caption in page.caption)


def test_radar_update_renders_one_global_progress_not_an_inner_download_bar():
    script = '''
from datetime import datetime, date, timezone
from dataclasses import replace
from unittest.mock import patch
import pandas as pd
import app
from src.data_sources import FxSnapshot
from src.radar_review import RadarReview, RadarOutcome
now = datetime(2026, 11, 21, 13, tzinfo=timezone.utc)
review = RadarReview.start(["AAPL", "MSFT"], "a" * 64, now)
review = replace(review, outcomes={"MSFT": RadarOutcome("no_data")})
raw = pd.DataFrame({"open": [100.], "high": [101.], "low": [99.],
    "close": [100.], "volume": [1000]}, index=pd.DatetimeIndex(["2026-11-20"]))
app.render_automatic_review_status(review, compatible=True, now=now, working=True)
with patch.object(app, "cached_download", side_effect=lambda *a, **k: raw.copy()), \\
     patch.object(app, "cached_fundamentals", return_value={"sector": "Technology"}), \\
     patch.object(app, "cached_fx_rates", return_value=FxSnapshot(as_of=None, rates_per_eur={"EUR": 1.})):
    app.load_market_data(["AAPL"], date(2025, 1, 1), date(2026, 11, 21), True,
                        review_clock=pd.Timestamp(now), show_progress=False)
'''
    page = AppTest.from_string(script, default_timeout=25).run()
    assert not page.exception
    assert len(page.get("progress")) == 1
