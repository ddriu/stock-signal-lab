"""Reproducible private audit CLI. No credentials, DB writes, orders or emails.

Example: python -m scripts.run_strategy_audit --snapshots /private/path/snapshots
  --output /private/path/results --cache /private/path/prices --download
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.strategy_audit_data import normalize_audit_signals
from src.strategy_audit_engine import AuditConfig, run_strategy_audit
from src.strategy_audit_diagnostics import exit_counterfactuals, sector_results
from src.strategy_audit_service import (
    BENCHMARKS, audit_universe, fetch_audit_market_bundle, matched_benchmark,
    open_scenarios, prospective_results,
)


def variants(base: AuditConfig) -> dict[str, AuditConfig]:
    configs: dict[str, AuditConfig] = {}
    for mode, group in (("daily", "A"), ("activation", "B")):
        for index, exit_policy in enumerate(("reduce", "sell", "hold"), 1):
            configs[f"{group}{index}"] = replace(base, entry_mode=mode, exit_policy=exit_policy)
    for threshold in (80, 85, 90):
        for exit_policy in ("reduce", "sell", "hold"):
            configs[f"C{threshold}_{exit_policy}"] = replace(base, min_score=threshold, exit_policy=exit_policy)
    for sizing in ("fixed", "tiered", "factors"):
        for exit_policy in ("reduce", "sell", "hold"):
            configs[f"D_{sizing}_{exit_policy}"] = replace(base, amount_usd=100, sizing=sizing, exit_policy=exit_policy)
    for exit_policy in ("confirmed", "trailing10", "fixed8", "original"):
        configs["Exit_" + exit_policy] = replace(base, exit_policy=exit_policy)
    return configs


def _json(value: object) -> object:
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if value is pd.NA or value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _json(value.item())
    if isinstance(value, float) and not pd.notna(value):
        return None
    return value


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(_json(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _cache_key(ticker: str) -> str:
    return hashlib.sha256(ticker.encode()).hexdigest()[:20]


def save_bundle(cache: Path, bundle) -> None:
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(cache / "manifest.json", bundle.manifest)
    write_json(cache / "metadata.json", bundle.metadata)
    for ticker, bars in bundle.bars.items():
        key = _cache_key(ticker)
        bars.to_csv(cache / (key + ".csv"), index_label="session")
        write_json(cache / (key + ".attrs.json"), dict(bars.attrs))
    for ticker, raw in bundle.raw_prices.items():
        raw.to_csv(cache / (_cache_key(ticker) + ".raw.csv"), index_label="session")
    bundle.issues.to_csv(cache / "input_issues.csv", index=False)


def load_bundle(cache: Path) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    manifest = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))
    bars = {}
    for ticker in manifest["available"]:
        key = _cache_key(ticker)
        frame = pd.read_csv(cache / (key + ".csv"), index_col="session", parse_dates=["session", "open_at", "close_at"])
        for column in ("open_at", "close_at"):
            frame[column] = pd.to_datetime(frame[column], utc=True)
        for column in ("reduce_setup", "sell_setup"):
            frame[column] = frame[column].map(lambda v: v if isinstance(v, bool) else
                {"true": True, "false": False}.get(str(v).lower(), pd.NA))
        frame.attrs.update(json.loads((cache / (key + ".attrs.json")).read_text(encoding="utf-8")))
        bars[ticker] = frame
    return bars, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, required=True, help="Private JSON file or directory of frozen pages")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="Explicit opt-in to public price downloads")
    parser.add_argument("--source", choices=("technical", "growth"), default="technical")
    parser.add_argument("--start", default="2026-09-28")
    parser.add_argument("--end", default="2026-10-19")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    for directory in (args.output.resolve(), args.cache.resolve()):
        if directory == repo or repo in directory.parents:
            raise ValueError("Keep private results/caches OUTSIDE the public repository")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = sorted(args.snapshots.glob("snapshots-*.json")) if args.snapshots.is_dir() else [args.snapshots]
    records = [row for path in files for row in json.loads(path.read_text(encoding="utf-8"))]
    raw = pd.DataFrame(records)
    if raw.empty or raw.id.duplicated().any():
        raise ValueError("Frozen observations missing or duplicated by id")
    observed = normalize_audit_signals(raw, source=args.source)
    if args.download:
        universe = sorted(set(audit_universe(raw)) | set(BENCHMARKS.values()) | {"BA.L", "SGLN.L", "SSLN.L"})
        bundle = fetch_audit_market_bundle(universe, min(raw.analyzed_at).split(" ")[0], args.end)
        save_bundle(args.cache, bundle)
        print(json.dumps({"downloaded": len(bundle.bars), "missing": bundle.manifest["missing"]}), flush=True)
    bars, prices_manifest = load_bundle(args.cache)
    summary, comparisons = [], []
    results = {}
    base = AuditConfig(start=args.start, end=args.end)
    for name, config in variants(base).items():
        report = run_strategy_audit(observed.signals, bars, config)
        results[name] = report
        summary.append(dict(variant=name, **report.metrics))
        folder = args.output / name
        folder.mkdir(exist_ok=True)
        for table in ("daily", "lots", "trades", "issues"):
            frame = getattr(report, table)
            if table == "daily" and "date" not in frame.columns:
                frame = frame.reset_index()
            frame.to_csv(folder / (table + ".csv"), index=False)
        write_json(folder / "manifest.json", report.manifest)
        for label, ticker in BENCHMARKS.items():
            if ticker in bars:
                comparisons.append(dict(variant=name, benchmark=label,
                    **matched_benchmark(report.lots, bars[ticker], args.end)))
        print(json.dumps(_json(dict(variant=name, **report.metrics))), flush=True)
    pd.DataFrame(summary).to_csv(args.output / "comparison.csv", index=False)
    pd.DataFrame(comparisons).to_csv(args.output / "benchmarks.csv", index=False)
    def cohort_key(report):
        columns = ["event_id", "entry_at", "contribution_usd"]
        data = report.lots.loc[:, columns].astype(str).sort_values(columns).to_json(orient="records")
        return hashlib.sha256(data.encode()).hexdigest()

    configured = variants(base)
    hold_cache = {cohort_key(report): report for name, report in results.items()
                  if configured[name].exit_policy == "hold"}
    for name, report in results.items():
        # All exit variants preserve their own selected entry cohort and sizing.
        # A counterfactual may not acquire another model's/dollar amount's lots.
        cohort = observed.signals.loc[observed.signals.event_id.isin(report.lots.event_id)]
        hold_config = replace(configured[name], exit_policy="hold", entry_mode="daily")
        key = cohort_key(report)
        if key not in hold_cache:
            hold_cache[key] = run_strategy_audit(cohort, bars, hold_config)
        hold = hold_cache[key]
        exit_counterfactuals(report, hold, bars, end=args.end).to_csv(args.output / name / "exit_counterfactuals.csv", index=False)
        sector_results(report.lots).to_csv(args.output / name / "sectors.csv", index=False)
    observed.signals.to_csv(args.output / "observations.csv", index=False)
    observed.rejected.to_csv(args.output / "excluded_revisions.csv", index=False)
    open_scenarios(results["A1"].lots, results["A1"].metrics.get("withdrawals_usd")).to_csv(args.output / "hypothetical_scenarios.csv", index=False)
    prospective_results(observed.signals, bars, end=args.end).to_csv(args.output / "forward_sessions.csv", index=False)
    write_json(args.output / "manifest.json", dict(source_manifest=observed.manifest,
        coverage=observed.coverage, market_manifest=prices_manifest,
        source_ids_sha256=hashlib.sha256(json.dumps(sorted(raw.id.tolist())).encode()).hexdigest(),
        raw_history_sha256=hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        period=[args.start, args.end], source=args.source, generated_at=datetime.now(timezone.utc).isoformat(),
        warning="Exploratory diagnostics, not a strategy selection or proof of profitability"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
