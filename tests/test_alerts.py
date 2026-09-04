from __future__ import annotations

import pandas as pd
import pytest

from src.alerts import (
    AlertCandidate,
    DailyOverviewRow,
    build_alert_candidate,
    build_daily_overview_content,
    build_digest_content,
    build_alert_state,
    filter_changed_candidates,
    normalize_alert_preferences,
)
from src.signal_engine import SignalResult


def make_signal(
    *,
    score: int = 75,
    label: str = "Entrada fuerte",
    position_label: str = "Mantener",
) -> SignalResult:
    return SignalResult(
        ticker="TSM",
        as_of=pd.Timestamp("2026-07-28"),
        score=score,
        label=label,
        position_label=position_label,
        explanation="Señal probabilística de prueba.",
        positive_factors=("tendencia positiva",),
        risk_factors=(),
    )


def test_alert_preferences_require_email_when_enabled() -> None:
    with pytest.raises(ValueError, match="correo"):
        normalize_alert_preferences(owner="luci", enabled=True)


def test_buy_alert_is_only_for_unheld_candidate_above_threshold() -> None:
    preferences = normalize_alert_preferences(
        owner="luci",
        email="luci@example.com",
        enabled=True,
        minimum_buy_score=75,
    )
    signal = make_signal()

    candidate = build_alert_candidate(
        signal,
        price=150,
        held=False,
        preferences=preferences,
    )

    assert candidate is not None
    assert candidate.kind == "Compra"
    assert (
        build_alert_candidate(
            signal,
            price=150,
            held=True,
            preferences=preferences,
        )
        is None
    )


def test_position_alert_prioritizes_exit_over_entry_label() -> None:
    preferences = normalize_alert_preferences(
        owner="fer",
        email="fer@example.com",
        enabled=True,
    )
    candidate = build_alert_candidate(
        make_signal(position_label="Vender"),
        price=90,
        held=True,
        preferences=preferences,
    )

    assert candidate is not None
    assert candidate.kind == "Venta"
    assert candidate.signature == "position:Vender"


def test_repeated_signature_is_filtered_and_digest_has_disclaimer() -> None:
    candidate = AlertCandidate(
        ticker="TSM",
        kind="Compra",
        title="Momento técnico fuerte",
        entry_score=80,
        entry_label="Entrada fuerte",
        position_label="Mantener",
        price=155,
        as_of="2026-07-28",
        explanation="Explicación.",
        signature="entry:Entrada fuerte",
        held=False,
        company_name="Taiwan Semiconductor Manufacturing",
    )

    assert filter_changed_candidates(
        [candidate],
        {"TSM": "entry:Entrada fuerte"},
        only_changes=True,
    ) == []
    subject, plain, html = build_digest_content("Luci", [candidate])
    assert "1 alerta" in subject
    assert "Taiwan Semiconductor Manufacturing (TSM)" in subject
    assert "no constituyen asesoramiento financiero" in plain.lower()
    assert "Taiwan Semiconductor Manufacturing (TSM)" in plain
    assert "Taiwan Semiconductor Manufacturing (TSM)" in html


def test_digest_adds_opportunity_summary_without_linking_dotted_ticker() -> None:
    candidate = AlertCandidate(
        ticker="BA.L",
        kind="Compra",
        title="Momento técnico fuerte",
        entry_score=82,
        entry_label="Entrada fuerte",
        position_label="Mantener",
        price=18.25,
        as_of="2026-08-10",
        explanation="Explicación.",
        signature="entry:Entrada fuerte",
        held=False,
        company_name="BAE Systems plc",
        timing_score=68,
        opportunity_score=79,
        opportunity_status="🟢 COMPRABLE",
        preferred_entry="17.80–18.10",
        fundamental_filter_score=74,
        fundamental_filter_label="Interesante para estudiar",
    )

    _, plain, html = build_digest_content("David", [candidate])

    assert "TOP OPORTUNIDADES" in plain
    assert "BAE Systems plc" in plain
    assert "Oportunidad 79" in plain
    assert "Filtro fundamental 74/100" in plain
    assert "Calidad fundamental" in html
    assert "href=" not in html
    assert "http://ba.l" not in html.lower()


def test_alert_state_preserves_the_last_real_notification() -> None:
    state = build_alert_state(
        owner="luci",
        signal=make_signal(),
        price=160,
        held=False,
        notified=False,
        signature="entry:Entrada fuerte:ESPERAR_PRECIO",
        previous_notified_at="2026-08-19T08:00:00+02:00",
        evaluated_at="2026-08-20T08:00:00+02:00",
    )

    assert state.signature.endswith("ESPERAR_PRECIO")
    assert state.notified_at == "2026-08-19T08:00:00+02:00"


def test_daily_overview_combines_all_readings_and_keeps_missing_data_visible() -> None:
    rows = [
        DailyOverviewRow(
            ticker="HALO",
            company_name="Halozyme Therapeutics",
            held=True,
            price=108.68,
            as_of="2026-08-24",
            technical_score=80,
            technical_label="Entrada fuerte",
            position_label="Mantener",
            growth_score=76,
            growth_label="Crecimiento fuerte",
            fundamental_score=72,
            fundamental_label="Interesante para estudiar",
            opportunity_score=74,
            opportunity_status="VIGILAR",
            changed=True,
        ),
        DailyOverviewRow(
            ticker="XE",
            company_name="X-Energy",
            held=True,
            price=18.81,
            as_of="2026-08-24",
            technical_score=42,
            technical_label="Esperar",
            position_label="Reducir",
            growth_score=48,
            growth_label="Débil",
            opportunity_score=45,
            opportunity_status="ESPERAR",
        ),
        DailyOverviewRow(
            ticker="DGE.L",
            company_name="Diageo plc",
            held=False,
            price=0.0,
            as_of="",
            technical_score=None,
            technical_label="Sin datos",
            position_label="Sin datos",
            data_note="precios no disponibles",
        ),
    ]

    subject, plain, html = build_daily_overview_content("David", rows)

    assert "resumen diario" in subject
    assert "2/3 empresas procesadas" in subject
    assert "Empresas solicitadas: 3" in plain
    assert "Empresas procesadas con precios: 2" in plain
    assert "Halozyme Therapeutics (HALO)" in plain
    assert "Técnica 80" in plain
    assert "Crecimiento 76" in plain
    assert "Fundamental 72" in plain
    assert "Oportunidad 74" in plain
    assert "Diageo plc (DGE." in plain
    assert "Técnica N/D" in plain
    assert "Datos insuficientes" in plain
    assert html.count("Halozyme Therapeutics (HALO)") == 1
    assert "href=" not in html
