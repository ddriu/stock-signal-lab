"""Authenticated Home must enrich a populated cache without external effects.

Empty-journal startup tests do not enter the policy-enrichment path. These
fixtures exercise that real path while forbidding downloads, persistence and
SMTP; only price preparation and the downstream Home panel are substituted.
"""

import pytest
from streamlit.testing.v1 import AppTest


SCRIPT = '''
from contextlib import ExitStack
from datetime import date
from unittest.mock import patch
import pandas as pd
import streamlit as st
import app as application
from config import StrategyConfig, BacktestConfig
from src.auth import AuthConfig
from src.indicators import add_indicators

class ReadOnlyJournal:
    def list_favorites(self): return pd.DataFrame()
    def list_portfolio_snapshot_positions(self): return pd.DataFrame()
    def __getattr__(self, name):
        raise AssertionError("Unexpected journal access: " + name)

def forbidden(*args, **kwargs):
    raise AssertionError("Home enrichment attempted network, persistence or SMTP")

index = pd.bdate_range(end="2026-10-23", periods=260)
closes = [100 + offset * 0.1 for offset in range(len(index))]
raw = pd.DataFrame({
    "open": closes,
    "high": [value + 1 for value in closes],
    "low": [value - 1 for value in closes],
    "close": closes,
    "volume": [1000000] * len(index),
}, index=index)
frame = add_indicators(raw, StrategyConfig())
fundamentals = {
    "quoteType": "EQUITY", "sector": "Technology", "currency": "USD",
    "totalRevenue": 100000000, "revenueGrowth": 0.2,
    "earningsGrowth": 0.15, "operatingMargins": 0.2,
    "returnOnEquity": 0.15, "freeCashflow": 5000000,
}
if WITH_PERIODS:
    fundamentals["_metric_periods"] = {
        key: {
            "source": "SEC EDGAR", "periodicity": "annual",
            "period_start": "2025-01-01", "period_end": "2025-12-31",
            "filed_at": "2026-02-10", "comparable": True,
        }
        for key in ("revenueGrowth", "earningsGrowth", "operatingMargins", "returnOnEquity", "freeCashflow")
    }

st.session_state["main_navigation"] = "Inicio"
st.session_state["_portfolio_auto_refresh_done_qa"] = date.today().isoformat()
st.session_state["market_data"] = {"TEST": raw}
st.session_state["reference_data"] = {"SPY": raw}
st.session_state["fundamental_data"] = {"TEST": fundamentals}
user = AuthConfig(username="qa", password_hash="test-only-hash", display_name="QA")
summary = [{
    "Ticker": "TEST", "Fecha": "2026-10-23", "Precio": closes[-1],
    "Calidad empresa": 70, "Valoración": 60, "Riesgo controlado": 65,
    "Si ya la tienes": "Mantener",
}]

def home(user, journal, group_journal, prepared, reference, enriched, *args, **kwargs):
    assert len(enriched) == 1
    policy = enriched[0]["Política de análisis"]
    assert policy["position"]["held"] is True
    quality = policy["data_quality"]
    assert quality["not_a_sell_signal"] is True
    assert quality["affects_engine_scores"] is False
    if WITH_PERIODS:
        assert quality["metric_periods"]["revenueGrowth"]["period_end"] == "2025-12-31"
        assert quality["status"] == "review"
    else:
        assert quality["status"] == "unknown"
        assert quality["block_entry"] is True
    st.write("POPULATED_HOME_REACHED")

with ExitStack() as patches:
    patches.enter_context(patch("requests.sessions.Session.request", side_effect=forbidden))
    patches.enter_context(patch("smtplib.SMTP", side_effect=forbidden))
    patches.enter_context(patch("smtplib.SMTP_SSL", side_effect=forbidden))
    patches.enter_context(patch.object(application, "require_login", return_value=user))
    patches.enter_context(patch.object(application, "load_auth_accounts", return_value={}))
    patches.enter_context(patch.object(application, "cached_journal", return_value=ReadOnlyJournal()))
    patches.enter_context(patch.object(application, "render_app_header", return_value=None))
    patches.enter_context(patch.object(application, "build_sidebar", return_value=(
        [], date(2026, 1, 1), date(2026, 10, 26), True, "",
        StrategyConfig(), BacktestConfig(), False,
    )))
    patches.enter_context(patch.object(application, "load_market_data", side_effect=forbidden))
    patches.enter_context(patch.object(application, "_portfolio_tracking_tickers", return_value=["TEST"]))
    prepared = patches.enter_context(patch.object(application, "prepare_data", return_value=(
        {"TEST": frame}, summary, {}, {}, {}, {}, {},
    )))
    enriched = patches.enter_context(patch.object(
        application, "enrich_analysis_rows", wraps=application.enrich_analysis_rows,
    ))
    patches.enter_context(patch.object(application, "render_home", side_effect=home))
    patches.enter_context(patch.object(application, "persist_analysis_batch_snapshots", side_effect=forbidden))
    patches.enter_context(patch.object(application, "run_daily_alerts", side_effect=forbidden))
    application.main()
    assert prepared.call_count == 1
    assert enriched.call_count == 1
    assert enriched.call_args.kwargs["fundamental_info"] == {"TEST": fundamentals}
'''


@pytest.mark.parametrize("with_periods", [False, True])
def test_authenticated_home_enriches_populated_market_cache_without_external_effects(with_periods):
    page = AppTest.from_string(
        SCRIPT.replace("WITH_PERIODS", str(with_periods)), default_timeout=30,
    ).run()
    assert not page.exception, [item.message for item in page.exception]
    assert not page.error
    assert any("POPULATED_HOME_REACHED" in str(item.value) for item in page.markdown)
