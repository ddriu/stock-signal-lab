"""Explicit read-only market-data acquisition and equivalent audit references.

No database writes, order submission or email calls. Downloaded bars are a
reconstruction, not archived observations. Yahoo's split-adjusted price basis
is retained consistently; cash dividends are excluded on both sides.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
import math

import pandas as pd

from config import StrategyConfig
from src.price_units import resolve_quote_unit
from src.strategy_audit_data import normalize_audit_signals, prepare_audit_bars


SERVICE_VERSION = "strategy-audit-service-v1"
BENCHMARKS = {"S&P 500 (SPY)": "SPY", "Nasdaq 100 (QQQ)": "QQQ", "Mundial (VT)": "VT"}
_CALENDARS = {
    "NYQ": "XNYS", "NYSE": "XNYS", "NMS": "XNYS", "NGM": "XNYS",
    "NCM": "XNYS", "PCX": "XNYS", "ASE": "XNYS", "BTS": "XNYS",
    # IOB regular hours align with the LSE Main Market (LSE IOB factsheet).
    "LSE": "XLON", "IOB": "XLON", "GER": "XETR", "FRA": "XFRA", "PAR": "XPAR",
    "MCE": "XMAD", "MIL": "XMIL", "AMS": "XAMS", "HKG": "XHKG",
    "JPX": "XTKS", "TOR": "XTSE", "ASX": "XASX", "STO": "XSTO",
    "EBS": "XSWX", "KSC": "XKRX",
}


@dataclass(frozen=True)
class AuditMarketBundle:
    bars: dict[str, pd.DataFrame]
    raw_prices: dict[str, pd.DataFrame]
    metadata: dict[str, dict[str, object]]
    issues: pd.DataFrame
    manifest: dict[str, object]


def _download(ticker: str, start: str, end: str) -> tuple[pd.DataFrame, dict[str, object]]:
    import yfinance as yf

    instrument = yf.Ticker(ticker)
    raw = instrument.history(start=start, end=end, interval="1d", auto_adjust=False,
                             actions=True, repair=False, timeout=20, raise_errors=True)
    metadata = instrument.get_history_metadata()
    # Do not fetch present-day fundamentals to fill historical inputs.
    safe_metadata = {key: metadata.get(key) for key in
                     ("currency", "exchangeName", "exchangeTimezoneName", "instrumentType")}
    raw = raw.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close",
                              "Volume": "volume", "Stock Splits": "stock_splits", "Dividends": "dividends"})
    raw.attrs.update(adjustment_basis="split_adjusted", provider="Yahoo/yfinance",
                     downloaded_at=datetime.now(timezone.utc).isoformat())
    return raw, safe_metadata


def exchange_schedule(metadata: Mapping[str, object], start: str, end: str) -> pd.DataFrame:
    """Use exchange calendars, including DST/holidays/early closes; never guess."""
    import exchange_calendars as calendars

    exchange = str(metadata.get("exchangeName") or "").upper()
    calendar_name = _CALENDARS.get(exchange)
    if calendar_name is None:
        raise ValueError(f"Calendario sin verificar para mercado {exchange or 'desconocido'}")
    calendar = calendars.get_calendar(calendar_name)
    schedule = calendar.schedule.loc[start:end, ["open", "close"]].rename(
        columns={"open": "market_open", "close": "market_close"})
    schedule.attrs.update(calendar=calendar_name, calendar_version=calendars.__version__)
    return schedule


def audit_universe(snapshots: pd.DataFrame) -> list[str]:
    """Historic union of both observed entry sources, never today's favorites."""
    selected: set[str] = set()
    for source in ("technical", "growth"):
        signals = normalize_audit_signals(snapshots, source=source).signals
        if not signals.empty:
            selected.update(signals.loc[signals.entry_label.eq("Entrada fuerte"), "ticker"])
    return sorted(selected)


def fetch_audit_market_bundle(
    tickers: Iterable[str], start: object, end: object, *,
    strategy_config: StrategyConfig | None = None,
    download: Callable[[str, str, str], tuple[pd.DataFrame, dict[str, object]]] | None = None,
) -> AuditMarketBundle:
    """Download explicitly, preserving missing assets and a conservative FX lag.

    ``end`` is inclusive. 450 calendar days of warmup precede the first date.
    Daily FX closes become available at 02:00 UTC on the following date. This
    conservative proxy is explicit, and never uses today's close at its open.
    """
    strategy = strategy_config or StrategyConfig()
    begin = pd.Timestamp(start).date().isoformat()
    finish = pd.Timestamp(end).date().isoformat()
    warmup = (pd.Timestamp(begin) - pd.Timedelta(days=450)).date().isoformat()
    exclusive_end = (pd.Timestamp(finish) + pd.Timedelta(days=1)).date().isoformat()
    downloader = download or _download
    requested = list(dict.fromkeys(str(t).strip().upper() for t in tickers if str(t).strip()))
    prices: dict[str, pd.DataFrame] = {}
    metadata: dict[str, dict[str, object]] = {}
    bars: dict[str, pd.DataFrame] = {}
    issues: list[dict[str, object]] = []
    fx_cache: dict[str, pd.DataFrame] = {}
    calendars: dict[str, object] = {}
    for ticker in requested:
        try:
            raw, info = downloader(ticker, warmup, exclusive_end)
            if raw.empty:
                raise ValueError("El proveedor no devolvió OHLC")
            prices[ticker], metadata[ticker] = raw.copy(deep=True), dict(info)
            currency = str(info.get("currency") or "")
            unit = resolve_quote_unit(currency)
            if not currency:
                raise ValueError("Moneda de cotización no verificada")
            schedule = exchange_schedule(info, warmup, finish)
            calendars[ticker] = dict(schedule.attrs)
            fx = None
            if unit.display_currency != "USD":
                if unit.display_currency not in fx_cache:
                    fx_raw, _ = downloader(unit.display_currency + "USD=X", warmup, exclusive_end)
                    fx = fx_raw.loc[:, ["close"]].copy()
                    dates = pd.DatetimeIndex(fx.index)
                    # Date labels retain their source calendar day.
                    fx["available_at"] = pd.to_datetime(dates.date, utc=True) + pd.Timedelta(days=1, hours=2)
                    fx_cache[unit.display_currency] = fx
                fx = fx_cache[unit.display_currency]
            prepared, problems = prepare_audit_bars(
                raw, ticker=ticker, currency=currency, schedule=schedule, fx=fx, config=strategy,
                actions=raw,
            )
            prepared.attrs["expected_sessions"] = [pd.Timestamp(day).date().isoformat() for day in schedule.index]
            prepared.attrs["session_schedule"] = [dict(
                session=pd.Timestamp(day).date().isoformat(),
                open_at=row.market_open.isoformat(), close_at=row.market_close.isoformat(),
            ) for day, row in schedule.iterrows()]
            prepared.attrs["required_last_close_at"] = schedule.market_close.iloc[-1].isoformat() if not schedule.empty else None
            bars[ticker] = prepared
            issues.extend(problems.to_dict("records") if isinstance(problems, pd.DataFrame) else problems)
        except Exception as error:
            # Only safe type/category in logs; source data remains in the private result.
            issues.append(dict(code="market_input_unavailable", severity="error", ticker=ticker,
                               session=None, detail=f"{type(error).__name__}: {str(error)[:180]}"))
    manifest = dict(version=SERVICE_VERSION, requested=requested, available=sorted(bars),
                    missing=sorted(set(requested) - set(bars)), period=[begin, finish],
                    downloaded_at=datetime.now(timezone.utc).isoformat(), calendars=calendars,
                    strategy_config=asdict(strategy),
                    price_basis="Yahoo split-adjusted OHLC; reconstructed, not archived tape",
                    cash_dividends_included=False,
                    currency_metadata="Provider listing metadata obtained at download; historical continuity assumed",
                    fx_basis="Prior available daily close; conservative availability next day 02:00 UTC",
                    limitations=["No intraday execution tape or bid/ask", "Historical configurations may be absent",
                                 "Missing/delisted assets are retained in coverage, not erased",
                                 "No claim of survival-bias-free full-market universe"])
    return AuditMarketBundle(bars, prices, metadata, pd.DataFrame(issues), manifest)


def matched_benchmark(lots: pd.DataFrame, bars: pd.DataFrame, end: object) -> dict[str, object]:
    """Hold an index proxy with the same executed contributions, not a lump sum."""
    contribution = float(pd.to_numeric(lots.get("contribution_usd", pd.Series(dtype=float)), errors="coerce").sum())
    required_columns = {"open_at", "close_at", "open", "close", "fx_open", "fx_close"}
    if not required_columns.issubset(bars.columns):
        return dict(contributions_usd=contribution, value_usd=None, profit_usd=None,
                    roi_pct=None, pending_contributions=len(lots), data_complete=False,
                    reference="Benchmark input unavailable; no price or execution invented")
    cutoff = pd.Timestamp(end)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    if len(str(end)) == 10:
        cutoff += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    source = bars.loc[pd.to_datetime(bars.close_at, utc=True) <= cutoff].copy() if not bars.empty else bars.copy()
    quantity, pending = 0.0, 0
    for row in lots.to_dict("records"):
        opened = source.loc[pd.to_datetime(source.open_at, utc=True) >= pd.Timestamp(row["entry_at"])]
        if opened.empty:
            pending += 1
            continue
        opening = opened.iloc[0]
        intended = next((pd.Timestamp(item["open_at"]) for item in bars.attrs.get("session_schedule", [])
                         if pd.Timestamp(item["open_at"]) >= pd.Timestamp(row["entry_at"])), None)
        if intended is not None and pd.Timestamp(opening.open_at) != intended:
            pending += 1
            continue
        price = float(opening.open) * float(opening.fx_open)
        if not math.isfinite(price) or price <= 0:
            pending += 1
            continue
        quantity += float(row["contribution_usd"]) / price
    value = None
    schedule = bars.attrs.get("session_schedule", [])
    expected_closes = [pd.Timestamp(row["close_at"]) for row in schedule
                       if pd.Timestamp(row["close_at"]) <= cutoff]
    required = max(expected_closes) if expected_closes else bars.attrs.get("required_last_close_at")
    terminal_complete = (not required or (not source.empty and
        pd.Timestamp(source.iloc[-1].close_at) >= pd.Timestamp(required)))
    if not source.empty and not pending and terminal_complete:
        terminal = source.iloc[-1]
        price = float(terminal.close) * float(terminal.fx_close)
        if math.isfinite(price) and price > 0:
            value = quantity * price
    return dict(contributions_usd=contribution, value_usd=value,
                profit_usd=value - contribution if value is not None else None,
                roi_pct=(value / contribution - 1) * 100 if value is not None and contribution else None,
                pending_contributions=pending, data_complete=value is not None,
                reference="Matched executed contributions; first proxy opening not before each contribution; hold; no cash dividends")


def prospective_results(signals: pd.DataFrame, bars: Mapping[str, pd.DataFrame], *,
                        end: object, horizons: tuple[int, ...] = (5, 20, 60, 120)) -> pd.DataFrame:
    """Read-only followups: unknown or not-yet-reached horizons remain pending."""
    cutoff = pd.Timestamp(end)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    if len(str(end)) == 10:
        cutoff += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    rows = []
    for signal in signals.to_dict("records"):
        frame = bars.get(str(signal["ticker"]), pd.DataFrame())
        eligible = frame.loc[(pd.to_datetime(frame.open_at, utc=True) > signal["observed_at"])
                             & (pd.to_datetime(frame.close_at, utc=True) <= cutoff)] if not frame.empty else frame
        for horizon in horizons:
            result = dict(event_id=signal["event_id"], ticker=signal["ticker"],
                          observed_at=signal["observed_at"], sessions=horizon,
                          status="Pendiente", return_pct=None, price_origin="reconstructed")
            gap_free = True
            scheduled = frame.attrs.get("session_schedule", [])
            expected_open = next((pd.Timestamp(row["open_at"]) for row in scheduled
                                  if pd.Timestamp(row["open_at"]) > signal["observed_at"]), None)
            if expected_open is not None and not eligible.empty:
                gap_free = pd.Timestamp(eligible.iloc[0].open_at) == expected_open
            if len(eligible) >= horizon and frame.attrs.get("expected_sessions"):
                actual = [pd.Timestamp(day).date().isoformat() for day in eligible.index[:horizon]]
                expected = [day for day in frame.attrs["expected_sessions"] if day >= actual[0]][:horizon]
                gap_free = gap_free and actual == expected
            if len(eligible) >= horizon and gap_free:
                first, last = eligible.iloc[0], eligible.iloc[horizon - 1]
                entry, terminal = first.open * first.fx_open, last.close * last.fx_close
                if pd.notna(entry) and pd.notna(terminal) and entry > 0 and terminal > 0:
                    result.update(status="Observado", return_pct=(terminal / entry - 1) * 100)
            rows.append(result)
    return pd.DataFrame(rows)


def open_scenarios(lots: pd.DataFrame, withdrawals_usd: float | None = 0.0) -> pd.DataFrame:
    """Hypothetical price rises with constant FX, never forecasts or decisions."""
    opened = lots.loc[~lots.status.eq("closed")] if not lots.empty else lots
    known = opened.empty or opened.value_usd.notna().all()
    value = float(opened.value_usd.sum()) if known and not opened.empty else 0.0 if known else None
    return pd.DataFrame([dict(scenario=f"Hipótesis +{rate}% (no pronóstico)",
                              open_value_usd=value * (1 + rate / 100) if value is not None else None,
                              wealth_usd=withdrawals_usd + value * (1 + rate / 100) if value is not None and pd.notna(withdrawals_usd) else None,
                              fx="Constante; escenario de precios") for rate in (5, 10, 15, 20)])


def audit_excel(report, *, signals: pd.DataFrame, rejected: pd.DataFrame,
                comparisons: pd.DataFrame | None = None, benchmarks: pd.DataFrame | None = None,
                forward: pd.DataFrame | None = None, input_manifest: Mapping[str, object] | None = None) -> bytes:
    """Private in-memory export; strings cannot become spreadsheet formulas."""
    manifest = dict(report=report.manifest, inputs=dict(input_manifest or {}))
    manifest_rows = []
    # Excel cells have a 32,767-character limit. Preserve the full provenance
    # rather than silently truncating a large historic input manifest.
    for section, values in manifest.items():
        encoded = json.dumps(values, ensure_ascii=False, default=str, sort_keys=True)
        manifest_rows.extend(dict(section=section, part=index // 30000 + 1, content=encoded[index:index + 30000])
                             for index in range(0, len(encoded), 30000))
    daily = report.daily.reset_index() if "date" not in report.daily.columns else report.daily
    tables = {"Resumen": pd.DataFrame([report.metrics]), "Evolución": daily,
              "Lotes": report.lots, "Ventas": report.trades, "Incidencias": report.issues,
              "Señales observadas": signals, "Revisiones no duplicadas": rejected,
              "Comparación": comparisons, "Índices": benchmarks, "Seguimiento": forward,
              "Reglas y procedencia": pd.DataFrame(manifest_rows)}
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", engine_kwargs={
        "options": {"strings_to_formulas": False, "strings_to_urls": False}
    }) as writer:
        for sheet, data in tables.items():
            if data is None:
                continue
            safe = data.copy(deep=True)
            for column in safe:
                safe[column] = safe[column].map(lambda value:
                    value.isoformat() if isinstance(value, (pd.Timestamp, datetime)) else
                    json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list, tuple)) else value)
            safe.to_excel(writer, sheet_name=sheet[:31], index=False)
    return output.getvalue()
