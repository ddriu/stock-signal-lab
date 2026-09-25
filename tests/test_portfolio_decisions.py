from src.portfolio_decisions import build_portfolio_decision_rows


def test_exit_signal_is_presented_as_a_review_with_reason_and_date() -> None:
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

    assert rows[0]["Decisión"] == "Revisar posible salida"
    assert rows[0]["Motivo"] == "Precio bajo la media de 200 sesiones"
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

    assert rows[0]["Decisión"] == "Revisar exposición"
    assert "media de 50" in rows[0]["Motivo"]
