import json
from pathlib import Path

import pytest
import pandas as pd
from types import SimpleNamespace

from scripts import run_model_results as cli
from test_model_results_service import inputs, _growth_cohort_inputs


def test_cli_outputs_private_json_csv_manifest_without_download(monkeypatch, tmp_path, capsys):
    snapshots, bars = inputs()
    frozen = tmp_path / "snapshots.json"
    frozen.write_text(snapshots.to_json(orient="records"), encoding="utf-8")
    monkeypatch.setattr(cli, "load_bundle", lambda path: (bars, {"version": "synthetic"}))
    monkeypatch.setattr(cli, "fetch_audit_market_bundle", lambda *args, **kwargs:
                        pytest.fail("Downloads require explicit --download"))
    output = tmp_path / "private-output"
    assert cli.main(["--snapshots", str(frozen), "--cache", str(tmp_path / "cache"),
        "--output", str(output), "--start", "2025-01-02", "--end", "2025-01-13",
        "--git-sha", "synthetic", "--source", "both", "--excel"]) == 0
    assert (output.stat().st_mode & 0o077) == 0
    assert (output / "technical" / "comparison.csv").exists()
    assert (output / "technical" / "benchmarks.csv").exists()
    assert (output / "technical" / "model_results.xlsx").exists()
    assert (output / "growth" / "manifest.json").exists()
    manifest = json.loads((output / "technical" / "manifest.json").read_text())
    assert manifest["git_sha"] == "synthetic"
    assert manifest["source"] == "technical"
    csv = (output / "technical" / "observations.csv").read_text()
    assert "'=1+1" in csv
    assert "private@example.invalid" not in csv
    assert "private@example.invalid" not in capsys.readouterr().out


def test_cli_refuses_repository_outputs_before_reading_input(tmp_path):
    with pytest.raises(ValueError, match="OUTSIDE"):
        cli.main(["--snapshots", str(tmp_path / "missing.json"), "--cache", str(tmp_path / "cache"),
            "--output", str(Path(__file__).resolve().parents[1]),
            "--start", "2025-01-02", "--end", "2025-01-13"])


def test_cli_refuses_nonprivate_output_directory(tmp_path):
    folder = tmp_path / "public-mode"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        cli._private_output(folder)


def test_offline_reprepare_rebuilds_cutoff_calendar_and_does_not_reuse_flags(tmp_path):
    _, bars = inputs()
    source = tmp_path / "frozen"
    for frame in bars.values():
        frame["volume"] = 100
        frame.attrs["adjustment_basis"] = "split_adjusted"
    metadata = {ticker: {"currency": "USD", "exchangeName": "NMS"} for ticker in bars}
    cli.save_bundle(source, SimpleNamespace(bars=bars, raw_prices=bars, metadata=metadata,
        issues=pd.DataFrame(), manifest={"available": list(bars), "price_basis": "split-adjusted OHLC"}))
    bundle = cli.reprepare_frozen_cache(source, "2025-01-02", "2025-01-10")
    assert bundle.bars
    for frame in bundle.bars.values():
        assert frame.attrs["audit_data_version"] == cli.AUDIT_DATA_VERSION
        assert frame.attrs["required_last_close_at"].startswith("2025-01-10")
        assert frame.attrs["expected_sessions"][-1] == "2025-01-10"
        assert frame.reduce_setup.isna().all()  # eight prices cannot warm up SMA200


@pytest.mark.parametrize("source", ["technical", "both"])
def test_verified_cohort_requires_growth_before_reading_or_creating_outputs(monkeypatch, tmp_path, source):
    monkeypatch.setattr(cli, "_snapshots", lambda *args: pytest.fail("must reject before reading"))
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="requires --source growth"):
        cli.main(["--snapshots", str(tmp_path / "missing.json"), "--cache", str(tmp_path / "cache"),
            "--output", str(output), "--start", "2025-01-02", "--end", "2025-01-13",
            "--source", source, "--cohort", "verified"])
    assert not output.exists()


def test_verified_cli_is_separate_and_never_replaces_raw_outputs(monkeypatch, tmp_path):
    snapshots, bars = _growth_cohort_inputs()
    frozen = tmp_path / "snapshots.json"
    frozen.write_text(snapshots.to_json(orient="records"), encoding="utf-8")
    monkeypatch.setattr(cli, "load_bundle", lambda path: (bars, {"version": "synthetic"}))
    monkeypatch.setattr(cli, "fetch_audit_market_bundle", lambda *args, **kwargs:
                        pytest.fail("no authorized download"))
    output = tmp_path / "private"
    args = ["--snapshots", str(frozen), "--cache", str(tmp_path / "cache"), "--output", str(output),
            "--start", "2025-01-02", "--end", "2025-01-13", "--source", "growth"]
    assert cli.main(args) == 0
    raw = (output / "growth" / "manifest.json").read_bytes()
    outer = (output / "manifest.json").read_bytes()
    assert cli.main([*args, "--cohort", "verified", "--excel"]) == 0
    assert (output / "growth" / "manifest.json").read_bytes() == raw
    assert (output / "manifest.json").read_bytes() == outer
    verified = json.loads((output / "growth-verified" / "manifest.json").read_text())
    assert verified["source"] == "growth" and verified["cohort"] == "verified"
    assert verified["report_hash"] != json.loads(raw)["report_hash"]
    assert (output / "growth-verified" / "model_results.xlsx").exists()
    assert json.loads((output / "manifest-verified.json").read_text())["reports"][0]["cohort"] == "verified"
