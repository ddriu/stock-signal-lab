import json

import pandas as pd
import pytest

from scripts import run_strategy_audit, write_strategy_audit_report


def test_all_predeclared_variants_preserve_original_and_exact_thresholds():
    variants = run_strategy_audit.variants(run_strategy_audit.AuditConfig())
    assert len(variants) == 28
    assert variants["A1"].exit_policy == "reduce"
    assert variants["A2"].exit_policy == "sell"
    assert variants["A3"].exit_policy == "hold"
    assert variants["B1"].entry_mode == "activation"
    assert variants["C85_reduce"].min_score == 85
    assert variants["D_fixed_reduce"].amount_usd == 100
    assert variants["D_tiered_reduce"].sizing == "tiered"
    assert variants["Exit_original"].exit_policy == "original"


def test_human_report_contains_partial_cutoff_and_matched_benchmark_without_new_downloads(monkeypatch, tmp_path):
    result = tmp_path / "result"
    (result / "A1").mkdir(parents=True)
    manifest = dict(source="technical", period=["2026-10-02", "2026-10-09"],
        coverage=dict(input_rows=1, historical_tickers=1, accepted_signals=1, rejected_rows=0), raw_history_sha256="frozen")
    (result / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    pd.DataFrame([dict(variant="A1", n_lots=1, unfilled_entries=2,
        last_complete_valuation_date="2026-10-05", last_complete_contributions_usd=1.,
        last_complete_value_usd=.9, last_complete_roi_pct=-10.)]).to_csv(result / "comparison.csv", index=False)
    pd.DataFrame([dict(entry_at="2026-10-05T13:30:00Z", contribution_usd=1.)]).to_csv(result / "A1/lots.csv", index=False)
    monkeypatch.setattr(write_strategy_audit_report, "load_bundle", lambda cache: ({}, {"downloaded_at": "2026-10-06T00:00Z"}))
    output = tmp_path / "private-report.md"
    assert write_strategy_audit_report.main(["--results", str(result), "--cache", str(tmp_path), "--output", str(output)]) == 0
    content = output.read_text(encoding="utf-8")
    assert "2026-10-05" in content
    assert "−3,05% no se fuerzan" in content
    assert "sin costes ni impuestos" in content
    references = pd.read_csv(output.with_suffix(".benchmarks.csv"))
    assert len(references) == 3
    assert references.value_usd.isna().all()
    assert not references.data_complete.any()


def test_cli_refuses_financial_outputs_inside_public_repository(tmp_path):
    with pytest.raises(ValueError, match="OUTSIDE"):
        run_strategy_audit.main(["--snapshots", str(tmp_path / "missing.json"), "--cache", str(tmp_path),
                                 "--output", str(run_strategy_audit.Path(__file__).resolve().parents[1])])
