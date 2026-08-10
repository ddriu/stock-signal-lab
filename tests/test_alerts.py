from __future__ import annotations

import pandas as pd
import pytest

from src.alerts import (
    AlertCandidate,
    build_alert_candidate,
    build_digest_content,
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
    )

    _, plain, html = build_digest_content("David", [candidate])

    assert "TOP OPORTUNIDADES" in plain
    assert "BAE Systems plc" in plain
    assert "Oportunidad 79" in plain
    assert "href=" not in html
    assert "http://ba.l" not in html.lower()
