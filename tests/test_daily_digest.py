"""Synthetic observed history; no network, account data, writes or transport."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from src.analysis_policy import encode_policy_note
from src.daily_digest import (
    _calendar_name, build_daily_score_report, render_daily_score_email,
    seal_daily_score_report, daily_score_reading, daily_score_coverage,
    daily_score_sort_key,
)


def observation(ticker="TEST", day="2026-10-23", score=88, *, profile="biotech", source="Yahoo", version="dual_strategy_v1", quality="ok", metric_key="same", label="Entrada fuerte", created=None, row_id=1, **changes):
    policy = {"policy_version": version, "snapshot_method": "dual", "ticker": ticker,
        "as_of": day, "source": source, "currency": "USD", "config": {"growth": {"strong_score": 82}},
        "primary": {"id": "growth_momentum", "strategy_id": "growth_momentum", "score": score,
            "status": label, "growth_score": 90, "momentum_score": score, "context_score": 80,
            "sector_key": profile, "data_status": "complete", "positive_factors": ["Cotiza por encima de la media intermedia"], "risk_factors": []},
        "comparative": {}, "position": {"invalidated": False},
        "data_quality": {"status": quality, "block_entry": quality != "ok", "comparability_key": metric_key,
            "reasons": [] if quality == "ok" else ["Fundamentales antiguos; actualizar antes de comprar."]}}
    for key, value in changes.items():
        if key.startswith("primary_"):
            policy["primary"][key.removeprefix("primary_")] = value
        else:
            policy[key] = value
    return dict(id=row_id, ticker=ticker, analyzed_at=day,
        created_at=created or f"{day}T22:30:00Z", entry_score=1, owner="private@example.invalid",
        note=encode_policy_note("Human private note", policy))


def test_five_exchange_sessions_keep_gaps_and_never_substitute_technical_score():
    data = pd.DataFrame([observation(day="2026-10-23", score=88), observation(day="2026-10-21", score=74)])
    row = build_daily_score_report(data, as_of="2026-10-23")["rows"][0]
    assert [item["session"] for item in row["history"]] == ["2026-10-23", "2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19"]
    assert [item["score"] for item in row["history"]] == [88, None, 74, None, None]
    assert row["score"] == 88 and row["change"] is None
    assert row["comparison_status"] == "missing_previous"


def test_actual_holiday_calendar_not_weekday_approximation():
    report = build_daily_score_report(pd.DataFrame([observation(day="2026-07-06")]), as_of="2026-07-06")
    assert report["session_dates"] == ["2026-07-06", "2026-07-02", "2026-07-01", "2026-06-30", "2026-06-29"]


def test_each_market_preserves_its_actual_sessions():
    data = pd.DataFrame([observation("TEST", "2026-07-06"), observation("TEST.MC", "2026-07-06")])
    rows = {row["ticker"]: row for row in build_daily_score_report(data, as_of="2026-07-06")["rows"]}
    assert rows["TEST"]["history"][1]["session"] == "2026-07-02"
    assert rows["TEST.MC"]["history"][1]["session"] == "2026-07-03"


def test_us_holiday_does_not_hide_a_completed_european_session():
    data = pd.DataFrame([observation("US", "2026-02-13", created="2026-02-13T22:30:00Z"),
        observation("EU.MC", "2026-02-16", created="2026-02-16T18:00:00Z")])
    report = build_daily_score_report(data, as_of="2026-02-16", reference_time="2026-02-16T22:00:00Z")
    rows = {row["ticker"]: row for row in report["rows"]}
    assert report["as_of"] == "2026-02-16"
    assert rows["EU.MC"]["session"] == "2026-02-16" and rows["EU.MC"]["score"] == 88
    assert rows["US"]["session"] == "2026-02-13" and rows["US"]["score"] == 88
    assert report["session_dates"][:2] == ["2026-02-16", "2026-02-13"]


def test_europe_closed_us_still_open_each_retains_its_completed_session():
    data = pd.DataFrame([observation("US", "2026-10-23", score=80),
        observation("US", "2026-10-26", score=99, created="2026-10-26T15:00:00Z"),
        observation("EU.MC", "2026-10-26", created="2026-10-26T17:00:00Z")])
    report = build_daily_score_report(data, as_of="2026-10-26", reference_time="2026-10-26T18:00:00Z")
    rows = {row["ticker"]: row for row in report["rows"]}
    assert report["as_of"] == "2026-10-26"
    assert rows["EU.MC"]["session"] == "2026-10-26" and rows["EU.MC"]["score"] == 88
    assert rows["US"]["session"] == "2026-10-23" and rows["US"]["score"] == 80


def test_unknown_market_suffix_does_not_fabricate_us_sessions_or_scores():
    row = build_daily_score_report(pd.DataFrame([observation("UNKNOWN.ZZ")]), as_of="2026-10-23")["rows"][0]
    assert row["calendar"] == "no_verificado"
    assert row["history"] == [] and row["score"] is None and row["status"] == "review_data"


def test_swiss_holiday_is_not_replaced_with_us_calendar():
    row = build_daily_score_report(pd.DataFrame([observation("TEST.SW", "2026-04-07")]), as_of="2026-04-07")["rows"][0]
    assert row["calendar"] == "XSWX"
    assert [item["session"] for item in row["history"]][:2] == ["2026-04-07", "2026-04-02"]


def test_known_us_share_class_symbols_remain_us_calendar():
    assert _calendar_name("BRK.B") == "XNYS"
    assert _calendar_name("BRK-B") == "XNYS"
    assert _calendar_name("MSFT") == "XNYS"
    assert _calendar_name("UNKNOWN.ZZ") == "no_verificado"


@pytest.mark.parametrize("changed", [dict(profile="general"), dict(source="New provider"), dict(version="v2"), dict(metric_key="corrected")])
def test_classification_configuration_source_or_fundamentals_changes_are_not_market_declines(changed):
    data = pd.DataFrame([observation(day="2026-10-22", score=90), observation(day="2026-10-23", score=83, **changed)])
    row = build_daily_score_report(data, as_of="2026-10-23")["rows"][0]
    assert row["change"] is None and row["comparison_status"] == "not_comparable"
    assert "Análisis actualizado" in row["change_reason"]
    assert "deterioro" not in row["change_reason"].lower()


def test_same_session_revisions_are_deterministic_not_new_days():
    data = pd.DataFrame([observation(score=80, row_id=9), observation(score=88, row_id=10)])
    first = build_daily_score_report(data, as_of="2026-10-23")
    assert first == build_daily_score_report(data.iloc[::-1], as_of="2026-10-23")
    row = first["rows"][0]
    assert row["score"] == 88 and row["revision_count"] == 1
    assert len(row["history"]) == 5 and row["history"][0]["revised"]


def test_legacy_or_corrupt_policy_never_becomes_a_technical_growth_signal():
    data = pd.DataFrame([dict(ticker="LEGACY", analyzed_at="2026-10-23", entry_score=100, note="legacy")])
    row = build_daily_score_report(data, as_of="2026-10-23")["rows"][0]
    assert row["score"] is None and row["status"] == "review_data"
    assert all(item["score"] is None for item in row["history"])


def test_blocked_fundamentals_keep_raw_score_but_never_confirm_entry():
    row = build_daily_score_report(pd.DataFrame([observation(quality="blocked")]), as_of="2026-10-23")["rows"][0]
    assert row["score"] == 88 and row["label"] == "Entrada fuerte"
    assert row["status"] == "review_data" and row["quality"]["block_entry"]
    assert "Actualizar" in row["next_step"]


def test_pending_strong_signal_is_visible_in_email_but_not_confirmed_as_a_buy():
    report = build_daily_score_report(pd.DataFrame([observation(quality="blocked")]), as_of="2026-10-23")
    before = deepcopy(report)
    _, text, html = render_daily_score_email(report)
    assert report == before
    assert "Motor: Entrada fuerte" in text and "Motor: Entrada fuerte" in html
    assert "Datos pendientes" in text and "Compra no confirmada" in text
    assert "0 entradas fuertes con datos suficientes" in text
    assert "1 señales fuertes del motor pendientes de verificar" in text
    assert "Fundamentales antiguos" in text


@pytest.mark.parametrize("changes", [{"quality": None}, {"score": None}, {"status": "review_data"}])
def test_legacy_or_inconsistent_quality_cannot_be_upgraded_by_renderer(changes):
    row = {"score": 88, "label": "Entrada fuerte", "status": "entry_strong",
           "quality": {"block_entry": False}, **changes}
    reading = daily_score_reading(row)
    assert reading["signal"] == "Entrada fuerte" and reading["blocked"]
    assert reading["decision"] == "Compra no confirmada"
    assert reading["reasons"]


def test_verified_signal_remains_a_study_not_an_automatic_purchase():
    row = {"score": 88, "label": "Entrada fuerte", "status": "entry_strong", "quality": {"block_entry": False}}
    assert daily_score_reading(row)["decision"] == "Estudiar entrada"
    assert daily_score_reading({**row, "held": True})["decision"] == "Revisar antes de ampliar"


def test_counts_and_order_separate_verified_pending_and_absent_data():
    rows = [
        {"ticker": "PENDING", "score": 99, "label": "Entrada fuerte", "status": "review_data", "quality": {"block_entry": True}},
        {"ticker": "VERIFIED", "score": 85, "label": "Entrada fuerte", "status": "entry_strong", "quality": {"block_entry": False}},
        {"ticker": "HELD", "held": True, "score": 20, "status": "wait", "quality": {"block_entry": False}},
        {"ticker": "MISSING", "score": None, "label": "Entrada fuerte", "status": "review_data"},
    ]
    assert [row["ticker"] for row in sorted(rows, key=daily_score_sort_key)] == ["HELD", "VERIFIED", "PENDING", "MISSING"]
    assert daily_score_coverage(rows) == {"total": 4, "with_score": 3, "missing_score": 1,
        "sufficient_data": 2, "pending_data": 2, "strong_verified": 1, "strong_pending": 1, "candidate_verified": 0}


def test_new_method_changes_do_not_rewrite_or_invalidate_old_report_identity():
    report = {"version": "daily-score-report-v1", "as_of": "2026-10-23", "rows": [
        {"ticker": "TEST", "score": 88, "label": "Entrada fuerte", "status": "review_data", "quality": {"block_entry": True}}]}
    sealed = seal_daily_score_report(report)
    before = deepcopy(sealed)
    render_daily_score_email(sealed)
    daily_score_coverage(sealed["rows"])
    assert sealed == before
    assert seal_daily_score_report(sealed)["report_id"] == before["report_id"]


def test_pending_strong_summary_is_not_hidden_when_five_position_alerts_fill_email():
    report = build_daily_score_report(pd.DataFrame([observation("PENDING", quality="blocked")]), as_of="2026-10-23")
    report["alerts"] = [{"ticker": f"H{index}", "held": True, "title": "Revisar exposición"} for index in range(5)]
    _, text, html = render_daily_score_email(seal_daily_score_report(report))
    assert "PENDING: 88/100" not in text  # always retain ticker identity in parentheses
    assert "(PENDING): 88/100" in text
    assert "Compra no confirmada" in html


def test_legacy_string_score_renders_without_crashing_or_promoting_pending_data():
    report = {"as_of": "2026-10-23", "rows": [{"ticker": "TEST", "score": "88",
        "label": "Entrada fuerte", "status": "review_data", "quality": {"block_entry": True}}]}
    _, text, html = render_daily_score_email(report)
    assert "88/100" in text and "Compra no confirmada" in html


def test_first_portfolio_then_favorites_without_leaking_private_notes_or_accounts():
    data = pd.DataFrame([observation("FAV", score=99), observation("HELD", score=51, label="No preparada")])
    report = build_daily_score_report(data, held_tickers=["HELD"], company_names={"HELD": "Held Full Company"}, as_of="2026-10-23")
    assert [row["ticker"] for row in report["rows"]] == ["HELD", "FAV"]
    assert report["rows"][0]["company_name"] == "Held Full Company"
    assert "sin vender solo por la nota" in report["rows"][0]["next_step"]
    assert "private@example.invalid" not in json.dumps(report)
    assert "Human private note" not in json.dumps(report)


def test_small_score_change_is_stable_and_score_zero_is_kept():
    data = pd.DataFrame([observation(day="2026-10-22", score=0), observation(day="2026-10-23", score=1)])
    row = build_daily_score_report(data, as_of="2026-10-23")["rows"][0]
    assert row["change"] == 1 and row["comparison_status"] == "stable"
    assert row["history"][1]["score"] == 0


def test_larger_movement_explains_the_factor_not_a_generic_sell_comment():
    data = pd.DataFrame([observation(day="2026-10-22", score=90), observation(day="2026-10-23", score=80)])
    row = build_daily_score_report(data, as_of="2026-10-23")["rows"][0]
    assert row["change"] == -10 and "Baja la fuerza del precio de 90 a 80" in row["change_reason"]


def test_future_observation_is_excluded_from_requested_cut():
    report = build_daily_score_report(pd.DataFrame([observation(), observation(day="2026-10-26", score=99)]), as_of="2026-10-23")
    assert report["rows"][0]["score"] == 88


def test_open_session_is_not_latest_closed_and_intraday_notes_never_certify_close():
    data = pd.DataFrame([observation(day="2026-10-23", score=80), observation(day="2026-10-26", score=99, created="2026-10-26T15:00:00Z")])
    before = build_daily_score_report(data, as_of="2026-10-26", reference_time="2026-10-26T18:00:00Z")
    assert before["as_of"] == "2026-10-23" and before["rows"][0]["score"] == 80
    after = build_daily_score_report(data, as_of="2026-10-26", reference_time="2026-10-26T22:00:00Z")
    assert after["as_of"] == "2026-10-26" and after["rows"][0]["score"] is None
    assert after["rows"][0]["history"][1]["score"] == 80


def test_missing_creation_date_does_not_certify_a_closing_signal():
    item = observation()
    item["created_at"] = None
    row = build_daily_score_report(pd.DataFrame([item]), as_of="2026-10-23")["rows"][0]
    assert row["score"] is None and row["status"] == "review_data"


def test_reference_clock_is_not_hashed_into_report_identity():
    data = pd.DataFrame([observation()])
    first = build_daily_score_report(data, as_of="2026-10-23", reference_time="2026-10-24T02:00:00Z")
    second = build_daily_score_report(data, as_of="2026-10-23", reference_time="2026-10-25T02:00:00Z")
    assert first["report_id"] == second["report_id"]


def test_email_escape_utf8_limit_and_exact_payload_identity():
    records = [observation(f"T{index}") for index in range(10)]
    report = build_daily_score_report(pd.DataFrame(records), company_names={"T0": "Compañía <script>alert('x')</script>"}, as_of="2026-10-23")
    report["alerts"] = [dict(ticker="T0", company_name="Compañía & socios", title="Revisar exposición",
        change_reason="Está débil <script>", next_step="No vender automáticamente", strategy_label="Tesis propia",
        horizon="Mensual", as_of="2026-10-23")]
    report = seal_daily_score_report(report)
    before = deepcopy(report)
    _, text, html = render_daily_score_email(report, app_url="javascript:alert(1)")
    assert report == before and "Compañía" in text and "exposición" in text and "sesiones" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html and "https://stock-signal-lab" in html
    assert html.count("<tr><td>") == 4
    assert report["report_id"] in html and report["report_id"] in text
    assert "CAMBIOS QUE REVISAR" in text
    changed = deepcopy(report)
    changed["alerts"][0]["title"] = "Otra lectura"
    assert seal_daily_score_report(changed)["report_id"] != report["report_id"]


def test_new_ticker_has_gaps_not_look_ahead_reconstructed_scores():
    row = build_daily_score_report(pd.DataFrame(), tickers=["NEW"], as_of="2026-10-23")["rows"][0]
    assert row["score"] is None and row["comparison_status"] == "no_data"
    assert len(row["history"]) == 5 and all(item["score"] is None for item in row["history"])
