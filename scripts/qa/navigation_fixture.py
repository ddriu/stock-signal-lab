"""Real Streamlit + isolated SQLite fixture for repeatable local Chrome QA.

Run this file as the Streamlit entrypoint, not the production app. It patches
only credentials/storage transport/external providers; the real app main,
renderers, navigation, cache, Radar checkpoints and engines run unchanged.
"""

from contextlib import ExitStack
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from time import perf_counter, sleep
from unittest.mock import patch
import hashlib
import importlib
import json
import os
import sys

import exchange_calendars as calendars
import numpy as np
import pandas as pd
import streamlit as st


source = Path(os.environ["SSL_QA_SOURCE"]).resolve(strict=True)
data_dir = Path(os.environ["SSL_QA_DATA_DIR"]).resolve(strict=True)
if (data_dir.parent != Path("/private/tmp") or not data_dir.name.startswith("ssl-nav-")
        or not (source / "app.py").is_file()):
    raise RuntimeError("QA requires isolated /private/tmp/ssl-nav-* data and a source checkout")
previous = os.environ.get("SSL_QA_PREVIOUS_SOURCE")
previous = Path(previous).resolve(strict=True) if previous else None
if previous and (previous.parent != Path("/private/tmp")
                 or not previous.name.startswith("ssl-nav-")
                 or not (previous / "app.py").is_file()):
    raise RuntimeError("Warm QA requires an isolated previous source archive")
loaded_app = sys.modules.get("app")
qa_switch = bool(st.session_state.get("_qa_switch_release"))
legacy_active = bool(previous and not qa_switch and (
    loaded_app is None or Path(loaded_app.__file__).resolve().parent == previous
))
selected_source = previous if legacy_active else source
sys.path[:] = [str(selected_source), *[
    value for value in sys.path if value not in {str(source), str(previous)}
]]
if previous and qa_switch and loaded_app and Path(loaded_app.__file__).resolve().parent == previous:
    # Equivalent to replacing files in the running checkout. Keep sys.modules,
    # st.cache_resource and the current authenticated session intact.
    package = sys.modules["src"]
    package.__path__[:] = [str(source / "src")]
    package.__spec__.submodule_search_locations = package.__path__
    importlib.invalidate_caches()
    application = importlib.reload(loaded_app)
else:
    import app as application
from src import auth
from src.daily_digest import seal_daily_score_report
from src.data_loader import DataDownloadError, FUNDAMENTAL_SNAPSHOT_VERSION
from src.data_sources import FxSnapshot
from src.hourly_market_schedule import completed_daily_prices
from src.journal import TradingJournal
from src.market_cache import MarketCacheStore, encode_prices


OWNER_A = "qa_private_a"
OWNER_B = "qa_private_b"
GROUP = "grupo_compartido"
PASSWORD = "synthetic-navigation-only"
USERS = {
    owner: auth.AuthConfig(
        owner, auth.hash_password(PASSWORD, iterations=100_000, salt=owner.encode()),
        display_name=label,
    )
    for owner, label in ((OWNER_A, "Cuenta sintética Alfa"), (OWNER_B, "Cuenta sintética Beta"))
}
TICKERS = {
    OWNER_A: tuple(f"QAA{n:03}" for n in range(142)),
    OWNER_B: ("QBB000", "QBB001"),
    GROUP: (),
}
DELAY = float(os.environ.get("SSL_QA_PROVIDER_DELAY", ".12"))


def journal(owner):
    if owner not in TICKERS:
        raise ValueError("Non-synthetic journal forbidden")
    return TradingJournal(data_dir / f"{owner}.sqlite", owner=owner)


def record(event, **values):
    line = json.dumps({"event": event, **values}, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(data_dir / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


@lru_cache(maxsize=128)
def session_dates(start, end):
    calendar = calendars.get_calendar("XNYS")
    bounded = max(pd.Timestamp(start), calendar.first_session)
    # Provider series carry explicit session labels, not a generic "C" offset
    # which loses the exchange-specific holiday calendar on JSON round-trip.
    return pd.DatetimeIndex(calendar.sessions_in_range(
        bounded, min(pd.Timestamp(end), calendar.last_session)
    ).to_numpy())


def prices(symbol, start, end):
    index = session_dates(start, end)
    offset = int(hashlib.sha256(symbol.encode()).hexdigest()[:4], 16) % 47
    steps = (index - pd.Timestamp("2024-01-01")).days.to_numpy()
    close = (90 + offset) * np.exp(.00031 * steps + .024 * np.sin(steps / 17 + offset))
    frame = pd.DataFrame(
        {"open": close * .997, "high": close * 1.015, "low": close * .987,
         "close": close, "volume": np.full(len(index), 2_000_000.)}, index=index,
    )
    frame.attrs = {"ticker": symbol, "provider": "QA sintético", "quote_currency": "USD",
                   "fetched_at_utc": pd.Timestamp.now(tz="UTC").isoformat()}
    return frame


def fundamentals(symbol):
    return {
        "symbol": symbol, "shortName": f"Empresa sintética {symbol}",
        "sector": "Technology", "industry": "Software", "country": "United States",
        "currency": "USD", "financialCurrency": "USD", "quoteType": "EQUITY",
        "_snapshot_version": FUNDAMENTAL_SNAPSHOT_VERSION,
        "returnOnEquity": .22, "returnOnInvestedCapital": .18, "profitMargins": .18,
        "operatingMargins": .21, "grossMargins": .58, "revenueGrowth": .20,
        "earningsGrowth": .24, "debtToEquity": 25., "currentRatio": 1.8,
        "freeCashflow": 500_000_000., "operatingCashflow": 650_000_000.,
        "marketCap": 8_000_000_000., "totalCash": 1_000_000_000.,
        "forwardPE": 22., "trailingPE": 25., "pegRatio": 1.2, "priceToBook": 3.,
    }


@st.cache_resource(show_spinner=False)
def seed():
    now = pd.Timestamp.now(tz="UTC")
    end = now.date()
    recent = completed_daily_prices(
        "SPY", prices("SPY", end - timedelta(days=35), end), now=now
    ).index[-1].date()
    report_ids = {}
    for owner, tickers in TICKERS.items():
        db = journal(owner)
        if not tickers:
            continue
        for number, ticker in enumerate(tickers):
            db.add_favorite(ticker, f"Empresa sintética {ticker}", "NASDAQ", recorded_by=owner)
            if number < 16 and db.list_operations().query("ticker == @ticker").empty:
                db.add_operation(ticker, "Compra", 1 + number % 5, 80 + number, .5,
                                 recent - timedelta(days=60 + number), currency="USD",
                                 recorded_by=owner, account_name="Bróker sintético")
        report = seal_daily_score_report({
            "as_of": recent.isoformat(), "calendar_status": "complete",
            "rows": [{
                "ticker": ticker, "company_name": f"Empresa sintética {ticker}",
                "held": number < 16, "score": 84 if number in (0, 17, 23) else 68,
                "label": "Entrada fuerte" if number in (0, 17, 23) else "Esperar",
                "status": "entry_strong" if number in (0, 17, 23) else "watch",
                "quality": {"block_entry": False, "reasons": []}, "source": "QA sintético",
                "session": recent.isoformat(), "policy_version": "qa",
                "comparison_status": "stable", "change": 0,
                "next_step": "Consultar el cierre, sin operar.",
                "history": [{"session": recent.isoformat(), "score": 68}],
            } for number, ticker in enumerate(tickers)],
        })
        report_ids[owner] = db.save_daily_score_report(report)
        cache = MarketCacheStore(db)
        for ticker in (*tickers[:24], "SPY", "XLK"):
            raw = prices(ticker, recent - timedelta(days=810), recent)
            metadata = {
                "auto_adjust": True, "interval": "1d", "rows": len(raw),
                "history_start": raw.index[0].isoformat(), "history_end": raw.index[-1].isoformat(),
                "completed_price_date": recent.isoformat(), "source": "QA sintético",
                "quote": {"price": float(raw.close.iloc[-1]), "price_date": recent.isoformat(),
                          "currency": "USD", "indicative": False, "source": "QA sintético"},
            }
            cache.put("prices", ticker, encode_prices(raw), metadata, observed_at=now)
            if ticker in tickers:
                cache.put("fundamentals", ticker, fundamentals(ticker), {
                    "source_version": FUNDAMENTAL_SNAPSHOT_VERSION, "currency": "USD",
                    "sector": "Technology",
                }, observed_at=now)
        cache.put("fx", "ECB", {"as_of": recent.isoformat(), "rates_per_eur": {"EUR": 1., "USD": 1.1},
                                "source": "QA sintético"}, {}, observed_at=now)
    (data_dir / "fixture.json").write_text(json.dumps({"report_ids": report_ids, "latest": str(recent)}))
    return report_ids


def logged_provider(kind, symbol):
    record("provider", kind=kind, symbol=symbol,
           owner=st.session_state.get("_authenticated_user"),
           route=st.session_state.get("main_navigation"))


def qa_prices(ticker, start, end, *_args, **_kwargs):
    logged_provider("price", ticker)
    sleep(DELAY)
    if ticker == "QAA002":
        raise DataDownloadError("QA: la fuente no devolvió datos")
    result = prices(ticker, start, end)
    if ticker == "QAA003":
        latest = completed_daily_prices(ticker, result, now=pd.Timestamp.now(tz="UTC")).index[-1]
        result = result.loc[result.index < latest].copy()
    return result


def qa_fundamentals(ticker, *_args, **_kwargs):
    logged_provider("fundamentals", ticker)
    return fundamentals(ticker)


def qa_fx():
    logged_provider("fx", "ECB")
    return FxSnapshot(date.today(), {"EUR": 1.0, "USD": 1.1}, "QA sintético")


def forbidden(*_args, **_kwargs):
    record("blocked_external")
    raise AssertionError("QA fixture blocks all real provider and SMTP traffic")


seed()
started = perf_counter()
with ExitStack() as mocks:
    for target in (
        "requests.sessions.Session.request", "curl_cffi.requests.Session.request",
        "urllib.request.urlopen", "smtplib.SMTP", "smtplib.SMTP_SSL",
    ):
        mocks.enter_context(patch(target, side_effect=forbidden))
    mocks.enter_context(patch.object(auth, "load_auth_accounts", lambda: USERS))
    for name, substitute in {
        "load_auth_accounts": lambda: USERS,
        "create_journal": journal,
        "cached_download": qa_prices,
        "cached_fundamentals": qa_fundamentals,
        "cached_fx_rates": qa_fx,
        "cached_company_search": lambda *_: [],
        "cached_speculative_candidates": lambda: [],
    }.items():
        mocks.enter_context(patch.object(application, name, substitute))
    try:
        getattr(application, "run_app", application.main)()
        if legacy_active and st.session_state.get("_authenticated_user") in TICKERS:
            # Keep the mixed-runtime scenario independent of the legacy
            # dropdown's own selection/rerun race. The visual baseline mode
            # separately exercises that dropdown without a fixture shortcut.
            st.button(
                "Abrir Radar de la versión anterior (prueba)",
                on_click=lambda: st.session_state.update(
                    _requested_main_navigation="Analizar", analysis_navigation="Radar",
                ),
            )
            st.button("Aplicar actualización de prueba",
                      on_click=lambda: st.session_state.update(_qa_switch_release=True))
    finally:
        record("main_end", owner=st.session_state.get("_authenticated_user"),
               route=st.session_state.get("main_navigation"),
               detail=st.session_state.get("analysis_navigation"),
               elapsed_ms=round((perf_counter() - started) * 1000))
