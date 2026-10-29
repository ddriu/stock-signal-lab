"""Pure presentation of a stored report; no scoring, transport or persistence."""

from collections.abc import Mapping
from datetime import date, datetime
from html import escape
from math import isfinite
import re
from zoneinfo import ZoneInfo


def _text(value):
    return " ".join(str(value).split()) if value is not None else ""


def _score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 100 and isfinite(value) else None


def _note(row):
    value = _score(row.get("score"))
    return f"{value:g}/100" if value is not None else "Sin nota verificable"


def _date_label(value):
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return "fecha no documentada"


def _name(row):
    ticker = _text(row.get("ticker"))
    return f"{_text(row.get('company_name')) or ticker or 'Empresa sin identificar'} ({ticker or 'sin ticker'})"


def _quality(row):
    value = row.get("quality")
    return value if isinstance(value, Mapping) else {}


def _reason(row):
    reasons = _quality(row).get("reasons")
    return "; ".join(_text(value) for value in reasons if _text(value)) if isinstance(reasons, (list, tuple)) else ""


def _verified(row):
    return (not row.get("held") and row.get("status") in {"entry_strong", "entry_candidate"}
            and _quality(row).get("block_entry") is False and _score(row.get("score")) is not None)


def render_manual_review(report, *, generated_at: str):
    """Render the same sealed rows in a comprehensive, owner-requested digest.

    Scores and actions are never inferred or modified. Position decisions come
    only from report.positions; a raw entry label is never a holding exit rule.
    The caller verifies the report seal and handles its isolated delivery claim.
    """
    if not isinstance(report, Mapping):
        raise ValueError("A stored report is required")
    identifier = _text(report.get("report_id"))
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("A valid stored report identifier is required")
    anchor = date.fromisoformat(_text(report.get("as_of")))
    clock = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Generation time needs an explicit timezone")
    local = clock.astimezone(ZoneInfo("Europe/Madrid"))
    if anchor > local.date():
        raise ValueError("A future closing date cannot be reviewed")
    rows = [row for row in report.get("rows", []) if isinstance(row, Mapping)]
    held = [row for row in rows if row.get("held")]
    favorites = [row for row in rows if not row.get("held")]
    verified = sorted((row for row in favorites if _verified(row)),
                      key=lambda row: (row.get("status") != "entry_strong", -_score(row.get("score")), _text(row.get("ticker"))))
    watch = sorted((row for row in favorites if not _verified(row)),
                   key=lambda row: (-(_score(row.get("score")) if _score(row.get("score")) is not None else -1), _text(row.get("ticker"))))[:10]
    positions = {_text(item.get("ticker")): item for item in report.get("positions", []) if isinstance(item, Mapping)}
    alerts = [item for item in report.get("alerts", []) if isinstance(item, Mapping)]
    incidents = [item for item in report.get("incidents", []) if isinstance(item, Mapping)]
    url = f"https://stock-signal-lab-ddriu.streamlit.app/?digest={identifier}"
    subject = f"Stock Signal Lab · revisión completa solicitada · cierre {anchor:%d/%m/%Y}"
    plain, blocks = [subject, ""], []

    def paragraph(value):
        plain.extend([value, ""])
        blocks.append(f"<p>{escape(value)}</p>")

    def heading(value):
        plain.extend([value.upper(), ""])
        blocks.append(f"<h2>{escape(value)}</h2>")

    def table(headers, records):
        records = list(records)
        for record in records:
            plain.extend([" | ".join(record), ""])
        blocks.append("<table cellpadding='7' cellspacing='0' border='1' style='border-collapse:collapse;width:100%'>"
                      "<thead><tr>" + "".join(f"<th>{escape(value)}</th>" for value in headers) + "</tr></thead><tbody>"
                      + "".join("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in record) + "</tr>" for record in records)
                      + "</tbody></table>")

    paragraph(f"Revisión manual generada el {local:%d/%m/%Y a las %H:%M} (Madrid). "
              f"Precios del último cierre común: {_date_label(anchor)}; cada fila conserva su propia fecha. "
              "No son cotizaciones en tiempo real. La fotografía de empresa se consulta en esta revisión "
              "con sus propias fechas y limitaciones: esto no es un backtest de lo que se sabía entonces.")
    paragraph("Se respeta el ámbito de seguimiento guardado, incluidas listas compartidas si están activadas. "
              "Las posiciones son las identificadas en ese ámbito; no se presume una liquidez actual ni se promete rentabilidad.")
    missing = sum(_score(row.get("score")) is None for row in rows)
    blocked = sum(_quality(row).get("block_entry") is not False for row in rows)
    strong = sum(row.get("status") == "entry_strong" for row in verified)
    paragraph(f"Cobertura: {len(rows)} empresas revisadas ({len(held)} en cartera y {len(favorites)} favoritas sin posición); "
              f"{len(rows) - missing} con nota guardada, {missing} sin nota verificable y {blocked} con la entrada bloqueada o sin verificar. "
              "Estos recuentos se solapan: una incidencia de datos no es una señal de venta.")

    heading("Mi cartera · todas las posiciones")
    paragraph("Una nota de entrada baja no ordena vender. La calidad de datos para comprar se separa de la revisión del riesgo de una posición existente. "
              "No se recalculan aquí pesos, rentabilidad personal, impuestos ni costes de una rotación.")
    if not held:
        paragraph("No hay posiciones identificadas en este informe; no se presume que la cartera esté vacía.")
    else:
        records = []
        for row in held:
            position = positions.get(_text(row.get("ticker")), {})
            reading = _text(position.get("reading")) or "Sin decisión de posición verificada"
            reason = _text(position.get("reason")) or "Falta una lectura de posición guardada; no se deduce mantener o salir desde la nota."
            step = _text(position.get("next_step")) or "Contrastar tesis, estrategia, horizonte y exposición antes de actuar."
            if _quality(row).get("block_entry") is not False:
                step += " Compra o ampliación pendiente de datos. " + (_reason(row) or "Calidad de datos sin verificar.")
            records.append((_name(row), f"{_note(row)} · {_date_label(row.get('session'))}", reading, f"{reason} Siguiente paso: {step}"))
        table(("Empresa", "Nota y cierre", "Qué revisar", "Motivo y siguiente paso"), records)

    heading("Cambios y avisos que revisar")
    if not alerts:
        paragraph("No hay avisos nuevos seleccionados en esta revisión. Esto no significa ausencia de riesgo ni una orden de mantener todas las posiciones.")
    for alert in alerts:
        scope = "En cartera" if alert.get("held") else "Favorita / posible entrada nueva"
        paragraph(f"{_name(alert)} · {scope}: {_text(alert.get('title')) or 'Cambio que revisar'}. "
                  f"Qué cambió: {_text(alert.get('change_reason')) or 'Cambio no documentado'}. "
                  f"Siguiente paso: {_text(alert.get('next_step')) or 'Contrastar la evidencia antes de actuar'}. "
                  f"Estrategia: {_text(alert.get('strategy_label')) or 'no documentada'}; horizonte: {_text(alert.get('horizon')) or 'no documentado'}; "
                  f"fecha: {_date_label(alert.get('as_of'))}; fuente: {_text(alert.get('data_source')) or 'no documentada'}. "
                  f"Limitaciones: {_text(alert.get('data_limitations')) or 'No se ejecutan órdenes ni se garantiza el resultado.'}")

    heading("Favoritas · entradas verificadas por el modelo")
    paragraph(f"{strong} entradas fuertes y {len(verified) - strong} candidatas verificadas. "
              "Se conservan las categorías del motor, sin imponer otro umbral ni interpretarlas como probabilidades de ganar.")
    if not verified:
        paragraph("No hay entradas verificadas en esta revisión. No se promocionan puntuaciones altas con datos insuficientes a señales de compra.")
    else:
        table(("Empresa", "Nota y cierre", "Categoría", "Qué confirmar"), (
            (_name(row), f"{_note(row)} · {_date_label(row.get('session'))}",
             "Entrada fuerte" if row.get("status") == "entry_strong" else "Entrada candidata",
             f"{_text(row.get('chart_comment'))} {_text(row.get('next_step'))} Confirmar valoración, evento próximo, riesgo, precio y tamaño.")
            for row in verified))

    heading("Favoritas · hasta diez nombres en vigilancia")
    paragraph("Ordenadas por nota observada, no por rentabilidad esperada. NO son entradas verificadas ni recomendaciones de compra; "
              "una nota alta puede coexistir con fundamentales sin fecha o datos incompletos.")
    if not watch:
        paragraph("No hay otras favoritas que mostrar en vigilancia.")
    else:
        table(("Empresa", "Nota y cierre", "Por qué esperar o revisar"), (
            (_name(row), f"{_note(row)} · {_date_label(row.get('session'))}",
             f"{_reason(row) or 'La estrategia no confirma una entrada verificable.'} {_text(row.get('chart_comment'))} {_text(row.get('next_step'))}")
            for row in watch))

    heading("Incidencias y cobertura de datos")
    if incidents:
        for item in incidents[:8]:
            paragraph(f"{_text(item.get('ticker')) or 'Empresa sin identificar'} · {_date_label(item.get('as_of'))}: "
                      f"{_text(item.get('reason')) or 'Incidencia sin detalle'}. {_text(item.get('next_step'))}")
        if len(incidents) > 8:
            paragraph(f"Hay {len(incidents) - 8} incidencias adicionales; están en el informe completo enlazado.")
    else:
        paragraph("No se han registrado incidencias adicionales; esto no certifica que todos los datos estén completos.")
    sources = sorted({_text(row.get("source")) for row in rows if _text(row.get("source"))})
    versions = sorted({_text(row.get("policy_version")) for row in rows if _text(row.get("policy_version"))})
    paragraph(f"Fuentes registradas: {'; '.join(sources) or 'no documentadas'}. "
              f"Versiones de política: {'; '.join(versions) or 'no documentadas'}. "
              "Método: crecimiento y momentum como lectura de entrada; la gestión de posiciones mantiene sus reglas independientes. "
              "No se han cambiado parámetros, puntuaciones, stops ni estrategias.")
    paragraph("No se ha realizado una auditoría individual de todas las noticias ni se promete crecimiento futuro. "
              "La comparación entre empresas no incorpora las comisiones, divisas, impuestos o deslizamiento de una operación concreta. "
              "Los datos insuficientes no se convierten en señales de venta. No se ejecutan órdenes.")
    paragraph(f"Informe completo de las {len(rows)} empresas, historial de cinco sesiones y versión exacta: {url}")
    blocks.append(f"<p><a href='{escape(url, quote=True)}'>Abrir el informe completo y su historial</a></p>")
    html = ("<!doctype html><html lang='es'><head><meta charset='utf-8'></head>"
            "<body style='font-family:Arial,sans-serif;color:#17253c;max-width:960px;margin:auto'>"
            f"<h1>{escape(subject)}</h1>" + "".join(blocks) + "</body></html>")
    return subject, "\n".join(plain), html
