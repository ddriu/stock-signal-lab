"""Private retrospective four-model CLI; no database, orders or email.

Run: python -m scripts.run_model_results --snapshots /private/history.json
  --cache /private/prices --output /private/results --start YYYY-MM-DD
  --end YYYY-MM-DD [--download] [--source both] [--excel]
Only --download authorizes public market-data retrieval. Outputs stay outside
the repository. Cached inputs must match the frozen strategy/data version.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from config import StrategyConfig
from scripts.run_strategy_audit import _cache_key, load_bundle, save_bundle
from src.model_results_engine import ExecutionCosts
from src.model_results_service import (
    build_model_results, export_model_results_excel, public_frame, public_value, report_tables,
)
from src.strategy_audit_engine import _safe
from src.strategy_audit_service import (
    BENCHMARKS, AuditMarketBundle, audit_universe, exchange_schedule, fetch_audit_market_bundle,
)
from src.strategy_audit_data import AUDIT_DATA_VERSION, prepare_audit_bars
from src.price_units import resolve_quote_unit


def reprepare_frozen_cache(source: Path, start: str, end: str) -> AuditMarketBundle:
    """Reprepare frozen raw OHLC plus exact cached point-in-time FX, offline.

    Cached normalization/indicator flags are NEVER copied onto raw OHLC. Only
    verified original adjustment metadata is retained. FX records keep their
    recorded available_at timestamps, and the current preparation age checks
    decide which are usable. Calendars and required terminal closes are rebuilt
    for this requested cutoff, not inherited from a later period.
    """
    cached, source_manifest = load_bundle(source)
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    fx_records: dict[str, dict[pd.Timestamp, float]] = {}
    for ticker, frame in cached.items():
        currency = resolve_quote_unit(str(metadata.get(ticker, {}).get("currency", ""))).display_currency
        if currency == "USD":
            continue
        for side in ("open", "close"):
            field, available_field = "fx_" + side, "fx_" + side + "_available_at"
            if available_field not in frame:
                continue
            for rate, timestamp in zip(frame[field], frame[available_field]):
                if pd.isna(rate) or pd.isna(timestamp) or not float(rate) > 0:
                    continue
                available = pd.Timestamp(timestamp)
                if available.tzinfo is None:
                    raise ValueError("Frozen FX availability must have an explicit timezone")
                available = available.tz_convert("UTC")
                prior = fx_records.setdefault(currency, {}).get(available)
                if prior is not None and abs(prior - float(rate)) > 1e-12:
                    raise ValueError("Conflicting frozen FX quotes at one availability timestamp")
                fx_records[currency][available] = float(rate)
    warmup = (pd.Timestamp(start) - pd.Timedelta(days=450)).date().isoformat()
    bars, raw_prices, issues, calendars = {}, {}, [], {}
    for ticker, info in sorted(metadata.items()):
        raw_path = source / (_cache_key(ticker) + ".raw.csv")
        if not raw_path.exists():
            issues.append(dict(code="frozen_raw_missing", ticker=ticker, severity="error", session=None,
                               detail="Frozen raw OHLC unavailable; no replacement price used."))
            continue
        raw = pd.read_csv(raw_path, index_col="session", parse_dates=["session"])
        # Provider session labels keep their local calendar date across DST;
        # execution timestamps are supplied separately by exchange calendars.
        raw.index = pd.DatetimeIndex([pd.Timestamp(value).date() for value in raw.index], name="session")
        raw = raw.loc[(raw.index.date >= pd.Timestamp(warmup).date()) &
                      (raw.index.date <= pd.Timestamp(end).date())].copy()
        old_attrs = cached[ticker].attrs if ticker in cached else {}
        adjustment = old_attrs.get("adjustment_basis")
        if not adjustment and "split-adjusted" in str(source_manifest.get("price_basis", "")):
            adjustment = "split_adjusted"
        if not adjustment:
            raise ValueError("Frozen raw adjustment basis is not verified")
        raw.attrs.update(adjustment_basis=adjustment,
            provider="Frozen Yahoo raw cache; no network retrieval",
            downloaded_at=old_attrs.get("downloaded_at", source_manifest.get("downloaded_at")))
        raw_prices[ticker] = raw
        try:
            schedule = exchange_schedule(info, warmup, end)
            currency = resolve_quote_unit(str(info.get("currency", ""))).display_currency
            fx = pd.DataFrame([dict(close=rate, available_at=stamp)
                               for stamp, rate in sorted(fx_records.get(currency, {}).items())])
            prepared, problems = prepare_audit_bars(raw, ticker=ticker,
                currency=str(info.get("currency", "")), schedule=schedule, fx=fx, actions=raw,
                config=StrategyConfig())
            prepared.attrs.update(
                calendar_coverage_start=warmup, calendar_coverage_end=end,
                expected_sessions=[pd.Timestamp(day).date().isoformat() for day in schedule.index],
                session_schedule=[dict(session=pd.Timestamp(day).date().isoformat(),
                    open_at=row.market_open.isoformat(), close_at=row.market_close.isoformat())
                    for day, row in schedule.iterrows()],
                required_last_close_at=schedule.market_close.iloc[-1].isoformat() if not schedule.empty else None,
                frozen_cache_reprepared=True,
                frozen_fx_provenance="Exact cached fx_open/close_available_at timestamps and rates; no invented publication timestamps")
            bars[ticker] = prepared
            calendars[ticker] = dict(schedule.attrs)
            issues.extend(problems.to_dict("records"))
        except (ValueError, KeyError, TypeError) as error:
            issues.append(dict(code="frozen_input_unavailable", ticker=ticker, severity="error", session=None,
                               detail=f"{type(error).__name__}: {str(error)[:180]}"))
    if not bars:
        raise ValueError("Frozen cache re-preparation produced no valid market inputs; inspect preparation errors")
    manifest = dict(source_manifest)
    manifest.update(version="frozen-cache-reprepared-model-v4", audit_data_version=AUDIT_DATA_VERSION,
        period=[start, end], available=sorted(bars), requested=sorted(metadata),
        missing=sorted(set(metadata) - set(bars)), calendars=calendars,
        reconstructed_from_frozen_cache=True,
        fx_basis="Exact recorded availability/rates from frozen prepared cache; current 7-day age validation, no future rate fill",
        frozen_fx_observations={currency: len(rows) for currency, rows in fx_records.items()},
        source_downloaded_at=source_manifest.get("downloaded_at"),
        input_tape="Frozen raw OHLC, not live prices; holidays/expected closes rebuilt for this cutoff")
    return AuditMarketBundle(bars, raw_prices, metadata, pd.DataFrame(issues), manifest)


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(_safe(public_value(value)), ensure_ascii=False,
        sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _private_output(directory: Path) -> Path:
    resolved = directory.resolve()
    repo = Path(__file__).resolve().parents[1]
    if resolved == repo or repo in resolved.parents:
        raise ValueError("Keep private outputs/caches OUTSIDE the repository")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    if resolved.stat().st_mode & 0o077:
        raise ValueError("Output/cache directory must be private (permissions 0700)")
    return resolved


def _snapshots(path: Path) -> pd.DataFrame:
    files = sorted(path.glob("snapshots-*.json")) if path.is_dir() else [path]
    if not files:
        raise ValueError("No frozen snapshot files found")
    records = []
    for file in files:
        if file.suffix.lower() == ".csv":
            records.extend(pd.read_csv(file).to_dict("records"))
        else:
            value = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                raise ValueError("Snapshot JSON must contain an array of records")
            records.extend(value)
    if not records:
        raise ValueError("Frozen observations are empty")
    return pd.DataFrame(records)


def _csv(frame: pd.DataFrame, path: Path) -> None:
    safe = public_frame(frame)
    for column in safe:
        safe[column] = safe[column].map(lambda value:
            json.dumps(_safe(value), ensure_ascii=False, allow_nan=False)
            if isinstance(value, (dict, list, tuple)) else value)
        # CSV does not carry cell types. Escape potential formulas explicitly;
        # JSON retains canonical strings and Excel writes literal cell strings.
        safe[column] = safe[column].map(lambda value:
            "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value)
    safe.to_csv(path, index=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--source", choices=("technical", "growth", "both"), default="technical")
    parser.add_argument("--budget-usd", type=float, default=6000.0)
    parser.add_argument("--commission-usd", type=float, default=1.0)
    parser.add_argument("--spread-bps", type=float, default=10.0)
    parser.add_argument("--fx-bps", type=float, default=25.0)
    parser.add_argument("--git-sha", default="unknown")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--reprepare-from", type=Path, help="Offline frozen raw cache with explicit cached FX availability")
    parser.add_argument("--excel", action="store_true")
    args = parser.parse_args(argv)
    output, cache = _private_output(args.output), _private_output(args.cache)
    charges = ExecutionCosts(args.commission_usd, args.spread_bps, args.fx_bps)
    charges.validate()
    snapshots = _snapshots(args.snapshots)
    market_issues = pd.DataFrame()
    if args.download and args.reprepare_from:
        raise ValueError("Choose --download OR --reprepare-from, never both")
    if args.reprepare_from:
        bundle = reprepare_frozen_cache(args.reprepare_from, args.start, args.end)
        save_bundle(cache, bundle)
        market_issues = bundle.issues
    elif args.download:
        universe = sorted(set(audit_universe(snapshots)) | set(BENCHMARKS.values()))
        bundle = fetch_audit_market_bundle(universe, args.start, args.end)
        save_bundle(cache, bundle)
        market_issues = bundle.issues
    bars, market_manifest = load_bundle(cache)
    if not args.download and not args.reprepare_from and (cache / "input_issues.csv").exists():
        try:
            market_issues = pd.read_csv(cache / "input_issues.csv")
        except pd.errors.EmptyDataError:
            pass
    sources = ("technical", "growth") if args.source == "both" else (args.source,)
    generated = []
    for source in sources:
        report = build_model_results(snapshots, bars, start=args.start, end=args.end,
            source=source, budget_usd=args.budget_usd, costs=charges, git_sha=args.git_sha,
            market_manifest=market_manifest, market_issues=market_issues)
        folder = output / source
        folder.mkdir(exist_ok=True, mode=0o700)
        filenames = {"Resumen": "comparison", "Evolución": "daily", "Lotes": "lots",
            "Ventas": "trades", "Atribución": "attribution", "Índices": "benchmarks",
            "Incidencias": "issues", "Señales": "observations", "Registros apartados": "rejected",
            "Referencia original": "original_reference", "Lotes ref original": "original_lots",
            "Ventas ref original": "original_trades", "Backtest original separado": "original_backtest",
            "Mantener mismos lotes": "matched_hold", "Lotes mantener": "matched_hold_lots",
            "Evolución mantener": "matched_hold_daily"}
        for name, frame in report_tables(report).items():
            _csv(frame, folder / (filenames[name] + ".csv"))
        _json(folder / "manifest.json", report.manifest)
        _json(folder / "results.json", {mode: {model: result.metrics for model, result in models.items()}
            for mode, models in (("theoretical", report.theoretical), ("budget", report.budget))})
        if args.excel:
            (folder / "model_results.xlsx").write_bytes(export_model_results_excel(report))
        generated.append(dict(source=source, report_hash=report.manifest["report_hash"]))
    _json(output / "manifest.json", dict(reports=generated,
        outputs="Private retrospective simulations, never real executions or sent emails",
        csv_strings="Potential spreadsheet formulas escaped with a leading apostrophe; canonical strings retained in JSON"))
    # Log no positions, prices, account identity or absolute private paths.
    print(json.dumps(dict(status="completed", sources=list(sources), reports=len(generated))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
