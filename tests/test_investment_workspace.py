"""Saved evidence only: no providers, journal, orders or parameter changes."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from src.analysis_policy import build_policy_snapshot, encode_policy_note
from src.daily_digest import seal_daily_score_report
from src.investment_workspace import build_investment_workspace


DAY = "2026-11-04"
RECORDED = "2026-11-04T22:30:00Z"
REFERENCE = date(2026, 11, 5)


def observed(ticker="TEST", *, held=False, horizon=None, thesis=None, invalidated=False,
             score=85, label="Entrada fuerte", technical_score=90, technical_label="Entrada fuerte",
             position_label="Mantener", blocked=False, day=DAY, recorded=RECORDED, strategy_id=None):
    context = {"horizon": horizon or "No documentado", "thesis": thesis,
               "invalidated": invalidated, "invalidation_reason": "La premisa declarada ya no se cumple" if invalidated else None,
               "strategy_id": strategy_id or ("quality_value_thesis_risk" if horizon and "largo" in horizon.casefold() else "growth_momentum")}
    policy = build_policy_snapshot({"Ticker": ticker, "Fecha": day, "Fuente": "Synthetic close",
        "Precio": 100, "Moneda": "USD", "Calidad empresa": 72, "Valoración": 61,
        "Riesgo controlado": 70, "Si ya la tienes": position_label},
        growth_result=SimpleNamespace(score=score, label=label, growth_score=80,
            momentum_score=90, context_score=80, confidence_pct=90, sector_key="technology",
            as_of=pd.Timestamp(day), positive_factors=(), risk_factors=(), manual_checks=()),
        position_context=context, held=held)
    row = {"ticker": ticker, "company_name": ticker + " Company", "held": held, "score": score,
        "label": label, "status": "review_data" if blocked else "entry_strong" if label == "Entrada fuerte" else "wait",
        "quality": {"block_entry": blocked, "status": "review" if blocked else "ok",
                    "reasons": ["Periodo contable pendiente de verificar"] if blocked else []},
        "session": day, "source": "Synthetic close", "recorded_at": recorded,
        "snapshot_id": policy["snapshot_id"], "chart_comment": "El precio mantiene la media intermedia.",
        "next_step": "Confirmar costes y riesgo antes de decidir."}
    snapshot = {"ticker": ticker, "analyzed_at": day, "created_at": recorded, "price": 100,
        "entry_score": technical_score, "entry_label": technical_label,
        "note": encode_policy_note("private human note", policy)}
    return row, snapshot


def report(*rows):
    return seal_daily_score_report({"as_of": DAY, "rows": list(rows)})


def workspace(*pairs, **kwargs):
    return build_investment_workspace(report(*(pair[0] for pair in pairs)),
        snapshots=pd.DataFrame([pair[1] for pair in pairs]), reference_date=REFERENCE, **kwargs)


def test_three_separate_views_preserve_original_scores_without_averaging():
    view = workspace(observed())
    days, weeks, long = (view["horizons"][key] for key in ("days", "weeks", "long_term"))
    assert days["opportunities"][0]["score"] == 90
    assert weeks["opportunities"][0]["score"] == 85
    assert days["opportunities"][0]["score_kind"] == "Momento técnico"
    assert weeks["opportunities"][0]["score_kind"] == "Crecimiento + momentum"
    assert long["opportunities"] == []
    assert long["waiting"][0]["score"] is None
    assert long["waiting"][0]["action"] == "Documentar tesis"
    assert long["opportunity_count_label"] == "N/D"
    assert any("intradía" in text for text in days["limitations"])
    assert view["coverage"]["with_score"] == view["coverage"]["current_with_score"] == 1


def test_growth_and_technical_views_do_not_require_consensus():
    view = workspace(observed(score=35, label="Esperar", technical_score=90))
    assert len(view["horizons"]["days"]["opportunities"]) == 1
    assert view["horizons"]["weeks"]["opportunities"] == []
    blocked = workspace(observed(blocked=True))
    assert len(blocked["horizons"]["days"]["opportunities"]) == 1
    assert blocked["horizons"]["weeks"]["opportunities"] == []
    assert blocked["status"] == "partial"


def test_missing_or_stale_data_is_not_zero_opportunities_or_a_sale():
    pair = observed()
    for reference in (None, date(2026, 12, 1)):
        view = build_investment_workspace(report(pair[0]), snapshots=[pair[1]], reference_date=reference)
        assert view["status"] == "unavailable"
        for horizon in view["horizons"].values():
            assert horizon["opportunities"] == []
            assert horizon["opportunity_count_label"] == "N/D"
    missing = build_investment_workspace(None, holdings=["MINE"], reference_date=REFERENCE)
    assert missing["status"] == "unavailable"
    assert missing["portfolio"][0]["action"] == "Comprobar plan"
    assert missing["unassigned_portfolio"][0]["ticker"] == "MINE"


def test_input_report_and_snapshots_are_immutable_and_archive_uses_exact_observation():
    row, old = observed(score=70, label="Esperar")
    newer_row, newer = observed(score=99, recorded="2026-11-05T01:00:00Z")
    source = report(row)
    records = pd.DataFrame([old, newer])
    original_report, original_records = deepcopy(source), records.copy(deep=True)
    view = build_investment_workspace(source, snapshots=records, reference_date=REFERENCE)
    assert view["horizons"]["weeks"]["waiting"][0]["score"] == 70
    assert view["report_id"] == source["report_id"] == original_report["report_id"]
    assert source == original_report
    pd.testing.assert_frame_equal(records, original_records)
    only_new = build_investment_workspace(source, snapshots=[newer], reference_date=REFERENCE)
    assert only_new["horizons"]["days"]["opportunity_count_label"] == "N/D"


def test_a_matching_identity_alone_cannot_join_a_different_ticker_date_or_recording_time():
    row, original = observed()
    for change in ({"ticker": "OTHER"}, {"analyzed_at": "2026-11-03"}, {"created_at": "2026-11-05T01:00:00Z"}):
        view = build_investment_workspace(report(row), snapshots=[{**original, **change}], reference_date=REFERENCE)
        assert view["horizons"]["days"]["opportunities"] == []
        assert view["horizons"]["days"]["opportunity_count_label"] == "N/D"


def test_position_without_declared_plan_is_not_assigned_by_company_or_score():
    view = workspace(observed("MA", held=True, score=99), observed("PENNY", held=True, score=3))
    assert {row["ticker"] for row in view["unassigned_portfolio"]} == {"MA", "PENNY"}
    assert all(row["action"] == "Definir plan" for row in view["portfolio"])
    assert all(horizon["portfolio"] == [] for horizon in view["horizons"].values())


def test_long_term_thesis_is_not_sold_because_technical_entry_is_weak():
    view = workspace(observed(held=True, horizon="Largo plazo; 3 años", thesis="Negocio y valoración documentados",
        score=10, label="Esperar", technical_score=5, technical_label="Esperar", position_label="Vender"))
    row = view["horizons"]["long_term"]["portfolio"][0]
    assert row["action"] == "Revisar tesis"
    assert row["score"] is None and row["score_kind"] == "Sin nota conjunta"
    assert view["horizons"]["days"]["portfolio"] == []
    assert "técnica" not in row["reason"]


def test_explicit_invalidation_is_reviewed_without_creating_a_sell_order():
    view = workspace(observed("RISK", held=True, horizon="Semanas", thesis="Original", invalidated=True),
                     observed("SAFE", held=True, horizon="Semanas", thesis="Original"))
    assert view["portfolio"][0]["ticker"] == "RISK"
    risk = view["portfolio"][0]
    assert risk["action"] == "Revisar posible salida"
    assert "premisa" in risk["reason"]
    assert "No se envía una orden" in risk["next_step"]
    assert not any(key in risk for key in ("quantity", "order", "side", "execute"))


def test_held_growth_candidate_is_review_for_amplification_not_a_new_buy():
    view = workspace(observed(held=True, horizon="2-8 semanas", thesis="Original"))
    held = view["horizons"]["weeks"]["portfolio"][0]
    assert held["action"] == "Revisar ampliación"
    assert all(horizon["opportunities"] == [] for horizon in view["horizons"].values())


def test_held_missing_or_blocked_strategy_evidence_is_pending_not_keep_or_sell():
    missing = observed(held=True, horizon="Semanas", score=None, label="Esperar")
    blocked = observed(held=True, horizon="Semanas", blocked=True, position_label="Vender")
    for pair in (missing, blocked):
        view = workspace(pair)
        row = view["portfolio"][0]
        assert row["action"] == "Actualizar datos"
        assert row["data_status"] == "pending"
        assert "no son una señal de venta" in row["next_step"]
        assert all(horizon["opportunities"] == [] for horizon in view["horizons"].values())


def test_long_term_candidate_needs_documented_horizon_and_thesis_not_only_quality():
    missing = workspace(observed())
    assert not missing["horizons"]["long_term"]["opportunities"]
    documented = workspace(observed(horizon="Largo plazo; 3 años", thesis="Evidencia explícita"))
    row = documented["horizons"]["long_term"]["opportunities"][0]
    assert row["action"] == "Estudiar tesis"
    assert row["score"] is None
    assert "No se confirma una compra automática" in row["next_step"]


def test_duplicate_company_does_not_choose_the_highest_score():
    high, _ = observed("DUP", score=99)
    low, _ = observed("DUP", score=5, label="Esperar")
    other, snapshot = observed("OTHER")
    view = build_investment_workspace(report(high, low, other), snapshots=[snapshot], reference_date=REFERENCE)
    assert view["duplicates"] == ["DUP"]
    assert view["coverage"]["total"] == 2
    assert {row["ticker"] for row in view["horizons"]["weeks"]["opportunities"]} == {"OTHER"}
    assert view["horizons"]["weeks"]["opportunity_count_label"] == "≥ 1"


@pytest.mark.parametrize("mutate", ["tamper", "future", "no_reference", "no_seal"])
def test_unverifiable_report_fails_closed(mutate):
    row, snapshot = observed()
    source = report(row)
    reference = REFERENCE
    if mutate == "tamper":
        source["rows"][0]["score"] = 100
    elif mutate == "future":
        source = seal_daily_score_report({**source, "as_of": "2026-12-01"})
    elif mutate == "no_reference":
        reference = None
    else:
        source.pop("report_id")
    view = build_investment_workspace(source, snapshots=[snapshot], reference_date=reference)
    assert view["status"] == "unavailable"
    assert all(not horizon["opportunities"] for horizon in view["horizons"].values())


def test_valid_zero_score_is_preserved_and_not_replaced_by_a_missing_value():
    view = workspace(observed(score=0, label="Esperar", technical_score=0, technical_label="Esperar"))
    assert view["horizons"]["weeks"]["waiting"][0]["score"] == 0
    assert view["horizons"]["weeks"]["opportunity_count_label"] == "0"
    assert view["horizons"]["days"]["waiting"][0]["score"] == 0


def test_holdings_accept_a_single_ticker_and_skip_explicitly_closed_positions():
    one = build_investment_workspace(None, holdings="ANET", reference_date=REFERENCE)
    assert [row["ticker"] for row in one["portfolio"]] == ["ANET"]
    frame = pd.DataFrame([{"ticker": "OPEN", "quantity": 1}, {"ticker": "CLOSED", "quantity": 0}])
    view = build_investment_workspace(None, holdings=frame, reference_date=REFERENCE)
    assert [row["ticker"] for row in view["portfolio"]] == ["OPEN"]
    single = build_investment_workspace(None, holdings={"ticker": "SINGLE", "quantity": 1}, reference_date=REFERENCE)
    assert [row["ticker"] for row in single["portfolio"]] == ["SINGLE"]


def test_missing_context_is_not_the_same_as_a_missing_plan():
    row, snapshot = observed(held=True, horizon="Semanas", thesis="Plan existente")
    shallow = build_investment_workspace(report(row), reference_date=REFERENCE)
    assert shallow["portfolio"][0]["action"] == "Comprobar plan"
    assert shallow["portfolio"][0]["plan_status"] == "unverified"
    assert "no incluye" in shallow["portfolio"][0]["reason"]
    complete = build_investment_workspace(report(row), snapshots=[snapshot], reference_date=REFERENCE)
    assert complete["portfolio"][0]["plan_status"] == "documented"
    assert complete["unverified_portfolio"] == []


@pytest.mark.parametrize("recorded",["2026-11-04T20:00:00Z", "2026-11-06T00:00:00Z"])
def test_sealed_but_intraday_or_future_record_does_not_enable_an_entry(recorded):
    row, snapshot = observed(recorded=recorded)
    view = build_investment_workspace(report(row), snapshots=[snapshot], reference_date=REFERENCE)
    assert view["status"] == "unavailable"
    assert not view["horizons"]["days"]["opportunities"]
    assert not view["horizons"]["weeks"]["opportunities"]


def test_aware_review_clock_rejects_a_later_record_on_the_same_day():
    row, snapshot = observed(recorded="2026-11-04T22:30:00Z")
    view = build_investment_workspace(report(row), snapshots=[snapshot],
        reference_date=pd.Timestamp("2026-11-04T22:00:00Z"))
    assert view["status"] == "unavailable"


def test_snapshot_legacy_naive_utc_time_matches_report_builder_convention():
    row, snapshot = observed()
    snapshot["created_at"] = "2026-11-04T22:30:00"
    view = build_investment_workspace(report(row), snapshots=[snapshot], reference_date=REFERENCE)
    assert len(view["horizons"]["days"]["opportunities"]) == 1


def test_auxiliary_technical_exit_does_not_become_a_growth_or_quality_exit():
    growth = workspace(observed(held=True, horizon="Semanas", thesis="Plan", position_label="Vender"))
    row = growth["portfolio"][0]
    assert row["action"] == "Contrastar señal técnica"
    assert "auxiliar" in row["reason"] and row["score"] == 85
    quality = workspace(observed(held=True, horizon="Semanas", thesis="Plan", position_label="Vender",
        strategy_id="quality_value_thesis_risk"))
    assert quality["portfolio"][0]["action"] == "Revisar tesis"
    assert quality["portfolio"][0]["score"] is None
    technical = workspace(observed(held=True, horizon="Días", thesis="Plan", position_label="Vender",
        strategy_id="technical"))
    assert technical["portfolio"][0]["action"] == "Revisar exposición"


def test_technical_position_does_not_amplify_from_the_growth_signal():
    view = workspace(observed(held=True, horizon="Días", technical_score=20, technical_label="Esperar",
        strategy_id="technical"))
    row = view["portfolio"][0]
    assert row["action"] == "Seguir plan"
    assert row["score_kind"] == "Momento técnico" and row["score"] == 20


def test_research_list_is_not_a_buy_count_or_a_new_combined_quality_score():
    view = workspace(observed("ZZZ"), observed("AAA"))
    long = view["horizons"]["long_term"]
    assert long["opportunities"] == [] and long["opportunity_count_label"] == "N/D"
    assert [row["ticker"] for row in long["research"]] == ["AAA", "ZZZ"]
    assert long["counts"]["research"] == 2
    assert all(row["score"] is None and row["action"] == "Estudiar empresa" for row in long["research"])
    assert all(row["quality_score"] == 72 and row["valuation_score"] == 61 for row in long["research"])
