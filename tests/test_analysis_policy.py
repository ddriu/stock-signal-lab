"""La política transforma evidencia existente sin proveedores ni persistencia."""

from copy import deepcopy
from datetime import date, datetime
import json
from types import SimpleNamespace

import pytest

from src.analysis_policy import (
    COMPARATIVE_STRATEGY_ID,
    POLICY_NOTE_MARKER,
    POLICY_VERSION,
    POSITION_PLAN_MARKER,
    PRIMARY_STRATEGY_ID,
    build_policy_snapshot,
    decode_policy_note,
    encode_policy_note,
    enrich_analysis_rows,
    position_contexts_from_snapshot,
)


def _row(**changes):
    return {
        "Ticker": "TEST", "Oportunidad": 99, "Momento entrada": 95,
        "Calidad empresa": 78, "Valoración": 72, "Riesgo controlado": 76,
        "Cobertura fundamental": 85, "Lectura entrada": "Entrada fuerte",
        "Si ya la tienes": "Mantener", "Fecha": date(2026, 9, 25),
        "Fuente": "datos guardados", **changes,
    }


def _growth(**changes):
    return SimpleNamespace(
        **{
            "score": 83, "growth_score": 90, "momentum_score": 82,
            "context_score": 65, "confidence_pct": 88,
            "label": "Entrada fuerte", "sector_key": "technology",
            "as_of": date(2026, 9, 25), **changes,
        }
    )


def test_enrichment_is_copy_only_and_preserves_order_and_legacy_readings():
    rows = [_row(), _row(Ticker="OTHER", **{"Oportunidad": 13})]
    original = deepcopy(rows)
    enriched = enrich_analysis_rows(rows, growth_results={" test ": _growth()})
    assert rows == original
    assert [row["Ticker"] for row in enriched] == ["TEST", "OTHER"]
    assert enriched[0] is not rows[0]
    assert enriched[0]["Oportunidad"] == 99
    assert enriched[0]["Lectura entrada"] == "Entrada fuerte"
    assert enriched[0]["Si ya la tienes"] == "Mantener"
    assert enriched[0]["Nota principal"] == 83
    assert enriched[1]["Nota principal"] is None


def test_two_approaches_are_not_averaged_or_presented_as_consensus():
    snapshot = build_policy_snapshot(_row(**{"Calidad empresa": 20, "Valoración": 25}), growth_result=_growth())
    assert snapshot["primary"]["score"] == 83
    assert snapshot["primary"]["strategy_id"] == PRIMARY_STRATEGY_ID
    assert snapshot["comparative"]["strategy_id"] == COMPARATIVE_STRATEGY_ID
    assert snapshot["comparative"]["quality_score"] == 20
    assert snapshot["comparative"]["valuation_score"] == 25
    assert "score" not in snapshot["comparative"]
    assert snapshot["primary"]["entry_candidate"]
    assert snapshot["disagreement"]["status"] == "different_readings"


def test_balanced_legacy_signal_never_substitutes_for_missing_growth():
    snapshot = build_policy_snapshot(_row())
    assert snapshot["primary"]["score"] is None
    assert snapshot["primary"]["data_status"] == "pending"
    assert not snapshot["primary"]["entry_candidate"]
    assert "Completar datos" in snapshot["primary"]["entry_reading"]


@pytest.mark.parametrize("label", [
    "Entrada fuerte", "Pendiente de fundamentales", "Datos empresariales parciales",
    "Datos empresariales insuficientes", "Datos insuficientes",
])
def test_missing_growth_cannot_enable_new_entry(label):
    snapshot = build_policy_snapshot(_row(), growth_result=_growth(growth_score=None, label=label))
    assert not snapshot["primary"]["entry_candidate"]
    assert snapshot["primary"]["data_status"] == "partial"
    assert snapshot["primary"]["entry_reading"].startswith("Completar datos")


def test_declared_partial_data_stays_partial_despite_available_scores():
    snapshot = build_policy_snapshot(_row(), growth_result=_growth(label="Datos empresariales parciales"))
    assert snapshot["primary"]["score"] == 83
    assert not snapshot["primary"]["entry_candidate"]


def test_fund_has_no_company_growth_but_may_have_a_valid_momentum_reading():
    snapshot = build_policy_snapshot(_row(), growth_result=_growth(growth_score=None, sector_key="etf"))
    assert snapshot["primary"]["growth_score"] is None
    assert snapshot["primary"]["data_status"] == "complete"
    assert snapshot["primary"]["entry_candidate"]


def test_zero_is_evidence_not_missing_and_json_has_no_nonfinite_values():
    snapshot = build_policy_snapshot(
        _row(**{"Calidad empresa": 0, "Valoración": 0, "Riesgo controlado": 0}),
        growth_result=_growth(score=0, growth_score=0, momentum_score=0, context_score=0, label="No preparada"),
        config={"reference": "SPY", "disabled_threshold": 0, "not_known": float("nan")},
    )
    assert snapshot["primary"]["score"] == 0
    assert snapshot["comparative"]["quality_score"] == 0
    assert snapshot["comparative"]["data_status"] == "complete"
    assert snapshot["config"]["disabled_threshold"] == 0
    assert snapshot["config"]["not_known"] is None
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), -1, 101, True])
def test_nonfinite_invalid_and_boolean_scores_are_missing(missing):
    snapshot = build_policy_snapshot(
        _row(**{"Calidad empresa": missing, "Valoración": missing, "Riesgo controlado": missing}),
        growth_result=_growth(growth_score=missing),
    )
    assert snapshot["primary"]["growth_score"] is None
    assert snapshot["comparative"]["quality_score"] is None
    assert snapshot["comparative"]["valuation_score"] is None
    assert snapshot["comparative"]["risk_score"] is None
    json.dumps(snapshot, allow_nan=False)


def test_long_term_contract_and_prior_thesis_are_not_rewritten_by_bad_entry():
    context = {"strategy": "Largo plazo", "horizon": "5–10 años", "thesis": "Ventaja competitiva y caja recurrente"}
    original = deepcopy(context)
    snapshot = build_policy_snapshot(
        _row(**{"Si ya la tienes": "Vender"}), growth_result=_growth(label="No preparada"),
        position_context=context, held=True,
    )
    position = snapshot["position"]
    assert context == original
    assert position["strategy_id"] == COMPARATIVE_STRATEGY_ID
    assert position["horizon"] == "5–10 años"
    assert position["thesis"] == context["thesis"]
    assert not position["invalidated"]
    assert "ruptura de tesis" in position["management_reading"]
    assert not position["management_reading"].startswith("Vender")


def test_held_without_documented_strategy_is_not_reassigned_to_growth():
    snapshot = build_policy_snapshot(_row(), growth_result=_growth(), held=True)
    assert snapshot["position"]["strategy_id"] == "previous_thesis"
    assert snapshot["position"]["horizon"] == "No documentado"
    assert snapshot["position"]["thesis_status"] == "not_documented"
    assert "no autoriza ampliar" in snapshot["position"]["management_reading"]


def test_missing_all_analysis_data_does_not_mean_sell():
    snapshot = build_policy_snapshot({"Ticker": "TEST", "Si ya la tienes": "Vender"}, held=True)
    assert snapshot["position"]["management_reading"] == "Actualizar datos; no inferir una venta ni ampliar"
    assert not snapshot["position"]["invalidated"]


def test_explicit_invalidation_is_independent_of_price_and_missing_analysis():
    snapshot = build_policy_snapshot(
        {"Ticker": "TEST", "Tesis invalidada": "Sí", "Motivo tesis": "Pérdida permanente del cliente principal"}, held=True,
    )
    assert snapshot["position"]["invalidated"]
    assert snapshot["position"]["invalidation_reason"] == "Pérdida permanente del cliente principal"
    assert snapshot["position"]["management_reading"].startswith("Revisar posible salida")


def test_string_false_invalidation_is_not_truthy():
    snapshot = build_policy_snapshot(_row(**{"Tesis invalidada": "False"}), held=True)
    assert not snapshot["position"]["invalidated"]


def test_seasonal_calendar_hypothesis_never_changes_scores_or_enables_entry():
    baseline = build_policy_snapshot(_row(), growth_result=_growth(label="No preparada"))
    seasonal = build_policy_snapshot(
        _row(), growth_result=_growth(label="No preparada"),
        cycle_context={"seasonality": "Comprar en octubre", "seasonality_validated": True},
    )
    assert seasonal["primary"] == baseline["primary"]
    assert seasonal["comparative"] == baseline["comparative"]
    assert seasonal["context"]["seasonality_status"] == "unvalidated_hypothesis"
    assert "no validada" in seasonal["context"]["seasonality"]
    assert not seasonal["context"]["creates_entry_signal"]


def test_documented_seasonality_remains_context_not_a_buy_rule():
    snapshot = build_policy_snapshot(_row(), cycle_context={
        "seasonality": "Demanda concentrada en el cuarto trimestre", "seasonality_validated": True,
        "seasonality_evidence": "Comparativa de cinco ejercicios ya aportada", "source": "Informe guardado",
    })
    assert snapshot["context"]["seasonality_status"] == "validated_context"
    assert not snapshot["primary"]["entry_candidate"]
    assert not snapshot["context"]["affects_scores"]


def test_cyclical_sector_does_not_assert_cycle_phase_or_seasonality():
    snapshot = build_policy_snapshot(_row(Sector="Energy"))
    assert snapshot["context"]["cycle_status"] == "sector_context"
    assert snapshot["context"]["cycle_evidence"] is None
    assert snapshot["context"]["seasonality_status"] == "not_documented"


def test_cycle_phase_without_evidence_is_explicitly_a_hypothesis():
    snapshot = build_policy_snapshot(_row(), cycle_context={"cycle": "Expansión"})
    assert snapshot["context"]["cycle_status"] == "hypothesis"
    assert snapshot["context"]["cycle"].startswith("Hipótesis cíclica no validada")


def test_fundamental_filter_is_separate_and_not_a_replacement_quality_score():
    snapshot = build_policy_snapshot(_row(**{"Calidad empresa": None}), fundamental_filter={"score": 100, "coverage_pct": 35})
    assert snapshot["comparative"]["quality_score"] is None
    assert snapshot["comparative"]["filter_score"] == 100
    assert snapshot["comparative"]["filter_coverage_pct"] == 35
    assert snapshot["comparative"]["data_status"] == "partial"


def test_position_contexts_accept_existing_manual_plan_and_thesis_marker():
    plan = {"strategy": "growth_momentum", "horizon": "3–9 meses", "thesis": "Ingresos acelerándose"}
    rows = [{
        "analysis_ticker": " test ",
        "comments": f"Comentario personal\n{POSITION_PLAN_MARKER} {json.dumps(plan)}\n[[SSL_TESIS_INVALIDADA]] La guía ha cambiado",
    }]
    contexts = position_contexts_from_snapshot(rows)
    assert contexts["TEST"]["strategy_id"] == PRIMARY_STRATEGY_ID
    assert contexts["TEST"]["strategy_label"] == "Crecimiento + momentum"
    assert contexts["TEST"]["horizon"] == plan["horizon"]
    assert contexts["TEST"]["thesis"] == plan["thesis"]
    assert contexts["TEST"]["prior_comments"] == "Comentario personal"
    assert contexts["TEST"]["invalidated"]
    assert contexts["TEST"]["invalidation_reason"] == "La guía ha cambiado"
    enriched = enrich_analysis_rows([_row()], position_contexts=contexts)
    assert enriched[0]["Horizonte posición"] == plan["horizon"]
    assert "salida" in enriched[0]["Gestión posición"]


def test_freeform_comments_are_preserved_without_guessing_horizon():
    context = position_contexts_from_snapshot([{"analysis_ticker": "TEST", "comments": "Mi tesis original, sin fecha fija"}])["TEST"]
    assert context["thesis"] == "Mi tesis original, sin fecha fija"
    assert context["horizon"] == "No documentado"
    assert context["strategy_id"] == "previous_thesis"


def test_existing_position_metadata_can_be_reused_without_losing_prior_comments():
    context = position_contexts_from_snapshot([{
        "analysis_ticker": "TEST", "comments": "Mi tesis y comentario previos",
    }])["TEST"]
    first = build_policy_snapshot(_row(), growth_result=_growth(), position_context=context)
    reused = build_policy_snapshot(_row(), growth_result=_growth(), position_context=first["position"])
    assert first["position"] == reused["position"]
    assert first["snapshot_id"] == reused["snapshot_id"]


def test_conflicting_lot_contracts_are_not_silently_overwritten():
    contexts = position_contexts_from_snapshot([
        {"analysis_ticker": "TEST", "strategy": "Largo plazo", "horizon": "5 años", "thesis": "Caja"},
        {"analysis_ticker": "TEST", "strategy": "Crecimiento y momentum", "horizon": "6 meses", "thesis": "Aceleración"},
    ])
    assert contexts["TEST"]["strategy_id"] == "mixed"
    assert contexts["TEST"]["horizon"] == "Horizontes distintos / revisar por posición"
    assert "Caja" in contexts["TEST"]["thesis"] and "Aceleración" in contexts["TEST"]["thesis"]
    assert len(contexts["TEST"]["contracts"]) == 2
    assert contexts["TEST"]["contracts"][0]["horizon"] == "5 años"
    assert contexts["TEST"]["contracts"][1]["horizon"] == "6 meses"


@pytest.mark.parametrize("alias,canonical", [("CEBS", "CEBS.DE"), ("6VO", "RDDT")])
def test_position_plan_and_invalidation_follow_the_canonical_analysis_symbol(alias, canonical):
    plan = {"strategy": "Largo plazo", "horizon": "5 años", "thesis": "Mantener mientras la tesis del fondo siga vigente"}
    contexts = position_contexts_from_snapshot([{
        "analysis_ticker": f" {alias.lower()} ",
        "comments": f"Plan original\n{POSITION_PLAN_MARKER} {json.dumps(plan)}\n[[SSL_TESIS_INVALIDADA]] Cambio estructural confirmado",
    }])
    assert list(contexts) == [canonical]
    assert contexts[canonical]["strategy_id"] == COMPARATIVE_STRATEGY_ID
    assert contexts[canonical]["horizon"] == "5 años"
    enriched = enrich_analysis_rows(
        [_row(Ticker=canonical)], growth_results={alias: _growth()},
        held_tickers=[alias], position_contexts=contexts,
    )[0]
    position = enriched["Política de análisis"]["position"]
    assert enriched["Nota principal"] == 83
    assert position["held"]
    assert position["invalidated"]
    assert position["invalidation_reason"] == "Cambio estructural confirmado"
    assert position["horizon"] == "5 años"
    assert "Revisar posible salida" in enriched["Gestión posición"]


@pytest.mark.parametrize("alias,canonical", [("CEBS", "CEBS.DE"), ("6VO", "RDDT")])
@pytest.mark.parametrize("invalidated_first", [True, False])
def test_alias_lots_merge_conflicts_and_keep_thesis_invalidation(alias, canonical, invalidated_first):
    rows = [
        {"analysis_ticker": alias, "strategy": "Largo plazo", "horizon": "5 años",
         "thesis": "Tesis original de caja", "comments": "[[SSL_TESIS_INVALIDADA]] Guía reducida de forma permanente"},
        {"analysis_ticker": canonical, "strategy": "Crecimiento y momentum", "horizon": "6 meses",
         "thesis": "Tesis de aceleración", "comments": "Segundo lote independiente"},
    ]
    if not invalidated_first:
        rows.reverse()
    original = deepcopy(rows)
    contexts = position_contexts_from_snapshot(rows)
    assert rows == original
    assert list(contexts) == [canonical]
    position = contexts[canonical]
    assert position["strategy_id"] == "mixed"
    assert position["horizon"] == "Horizontes distintos / revisar por posición"
    assert position["invalidated"]
    assert position["invalidation_reason"] == "Guía reducida de forma permanente"
    assert "Tesis original de caja" in position["thesis"]
    assert "Tesis de aceleración" in position["thesis"]
    assert len(position["contracts"]) == 2


def test_aliases_canonicalize_every_lookup_without_rewriting_the_display_row():
    row = _row(Ticker="6VO")
    enriched = enrich_analysis_rows(
        [row], growth_results={"RDDT": _growth()},
        fundamental_filters={"reddit": {"score": 80, "coverage_pct": 90}},
        held_tickers=["RDDT"],
        position_contexts={"reddit": {"strategy": "Largo plazo", "horizon": "5 años", "thesis": "Tesis registrada"}},
        cycle_contexts={"RDDT": {"cycle": "Expansión"}},
    )[0]
    assert row["Ticker"] == enriched["Ticker"] == "6VO"
    assert enriched["Política de análisis"]["ticker"] == "RDDT"
    assert enriched["Nota principal"] == 83
    assert enriched["Política de análisis"]["comparative"]["filter_score"] == 80
    assert enriched["Política de análisis"]["position"]["held"]
    assert enriched["Horizonte posición"] == "5 años"
    assert enriched["Contexto cíclico"].endswith("Expansión")


@pytest.mark.parametrize("alias,canonical", [("CEBS", "CEBS.DE"), ("6VO", "RDDT"), ("service   now", "NOW")])
def test_alias_and_canonical_symbol_share_a_policy_snapshot_identity(alias, canonical):
    by_alias = build_policy_snapshot(_row(Ticker=alias), growth_result=_growth())
    by_canonical = build_policy_snapshot(_row(Ticker=canonical), growth_result=_growth())
    assert by_alias["ticker"] == by_canonical["ticker"] == canonical
    assert by_alias["snapshot_id"] == by_canonical["snapshot_id"]


def test_policy_note_roundtrip_keeps_human_text_and_only_one_marker():
    snapshot = build_policy_snapshot(_row(), growth_result=_growth())
    note = encode_policy_note("Mi seguimiento personal", snapshot)
    assert note.startswith("Mi seguimiento personal\n")
    assert decode_policy_note(note) == snapshot
    assert encode_policy_note(note, snapshot) == note
    assert note.count(POLICY_NOTE_MARKER) == 1
    assert snapshot["policy_version"] == POLICY_VERSION
    assert snapshot["as_of"] == "2026-09-25"
    assert snapshot["source"] == "datos guardados"


def test_snapshot_id_is_stable_and_changes_when_the_contract_changes():
    first = build_policy_snapshot(_row(), growth_result=_growth(), config={"benchmark": "SPY", "profile": "base"})
    reordered = build_policy_snapshot(_row(), growth_result=_growth(), config={"profile": "base", "benchmark": "SPY"})
    changed = build_policy_snapshot(_row(), growth_result=_growth(), config={"profile": "growth", "benchmark": "SPY"})
    assert first["snapshot_id"] == reordered["snapshot_id"]
    assert first["snapshot_id"] != changed["snapshot_id"]


def test_corrected_price_changes_snapshot_identity_without_changing_scores():
    first = build_policy_snapshot(_row(Precio=100.0, Moneda="EUR"), growth_result=_growth(price=100.0))
    corrected = build_policy_snapshot(_row(Precio=101.0, Moneda="EUR"), growth_result=_growth(price=100.0))
    assert first["price"] == 100
    assert corrected["price"] == 101
    assert first["currency"] == corrected["currency"] == "EUR"
    assert first["primary"] == corrected["primary"]
    assert first["as_of"] == corrected["as_of"]
    assert first["snapshot_id"] != corrected["snapshot_id"]


@pytest.mark.parametrize("fields", [{"Precio": 0}, {"price": 0}, {"Cierre": 0}])
def test_price_zero_is_preserved_for_all_existing_row_aliases(fields):
    snapshot = build_policy_snapshot(_row(**fields), growth_result=_growth(price=110))
    assert snapshot["price"] == 0


def test_price_falls_back_to_result_when_rows_have_no_finite_price():
    snapshot = build_policy_snapshot(_row(Precio=float("nan"), price=True, Cierre=None), growth_result=_growth(price=110))
    assert snapshot["price"] == 110
    assert build_policy_snapshot(_row())["price"] is None
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("fields", [{"Moneda": " eur "}, {"Currency": "eur"}, {"currency": "EUR"}])
def test_currency_accepts_app_runner_and_snapshot_names(fields):
    assert build_policy_snapshot(_row(**fields))["currency"] == "EUR"


def test_daily_identity_matches_iso_date_and_midnight_timestamp():
    as_date = build_policy_snapshot(_row(), growth_result=_growth())
    as_midnight = build_policy_snapshot(
        _row(Fecha="2026-09-25T00:00:00Z"),
        growth_result=_growth(as_of=datetime(2026, 9, 25)),
    )
    assert as_date["as_of"] == as_midnight["as_of"] == "2026-09-25"
    assert as_date["primary"]["as_of"] == as_midnight["primary"]["as_of"]
    assert as_date["snapshot_id"] == as_midnight["snapshot_id"]


def test_app_and_digest_price_aliases_share_identity_for_the_same_daily_data():
    shared = _row(**{"Fuente": "Yahoo Finance / yfinance · cierre diario"})
    app = build_policy_snapshot({**shared, "Cierre": 110, "Moneda": "EUR"}, growth_result=_growth(price=110))
    digest = build_policy_snapshot(
        {**shared, "Fecha": "2026-09-25", "Precio": 110.0, "Currency": "eur"},
        growth_result=_growth(price=110.0, as_of=datetime(2026, 9, 25)),
    )
    assert app == digest


def test_errors_are_persisted_without_manufacturing_entry_or_losing_known_scores():
    snapshot = build_policy_snapshot(
        _row(**{"Incidencia principal": "Falta histórico suficiente", "Incidencia comparativa": "Filtro incompleto"}),
        growth_result=_growth(),
    )
    assert snapshot["primary"]["data_error"] == "Falta histórico suficiente"
    assert not snapshot["primary"]["entry_candidate"]
    assert snapshot["primary"]["score"] == 83
    assert snapshot["comparative"]["data_error"] == "Filtro incompleto"
    assert snapshot["comparative"]["data_status"] == "partial"
    assert snapshot["comparative"]["quality_score"] == 78
    assert decode_policy_note(encode_policy_note("Seguimiento", snapshot)) == snapshot


@pytest.mark.parametrize("note", [
    "Dinámica · Plan mensual", f"{POLICY_NOTE_MARKER} not json",
    f'{POLICY_NOTE_MARKER} {{"policy_version": "dual_strategy_v1"}}',
    f'{POLICY_NOTE_MARKER} {{"policy_version": "dual_strategy_v1", "primary": {{"score": NaN}}, "comparative": {{}}, "position": {{}}}}',
    f'{POLICY_NOTE_MARKER} {{"policy_version": "dual_strategy_v1", "primary": {{"score": 1e999}}, "comparative": {{}}, "position": {{}}}}',
])
def test_legacy_invalid_or_nonfinite_policy_notes_are_not_new_strategy_evidence(note):
    assert decode_policy_note(note) is None


def test_tampered_snapshot_hash_is_not_accepted():
    snapshot = build_policy_snapshot(_row(), growth_result=_growth())
    snapshot["primary"]["score"] = 100
    assert decode_policy_note(f"{POLICY_NOTE_MARKER} {json.dumps(snapshot)}") is None
