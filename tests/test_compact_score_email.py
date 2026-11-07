"""Compact rendering only: synthetic sealed reports, no transport or data access."""
from copy import deepcopy

import pytest

from src.daily_digest import _email_delta, _email_excerpt, render_daily_score_email, seal_daily_score_report


def row(ticker, *, held=False, blocked=False, score=88):
    return dict(ticker=ticker, company_name="Compañía " + ticker, held=held,
        score=score, label="Entrada fuerte", status="review_data" if blocked else "entry_strong",
        quality=dict(block_entry=blocked, reasons=["Falta verificar el último trimestre."] if blocked else []),
        comparison_status="changed", change=-4, change_reason="La fuerza del precio baja.",
        next_step="Confirmar precio, evento y tamaño; no comprar antes de verificarlo.",
        source="Fuente de prueba", session="2026-11-06", policy_version="test-v1",
        history=[dict(session="2026-11-05", score=92), dict(session="2026-11-04", score=90)])


def test_five_alerts_do_not_hide_scores_or_three_independent_new_candidates():
    report = seal_daily_score_report(dict(as_of="2026-11-06",
        rows=[*[row("HELD"+str(i), held=True) for i in range(6)],
              *[row("NEW"+str(i)) for i in range(4)], row("PENDING", blocked=True)],
        alerts=[dict(ticker="HELD"+str(i), title="Revisar exposición", change_reason="Cambió la tesis.",
            next_step="No vender automáticamente.", horizon="Semanas", strategy_label="Tesis propia",
            data_source="Fuente de prueba", as_of="2026-11-06") for i in range(5)]))
    before = deepcopy(report)
    _, plain, html = render_daily_score_email(report, app_url="https://example.invalid/?digest="+report["report_id"])
    assert report == before
    assert html.count("<tr><td style=") == 5
    assert "88/100" in html and "-4 puntos" in plain
    candidate_html = html.split("Posibles entradas · hasta tres", 1)[1].split("</ul>", 1)[0]
    assert all("NEW"+str(i) in candidate_html for i in range(3))
    assert "NEW3" not in candidate_html and "PENDING" not in candidate_html
    assert "No vender automáticamente" in html
    assert "Compra no confirmada" in html
    assert report["report_id"] in html and report["report_id"] in plain


def test_mobile_markup_has_two_columns_and_no_five_session_history_in_rows():
    report = seal_daily_score_report(dict(as_of="2026-11-06", rows=[row("TEST", held=True)]))
    _, plain, html = render_daily_score_email(report)
    assert "max-width:600px" in html and "table-layout:fixed" in html
    assert "font-size:14px" in html and "min-width:" not in html
    assert html.count("<th style=") == 2
    assert "2026-11-05" not in html + plain and "2026-11-04" not in html + plain
    assert "Cinco sesiones" in html  # History remains available in the app.


@pytest.mark.parametrize("value,status,expected", [(-4,"changed","-4 puntos"),
    (3,"changed","+3 puntos"), (1,"stable","+1 puntos · estable"),
    (-4,"not_comparable","Sin comparación"), (None,"changed","Sin comparación"),
    (float("nan"),"changed","Sin comparación"), (True,"changed","Sin comparación")])
def test_signed_delta_is_never_fabricated_for_incomparable_scores(value, status, expected):
    assert _email_delta(dict(change=value, comparison_status=status)) == expected


def test_excerpt_does_not_clip_a_long_sentence_before_its_negation():
    reason = "El contexto " + "parece favorable " * 20 + "pero no confirma una entrada."
    assert _email_excerpt(reason) == "Motivo extenso: consulta la explicación completa antes de decidir."
    assert _email_excerpt("No confirma entrada. " + "detalle " * 80).startswith("No confirma entrada.")
