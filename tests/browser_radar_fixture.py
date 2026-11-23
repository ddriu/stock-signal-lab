"""Isolated browser fixture for the Radar replaceable surface and durable queue.

Real: app.main (Radar), native page navigation, _analysis_result_surface,
_radar_review_step, _run_radar_batch, review/checkpoint CAS and SQLite snapshots.
Synthetic: login, providers, analysis, result cards and non-Radar page bodies.
No environment credentials, Supabase, provider HTTP or SMTP are used.

Run from the repository root with PYTHONPATH=. and streamlit run this file.
Test users qa / qa2; password local-radar-qa (test-only, deliberately public).
Storage: /private/tmp/ssl-radar-browser-20261123/<user>.sqlite.
"""

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import streamlit as st

from config import BacktestConfig, StrategyConfig
from src.auth import AuthConfig
from src.journal import TradingJournal
from src.radar_review import RadarReview


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("SSL_RADAR_QA_DIR", "/private/tmp/ssl-radar-browser-20261123"))
USERS = {"qa", "qa2"}
UNIVERSE = [f"T{i:03}" for i in range(175)]
PRICE_DAY = "2026-11-20"
OLD_DAY = "2026-11-19"
INCIDENT = "T007"


def private_app_module():
    # A module per *run* also isolates overlapping fast reruns of one session.
    # Reusing a session module could capture the prior run's patched dispatch as
    # real_main while the prior run was still active. Registered fragment
    # callbacks keep these private globals alive after the main script returns.
    name = "_ssl_radar_browser_" + uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def local_login():
    username = st.session_state.get("_qa_authenticated_user")
    if username not in USERS:
        st.title("Stock Signal Lab · QA local")
        st.info("Entorno aislado. Datos simulados; no es una recomendación de inversión.")
        with st.form("qa_login"):
            selected = st.text_input("Usuario", value="qa")
            password = st.text_input("Contraseña", type="password")
            submitted = st.form_submit_button("Entrar en QA")
        if submitted:
            if selected in USERS and password == "local-radar-qa":
                st.session_state["_qa_authenticated_user"] = selected
                st.rerun()
            st.error("Cuenta de prueba no válida.")
        st.stop()
    return AuthConfig(username, "synthetic-not-a-password-hash", display_name=f"QA {username}")


def add_snapshot(journal, ticker, day, price, marker):
    return journal.add_analysis_snapshot(
        ticker=ticker, analyzed_at=day, price=price, opportunity_score=61,
        confidence_pct=100, company_score=63, entry_score=60,
        valuation_score=58, relative_score=62, risk_score=55,
        opportunity_label="SIMULADO · Esperar", entry_label="SIMULADO",
        position_label="Sin posición real", note=marker,
        explanation="Dato sintético exclusivo de la prueba de navegador.",
    )


def local_journal(owner):
    safe_owner = owner if owner in USERS else "group-empty"
    journal = TradingJournal(DATA / f"{safe_owner}.sqlite", owner=safe_owner)
    with journal._connect() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS qa_provider_calls ("
                           "id INTEGER PRIMARY KEY, run_id TEXT, ticker TEXT, phase TEXT, at TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS qa_seed (id INTEGER PRIMARY KEY)")
        seeded = connection.execute("SELECT 1 FROM qa_seed WHERE id=1").fetchone()
    if not seeded and safe_owner in USERS:
        for ticker in UNIVERSE:
            journal.add_favorite(ticker, name=f"Empresa simulada {ticker}", recorded_by=safe_owner)
        for ticker in UNIVERSE[:3]:
            add_snapshot(journal, ticker, OLD_DAY, 90, "QA_PREVIOUS_RESULT")
        with journal._connect() as connection:
            connection.execute("INSERT INTO qa_seed(id) VALUES(1)")
    return journal


def synthetic_load(tickers, *_args, **kwargs):
    owner = st.session_state["_qa_authenticated_user"]
    journal = local_journal(owner)
    checkpoint = journal.get_radar_review_checkpoint()
    run_id = checkpoint["run_id"] if checkpoint else "outside-review"
    callback = kwargs.get("progress_callback", lambda *_: None)
    market = dict(st.session_state.get("market_data", {}))
    fundamentals = dict(st.session_state.get("fundamental_data", {}))
    refreshed, errors, diagnostics = set(), [], {}
    for ticker in tickers:
        for stage in ("Consultando precio", "Consultando fundamentales"):
            callback(ticker, stage)
            with journal._connect() as connection:
                connection.execute("INSERT INTO qa_provider_calls(run_id,ticker,phase,at) VALUES(?,?,?,?)",
                                   (run_id, ticker, stage, datetime.now(timezone.utc).isoformat()))
            time.sleep(float(os.environ.get("SSL_RADAR_QA_DELAY", "0.06")))
        if ticker == INCIDENT:
            diagnostics[ticker] = "provider_unavailable"
            errors.append(f"{ticker}: incidencia simulada del proveedor; se conserva lo anterior como histórico.")
            continue
        frame = pd.DataFrame({"open": [99.0], "high": [102.0], "low": [98.0],
                              "close": [100.0], "volume": [1_000_000]},
                             index=pd.DatetimeIndex([PRICE_DAY]))
        frame.attrs.update(quote_units_normalized=True, display_currency="USD",
                           quote_currency="USD", provider="SIMULATED_QA", synthetic=True)
        market[ticker], fundamentals[ticker] = frame, {"qa_complete": True}
        kwargs["review_reference_data"][application.benchmark_for_ticker(ticker)] = frame.copy()
        refreshed.add(ticker)
    st.session_state["market_data"] = market
    st.session_state["fundamental_data"] = fundamentals
    st.session_state["download_errors"] = errors
    st.session_state["_last_price_refresh"] = {"stale": [], "diagnostics": diagnostics}
    return refreshed


def synthetic_analysis(prices, *_args, **_kwargs):
    rows = [{"Ticker": ticker, "Fecha": frame.index[-1].date(), "Política de análisis": {
        "snapshot_id": sha256(f"QA/{ticker}/{PRICE_DAY}".encode()).hexdigest(),
        "primary": {"data_status": "complete"}, "comparative": {"data_status": "complete"},
    }} for ticker, frame in prices.items()]
    return prices, rows, {}, {}, {}, {}, {}


def synthetic_persist(journal, dates, prepared, *_args, **_kwargs):
    # The investment calculation is simulated, but these snapshot writes are
    # actual journal writes, with real SQLite validation and idempotent lookup.
    existing = journal.list_analysis_snapshots()
    keys = {(row.ticker, pd.Timestamp(row.analyzed_at).date().isoformat())
            for row in existing.itertuples()}
    saved, unchanged = [], []
    for ticker, day in dates.items():
        if (ticker, day) in keys:
            unchanged.append(ticker)
        else:
            add_snapshot(journal, ticker, day, float(prepared[ticker].close.iloc[-1]), "QA_CURRENT_RESULT")
            saved.append(ticker)
    return SimpleNamespace(saved_tickers=tuple(saved), existing_tickers=tuple(unchanged))


def synthetic_cards(*_args, **kwargs):
    journal = kwargs.get("journal") or local_journal(st.session_state["_qa_authenticated_user"])
    snapshots = journal.list_analysis_snapshots()
    current = snapshots[snapshots.note.eq("QA_CURRENT_RESULT")]
    if current.empty:
        st.subheader("RESULTADO ANTERIOR · QA_PREVIOUS_RESULT")
        st.caption(f"Datos sintéticos guardados del {OLD_DAY}; no son de esta revisión.")
        columns = st.columns(3)
        for column, label in zip(columns, ("Esperar precio: 148", "Comprables: 3", "Extendidas: 1")):
            column.info(label + " · SIMULADO")
    else:
        st.subheader("RESULTADO GUARDADO · QA_CURRENT_RESULT")
        st.write(f"{len(current)} análisis sintéticos guardados · cierre {PRICE_DAY}.")
        st.dataframe(current[["ticker", "analyzed_at", "price"]].head(8), hide_index=True,
                     width="stretch")
    with st.expander("Evidencia local de la prueba"):
        review = journal.get_radar_review_checkpoint()
        st.json({"owner": journal.owner, "database": str(journal.database_path),
                 "review": review, "snapshot_count": len(snapshots)}, expanded=False)


def fixture_other_page(route, pages):
    user = local_login()
    application._bind_navigation_owner(user.username)
    application._bind_radar_execution_view(route.key)
    application.render_app_header(user)
    application._render_route_links(application.PRIMARY_ROUTES, pages, selected=route.group, primary=True)
    if route.group in application.LOCAL_ROUTES:
        application._render_route_links(application.LOCAL_ROUTES[route.group], pages, selected=route.key)
    st.header(f"{route.label} · cuerpo QA simulado")
    st.caption("Esta página permite interrumpir y volver al Radar. Su contenido no se valida aquí.")
    journal = local_journal(user.username)
    if route.key == "favoritas":
        st.dataframe(journal.list_favorites()[["ticker", "name"]], hide_index=True, width="stretch")
    else:
        st.write(f"Cuenta local: {user.username} · 175 favoritas sintéticas.")


st.set_page_config(page_title="Radar QA aislado", layout="wide")
application = private_app_module()
real_main = application.main


def dispatch(*, route=None, pages=None):
    if route and route.key == "radar":
        return real_main(route=route, pages=pages)
    return fixture_other_page(route or application.ROUTES["hoy"], pages)


def forbidden_io(*_args, **_kwargs):
    raise AssertionError("QA fixture attempted an unmocked external I/O boundary")


def local_sidebar(favorites, _labels, **_kwargs):
    st.caption("QA: ajustes fijos, sin claves ni proveedores reales.")
    return (favorites, date(2024, 11, 23), date(2026, 11, 23), True, "",
            StrategyConfig(), BacktestConfig(), False)


patches = {
    "main": dispatch, "require_login": local_login,
    "load_auth_accounts": lambda: {}, "cached_journal": local_journal,
    "persistent_journal_enabled": lambda: True, "build_sidebar": local_sidebar,
    "load_market_data": synthetic_load, "_analysis_view_context": synthetic_analysis,
    "persist_analysis_batch_snapshots": synthetic_persist,
    "growth_fundamental_status": lambda info: "complete" if info else "pending",
    "render_opportunities_page": synthetic_cards,
    "hydrate_saved_market": lambda *_a, **_kw: None,
    "_refresh_saved_market_on_timer": lambda *_a, **_kw: None,
    "download_prices": forbidden_io, "cached_download": forbidden_io,
}
# Permanent substitutions are safe only because `application` belongs to this
# isolated run and is never the shared imported `app` module. Fragment timers
# execute after the main script finishes: restoring providers here would leak
# real I/O into an ostensibly offline browser fixture.
try:
    for key, value in patches.items():
        setattr(application, key, value)
    st.caption("QA LOCAL · Proveedores/análisis simulados · SQLite aislado · Sin correos ni datos reales")
    application.run_app()
finally:
    # Removing the import registry entry does not restore or destroy the private
    # globals retained by registered fragments. Their providers remain fake.
    sys.modules.pop(application.__name__, None)
