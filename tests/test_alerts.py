from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from src.alerts import (
    AlertCandidate,
    DailyOverviewRow,
    build_alert_candidate,
    build_daily_overview_content,
    build_digest_content,
    build_alert_state,
    describe_state_change,
    filter_changed_candidates,
    normalize_alert_preferences,
)
from src.signal_engine import SignalResult


@pytest.mark.parametrize("builder", [build_digest_content, build_daily_overview_content])
def test_email_html_declares_utf8_before_accented_text(builder) -> None:
    _, plain, rendered = builder("Fermín & María", [])

    assert rendered.startswith('<!DOCTYPE html>\n<html lang="es">')
    assert '<meta charset="utf-8">' in rendered[:1024]
    assert rendered.index('<meta charset="utf-8">') < rendered.index("Fermín")
    assert "Hola Fermín & María," in plain
    assert "Hola Fermín &amp; María," in rendered
    assert "aplicación" in plain
    assert "aplicación" in rendered
    assert rendered.encode("utf-8").decode("utf-8") == rendered
    assert rendered.endswith("</body>\n</html>\n")


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


def test_isolated_technical_sale_never_becomes_a_sale_email() -> None:
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

    assert candidate is None


def test_confirmed_exposure_review_uses_shared_decision_without_sale_badge() -> None:
    preferences = normalize_alert_preferences(owner="fer", email="fer@example.com", enabled=True)
    candidate = build_alert_candidate(
        make_signal(position_label="Vender"), price=90, held=True, preferences=preferences,
        position_decision={"Decisión": "Revisar exposición", "Motivo": "Riesgo confirmado."},
    )
    assert candidate is not None
    assert candidate.kind == "Revisión de riesgo"
    assert candidate.position_label == "Revisar exposición"
    assert candidate.signature == "position:Revisar exposición"
    _, plain, rendered = build_digest_content("Fermín", [candidate])
    assert "Riesgo confirmado." in plain
    assert "VENTA" not in rendered


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


def test_daily_mail_shows_market_date_data_coverage_and_shared_holding_decision() -> None:
    row = DailyOverviewRow(
        ticker="MCD", company_name="McDonald's", held=True, price=100,
        as_of="2026-10-02", technical_score=5, technical_label="Esperar",
        position_label="Vender", fundamental_score=90, confidence_pct=80,
        portfolio_decision="Esperar confirmación", decision_reason="La tesis sigue intacta.",
        data_note="Última sesión disponible",
    )
    _, plain, rendered = build_daily_overview_content("David", [row])
    assert "Esperar confirmación" in plain and "Esperar confirmación" in rendered
    assert "Revisar posible salida" not in plain
    assert "2026-10-02" in plain and "2026-10-02" in rendered
    assert "80%" in plain and "80%" in rendered
    assert "Última sesión disponible" in rendered


def test_buyable_price_without_data_coverage_is_not_a_validated_entry() -> None:
    row = DailyOverviewRow(
        ticker="AAA", company_name="Empresa", held=False, price=100,
        as_of="2026-10-02", technical_score=90, technical_label="Entrada fuerte",
        position_label="Mantener", opportunity_score=90,
        opportunity_status="🟢 COMPRABLE", confidence_pct=30,
    )
    _, plain, rendered = build_daily_overview_content("David", [row])
    assert "Entradas validadas para revisar: 0" in plain
    assert "Entrada validada para revisar" not in rendered


def test_candidate_email_leads_with_conditional_action_and_explains_category_change() -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)
    candidate = build_alert_candidate(
        make_signal(), price=150, held=False, preferences=preferences,
        company_name="Taiwan Semiconductor Manufacturing",
        strategy_label="Crecimiento + momentum", horizon="6–18 meses",
        data_source="Yahoo Finance vía yfinance", data_limitations="Fundamentales sin fecha verificable.",
        previous_signature="neutral", next_step="Validar la zona y dimensionar la posición.",
    )
    assert candidate is not None
    subject, plain, rendered = build_digest_content("David", [candidate])
    for content in (plain, rendered):
        assert "Candidata condicionada" in content
        assert "Estrategia: Crecimiento + momentum" in content
        assert "Horizonte: 6–18 meses" in content
        assert "Antes: Vigilancia → Ahora: Entrada fuerte" in content
        assert "2026-07-28" in content
        assert "Yahoo Finance vía yfinance" in content
        assert "Fundamentales sin fecha verificable." in content
        assert "Validar la zona y dimensionar la posición." in content
        assert content.index("Siguiente paso:") < content.index("Método principal")
    assert "candidatas condicionadas" in subject
    assert ">COMPRA<" not in rendered
    assert "Este correo no ordena" in plain


def test_first_reading_does_not_claim_a_change_without_comparable_history() -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)
    candidate = build_alert_candidate(make_signal(), price=150, held=False, preferences=preferences)
    assert candidate is not None
    _, plain, rendered = build_digest_content("David", [candidate])
    assert "Sin estado anterior comparable" in plain
    assert "Sin estado anterior comparable" in rendered
    assert "Estas señales han cambiado" not in plain
    assert "Horizonte: no documentado" in plain
    assert "Fuente: no documentada" in plain


def test_emails_keep_both_methods_complete_without_cross_method_score() -> None:
    snapshot = {
        "primary": {
            "label": "Crecimiento + momentum", "score": 81, "growth_score": 88,
            "momentum_score": 75, "context_score": 70, "status": "Crecimiento fuerte",
            "entry_reading": "Esperar un precio razonable",
        },
        "comparative": {
            "label": "Calidad / valoración / tesis / riesgo", "quality_score": 40,
            "valuation_score": 35, "risk_score": 30, "filter_score": 38,
            "thesis_status": "pendiente de revisar", "reading": "Valoración exigente",
        },
        "position": {"strategy_label": "Tesis de dividendo previa", "horizon": "3–5 años"},
        "disagreement": "Momentum favorable con valoración exigente",
    }
    row = DailyOverviewRow(
        ticker="AAA", company_name="Empresa A", held=True, price=100,
        as_of="2026-10-02", technical_score=75, technical_label="Entrada fuerte",
        position_label="Mantener", growth_score=88, fundamental_score=40,
        portfolio_decision="Esperar confirmación", decision_reason="Confirmar la tesis de dividendo.",
        policy_snapshot=snapshot,
    )
    _, plain, rendered = build_daily_overview_content("David", [row])
    for content in (plain, rendered):
        assert "Estrategia: Tesis de dividendo previa" in content
        assert "Horizonte: 3–5 años" in content
        assert "Crecimiento 88/100" in content
        assert "Momentum 75/100" in content
        assert "Técnica de precio auxiliar: Técnica 75/100" in content
        assert "Contexto 70/100" in content
        assert "Puntuación del método 81/100" in content
        assert "Fundamental 40/100" in content
        assert "Valoración 35/100" in content
        assert "Riesgo 30/100" in content
        assert "Filtro fundamental 38/100" in content
        assert "Valoración exigente" in content
        assert "Divergencia entre métodos" in content
        assert "score conjunto" not in content
        assert "RESUMEN DIARIO COMBINADO" not in content
    assert plain.index("Esperar confirmación") < plain.index("Puntuación del método")
    assert rendered.count("Empresa A (AAA)") == 1


def test_exposure_and_thesis_emails_do_not_use_automatic_trade_badges() -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)
    exposure = build_alert_candidate(
        make_signal(position_label="Vender"), price=90, held=True, preferences=preferences,
        position_decision={"Decisión": "Revisar exposición", "Motivo": "Concentración confirmada."},
        previous_signature="position:Mantener",
    )
    thesis = build_alert_candidate(
        make_signal(position_label="Vender"), price=90, held=True, preferences=preferences,
        position_decision={"Decisión": "Revisar posible salida", "Motivo": "Tesis pendiente de contrastar."},
    )
    assert exposure is not None and thesis is not None
    _, plain, rendered = build_digest_content("David", [exposure, thesis])
    for content in (plain, rendered):
        assert "Revisión de exposición" in content
        assert "Revisión de tesis" in content
        assert "Concentración confirmada." in content
        assert "antes de decidir" in content
    assert ">VENTA<" not in rendered
    assert ">REDUCCIÓN<" not in rendered


def test_email_source_and_manual_context_are_html_escaped() -> None:
    row = DailyOverviewRow(
        ticker="AAA", company_name="Empresa A", held=False, price=100,
        as_of="2026-10-02", technical_score=80, technical_label="Entrada fuerte",
        position_label="Mantener", data_source="Proveedor <externo>",
        strategy_label="Tesis <manual>", next_step="Validar <datos>",
    )
    _, plain, rendered = build_daily_overview_content("David", [row])
    assert "Proveedor <externo>" in plain
    assert "Proveedor &lt;externo&gt;" in rendered
    assert "Tesis &lt;manual&gt;" in rendered
    assert "Validar &lt;datos&gt;" in rendered


def test_score_only_change_keeps_deduplication_and_notification_timestamp() -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)
    candidate = build_alert_candidate(make_signal(score=80), price=150, held=False, preferences=preferences)
    assert candidate is not None
    score_update = replace(candidate, entry_score=81, price=151)
    assert filter_changed_candidates(
        [score_update], {"TSM": candidate.signature}, only_changes=True,
    ) == []
    state = build_alert_state(
        owner="david", signal=make_signal(score=81), price=151, held=False, notified=False,
        previous_notified_at="2026-07-28T08:00:00+02:00", evaluated_at="2026-07-29T08:00:00+02:00",
    )
    assert state.notified_at == "2026-07-28T08:00:00+02:00"


def test_main_method_missing_data_cannot_become_buy_from_auxiliary_scores() -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)
    policy = {"primary": {
        "entry_candidate": False, "data_status": "pending",
        "entry_reading": "Faltan datos de crecimiento; sin nueva entrada confirmada",
    }}
    candidate = build_alert_candidate(
        make_signal(score=95), price=150, held=False, preferences=preferences,
        policy_snapshot=policy,
    )
    assert candidate is None
    row = DailyOverviewRow(
        ticker="AAA", company_name="Empresa A", held=False, price=100,
        as_of="2026-10-02", technical_score=95, technical_label="Entrada fuerte",
        position_label="Mantener", fundamental_score=95, opportunity_score=95,
        opportunity_status="🟢 COMPRABLE", confidence_pct=95, policy_snapshot=policy,
    )
    _, plain, rendered = build_daily_overview_content("David", [row])
    assert "Entradas validadas para revisar: 0" in plain
    assert "Vigilar · Faltan datos de crecimiento" in plain
    assert "Candidata condicionada" not in plain
    assert "Candidata condicionada" not in rendered


def test_primary_method_eligible_does_not_require_auxiliary_technical_agreement() -> None:
    preferences = normalize_alert_preferences(
        owner="david", email="david@example.com", enabled=True, minimum_buy_score=75,
    )
    policy = {"primary": {
        "id": "growth_momentum", "label": "Crecimiento + momentum",
        "entry_candidate": True, "data_status": "complete", "score": 82,
        "status": "Entrada fuerte", "growth_score": 90, "momentum_score": 77,
        "entry_reading": "Candidata a nueva entrada; confirmar precio y tamaño",
    }}
    candidate = build_alert_candidate(
        make_signal(score=25, label="Esperar"), price=150, held=False,
        preferences=preferences, policy_snapshot=policy,
    )
    assert candidate is not None
    assert candidate.entry_score == 25 and candidate.entry_label == "Esperar"
    assert candidate.title == "Candidata · Crecimiento + momentum"
    assert candidate.signature == "entry:growth_momentum:Entrada fuerte"
    assert candidate.explanation == "Candidata a nueva entrada; confirmar precio y tamaño"
    _, plain, rendered = build_digest_content("David", [candidate])
    for content in (plain, rendered):
        assert "Candidata condicionada" in content
        assert "Momentum 77/100" in content
        assert "Técnica de precio auxiliar: Técnica 25/100 (Esperar)" in content
        assert "77/100 (Esperar)" not in content
    score_updated = {"primary": {**policy["primary"], "score": 83}}
    updated = build_alert_candidate(
        make_signal(score=35, label="Esperar"), price=155, held=False,
        preferences=preferences, policy_snapshot=score_updated,
    )
    assert updated is not None and updated.signature == candidate.signature


@pytest.mark.parametrize("data_status", ["pending", "partial", "", None])
def test_incomplete_primary_method_never_becomes_candidate(data_status: str | None) -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)
    policy = {"primary": {
        "entry_candidate": True, "data_status": data_status, "score": 99,
        "status": "Entrada fuerte",
    }}
    assert build_alert_candidate(
        make_signal(score=99), price=150, held=False, preferences=preferences,
        policy_snapshot=policy,
    ) is None


@pytest.mark.parametrize("score", [65, None, True, float("nan"), float("inf")])
def test_primary_method_uses_its_own_preference_threshold_and_valid_score(score: object) -> None:
    preferences = normalize_alert_preferences(
        owner="david", email="david@example.com", enabled=True, minimum_buy_score=75,
    )
    policy = {"primary": {
        "entry_candidate": True, "data_status": "complete", "score": score,
    }}
    assert build_alert_candidate(
        make_signal(score=99), price=150, held=False, preferences=preferences,
        policy_snapshot=policy,
    ) is None


@pytest.mark.parametrize(
    ("previous", "current", "kind"),
    [
        ("Vigilancia activa", "Entrada candidata", "improvement"),
        ("Entrada candidata", "Entrada fuerte", "improvement"),
        ("Entrada fuerte", "Esperar mejor precio", "deterioration"),
        ("Esperar mejor precio", "Vigilancia activa", "deterioration"),
        ("No preparada", "Vigilancia activa", "improvement"),
        ("Pendiente de fundamentales", "Entrada candidata", "improvement"),
        ("Entrada candidata", "Datos empresariales parciales", "deterioration"),
        ("Datos empresariales parciales", "Pendiente de fundamentales", "change"),
    ],
)
def test_growth_signature_categories_have_distinct_human_states_and_ranks(
    previous: str, current: str, kind: str,
) -> None:
    changed, actual_kind, before, after = describe_state_change(
        f"entry:growth_momentum:{previous}", f"entry:growth_momentum:{current}", held=False,
    )
    assert changed is True and actual_kind == kind
    assert before == previous and after == current
    assert before != after
    assert before != "growth_momentum" and after != "growth_momentum"


def test_signature_migration_keeps_legacy_labels_and_does_not_fake_score_change() -> None:
    changed, kind, before, after = describe_state_change(
        "entry:Entrada fuerte:COMPRABLE", "entry:growth_momentum:Entrada fuerte", held=False,
    )
    assert (changed, kind, before, after) == (True, "change", "Entrada validada", "Entrada fuerte")
    assert describe_state_change(
        "entry:Entrada fuerte:ESPERAR_PRECIO", "entry:Entrada fuerte:COMPRABLE", held=False,
    ) == (True, "improvement", "Esperar mejor precio", "Entrada validada")
    assert describe_state_change(
        "entry:growth_momentum:Entrada fuerte", "entry:growth_momentum:Entrada fuerte", held=False,
    ) == (False, "", "Entrada fuerte", "Entrada fuerte")


def test_top_candidates_rank_by_primary_score_and_include_missing_auxiliary_opportunity() -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)

    def candidate(ticker: str, primary_score: int, auxiliary_score: int | None) -> AlertCandidate:
        policy = {"primary": {
            "label": "Crecimiento + momentum", "entry_candidate": True,
            "data_status": "complete", "score": primary_score, "status": "Entrada candidata",
        }}
        value = build_alert_candidate(
            make_signal(score=25, label="Esperar"), price=150, held=False,
            preferences=preferences, policy_snapshot=policy, opportunity_score=auxiliary_score,
        )
        assert value is not None
        return replace(value, ticker=ticker, company_name=f"Empresa {ticker}")

    _, plain, rendered = build_digest_content("David", [
        candidate("LOW", 80, 99), candidate("MISSING", 85, None), candidate("HIGH", 90, 10),
    ])
    plain_top = plain.split("TOP OPORTUNIDADES", 1)[1].split("QUÉ REVISAR Y POR QUÉ", 1)[0]
    html_top = rendered.split("TOP OPORTUNIDADES", 1)[1].split("<p>Qué revisar y por qué:", 1)[0]
    for content in (plain_top, html_top):
        assert content.index("Empresa HIGH") < content.index("Empresa MISSING") < content.index("Empresa LOW")
        assert "Nota principal 85/100" in content


def test_daily_extract_is_limited_and_keeps_complete_counts_and_priority_holdings() -> None:
    rows = [
        DailyOverviewRow(
            ticker=f"W{index:02d}", company_name=f"Empresa {index:02d}", held=False,
            price=100, as_of="2026-10-02", technical_score=20, technical_label="Esperar",
            position_label="Mantener",
        )
        for index in range(20)
    ]
    risk = replace(
        rows[0], ticker="RISK", company_name="Posición con riesgo", held=True,
        portfolio_decision="Revisar exposición", decision_reason="Concentración confirmada.",
    )
    changed = replace(
        rows[0], ticker="CHANGED", company_name="Estado cambiado", changed=True,
        change_kind="deterioration", previous_state="Entrada fuerte", current_state="Vigilancia activa",
    )
    candidate = replace(
        rows[0], ticker="CANDIDATE", company_name="Candidata principal", change_kind="new",
        policy_snapshot={"primary": {
            "entry_candidate": True, "data_status": "complete", "score": 92,
            "entry_reading": "Candidata a nueva entrada; confirmar precio y tamaño",
        }},
    )
    subject, plain, rendered = build_daily_overview_content(
        "David", [*rows, candidate, changed, risk],
        app_url="https://stock-signal-lab-ddriu.streamlit.app/",
    )
    assert "23/23 empresas procesadas" in subject
    assert "Empresas solicitadas: 23" in plain
    assert "Empresas procesadas con precios: 23" in plain
    assert "Posiciones para revisar (sin orden de venta): 1" in plain
    assert "Cambios relevantes desde el análisis anterior: 1" in plain
    assert "Primeras lecturas: 1" in plain
    for content in (plain, rendered):
        assert "Extracto priorizado: 15 de 23 empresas" in content
        assert "Empresas fuera del extracto: 8; sí están incluidas en los recuentos." in content
        assert content.index("Posición con riesgo") < content.index("Estado cambiado") < content.index("Candidata principal")
        assert "Empresa 19 (W19)" not in content
        assert "TODAS LAS FAVORITAS Y POSICIONES" not in content
        assert "https://stock-signal-lab-ddriu.streamlit.app/" in content
    assert rendered.count('<tr style="border-bottom:') == 15
    assert 'href="https://stock-signal-lab-ddriu.streamlit.app/"' in rendered


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://[", "https://user:password@example.com"])
def test_daily_mail_does_not_emit_unsafe_or_invalid_app_link(url: str) -> None:
    _, _, rendered = build_daily_overview_content("David", [], app_url=url)
    assert "href=" not in rendered


def test_primary_evidence_and_event_conflict_remain_visible_without_forcing_consensus() -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)
    policy = {
        "policy_version": "dual-2026-10", "snapshot_id": "abc123def4567890",
        "primary": {
            "label": "Crecimiento + momentum", "score": 85, "growth_score": 92,
            "momentum_score": 78, "context_score": 71, "status": "Entrada fuerte",
            "entry_candidate": True, "data_status": "complete",
            "entry_reading": "Candidata a nueva entrada; confirmar precio y tamaño",
            "positive_factors": [
                "Ingresos y beneficios aceleran", "Momentum relativo favorable",
                "Contexto de mercado favorable", "Cuarto motivo no seleccionado",
            ],
            "risk_factors": ["Valoración exigente", "Resultados próximos pueden cambiar el escenario"],
            "manual_checks": ["Confirmar crecimiento con última publicación", "Revisar tamaño de posición"],
        },
        "comparative": {"quality_score": 70, "filter_score": None},
    }
    candidate = build_alert_candidate(
        make_signal(score=30, label="Esperar"), price=150, held=False,
        preferences=preferences, policy_snapshot=policy,
        opportunity_score=45, opportunity_status="🔴 EVENTO / NO ENTRAR", timing_score=40,
        event_label="Resultados mañana", preferred_entry="148–150",
        fundamental_filter_score=62, fundamental_filter_label="Filtro orientativo",
        next_step="Validar precio y tamaño.",
    )
    assert candidate is not None
    _, plain, rendered = build_digest_content("David", [candidate])
    for content in (plain, rendered):
        assert "Candidata condicionada" in content
        assert "Puntuación del método 85/100" in content
        assert "Oportunidad 45/100" in content
        assert "EVENTO / NO ENTRAR" in content
        assert "Evento auxiliar: Resultados mañana" in content
        assert "Zona 148–150" in content
        assert "Filtro fundamental 62/100" in content
        assert "Contraste de entrada: el método principal identifica una candidata" in content
        assert "Se conservan ambas lecturas" in content
        assert "Validar precio y tamaño. Revisar Resultados mañana y esperar información nueva" in content
        assert "Ingresos y beneficios aceleran" in content
        assert "Momentum relativo favorable" in content
        assert "Contexto de mercado favorable" in content
        assert "Cuarto motivo no seleccionado" not in content
        assert "Argumento contrario / riesgos: Valoración exigente" in content
        assert "Resultados próximos pueden cambiar el escenario" in content
        assert "Pendientes del método principal: Confirmar crecimiento con última publicación" in content
        assert "Revisar tamaño de posición" in content
        assert "no garantizan rentabilidad" in content
        assert "política dual-2026-10 · Snapshot abc123def456" in content
        assert "abc123def4567890" not in content
    assert ">COMPRA<" not in rendered


def test_daily_extract_shares_primary_evidence_and_snapshot_reference_without_recalculation() -> None:
    policy = {
        "policy_version": "dual-2026-10", "snapshot_id": "fed987cba6543210",
        "primary": {
            "entry_candidate": True, "data_status": "complete", "score": 85,
            "entry_reading": "Candidata a nueva entrada; confirmar precio y tamaño",
            "positive_factors": ["Crecimiento publicado favorable"],
            "risk_factors": ["Precio exigente"], "manual_checks": ["Validar la tesis y el peso"],
        },
    }
    row = DailyOverviewRow(
        ticker="AAA", company_name="Empresa A", held=False, price=150,
        as_of="2026-10-02", technical_score=30, technical_label="Esperar",
        position_label="Mantener", policy_snapshot=policy,
    )
    _, plain, rendered = build_daily_overview_content("David", [row])
    for content in (plain, rendered):
        assert "Crecimiento publicado favorable" in content
        assert "Argumento contrario / riesgos: Precio exigente" in content
        assert "Pendientes del método principal: Validar la tesis y el peso" in content
        assert "política dual-2026-10 · Snapshot fed987cba654" in content
        assert "sin nuevo cálculo" in content
        assert "fed987cba6543210" not in content
    assert policy["snapshot_id"] == "fed987cba6543210"


@pytest.mark.parametrize("auxiliary_status", ["🟡 ESPERAR PRECIO", "🟠 EXTENDIDA / NO PERSEGUIR"])
def test_auxiliary_wait_and_extended_status_are_not_erased_or_made_mandatory_consensus(
    auxiliary_status: str,
) -> None:
    preferences = normalize_alert_preferences(owner="david", email="david@example.com", enabled=True)
    candidate = build_alert_candidate(
        make_signal(score=30, label="Esperar"), price=150, held=False, preferences=preferences,
        policy_snapshot={"primary": {
            "score": 85, "entry_candidate": True, "data_status": "complete", "status": "Entrada fuerte",
        }},
        opportunity_score=45, opportunity_status=auxiliary_status,
    )
    assert candidate is not None
    _, plain, rendered = build_digest_content("David", [candidate])
    for content in (plain, rendered):
        assert auxiliary_status in content
        assert "Oportunidad 45/100" in content
        assert "Contraste de entrada" in content
        assert "Se conservan ambas lecturas" in content
