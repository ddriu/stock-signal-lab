"""Contrato de elegibilidad de una favorita como destino azul.

Estos tests aíslan los filtros que hoy están repartidos entre ``horizon_score``
y ``_candidate_color``. Son deliberadamente de caja negra: el diagnóstico que
se exponga en la interfaz deberá explicar los mismos bloqueos sin relajar los
guardarraíles de entrada.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.portfolio_rotation import (
    build_rotation_dashboard,
    summarize_candidate_eligibility,
)


TODAY = date(2026, 9, 27)


def _favorite(ticker: str = "FAV", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "Ticker": ticker,
        "Momento entrada": 92,
        "Fuerza relativa": 90,
        "Calidad empresa": 82,
        "Valoración": 78,
        "Riesgo controlado": 80,
        "Confianza datos": 90,
        "Lectura entrada": "Entrada interesante",
        "Si ya la tienes": "Mantener",
        "Oportunidad": 84,
        "Sector": "Technology",
        "Fecha": TODAY,
    }
    row.update(overrides)
    return row


def _candidate(row: dict[str, object], *, horizon: str = "Mensual") -> dict[str, object]:
    dashboard = build_rotation_dashboard(
        [row],
        [],
        [row["Ticker"]],
        horizon=horizon,
        today=TODAY,
    )
    return dashboard.candidates[0]


def test_fresh_complete_favorite_with_valid_entry_is_blue() -> None:
    candidate = _candidate(_favorite())

    assert candidate["Color"] == "Azul"
    assert candidate["Cobertura"] == 100.0
    assert candidate["Confianza"] >= 70.0


@pytest.mark.parametrize(
    ("overrides", "expected_color", "message_fragment"),
    [
        ({"Fecha": date(2026, 8, 1)}, "Gris", "actualizar datos"),
        ({"Calidad empresa": None}, "Gris", "calidad y riesgo"),
        ({"Riesgo controlado": None}, "Gris", "calidad y riesgo"),
        ({"Valoración": None}, "Gris", "todos los factores"),
        ({"Lectura entrada": "Esperar"}, "Amarillo", "todavía no cumple"),
        ({"Momento entrada": 40, "Fuerza relativa": 40}, "Amarillo", "todavía no cumple"),
        ({"Confianza datos": 60}, "Amarillo", "todavía no cumple"),
        ({"Calidad empresa": 54}, "Amarillo", "todavía no cumple"),
        ({"Riesgo controlado": 49}, "Amarillo", "todavía no cumple"),
    ],
)
def test_each_blue_guardrail_has_an_observable_non_blue_result(
    overrides: dict[str, object],
    expected_color: str,
    message_fragment: str,
) -> None:
    candidate = _candidate(_favorite(**overrides))

    assert candidate["Color"] == expected_color
    assert message_fragment in str(candidate["Acción"]).casefold()


def test_held_favorites_are_not_counted_as_destination_candidates() -> None:
    dashboard = build_rotation_dashboard(
        [_favorite("HELD"), _favorite("FREE")],
        ["HELD"],
        ["HELD", "FREE"],
        allocations_pct={"HELD": 5.0},
        horizon="Mensual",
        today=TODAY,
    )

    assert [row["Ticker"] for row in dashboard.candidates] == ["FREE"]


def test_candidate_table_limit_cannot_be_used_as_total_favorite_diagnostic() -> None:
    rows = [_favorite(f"FAV{index:02d}") for index in range(20)]
    dashboard = build_rotation_dashboard(
        rows,
        [],
        [row["Ticker"] for row in rows],
        horizon="Mensual",
        limit_candidates=5,
        today=TODAY,
    )

    # La interfaz puede mostrar sólo cinco filas; un contador global de motivos
    # deberá calcularse antes de este truncado para representar las 20 favoritas.
    assert len(dashboard.candidates) == 5
    assert all(row["Color"] == "Azul" for row in dashboard.candidates)


def test_candidate_diagnostic_uses_the_full_favorite_universe() -> None:
    rows = [_favorite(f"FAV{index:02d}") for index in range(20)]

    diagnostic = summarize_candidate_eligibility(
        rows,
        [],
        [row["Ticker"] for row in rows],
        horizon="Mensual",
        today=TODAY,
    )

    assert diagnostic["candidate_total"] == 20
    assert diagnostic["analyzed"] == 20
    assert diagnostic["fresh"] == 20
    assert diagnostic["complete"] == 20
    assert diagnostic["blue"] == 20


def test_candidate_diagnostic_separates_missing_stale_and_near_miss() -> None:
    rows = [
        _favorite("BLUE"),
        _favorite("WAIT", **{"Lectura entrada": "Esperar"}),
        _favorite("OLD", Fecha=date(2026, 8, 1)),
        _favorite("PARTIAL", **{"Calidad empresa": None}),
    ]

    diagnostic = summarize_candidate_eligibility(
        rows,
        ["HELD"],
        ["BLUE", "WAIT", "OLD", "PARTIAL", "UNKNOWN", "MISSING", "HELD"],
        horizon="Mensual",
        today=TODAY,
    )

    assert diagnostic["total_favorites"] == 7
    assert diagnostic["held_excluded"] == 1
    assert diagnostic["candidate_total"] == 6
    assert diagnostic["analyzed"] == 4
    assert diagnostic["blue"] == 1
    assert diagnostic["primary_rejection_counts"]["missing_summary"] == 2
    assert diagnostic["primary_rejection_counts"]["stale"] == 1
    assert diagnostic["primary_rejection_counts"]["missing_quality_or_risk"] == 1
    assert diagnostic["primary_rejection_counts"]["invalid_entry"] == 1
    assert [row["ticker"] for row in diagnostic["near_misses"]] == ["WAIT"]


def test_candidate_diagnostic_funnel_is_monotonic() -> None:
    rows = [
        _favorite("BLUE"),
        _favorite("OLD_COMPLETE", Fecha=date(2026, 8, 1)),
        _favorite("FRESH_PARTIAL", **{"Valoración": None}),
    ]

    diagnostic = summarize_candidate_eligibility(
        rows,
        [],
        ["BLUE", "OLD_COMPLETE", "FRESH_PARTIAL", "NO_SUMMARY"],
        horizon="Mensual",
        today=TODAY,
    )

    assert diagnostic["candidate_total"] == 4
    assert diagnostic["analyzed"] == 3
    assert diagnostic["fresh"] == 2
    assert diagnostic["complete"] == 1
    assert diagnostic["blue"] == 1
    assert (
        diagnostic["candidate_total"]
        >= diagnostic["analyzed"]
        >= diagnostic["fresh"]
        >= diagnostic["complete"]
        >= diagnostic["blue"]
    )


def test_candidate_diagnostic_matches_dashboard_color_contract() -> None:
    rows = [
        _favorite("BLUE"),
        _favorite("WAIT", **{"Lectura entrada": "Esperar"}),
        _favorite("OLD", Fecha=date(2026, 8, 1)),
    ]
    favorites = [row["Ticker"] for row in rows]

    dashboard = build_rotation_dashboard(
        rows,
        [],
        favorites,
        horizon="Mensual",
        limit_candidates=20,
        today=TODAY,
    )
    diagnostic = summarize_candidate_eligibility(
        rows,
        [],
        favorites,
        horizon="Mensual",
        today=TODAY,
    )
    dashboard_counts = {
        color: sum(row["Color"] == color for row in dashboard.candidates)
        for color in ("Azul", "Amarillo", "Gris")
    }

    assert diagnostic["blue"] == dashboard_counts["Azul"]
    assert diagnostic["yellow"] == dashboard_counts["Amarillo"]
    assert diagnostic["gray"] == dashboard_counts["Gris"]
