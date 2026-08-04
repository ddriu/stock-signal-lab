import pandas as pd

from src.opportunity_catalog import build_opportunity_catalog


def test_all_favorites_appear_without_downloading_again() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "ticker": "MA",
                "analyzed_at": "2026-08-01T18:00:00",
                "price": 575.0,
                "opportunity_score": 72,
                "company_score": 88,
                "entry_score": 64,
                "opportunity_label": "Candidata",
                "entry_label": "Vigilancia",
                "position_label": "Mantener",
            }
        ]
    )

    rows = build_opportunity_catalog(["MA", "RTX"], snapshots, [])

    assert [row["Ticker"] for row in rows] == ["MA", "RTX"]
    assert rows[0]["Oportunidad"] == 72
    assert rows[0]["Comprobación"] == "Pendiente de actualizar"
    assert rows[1]["Lectura conjunta"] == "Pendiente de comprobar"
    assert rows[1]["Comprobación"] == "Sin análisis previo"


def test_live_analysis_replaces_saved_snapshot_and_keeps_nonfavorite() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "ticker": "MA",
                "analyzed_at": "2026-08-01",
                "opportunity_score": 60,
            }
        ]
    )
    live = [
        {"Ticker": "MA", "Oportunidad": 79, "Lectura conjunta": "Candidata"},
        {"Ticker": "AXP", "Oportunidad": 70, "Lectura conjunta": "Vigilancia"},
    ]

    rows = build_opportunity_catalog(["MA"], snapshots, live)

    assert [row["Ticker"] for row in rows] == ["MA", "AXP"]
    assert rows[0]["Oportunidad"] == 79
    assert rows[0]["Comprobación"] == "Actualizado en esta sesión"
    assert rows[1]["Origen"] == "Datos actuales"


def test_latest_saved_snapshot_is_used_even_if_input_is_unsorted() -> None:
    snapshots = pd.DataFrame(
        [
            {"ticker": "HALO", "analyzed_at": "2026-07-01", "entry_score": 40},
            {"ticker": "HALO", "analyzed_at": "2026-08-03", "entry_score": 68},
        ]
    )

    rows = build_opportunity_catalog(["halo"], snapshots, [])

    assert len(rows) == 1
    assert rows[0]["Momento entrada"] == 68
    assert rows[0]["Fecha"] == "2026-08-03"
