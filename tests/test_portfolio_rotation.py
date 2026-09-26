from datetime import date

import pandas as pd

from src.portfolio_rotation import (
    build_pair_correlations,
    build_recovery_hurdles,
    build_rotation_dashboard,
    build_rotation_priorities,
    horizon_score,
)


TODAY = date(2026, 9, 26)


def _row(ticker: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "Ticker": ticker,
        "Momento entrada": 72,
        "Fuerza relativa": 74,
        "Calidad empresa": 76,
        "Valoración": 70,
        "Riesgo controlado": 72,
        "Confianza datos": 85,
        "Lectura entrada": "Entrada interesante",
        "Si ya la tienes": "Mantener",
        "Oportunidad": 73,
        "Sector": "Technology",
        "Fecha": TODAY,
    }
    row.update(overrides)
    return row


def test_horizon_score_changes_weights_without_reusing_opportunity() -> None:
    row = _row(
        "MIX",
        **{
            "Momento entrada": 95,
            "Fuerza relativa": 90,
            "Calidad empresa": 40,
            "Valoración": 35,
            "Oportunidad": 99,
        },
    )

    weekly = horizon_score(row, "Semanal", today=TODAY)
    annual = horizon_score(row, "Anual", today=TODAY)

    assert weekly["Score horizonte"] > annual["Score horizonte"]
    assert weekly["Confianza"] == 85


def test_stale_data_is_grey_instead_of_a_buy_or_sell_signal() -> None:
    dashboard = build_rotation_dashboard(
        [_row("OLD", Fecha=date(2025, 1, 1))],
        ["OLD"],
        [],
        horizon="Mensual",
        today=TODAY,
    )

    assert dashboard.positions[0]["Color"] == "Gris"
    assert "Actualizar" in str(dashboard.positions[0]["Acción"])


def test_positions_that_need_review_are_listed_before_healthy_positions() -> None:
    dashboard = build_rotation_dashboard(
        [
            _row("HEALTHY"),
            _row(
                "REVIEW",
                **{
                    "Si ya la tienes": "Reducir",
                    "Oportunidad": 30,
                    "Riesgo controlado": 35,
                },
            ),
        ],
        ["HEALTHY", "REVIEW"],
        [],
        allocations_pct={"HEALTHY": 10, "REVIEW": 5},
        horizon="Mensual",
        today=TODAY,
    )

    assert [row["Ticker"] for row in dashboard.positions] == ["REVIEW", "HEALTHY"]
    assert dashboard.positions[0]["Color"] == "Naranja"


def test_priorities_are_limited_and_merge_the_best_switch_into_its_source() -> None:
    dashboard = build_rotation_dashboard(
        [
            _row(
                "OLD",
                **{
                    "Momento entrada": 20,
                    "Fuerza relativa": 20,
                    "Calidad empresa": 60,
                    "Valoración": 40,
                    "Riesgo controlado": 50,
                    "Si ya la tienes": "Reducir",
                    "Oportunidad": 35,
                    "Sector": "Industrials",
                },
            ),
            _row(
                "FAV",
                **{
                    "Momento entrada": 95,
                    "Fuerza relativa": 95,
                    "Calidad empresa": 90,
                    "Valoración": 80,
                    "Riesgo controlado": 85,
                    "Confianza datos": 90,
                    "Sector": "Technology",
                },
            ),
        ],
        ["OLD"],
        ["FAV"],
        allocations_pct={"OLD": 8.0},
        horizon="Mensual",
        recovery_hurdles={
            ("OLD", "FAV"): {
                "Umbral recuperación": 1.0,
                "Fricción económica": 2.0,
                "Reserva fiscal": 0.0,
                "Capital prudente": 100.0,
            }
        },
        pair_correlations={("OLD", "FAV"): 0.20},
        today=TODAY,
    )

    priorities = build_rotation_priorities(dashboard, limit=1)

    assert len(priorities) == 1
    assert priorities[0]["Posición"] == "OLD"
    assert priorities[0]["Alternativa"] == "FAV"
    assert priorities[0]["Empresa"] == "FAV"
    assert "OLD → FAV" in str(priorities[0]["Acción"])


def test_manual_thesis_invalidation_stays_red_even_with_old_market_data() -> None:
    dashboard = build_rotation_dashboard(
        [
            _row(
                "BROKEN",
                Fecha=date(2025, 1, 1),
                **{
                    "Tesis invalidada": True,
                    "Motivo tesis": "La empresa retiró permanentemente su producto principal.",
                },
            )
        ],
        ["BROKEN"],
        [],
        horizon="Mensual",
        today=TODAY,
    )

    assert dashboard.positions[0]["Color"] == "Rojo"
    assert "producto principal" in str(dashboard.positions[0]["Motivo"])


def test_switch_candidates_are_limited_to_explicit_favorites() -> None:
    dashboard = build_rotation_dashboard(
        [
            _row(
                "OLD",
                **{
                    "Momento entrada": 25,
                    "Fuerza relativa": 25,
                    "Calidad empresa": 62,
                    "Valoración": 45,
                    "Riesgo controlado": 52,
                    "Si ya la tienes": "Reducir",
                    "Oportunidad": 40,
                    "Sector": "Industrials",
                },
            ),
            _row(
                "FAV",
                Sector="Technology",
                **{
                    "Momento entrada": 82,
                    "Fuerza relativa": 84,
                    "Calidad empresa": 82,
                    "Valoración": 76,
                    "Riesgo controlado": 78,
                },
            ),
            _row(
                "NOT_FAVORITE",
                **{
                    "Momento entrada": 99,
                    "Fuerza relativa": 99,
                    "Calidad empresa": 99,
                    "Valoración": 99,
                    "Riesgo controlado": 99,
                },
            ),
        ],
        ["OLD"],
        ["FAV"],
        allocations_pct={"OLD": 12},
        horizon="Mensual",
        recovery_hurdles={
            ("OLD", "FAV"): {
                "Umbral recuperación": 1.5,
                "Fricción económica": 3.0,
                "Reserva fiscal": 0.0,
                "Capital prudente": 175.0,
            }
        },
        pair_correlations={("OLD", "FAV"): 0.30},
        today=TODAY,
    )

    assert {row["Ticker"] for row in dashboard.candidates} == {"FAV"}
    assert dashboard.switches[0]["Alternativa"] == "FAV"


def test_daily_view_does_not_propose_normal_rotation() -> None:
    dashboard = build_rotation_dashboard(
        [
            _row(
                "OLD",
                **{
                    "Momento entrada": 10,
                    "Fuerza relativa": 10,
                    "Riesgo controlado": 35,
                    "Si ya la tienes": "Reducir",
                },
            ),
            _row(
                "FAV",
                **{
                    "Momento entrada": 95,
                    "Fuerza relativa": 95,
                    "Riesgo controlado": 90,
                    "Lectura entrada": "Entrada fuerte",
                },
            ),
        ],
        ["OLD"],
        ["FAV"],
        allocations_pct={"OLD": 20},
        horizon="Diario",
        today=TODAY,
    )

    assert dashboard.policy.normal_rotations is False
    assert dashboard.switches == ()


def test_favorite_without_quality_is_not_marked_blue() -> None:
    dashboard = build_rotation_dashboard(
        [_row("FAV", **{"Calidad empresa": None})],
        [],
        ["FAV"],
        horizon="Mensual",
        today=TODAY,
    )

    assert dashboard.candidates[0]["Color"] == "Gris"
    assert "calidad" in str(dashboard.candidates[0]["Acción"]).lower()


def test_position_color_respects_the_selected_horizon() -> None:
    source = _row(
        "HELD",
        **{
            "Momento entrada": 95,
            "Fuerza relativa": 95,
            "Calidad empresa": 60,
            "Valoración": 0,
            "Riesgo controlado": 60,
            "Confianza datos": 90,
            "Si ya la tienes": "Mantener",
            "Oportunidad": 80,
        },
    )

    weekly = build_rotation_dashboard(
        [source],
        ["HELD"],
        [],
        allocations_pct={"HELD": 10.0},
        horizon="Semanal",
        today=TODAY,
    )
    annual = build_rotation_dashboard(
        [source],
        ["HELD"],
        [],
        allocations_pct={"HELD": 10.0},
        horizon="Anual",
        today=TODAY,
    )

    assert weekly.positions[0]["Color"] == "Azul"
    assert annual.positions[0]["Color"] == "Amarillo"


def test_low_confidence_position_cannot_be_blue() -> None:
    dashboard = build_rotation_dashboard(
        [_row("HELD", **{"Confianza datos": 20, "Oportunidad": 90})],
        ["HELD"],
        [],
        allocations_pct={"HELD": 10.0},
        horizon="Mensual",
        today=TODAY,
    )

    assert dashboard.positions[0]["Color"] == "Gris"


def test_weekly_view_limits_changes_to_one_pair() -> None:
    weak = {
        "Momento entrada": 20,
        "Fuerza relativa": 20,
        "Calidad empresa": 60,
        "Valoración": 40,
        "Riesgo controlado": 50,
        "Si ya la tienes": "Reducir",
        "Oportunidad": 35,
        "Sector": "Industrials",
    }
    strong = {
        "Momento entrada": 95,
        "Fuerza relativa": 95,
        "Calidad empresa": 90,
        "Valoración": 80,
        "Riesgo controlado": 85,
        "Confianza datos": 90,
        "Sector": "Technology",
    }
    costs = {
        (origin, candidate): {
            "Umbral recuperación": 1.0,
            "Fricción económica": 2.0,
            "Reserva fiscal": 0.0,
            "Capital prudente": 100.0,
        }
        for origin in ("OLD1", "OLD2")
        for candidate in ("FAV1", "FAV2")
    }

    dashboard = build_rotation_dashboard(
        [
            _row("OLD1", **weak),
            _row("OLD2", **weak),
            _row("FAV1", **strong),
            _row("FAV2", **strong),
        ],
        ["OLD1", "OLD2"],
        ["FAV1", "FAV2"],
        allocations_pct={"OLD1": 8.0, "OLD2": 7.0},
        horizon="Semanal",
        recovery_hurdles=costs,
        pair_correlations={
            (origin, candidate): 0.20
            for origin in ("OLD1", "OLD2")
            for candidate in ("FAV1", "FAV2")
        },
        today=TODAY,
    )

    assert len(dashboard.switches) == 1


def test_high_recovery_hurdle_blocks_a_switch() -> None:
    rows = [
        _row(
            "OLD",
            **{
                "Momento entrada": 25,
                "Fuerza relativa": 25,
                "Si ya la tienes": "Reducir",
                "Oportunidad": 40,
                "Sector": "Industrials",
            },
        ),
        _row("FAV", Sector="Technology"),
    ]
    dashboard = build_rotation_dashboard(
        rows,
        ["OLD"],
        ["FAV"],
        allocations_pct={"OLD": 12},
        horizon="Mensual",
        recovery_hurdles={
            ("OLD", "FAV"): {
                "Umbral recuperación": 12.0,
                "Fricción económica": 5.0,
                "Reserva fiscal": 40.0,
            }
        },
        pair_correlations={("OLD", "FAV"): 0.30},
        today=TODAY,
    )

    assert dashboard.switches == ()


def test_missing_correlation_blocks_rotation_instead_of_claiming_diversification() -> None:
    dashboard = build_rotation_dashboard(
        [
            _row(
                "OLD",
                **{
                    "Momento entrada": 20,
                    "Fuerza relativa": 20,
                    "Oportunidad": 35,
                    "Si ya la tienes": "Reducir",
                    "Sector": "Industrials",
                },
            ),
            _row(
                "FAV",
                **{
                    "Momento entrada": 95,
                    "Fuerza relativa": 95,
                    "Calidad empresa": 90,
                    "Valoración": 85,
                    "Riesgo controlado": 85,
                    "Confianza datos": 90,
                    "Sector": "Technology",
                },
            ),
        ],
        ["OLD"],
        ["FAV"],
        allocations_pct={"OLD": 10.0},
        recovery_hurdles={
            ("OLD", "FAV"): {
                "Umbral recuperación": 1.0,
                "Fricción económica": 2.0,
                "Reserva fiscal": 0.0,
                "Capital prudente": 100.0,
            }
        },
        horizon="Mensual",
        today=TODAY,
    )

    assert dashboard.switches == ()


def test_zero_switch_limit_is_respected() -> None:
    dashboard = build_rotation_dashboard(
        [
            _row(
                "OLD",
                **{
                    "Momento entrada": 20,
                    "Fuerza relativa": 20,
                    "Oportunidad": 35,
                    "Si ya la tienes": "Reducir",
                    "Sector": "Industrials",
                },
            ),
            _row(
                "FAV",
                **{
                    "Momento entrada": 95,
                    "Fuerza relativa": 95,
                    "Calidad empresa": 90,
                    "Valoración": 85,
                    "Riesgo controlado": 85,
                    "Confianza datos": 90,
                    "Sector": "Technology",
                },
            ),
        ],
        ["OLD"],
        ["FAV"],
        allocations_pct={"OLD": 10.0},
        recovery_hurdles={
            ("OLD", "FAV"): {
                "Umbral recuperación": 1.0,
                "Fricción económica": 2.0,
                "Reserva fiscal": 0.0,
                "Capital prudente": 100.0,
            }
        },
        pair_correlations={("OLD", "FAV"): 0.25},
        horizon="Mensual",
        limit_switches=0,
        today=TODAY,
    )

    assert dashboard.switches == ()


def test_pair_correlations_use_aligned_recent_returns() -> None:
    index = pd.date_range("2026-01-01", periods=60, freq="B")
    prices = pd.Series([100 + value * value / 100 for value in range(60)], index=index)

    correlations = build_pair_correlations(
        {
            "OLD": pd.DataFrame({"close": prices}),
            "FAV": pd.DataFrame({"close": prices * 2}),
        },
        ["OLD"],
        ["FAV"],
    )

    assert correlations[("OLD", "FAV")] > 0.999


def test_recovery_hurdle_prefers_fifo_operation_context() -> None:
    open_positions = pd.DataFrame(
        [
            {
                "Ticker": "OLD",
                "Ahora vale": 1_000.0,
                "Coste de vender": 2.0,
                "Recibirías si vendieras": 998.0,
                "Impuesto aproximado": 39.6,
                "Capital neto disponible": 958.4,
            }
        ]
    )

    hurdles = build_recovery_hurdles(
        open_positions,
        [{"Ticker": "OLD", "Moneda": "EUR"}, {"Ticker": "FAV", "Moneda": "USD"}],
        ["FAV"],
        tax_rate_pct=20.0,
        sell_fee_eur=1.0,
        buy_fee_eur=1.0,
        spread_pct=0.1,
        fx_cost_pct=0.2,
    )

    hurdle = hurdles[("OLD", "FAV")]
    assert hurdle["Origen coste"] == "Operaciones FIFO"
    assert 0 < float(hurdle["Capital prudente"]) < 958.4
    assert float(hurdle["Umbral recuperación"]) > 4.0


def test_recovery_hurdle_can_use_complete_snapshot_cost_basis() -> None:
    snapshot = pd.DataFrame(
        [{"analysis_ticker": "OLD", "value_eur": 1_000.0, "cost_estimate_eur": 800.0}]
    )

    hurdles = build_recovery_hurdles(
        pd.DataFrame(),
        [{"Ticker": "OLD", "Moneda": "EUR"}, {"Ticker": "FAV", "Moneda": "EUR"}],
        ["FAV"],
        snapshot=snapshot,
        tax_rate_pct=20.0,
        sell_fee_eur=1.0,
        buy_fee_eur=1.0,
        spread_pct=0.1,
        fx_cost_pct=0.2,
    )

    assert hurdles[("OLD", "FAV")]["Origen coste"] == "Coste aproximado de la fotografía"


def test_authoritative_snapshot_cost_overrides_older_fifo_context() -> None:
    open_positions = pd.DataFrame(
        [
            {
                "Ticker": "OLD",
                "Ahora vale": 1_000.0,
                "Coste de vender": 2.0,
                "Recibirías si vendieras": 998.0,
                "Impuesto aproximado": 40.0,
                "Capital neto disponible": 958.0,
            }
        ]
    )
    snapshot = pd.DataFrame(
        [
            {
                "analysis_ticker": "OLD",
                "value_eur": 500.0,
                "cost_estimate_eur": 450.0,
                "source": "Fotografía completa del bróker",
            }
        ]
    )

    hurdles = build_recovery_hurdles(
        open_positions,
        [{"Ticker": "OLD", "Moneda": "EUR"}, {"Ticker": "FAV", "Moneda": "EUR"}],
        ["FAV"],
        snapshot=snapshot,
        tax_rate_pct=20.0,
        sell_fee_eur=1.0,
        buy_fee_eur=1.0,
        spread_pct=0.1,
        fx_cost_pct=0.2,
    )

    hurdle = hurdles[("OLD", "FAV")]
    assert hurdle["Origen coste"] == "Coste aproximado de la fotografía"
    assert float(hurdle["Valor bruto"]) == 500.0


def test_recovery_hurdle_omits_snapshot_without_complete_cost_basis() -> None:
    snapshot = pd.DataFrame(
        [{"analysis_ticker": "OLD", "value_eur": 1_000.0, "cost_estimate_eur": None}]
    )

    hurdles = build_recovery_hurdles(
        pd.DataFrame(),
        [{"Ticker": "OLD", "Moneda": "EUR"}, {"Ticker": "FAV", "Moneda": "EUR"}],
        ["FAV"],
        snapshot=snapshot,
        tax_rate_pct=20.0,
        sell_fee_eur=1.0,
        buy_fee_eur=1.0,
        spread_pct=0.1,
        fx_cost_pct=0.2,
    )

    assert hurdles == {}
