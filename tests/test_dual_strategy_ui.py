"""Offline checks for independent analysis methods and explicit position plans."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app as application
from src.analysis_policy import (
    COMPARATIVE_STRATEGY_ID,
    PRIMARY_STRATEGY_ID,
    build_policy_snapshot,
    decode_policy_note,
    encode_policy_note,
)
from src.portfolio_thesis import build_position_plan_update_rows


def _snapshot() -> pd.DataFrame:
    return pd.DataFrame([{
        "snapshot_date": "2026-10-13", "platform": "Test broker",
        "asset_name": "Test company", "asset_type": "Acción",
        "analysis_ticker": "TEST", "currency": "EUR", "value_eur": 100.0,
        "comments": "Tesis previa con caja recurrente y crecimiento moderado.",
    }])


def _growth() -> SimpleNamespace:
    return SimpleNamespace(
        score=83, growth_score=81, momentum_score=91, context_score=70,
        confidence_pct=100, label="Entrada fuerte", as_of="2026-10-13",
        sector_key="technology",
    )


def _dual_snapshot_record() -> dict[str, object]:
    row = {
        "Ticker": "TEST", "Fecha": "2026-10-13", "Calidad empresa": 77,
        "Valoración": 48, "Riesgo controlado": 59, "Si ya la tienes": "Mantener",
    }
    policy = build_policy_snapshot(row, growth_result=_growth(), held=True)
    return {
        "id": 1, "ticker": "TEST", "analyzed_at": "2026-10-13", "price": 100,
        "opportunity_score": 83, "company_score": 81, "entry_score": 91,
        "valuation_score": None, "relative_score": 64, "risk_score": 70,
        "confidence_pct": 100, "opportunity_label": "Dinámica · Entrada fuerte",
        "entry_label": "Entrada fuerte", "position_label": "Plan mensual",
        "expected_return_pct": None, "positive_rate_pct": None,
        "expected_price": None, "horizon_days": 126,
        "note": application._analysis_snapshot_note("Mi hipótesis", policy, PRIMARY_STRATEGY_ID),
    }


def test_primary_is_default_strategy_without_removing_comparative_routes() -> None:
    assert application.STRATEGY_OPTIONS[0] == "Crecimiento y momentum"
    assert "Calidad fundamental" in application.STRATEGY_OPTIONS
    assert "Radar" in application.ANALYSIS_OPTIONS
    assert "Empresa" in application.ANALYSIS_OPTIONS
    assert application.LEGACY_ANALYSIS_ROUTES["Crecimiento y momentum"] == (
        "Estrategias", "analysis_strategy_navigation", "Crecimiento y momentum",
    )


def test_first_analysis_visit_defaults_to_primary_with_no_remote_data(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STOCK_SIGNAL_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_SIGNAL_LAB_USERNAME", "test")
    monkeypatch.setenv("STOCK_SIGNAL_LAB_PASSWORD_HASH", "test-only-hash")
    page = AppTest.from_file(str(Path(application.__file__)), default_timeout=30)
    page.session_state["_authenticated_user"] = "test"
    page.session_state["main_navigation"] = "Analizar"
    with patch.object(application, "download_prices", side_effect=AssertionError("No remote download")):
        page.run()
    assert not page.exception
    assert not page.error
    assert page.session_state["analysis_navigation"] == "Estrategias"
    assert page.session_state["analysis_strategy_navigation"] == "Crecimiento y momentum"


def test_home_overview_exposes_explicit_routes_without_computing_analysis() -> None:
    script = '''
from unittest.mock import patch
import app

def forbidden(*args, **kwargs):
    raise AssertionError("The overview must not calculate or persist analyses")

with patch.object(app, "evaluate_growth_momentum", side_effect=forbidden):
    app.render_analysis_approach_overview()
'''
    page = AppTest.from_string(script, default_timeout=30).run()
    assert not page.exception
    assert any("no hay score mezclado" in item.value.lower() for item in page.caption)
    page.button(key="dual_approach_open_primary").click().run()
    assert not page.exception
    assert page.session_state["_requested_analysis_navigation"] == "Crecimiento y momentum"
    page.button(key="dual_approach_open_comparative").click().run()
    assert not page.exception
    assert page.session_state["_requested_analysis_navigation"] == "Empresa"


def test_history_reconstructs_each_method_without_aliasing_growth_as_quality() -> None:
    record = _dual_snapshot_record()
    snapshots = pd.DataFrame([record])
    original = snapshots.copy(deep=True)
    primary = application._analysis_history_method_rows(snapshots, PRIMARY_STRATEGY_ID)
    comparative = application._analysis_history_method_rows(snapshots, COMPARATIVE_STRATEGY_ID)

    assert primary.iloc[0]["company_score"] == 81
    assert primary.iloc[0]["entry_score"] == 91
    assert primary.iloc[0]["risk_score"] == 70
    assert primary.iloc[0]["method_coverage_pct"] == 100
    assert comparative.iloc[0]["company_score"] == 77
    assert comparative.iloc[0]["valuation_score"] == 48
    assert comparative.iloc[0]["risk_score"] == 59
    assert pd.isna(comparative.iloc[0]["opportunity_score"])
    assert pd.isna(comparative.iloc[0]["entry_score"])
    assert primary.iloc[0]["note"] == "Mi hipótesis"
    pd.testing.assert_frame_equal(snapshots, original)


def test_legacy_history_keeps_method_and_unknown_coverage_explicit() -> None:
    dynamic = {**_dual_snapshot_record(), "note": "Hipótesis legacy"}
    balanced = {
        **dynamic, "id": 2, "opportunity_score": 65, "company_score": 74,
        "entry_score": 60, "valuation_score": 56, "risk_score": 62,
        "position_label": "Mantener", "opportunity_label": "Candidata",
    }
    snapshots = pd.DataFrame([dynamic, balanced])
    primary = application._analysis_history_method_rows(snapshots, PRIMARY_STRATEGY_ID)
    comparative = application._analysis_history_method_rows(snapshots, COMPARATIVE_STRATEGY_ID)

    assert primary["id"].tolist() == [1]
    assert comparative["id"].tolist() == [2]
    assert primary.iloc[0]["data_status"] == "Cobertura no documentada"
    assert "Legacy" in comparative.iloc[0]["policy_version"]


def test_saved_history_switches_factor_labels_and_hides_raw_metadata() -> None:
    record = _dual_snapshot_record()
    script = f'''
import pandas as pd
from app import render_saved_analysis_history

class Journal:
    def list_analysis_snapshots(self):
        return pd.DataFrame([{record!r}])

render_saved_analysis_history(Journal())
'''
    page = AppTest.from_string(script, default_timeout=30).run()
    assert not page.exception
    assert "Crecimiento" in page.dataframe[0].value.columns
    assert "Nota principal" in page.dataframe[0].value.columns
    assert "[[SSL_ANALYSIS_POLICY]]" not in str(page.dataframe[0].value)
    page.selectbox(key="saved_history_method").set_value(COMPARATIVE_STRATEGY_ID).run()
    assert not page.exception
    assert "Calidad negocio" in page.dataframe[0].value.columns
    assert "Crecimiento" not in page.dataframe[0].value.columns
    assert page.dataframe[0].value.iloc[0]["Calidad negocio"] == 77


def test_position_view_reads_manual_assignment_without_reassigning_legacy_position() -> None:
    original = _snapshot()
    legacy = application._position_strategy_rows(original)
    assert "previa" in legacy.iloc[0]["Estrategia asignada"].lower()
    updated = build_position_plan_update_rows(
        original, "TEST", strategy=COMPARATIVE_STRATEGY_ID,
        horizon="3–5 años", thesis="Mantener mientras caja y ventajas competitivas persistan.",
    )
    assigned = application._position_strategy_rows(updated)
    assert assigned.iloc[0]["Horizonte asignado"] == "3–5 años"
    assert "Calidad" in assigned.iloc[0]["Estrategia asignada"]
    assert "caja" in assigned.iloc[0]["Tesis guardada"]
    assert "SSL_POSITION_PLAN" not in original.iloc[0]["comments"]


def test_position_editor_does_not_write_when_opened() -> None:
    script = f'''
import pandas as pd
from app import _render_thesis_override

class Journal:
    def upsert_portfolio_snapshot_positions(self, *args, **kwargs):
        raise AssertionError("Opening the editor must not change the stored thesis")

_render_thesis_override(
    Journal(), pd.DataFrame({_snapshot().to_dict("records")!r}), ["TEST"],
    actor_username="test",
)
'''
    page = AppTest.from_string(script, default_timeout=30).run()
    assert not page.exception
    assert any(button.label == "Guardar plan de esta posición" for button in page.button)
    assert any(button.label == "Guardar estado de tesis" for button in page.button)
    assert not page.error


def test_position_editor_saves_only_the_explicit_plan_without_trading() -> None:
    script = f'''
import pandas as pd
import streamlit as st
from app import _render_thesis_override

class Journal:
    def upsert_portfolio_snapshot_positions(self, rows, *, recorded_by):
        st.session_state["saved_plan"] = rows.to_dict("records")
        st.session_state["writes"] = int(st.session_state.get("writes", 0)) + 1

_render_thesis_override(
    Journal(), pd.DataFrame({_snapshot().to_dict("records")!r}), ["TEST"],
    actor_username="test",
)
'''
    page = AppTest.from_string(script, default_timeout=30).run()
    next(widget for widget in page.selectbox if widget.label == "Estrategia de esta posición").set_value(COMPARATIVE_STRATEGY_ID)
    next(widget for widget in page.text_input if widget.label == "Horizonte de esta posición").set_value("3–5 años")
    next(widget for widget in page.text_area if widget.label == "Tesis que justifica mantener esta posición").set_value("Caja recurrente y ventajas competitivas sostenidas.")
    next(button for button in page.button if button.label == "Guardar plan de esta posición").click().run()
    assert not page.exception
    assert not page.error
    assert page.session_state["writes"] == 1
    stored = application._position_strategy_rows(pd.DataFrame(page.session_state["saved_plan"]))
    assert stored.iloc[0]["Horizonte asignado"] == "3–5 años"
    assert "Calidad" in stored.iloc[0]["Estrategia asignada"]


def test_incomplete_primary_does_not_replace_original_scores_or_sell_position() -> None:
    row = {"Ticker": "TEST", "Calidad empresa": 80, "Valoración": 73,
           "Riesgo controlado": 71, "Oportunidad": 79, "Si ya la tienes": "Mantener"}
    original = dict(row)
    with patch.object(application, "evaluate_growth_momentum", side_effect=ValueError("No prices")):
        enriched = application._enrich_current_analysis_policy(
            [row], {"TEST": pd.DataFrame()}, {}, {}, {}, {},
            position_snapshot=_snapshot(), held_tickers=["TEST"],
        )[0]

    assert row == original
    assert enriched["Oportunidad"] == 79
    assert enriched["Nota principal"] is None
    assert enriched["Incidencia principal"] == "No prices"
    assert enriched["Política de análisis"]["primary"]["entry_candidate"] is False
    assert "venta" not in enriched["Gestión posición"].lower() or "no inferir" in enriched["Gestión posición"].lower()
    assert enriched["Política de análisis"]["position"]["strategy_id"] == "previous_thesis"


def test_central_policy_calculates_primary_with_explicit_engine_config() -> None:
    def growth_with_required_config(*, config, **kwargs):
        config.validate()
        return _growth()

    row = {"Ticker": "TEST", "Fecha": "2026-10-13", "Cierre": 100,
           "Moneda": "EUR", "Calidad empresa": 77, "Valoración": 48,
           "Riesgo controlado": 59, "Oportunidad": 60}
    with patch.object(application, "evaluate_growth_momentum", side_effect=growth_with_required_config):
        enriched = application._enrich_current_analysis_policy(
            [row], {"TEST": pd.DataFrame()}, {}, {}, {}, {},
        )[0]

    assert enriched["Nota principal"] == 83
    assert enriched["Oportunidad"] == 60
    assert enriched["Política de análisis"]["config"]["growth"]["candidate_score"] == 75
    assert "technical" in enriched["Política de análisis"]["config"]
    assert enriched["Política de análisis"]["source"] == "Yahoo Finance / yfinance · cierre diario"


def test_snapshot_note_keeps_explicit_method_and_human_note() -> None:
    record = _dual_snapshot_record()
    policy = decode_policy_note(record["note"])
    assert policy is not None
    assert policy["snapshot_method"] == "dual"
    assert application._human_analysis_note(record["note"]) == "Mi hipótesis"
    assert policy["primary"]["score"] == 83
    assert "score" not in policy["comparative"]


def test_batch_and_company_views_keep_the_same_canonical_snapshot_identity() -> None:
    policy = build_policy_snapshot(
        {"Ticker": "TEST", "Fecha": "2026-10-13", "Cierre": 100, "Moneda": "EUR",
         "Calidad empresa": 77, "Valoración": 48, "Riesgo controlado": 59},
        growth_result=_growth(),
    )
    original = dict(policy)
    batch = decode_policy_note(encode_policy_note("Seguimiento automático por lote", policy))
    company = decode_policy_note(application._analysis_snapshot_note(
        "Seguimiento automático al abrir el análisis", policy, COMPARATIVE_STRATEGY_ID,
    ))
    growth = decode_policy_note(application._analysis_snapshot_note(
        "Nota de la pantalla principal", policy, PRIMARY_STRATEGY_ID,
    ))

    assert batch is not None and company is not None and growth is not None
    assert batch["snapshot_id"] == company["snapshot_id"] == growth["snapshot_id"]
    assert batch["snapshot_method"] == company["snapshot_method"] == "dual"
    assert policy == original
    assert company["primary"]["score"] == 83
    assert company["comparative"]["quality_score"] == 77


def test_dual_plan_keeps_both_history_views_without_legacy_factor_aliasing() -> None:
    record = _dual_snapshot_record()
    decoded = decode_policy_note(record["note"])
    assert decoded is not None and decoded["snapshot_method"] == "dual"
    history = pd.DataFrame([record])
    primary = application._analysis_history_method_rows(history, PRIMARY_STRATEGY_ID)
    comparative = application._analysis_history_method_rows(history, COMPARATIVE_STRATEGY_ID)

    assert primary["id"].tolist() == [1]
    assert comparative["id"].tolist() == [1]
    assert primary.iloc[0]["opportunity_score"] == 83
    assert primary.iloc[0]["entry_score"] == 91
    assert comparative.iloc[0]["company_score"] == 77
    assert comparative.iloc[0]["risk_score"] == 59
    assert pd.isna(comparative.iloc[0]["opportunity_score"])
    assert pd.isna(comparative.iloc[0]["entry_score"])


def test_partial_metadata_keeps_valid_factors_and_rejects_wrong_score_types() -> None:
    record = _dual_snapshot_record()
    policy = decode_policy_note(record["note"])
    assert policy is not None
    policy["primary"]["growth_score"] = {"not": "a number"}
    policy["primary"]["context_score"] = True
    policy["comparative"]["quality_score"] = "missing"
    record["note"] = encode_policy_note("Parcial", policy)

    primary = application._analysis_history_method_rows(pd.DataFrame([record]), PRIMARY_STRATEGY_ID)
    comparative = application._analysis_history_method_rows(pd.DataFrame([record]), COMPARATIVE_STRATEGY_ID)
    assert primary.iloc[0]["opportunity_score"] == 83
    assert primary.iloc[0]["entry_score"] == 91
    assert pd.isna(primary.iloc[0]["company_score"])
    assert pd.isna(primary.iloc[0]["risk_score"])
    assert pd.isna(comparative.iloc[0]["company_score"])
    assert comparative.iloc[0]["valuation_score"] == 48


@pytest.mark.parametrize("position_label", ["Mantener", "Plan mensual"])
def test_opening_company_does_not_repeat_a_canonical_dual_snapshot(position_label: str) -> None:
    record = {**_dual_snapshot_record(), "position_label": position_label}
    policy = decode_policy_note(record["note"])
    assert policy is not None
    journal = MagicMock()
    journal.list_analysis_snapshots.return_value = pd.DataFrame([record])
    journal.add_analysis_snapshot.side_effect = AssertionError("The same dual snapshot was stored twice")

    interface = MagicMock()
    interface.number_input.return_value = 0.0
    interface.columns.side_effect = lambda count: [MagicMock() for _ in range(count)]
    signal = SimpleNamespace(
        as_of=pd.Timestamp("2026-10-13"), score=65, label="Vigilancia",
        position_label="Mantener", positive_factors=(),
    )
    opportunity = SimpleNamespace(
        score=60, confidence_pct=100, label="Candidata", explanation="Contraste independiente",
        positive_factors=(),
    )
    frame = pd.DataFrame(
        {"close": [99.0, 100.0], "sma_long": [98.0, 98.0], "rsi": [50.0, 51.0]},
        index=pd.to_datetime(["2026-10-12", "2026-10-13"]),
    )
    with (
        patch.object(application, "st", interface),
        patch.object(application, "evaluate_latest_signal", return_value=signal),
        patch.object(application, "build_instrument_report", return_value=SimpleNamespace()),
        patch.object(application, "historical_forward_return_study", return_value=SimpleNamespace(reliable=False)),
        patch.object(application, "render_decision_strip"),
        patch.object(application, "render_extended_market_report", side_effect=RuntimeError("End of read-only test")),
        pytest.raises(RuntimeError, match="End of read-only test"),
    ):
        application.render_analysis(
            "TEST", frame, application.StrategyConfig(), application.BacktestConfig(),
            SimpleNamespace(score=77, sector="Technology"), SimpleNamespace(score=48),
            SimpleNamespace(score=64), SimpleNamespace(score=59), opportunity, {},
            journal=journal, policy_snapshot=policy,
        )
    journal.add_analysis_snapshot.assert_not_called()
