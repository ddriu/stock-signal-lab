from __future__ import annotations

import pandas as pd
import pytest

from src.portfolio_thesis import (
    THESIS_INVALIDATED_MARKER,
    apply_thesis_invalidations,
    build_thesis_update_rows,
    build_position_plan_update_rows,
    thesis_invalidations,
)


def _snapshot() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "snapshot_date": "2026-09-27",
                "platform": "Broker A",
                "asset_name": "Empresa",
                "asset_type": "Acción",
                "analysis_ticker": "TEST",
                "currency": "EUR",
                "value_eur": 100.0,
                "comments": "Posición de largo plazo",
            },
            {
                "snapshot_date": "2026-09-27",
                "platform": "Broker B",
                "asset_name": "Empresa secundaria",
                "asset_type": "Acción",
                "analysis_ticker": "TEST",
                "currency": "EUR",
                "value_eur": 50.0,
                "comments": "",
            },
        ]
    )


def test_thesis_marker_round_trip_preserves_personal_comments() -> None:
    updated = build_thesis_update_rows(
        _snapshot(),
        "TEST",
        invalidated=True,
        reason="La empresa ha retirado definitivamente su producto principal.",
    )

    assert len(updated) == 2
    assert "Posición de largo plazo" in updated.iloc[0]["comments"]
    assert THESIS_INVALIDATED_MARKER in updated.iloc[0]["comments"]
    assert thesis_invalidations(updated)["TEST"].startswith("La empresa")

    restored = build_thesis_update_rows(updated, "TEST", invalidated=False)
    assert THESIS_INVALIDATED_MARKER not in "\n".join(restored["comments"])
    assert restored.iloc[0]["comments"] == "Posición de largo plazo"


def test_invalidated_thesis_requires_a_concrete_reason() -> None:
    with pytest.raises(ValueError, match="mínimo 10"):
        build_thesis_update_rows(
            _snapshot(),
            "TEST",
            invalidated=True,
            reason="malo",
        )


def test_thesis_status_is_injected_into_the_decision_summary() -> None:
    marked = build_thesis_update_rows(
        _snapshot(),
        "TEST",
        invalidated=True,
        reason="Pérdida estructural del cliente que sostenía la tesis.",
    )

    rows = apply_thesis_invalidations(
        [{"Ticker": "TEST", "Si ya la tienes": "Mantener"}],
        marked,
    )

    assert rows[0]["Tesis invalidada"] is True
    assert "cliente" in str(rows[0]["Motivo tesis"])


def test_explicit_position_plan_preserves_values_comments_and_invalidations() -> None:
    from src.analysis_policy import position_contexts_from_snapshot
    original = build_thesis_update_rows(_snapshot(), "TEST", invalidated=True,
                                       reason="La empresa ha perdido un cliente esencial.")
    before = original.copy(deep=True)
    updated = build_position_plan_update_rows(original, "TEST", strategy="Calidad y valoración",
                                             horizon="Largo plazo", thesis="Negocio recurrente y flujo de caja creciente.")
    pd.testing.assert_frame_equal(original, before)
    pd.testing.assert_frame_equal(updated.drop(columns="comments"), before.drop(columns="comments"))
    contexts = position_contexts_from_snapshot(updated.to_dict("records"))
    assert contexts["TEST"]["strategy_id"] == "quality_value_thesis_risk"
    assert contexts["TEST"]["horizon"] == "Largo plazo"
    assert contexts["TEST"]["invalidated"] is True
    assert "Posición de largo plazo" in updated.iloc[0]["comments"]
    again = build_position_plan_update_rows(updated, "TEST", strategy="Crecimiento y momentum",
                                           horizon="Medio plazo", thesis="Crecimiento de caja y tendencia revisable.")
    assert all(comment.count("[[SSL_POSITION_PLAN]]") == 1 for comment in again["comments"])
    assert thesis_invalidations(again)["TEST"] == "La empresa ha perdido un cliente esencial."


def test_position_plan_requires_explicit_thesis() -> None:
    with pytest.raises(ValueError, match="10 y 4000"):
        build_position_plan_update_rows(_snapshot(), "TEST", strategy="Momentum",
                                        horizon="Semanal", thesis="")
