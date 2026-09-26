from src.portfolio_decisions import (
    build_portfolio_decision_rows,
    build_switch_candidate_rows,
)


def test_isolated_exit_signal_requires_confirmation_instead_of_selling() -> None:
    rows = build_portfolio_decision_rows(
        [
            {
                "Ticker": "ASTS",
                "Si ya la tienes": "Vender",
                "Lectura entrada": "Sin entrada",
                "Momento entrada": 5,
                "Oportunidad": 40,
                "Motivo posición": "Precio bajo la media de 200 sesiones",
                "Fecha": "2026-09-24",
            }
        ],
        ["ASTS"],
    )

    assert rows[0]["Decisión"] == "Esperar confirmación"
    assert rows[0]["Tipo"] == "Alerta técnica aislada"
    assert rows[0]["Motivo"] == "Precio bajo la media de 200 sesiones"
    assert "no vender" in rows[0]["Confirmar antes"]
    assert rows[0]["Fecha"] == "2026-09-24"


def test_reduce_signal_does_not_look_like_an_automatic_order() -> None:
    rows = build_portfolio_decision_rows(
        [
            {
                "Ticker": "RTX",
                "Si ya la tienes": "Reducir",
                "Lectura entrada": "Esperar",
                "Oportunidad": 55,
                "Motivo posición": "Pérdida de la media de 50 sesiones",
                "Fecha": "2026-09-24",
            }
        ],
        ["RTX"],
    )

    assert rows[0]["Decisión"] == "Mantener sin ampliar"
    assert "media de 50" in rows[0]["Motivo"]


def test_exit_review_requires_multiple_reliable_adverse_readings() -> None:
    rows = build_portfolio_decision_rows(
        [
            {
                "Ticker": "RISK",
                "Si ya la tienes": "Vender",
                "Lectura entrada": "Sin entrada",
                "Oportunidad": 30,
                "Calidad empresa": 35,
                "Riesgo controlado": 32,
                "Confianza datos": 82,
                "Motivo posición": "Ruptura de tendencia de largo plazo",
                "Fecha": "2026-09-25",
            }
        ],
        ["RISK"],
    )

    assert rows[0]["Decisión"] == "Revisar posible salida"
    assert rows[0]["Tipo"] == "Confluencia técnica y de riesgo"
    assert "fiscal" in rows[0]["Riesgo si actúas"]


def test_overweight_position_is_reviewed_even_when_technical_signal_is_healthy() -> None:
    rows = build_portfolio_decision_rows(
        [
            {
                "Ticker": "BIG",
                "Si ya la tienes": "Mantener",
                "Lectura entrada": "Entrada fuerte",
                "Oportunidad": 78,
                "Calidad empresa": 88,
                "Riesgo controlado": 70,
                "Confianza datos": 90,
            }
        ],
        ["BIG"],
        allocations_pct={"BIG": 24},
    )

    assert rows[0]["Decisión"] == "Revisar exposición"
    assert rows[0]["Tipo"] == "Concentración"


def test_switch_comparison_demands_material_advantage_and_similar_risk() -> None:
    summary = [
        {
            "Ticker": "OLD",
            "Oportunidad": 45,
            "Calidad empresa": 65,
            "Riesgo controlado": 58,
            "Confianza datos": 80,
        },
        {
            "Ticker": "NEW",
            "Oportunidad": 78,
            "Calidad empresa": 75,
            "Riesgo controlado": 60,
            "Confianza datos": 82,
            "Fecha": "2026-09-25",
        },
    ]

    rows = build_switch_candidate_rows(summary, ["OLD"])

    assert rows[0]["Alternativa"] == "NEW"
    assert rows[0]["Ventaja"] == 33
    assert rows[0]["Lectura"] == "Ventaja clara para estudiar"
    assert "fiscal" in rows[0]["Pendiente antes de cambiar"]


def test_switch_comparison_rejects_small_improvement() -> None:
    rows = build_switch_candidate_rows(
        [
            {"Ticker": "OLD", "Oportunidad": 67},
            {"Ticker": "NEW", "Oportunidad": 73},
        ],
        ["OLD"],
    )

    assert rows[0]["Lectura"] == "No compensa cambiar"
