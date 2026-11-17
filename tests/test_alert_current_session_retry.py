"""Synthetic provider-lag regressions; no real SMTP, prices or private data."""
from datetime import date

import pandas as pd

from src.alert_runner import _download_alert_frames
from src.data_loader import DataDownloadError


def prices(end, currency="USD"):
    index = pd.bdate_range("2025-03-03", end)
    close = pd.Series(range(len(index)), index=index, dtype=float) / 10 + 100
    frame = pd.DataFrame({"open": close, "high": close+1, "low": close-1,
                          "close": close, "volume": 1000.0}, index=index)
    frame.attrs = {"quote_currency": currency, "provider": "Synthetic provider"}
    return frame


def test_scheduled_lagging_prices_retry_once_and_replace_whole_observation():
    calls = []
    old, current = prices("2026-11-10"), prices("2026-11-11")
    current.loc[:, ["open", "high", "low", "close"]] *= 2
    def download(*args, **kwargs):
        calls.append((args, kwargs))
        return old if len(calls) == 1 else current
    frames, errors = _download_alert_frames({"ANET"}, today=date(2026,11,11),
        downloader=download, require_current_session=True)
    assert len(calls) == 2 and not errors
    assert frames["ANET"].index[-1].date() == date(2026,11,11)
    assert frames["ANET"].close.iloc[0] == current.close.iloc[0]
    assert frames["ANET"].attrs["alert_current_session_retry"] is True
    assert old.index[-1].date() == date(2026,11,10)


def test_default_manual_behavior_no_extra_calls():
    calls = []
    def download(*args, **kwargs):
        calls.append(args)
        return prices("2026-11-10")
    frames, _ = _download_alert_frames({"ANET"}, today=date(2026,11,11), downloader=download)
    assert len(calls) == 1 and frames["ANET"].index[-1].date() == date(2026,11,10)


def test_successful_current_or_holiday_close_does_not_retry():
    calls = []
    def download(*args, **kwargs):
        calls.append(args)
        return prices("2026-07-02")
    frames, errors = _download_alert_frames({"ANET"}, today=date(2026,7,3),
        downloader=download, require_current_session=True)
    assert len(calls) == 1 and not errors
    assert frames["ANET"].index[-1].date() == date(2026,7,2)


def test_still_lagging_provider_never_redates_or_fills_missing_session():
    calls = []
    def download(*args, **kwargs):
        calls.append(args)
        return prices("2026-11-10")
    frames, errors = _download_alert_frames({"ANET"}, today=date(2026,11,11),
        downloader=download, require_current_session=True)
    assert len(calls) == 2 and not errors
    assert frames["ANET"].index[-1].date() == date(2026,11,10)
    assert "alert_current_session_retry" not in frames["ANET"].attrs


def test_retry_failure_preserves_old_prices_and_exposes_safe_incident():
    calls = []
    def download(*args, **kwargs):
        calls.append(args)
        if len(calls)>1:
            raise DataDownloadError("private provider detail")
        return prices("2026-11-10")
    frames, errors = _download_alert_frames({"ANET"}, today=date(2026,11,11),
        downloader=download, require_current_session=True)
    assert len(calls) == 2 and frames["ANET"].index[-1].date() == date(2026,11,10)
    assert len(errors) == 1 and "private provider detail" not in errors[0]


def test_future_provider_rows_are_not_usable_for_requested_report():
    calls = []
    def download(*args, **kwargs):
        calls.append(args)
        return prices("2026-11-10" if len(calls)==1 else "2026-11-12")
    frames, _ = _download_alert_frames({"ANET"}, today=date(2026,11,11),
        downloader=download, require_current_session=True)
    assert frames["ANET"].index[-1].date() == date(2026,11,11)


def test_currency_conflict_never_replaces_or_converts_a_series():
    calls = []
    def download(*args, **kwargs):
        calls.append(args)
        return prices("2026-11-10") if len(calls)==1 else prices("2026-11-11", "EUR")
    frames, errors = _download_alert_frames({"ANET"}, today=date(2026,11,11),
        downloader=download, require_current_session=True)
    assert frames["ANET"].attrs["quote_currency"] == "USD"
    assert frames["ANET"].index[-1].date() == date(2026,11,10)
    assert errors and "distinta unidad" in errors[0]
