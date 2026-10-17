"""Synthetic evidence only: proposed manual reviews, never trading actions."""

from copy import deepcopy
import json

import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from src.analysis_policy import POSITION_PLAN_MARKER, build_policy_snapshot
from src.position_review import build_position_review_rows


TODAY = "2026-10-15"


def position(ticker="AAA", value=100, platform="Broker", sector="Sector A", **extra):
    return {"analysis_ticker": ticker, "asset_name": ticker + " Company", "platform": platform,
            "quantity": 10, "value_eur": value, "currency": "EUR", "liquidity_class": "liquid",
            "value_complete": True, "valuation_status": "Precio actualizado", "market_as_of": TODAY,
            "sector": sector, **extra}


def diversified(first=None):
    first = first or position()
    return pd.DataFrame([first, *[position(f"OTHER{i}", sector=f"Sector {i}") for i in range(9)]])


def summary(ticker="AAA", *, candidate=True, context=None, **extra):
    row = {"Ticker": ticker, "Fecha": TODAY, "Cierre": 10, "Moneda": "EUR",
           "Calidad empresa": 75, "Valoración": 70, "Riesgo controlado": 65,
           "Si ya la tienes": "Mantener", **extra}
    policy = build_policy_snapshot(row, growth_result={
        "score": 80, "growth_score": 80, "momentum_score": 80, "context_score": 80,
        "confidence_pct": 85, "label": "Entrada candidata" if candidate else "Esperar mejor precio",
        "as_of": TODAY,
    }, position_context=context or {"strategy": "Largo plazo", "horizon": "5 años", "thesis": "Ventaja competitiva sostenible y caja recurrente"}, held=True)
    return {**row, "Política de análisis": policy}


def prices(day=TODAY, close=10, **extra):
    return pd.DataFrame([{"close": close, "sma_short": 9.5, "sma_medium": 9, **extra}], index=pd.to_datetime([day]))


def review(positions=None, source=None, prepared=None, **kwargs):
    result = build_position_review_rows(positions if positions is not None else diversified(), [source or summary()], prepared if prepared is not None else {"AAA": prices()}, reference_date=TODAY, **kwargs)
    return result.loc[result.ticker == "AAA"].iloc[0]


def test_current_candidate_can_only_propose_manual_add_review():
    row = review()
    assert row.action == "Revisar ampliación"
    assert row.manual_review_only
    assert row.strategy == "Largo plazo"
    assert row.horizon == "5 años"
    assert row.company_weight_pct == 10
    assert row.capacity_eur == 50
    assert row.entry_zone_lower == 9
    assert row.entry_zone_upper == 9.5
    assert "no tiempo real" in row.price_label
    assert "no optimización" in row.risk_limitations


def test_bad_entry_is_not_a_sell_or_strategy_reassignment():
    row = review(source=summary(candidate=False, **{"Si ya la tienes": "Vender"}))
    assert row.action == "Mantener"
    assert row.strategy == "Largo plazo"
    assert row.horizon == "5 años"
    assert row.entry_zone is None
    assert "no obliga a vender" in row.reason


@pytest.mark.parametrize("day", ["2026-09-01", "2026-10-16"])
def test_stale_or_future_quotes_do_not_trigger_sales_or_new_entries(day):
    row = review(prepared={"AAA": prices(day)})
    assert row.action == "Completar pendientes"
    assert row.entry_zone is None
    assert "no inferir venta" in row.risk_limitations


def test_missing_quote_does_not_fallback_to_snapshot_as_real_time():
    row = review(prepared={})
    assert row.action == "Completar pendientes"
    assert pd.isna(row.latest_close)
    assert pd.isna(row.quote_date)


def test_explicit_thesis_invalidation_is_manual_review_independent_of_staleness():
    ctx = {"strategy": "Largo plazo", "horizon": "5 años", "thesis": "Caja recurrente",
           "invalidated": True, "invalidation_reason": "Pérdida permanente de la licencia principal"}
    row = review(source=summary(context=ctx), prepared={})
    assert row.action == "Estudiar venta"
    assert "licencia principal" in row.reason
    assert row.manual_review_only


def test_manual_thesis_marker_on_position_is_read_without_app_helpers():
    held = position(comments="[[SSL_TESIS_INVALIDADA]] Guía suspendida y caja comprometida")
    row = review(positions=diversified(held))
    assert row.action == "Estudiar venta"
    assert "Guía suspendida" in row.reason


def test_string_false_invalidation_is_not_a_sale():
    source = summary()
    source["Política de análisis"]["position"]["invalidated"] = "False"
    row = review(source=source)
    assert row.action == "Revisar ampliación"


def test_overconcentration_can_be_reviewed_with_stale_analysis_not_called_thesis_break():
    held = diversified(position(value=900, valuation_status="Valoración declarada", market_as_of="2026-09-01"))
    row = review(positions=held, prepared={})
    assert row.action == "Reducir exposición"
    assert row.company_weight_pct == 50
    assert not row.weights_current
    assert pd.isna(row.capacity_eur)
    assert "no es una ruptura de tesis" in row.reason


def test_company_limit_is_global_but_broker_rows_and_capacity_are_not_duplicated():
    held = diversified(position(value=40, platform="A"))
    held = pd.concat([held, pd.DataFrame([position(value=60, platform="B")])], ignore_index=True)
    report = build_position_review_rows(held, [summary()], {"AAA": prices()}, reference_date=TODAY)
    rows = report.loc[report.ticker == "AAA"]
    assert len(rows) == 2
    assert set(rows.platform) == {"A", "B"}
    assert set(rows.company_weight_pct) == {10}
    assert rows.capacity_eur.notna().sum() == 1
    assert rows.capacity_eur.sum() == 50
    assert all("compartido" in text for text in rows.capacity_scope)


def test_company_overweight_combines_two_brokers():
    held = pd.DataFrame([position(value=90, platform="A"), position(value=90, platform="B"), position("BBB", value=820, sector="Other")])
    report = build_position_review_rows(held, [summary()], {"AAA": prices()}, reference_date=TODAY, max_sector_weight_pct=100)
    rows = report.loc[report.ticker == "AAA"]
    assert set(rows.company_weight_pct) == {18}
    assert set(rows.action) == {"Reducir exposición"}
    assert rows.capacity_eur.sum() == 0


def test_individual_manual_plans_are_not_overwritten_by_consolidated_contract():
    plans = [{"strategy": "Largo plazo", "horizon": "5 años", "thesis": "Negocio recurrente sostenible"}, {"strategy": "growth_momentum", "horizon": "6 meses", "thesis": "Ingresos en aceleración fuerte"}]
    held = diversified(position(value=40, platform="A", comments=POSITION_PLAN_MARKER + " " + json.dumps(plans[0])))
    held = pd.concat([held, pd.DataFrame([position(value=60, platform="B", comments=POSITION_PLAN_MARKER + " " + json.dumps(plans[1]))])], ignore_index=True)
    result = build_position_review_rows(held, [summary()], {"AAA": prices()}, reference_date=TODAY)
    rows = result.loc[result.ticker == "AAA"].set_index("platform")
    assert rows.loc["A", "horizon"] == "5 años"
    assert rows.loc["B", "horizon"] == "6 meses"
    assert rows.loc["B", "strategy_id"] == "growth_momentum"


def test_missing_sector_makes_full_capacity_unknown_and_company_margin_conditional():
    held = diversified(position(sector=""))
    row = review(positions=held)
    assert pd.isna(row.capacity_eur)
    assert row.company_capacity_eur == 50
    assert pd.isna(row.sector_weight_pct)
    assert "sector sin comprobar" in row.capacity_scope
    assert row.action == "Completar pendientes"


def test_missing_sector_on_another_asset_also_prevents_false_compliance():
    held = diversified()
    held.loc[1, "sector"] = ""
    row = review(positions=held)
    assert pd.isna(row.capacity_eur)
    assert "Cobertura sectorial incompleta" in row.risk_limitations


def test_missing_value_on_untickered_liquid_asset_invalidates_denominator():
    held = pd.concat([diversified(), pd.DataFrame([position("", None, asset_name="Fondo ISIN sin cantidad")])], ignore_index=True)
    row = review(positions=held)
    assert pd.isna(row.company_weight_pct)
    assert pd.isna(row.capacity_eur)
    assert not row.weights_current


def test_cash_and_alternatives_do_not_dilute_company_weights():
    held = pd.concat([diversified(), pd.DataFrame([
        position("", 10000, platform="Segofactoring", liquidity_class="illiquid"),
        position("", 10000, platform="Broker", liquidity_class="cash"),
    ])], ignore_index=True)
    row = review(positions=held)
    assert row.company_weight_pct == 10
    assert row.capacity_eur == 50


def test_no_versioned_policy_does_not_replace_scores_with_legacy_signal():
    row = review(source={"Ticker": "AAA", "Si ya la tienes": "Vender", "Oportunidad": 100, "Nota principal": 100})
    assert row.action == "Completar pendientes"
    assert row.entry_zone is None
    assert "versionada" in row.risk_limitations


def test_undocumented_contract_is_not_inferred_from_candidate_score():
    source = summary()
    source["Política de análisis"]["position"].update(strategy_id="previous_thesis", strategy_label="No documentada", horizon="No documentado", thesis=None)
    row = review(source=source)
    assert row.action == "Completar pendientes"
    assert row.strategy_id == "previous_thesis"
    assert row.entry_zone is None


def test_current_primary_candidate_without_existing_technical_zone_stays_nd():
    row = review(prepared={"AAA": prices(sma_short=None, sma_medium=None)})
    assert row.action == "Revisar ampliación"
    assert row.entry_zone is None
    assert "no hay medias" in row.entry_zone_reason


def test_comparative_notes_remain_independent_not_a_consensus_or_score_rewrite():
    source = summary(**{"Calidad empresa": 10, "Valoración": 90, "Riesgo controlado": 15})
    policy = deepcopy(source["Política de análisis"])
    row = review(source=source)
    assert row.action == "Revisar ampliación"
    assert row.comparative_reading == policy["comparative"]["reading"]
    assert "por separado" in row.reason
    assert source["Política de análisis"] == policy


def test_priority_only_sorts_owned_names_and_never_adds_asml():
    held = pd.DataFrame([position("ZZZ"), position("DHR"), position("ASTS"), position("XE")])
    result = build_position_review_rows(held, [], {}, reference_date=TODAY, max_company_weight_pct=100, max_sector_weight_pct=100)
    assert list(result.ticker) == ["ASTS", "XE", "DHR", "ZZZ"]
    assert "ASML" not in set(result.ticker)


def test_inputs_are_not_mutated_and_empty_contract_has_stable_schema():
    held, sources, frames = diversified(), [summary()], {"AAA": prices()}
    original_held, original_sources, original_frame = held.copy(deep=True), deepcopy(sources), frames["AAA"].copy(deep=True)
    build_position_review_rows(held, sources, frames, reference_date=TODAY)
    assert_frame_equal(held, original_held)
    assert sources == original_sources
    assert_frame_equal(frames["AAA"], original_frame)
    empty = build_position_review_rows(pd.DataFrame(), [], {})
    assert empty.empty
    assert "capacity_scope" in empty.columns


def test_sector_limit_can_constrain_capacity_independently_of_company_cap():
    held = diversified()
    held.loc[1, "sector"] = "Sector A"
    row = review(positions=held, max_sector_weight_pct=22)
    assert row.company_weight_pct == 10
    assert row.sector_weight_pct == 20
    assert row.company_capacity_eur == 50
    assert row.capacity_eur == 20


def test_sector_overconcentration_proposes_manual_exposure_review():
    held = diversified()
    held.loc[1:4, "sector"] = "Sector A"
    row = review(positions=held)
    assert row.company_weight_pct == 10
    assert row.sector_weight_pct == 50
    assert row.action == "Reducir exposición"
    assert row.capacity_eur == 0


def test_fresh_quote_cannot_refresh_stale_primary_analysis():
    source = summary()
    source["Política de análisis"]["primary"]["as_of"] = "2026-09-01"
    row = review(source=source)
    assert row.action == "Completar pendientes"
    assert row.entry_zone is None
    assert "análisis principal" in row.risk_limitations


def test_partial_primary_entry_label_cannot_enable_add_or_entry_zone():
    source = summary()
    source["Política de análisis"]["primary"].update(data_status="partial", entry_candidate=True)
    row = review(source=source)
    assert row.action == "Completar pendientes"
    assert row.entry_zone is None


def test_partial_comparative_data_keeps_manual_review_pending_without_score_blend():
    source = summary()
    source["Política de análisis"]["comparative"]["data_status"] = "partial"
    row = review(source=source)
    assert row.action == "Completar pendientes"
    assert "consensus_score" not in row.index


def test_corrupt_newest_bar_does_not_fallback_to_old_valid_close():
    frame = pd.concat([prices("2026-10-14"), prices(TODAY, close=float("nan"))])
    row = review(prepared={"AAA": frame})
    assert row.action == "Completar pendientes"
    assert pd.isna(row.latest_close)


def test_numeric_but_explicitly_incomplete_valuation_invalidates_current_weights():
    held = diversified()
    held.loc[1, "value_complete"] = False
    row = review(positions=held)
    assert row.action == "Completar pendientes"
    assert pd.isna(row.company_weight_pct)
    assert pd.isna(row.capacity_eur)


def test_unrelated_or_unsupported_policy_is_not_used_for_a_stock():
    for change in ({"ticker": "DIFFERENT"}, {"policy_version": "future_incompatible_v99"}):
        source = summary()
        source["Política de análisis"].update(change)
        row = review(source=source)
        assert row.action == "Completar pendientes"
        assert pd.isna(row.policy_version)


def test_capacity_published_on_first_displayed_broker_even_if_input_reversed():
    held = diversified(position(value=40, platform="Z"))
    held = pd.concat([held, pd.DataFrame([position(value=60, platform="A")])], ignore_index=True)
    report = build_position_review_rows(held, [summary()], {"AAA": prices()}, reference_date=TODAY)
    rows = report.loc[report.ticker == "AAA"].reset_index(drop=True)
    assert rows.iloc[0].platform == "A"
    assert rows.iloc[0].capacity_eur == 50
    assert pd.isna(rows.iloc[1].capacity_eur)


@pytest.mark.parametrize("limit", [0, -1, 101, float("nan"), float("inf"), True])
def test_invalid_proposed_caps_are_rejected(limit):
    with pytest.raises(ValueError):
        review(max_company_weight_pct=limit)
