"""Send the next Arena session after the daily market report has been saved."""

from datetime import date, datetime, timezone
from html import escape
from zoneinfo import ZoneInfo
import os
import re
from time import sleep

import exchange_calendars
import requests

from src.alerts import is_valid_email
from src.email_sender import load_email_config, send_email
from src.storage import create_journal

ARENA = "https://ddriu-arena-ia.gabledtapir.chatgpt.site"
RECOVERABLE_PREPARE_CODES = frozenset({
    "model_plan_incomplete", "model_rate_limited", "model_provider_unavailable",
    "lease_busy", "lease_expired", "quotes_unavailable",
    "transport_timeout", "transport_error",
})


class ArenaDigestError(RuntimeError):
    """Safe stage/code only: no recipient, credentials or provider body in CI."""

    def __init__(self, phase, code, http_status=None):
        self.phase, self.code, self.http_status = phase, code, http_status
        super().__init__(f"Arena {phase}: {code}" + (f" (HTTP {http_status})" if http_status else ""))


def _failure_code(response):
    try:
        body = response.json()
        message = body.get("error", "") if isinstance(body, dict) else ""
    except (ValueError, TypeError):
        message = ""
    if not isinstance(message, str):
        return "http_error"
    if "error de OpenRouter (402)" in message:
        return "model_provider_payment_required"
    if "error de OpenRouter (429)" in message:
        return "model_rate_limited"
    if re.search(r"error de OpenRouter \(5\d{2}\)", message):
        return "model_provider_unavailable"
    for text, code in (
        ("Hay una ronda pendiente con otro informe.", "report_conflict"),
        ("Las decisiones guardadas no corresponden al informe actualizado.", "report_conflict"),
        ("La Arena está procesando otra ronda.", "lease_busy"),
        ("La sesión de ejecución caducó.", "lease_expired"),
        ("El informe carece de señales válidas o tiene duplicados.", "report_no_signals"),
        ("No se pudieron obtener cotizaciones", "quotes_unavailable"),
        ("Falta cotización", "quotes_unavailable"),
        ("La ronda requiere un informe renovado y una sesión futura.", "report_or_session_not_ready"),
        ("Todavía no está disponible el informe de Streamlit", "report_not_ready"),
        ("La bolsa ya abrió.", "execution_window_closed"),
        ("La temporada está cerrada.", "season_closed"),
    ):
        if text in message:
            return code
    if "Las respuestas válidas quedan guardadas. Puedes reintentar." in message:
        return "model_plan_incomplete"
    return "http_error"


def render_digest(digest):
    opening = datetime.fromisoformat(digest["open_at"]).astimezone(
        ZoneInfo("Europe/Madrid")
    )
    subject = f"Arena DDRIU · decisiones del {opening:%d/%m/%Y}"
    lines = [
        f"Informe de Stock Signal Lab del {digest['report_day']}.",
        f"Próxima sesión: {opening:%d/%m/%Y a las %H:%M}, hora peninsular.",
        f"Clasificación de la liga (comisión simulada: {digest['fee_percent']:g} % por orden).",
        "",
    ]
    entries = digest["standings"]
    if all(entry.get("invested_total") is not None for entry in entries):
        invested = sum(entry["invested_total"] for entry in entries)
        value = sum(entry["market_value"] for entry in entries)
        lines.extend([f"Total invertido en posiciones abiertas: {invested:.2f} USD (incluye comisiones).",
                      f"Valor conjunto al último cierre: {value:.2f} USD.", ""])
    for rank, entry in enumerate(digest["standings"], 1):
        lines.append(
            f"{rank}. {entry['name']} · mercado acumulado {entry['market_pnl']:+.2f} USD"
            f" · saldo {entry['equity']:.2f} USD · efectivo {entry['cash']:.2f} USD"
        )
        if entry["day_pnl"] is not None:
            lines.append(
                f"   Último cierre: {entry['day_pnl']:+.2f} USD de mercado"
                f" · incentivo {entry['day_bonus']:+.2f} USD"
            )
        else:
            lines.append("   Sin una jornada de liga liquidada para la fecha del informe.")
        if entry.get("invested_total") is not None:
            lines.append(f"   Invertido en posiciones abiertas: {entry['invested_total']:.2f} USD"
                         f" · valor al cierre {entry['market_value']:.2f} USD")
        if "positions" in entry:
            if not entry["positions"]:
                lines.append("   Posiciones abiertas: ninguna.")
            for position in entry["positions"]:
                units = position.get("units")
                quantity = f"{units:.8f} acciones" if units is not None else "cantidad sin dato"
                cost = position.get("cost_basis")
                cost_text = f"{cost:.2f} USD" if cost is not None else "sin dato"
                price = position.get("entry_price")
                price_text = f"{price:.8f} USD" if price is not None else "sin dato"
                lines.append(
                    f"   {position['ticker']} · {quantity} · compra media {price_text}"
                    f" · invertido {cost_text} · valor al cierre {position['value']:.2f} USD"
                    f" ({position['valuation_date']})"
                )
        lines.append("   Decisiones para la próxima sesión:")
        orders = entry["plan"]["orders"]
        if not orders:
            lines.append("   Mantener la cartera.")
        for order in orders:
            detail = (f"Comprar {order['ticker']} por {order['usd']:.2f} USD"
                      if order["action"] == "buy" else f"Vender toda la posición en {order['ticker']}")
            lines.append(f"   {detail}. {order['reason']}")
        lines.append("")
    lines.extend(["Importes y órdenes de la liga simulada.", ARENA + "/#decisiones"])
    plain = "\n".join(lines)
    return subject, plain, "<pre>" + escape(plain) + "</pre>"


def send_arena_digest(expected_session=None):
    if os.getenv("GITHUB_REPOSITORY_OWNER") != "ddriu" or not os.getenv("GITHUB_ACTIONS"):
        return "omitted_not_deployment"
    journal = create_journal("ddriu")
    report = journal.get_latest_daily_score_report()
    if not report:
        return "omitted_no_report"
    report_day = date.fromisoformat(report["as_of"])
    if expected_session is not None and report_day != expected_session:
        return "omitted_session_mismatch"
    calendar = exchange_calendars.get_calendar("XNYS")
    if not calendar.is_session(report_day):
        return "omitted_nontrading_report"
    session = calendar.next_session(report_day)
    opening, closing = calendar.session_open(session), calendar.session_close(session)
    now = datetime.now(timezone.utc)
    if now < calendar.session_close(report_day) or now >= opening:
        return "omitted_outside_execution_window"

    config = load_email_config()
    if not config.configured or not config.sender_email:
        raise ArenaDigestError("config", "smtp_not_configured")
    try:
        response = requests.post(
            f"{journal.url}/rest/v1/rpc/arena_mail_credentials",
            headers=journal.headers, json={}, timeout=12, allow_redirects=False,
        )
    except requests.Timeout:
        raise ArenaDigestError("credentials", "transport_timeout") from None
    except requests.RequestException:
        raise ArenaDigestError("credentials", "transport_error") from None
    if not response.ok:
        raise ArenaDigestError("credentials", "http_error", response.status_code)
    try:
        settings = response.json()
    except ValueError:
        raise ArenaDigestError("credentials", "invalid_response") from None
    if (not isinstance(settings, dict) or not is_valid_email(settings.get("recipient", ""))
            or not all(isinstance(settings.get(key), str) and settings[key] for key in ("site_token", "job_token"))):
        raise ArenaDigestError("credentials", "invalid_credentials")
    headers = {
        "OAI-Sites-Authorization": "Bearer " + settings["site_token"],
        "X-Arena-Mail-Token": settings["job_token"],
    }

    def arena(payload):
        phase = payload["action"]
        try:
            res = requests.post(ARENA + "/api/arena/daily-mail", headers=headers, json=payload,
                                timeout=145, allow_redirects=False)
        except requests.Timeout:
            raise ArenaDigestError(phase, "transport_timeout") from None
        except requests.RequestException:
            raise ArenaDigestError(phase, "transport_error") from None
        if not res.ok:
            raise ArenaDigestError(phase, _failure_code(res), res.status_code)
        try:
            data = res.json()
        except ValueError:
            raise ArenaDigestError(phase, "invalid_response") from None
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            raise ArenaDigestError(phase, "invalid_response")
        return data

    data = arena({
        "action": "prepare", "report_day": report_day.isoformat(), "report_id": report["report_id"],
        "session": session.date().isoformat(), "open_at": opening.isoformat(), "close_at": closing.isoformat(),
    })
    if data["status"] == "accepted":
        return "already_accepted"
    if data["status"] != "ready":
        raise ArenaDigestError("prepare", "reservation_uncertain")
    if not isinstance(data.get("digest"), dict) or not isinstance(data.get("delivery_id"), str):
        raise ArenaDigestError("prepare", "invalid_response")
    subject, plain, html = render_digest(data["digest"])
    try:
        receipt = send_email(settings["recipient"], subject, plain, html, config=config)
    except Exception as error:
        receipt = getattr(error, "smtp_receipt", None)
        if receipt is None:
            raise ArenaDigestError("send", "smtp_unconfirmed") from None
    if getattr(receipt, "status", None) != "smtp_accepted":
        raise ArenaDigestError("send", "smtp_unconfirmed")
    confirmation = arena({"action": "confirm", "session": session.date().isoformat(), "delivery_id": data["delivery_id"]})
    if confirmation["status"] != "accepted":
        raise ArenaDigestError("confirm", "reservation_uncertain")
    print("Correo de la Arena aceptado por Gmail.")
    return "sent"


def _record_status(status):
    """Distinguish an omission from delivery, including in Actions summaries."""
    print(f"Arena: estado={status}.")
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"status={status}\n")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"\nArena · estado: `{status}`. No se reenvían resúmenes de Stock Signal Lab.\n")


def main():
    """Separate scheduled stage: a failed Arena must not masquerade as SMTP."""
    import sys
    from src.digest_schedule import daily_digest_session_date
    session = None
    anchored = os.getenv("ARENA_EXPECTED_SESSION", "").strip()
    if anchored:
        try:
            session = date.fromisoformat(anchored)
        except ValueError:
            print("::error::Arena: sesión de origen inválida; no se intenta correo.", file=sys.stderr)
            return 1
    elif os.getenv("GITHUB_EVENT_NAME") == "schedule":
        session = daily_digest_session_date()
        # A late recovery may read an already saved report. Unlike the Stock
        # analysis window, its limit is the actual next exchange opening.
        # send_arena_digest enforces report close < now < next session open.
    for attempt in range(2):
        try:
            status = send_arena_digest(expected_session=session)
            _record_status(status or "completed")
            return 0
        except ArenaDigestError as error:
            if (attempt == 0 and error.phase == "prepare"
                    and error.code in RECOVERABLE_PREPARE_CODES):
                print(f"::warning::Arena pendiente: código={error.code}; un único reintento de preparación. Se conservan las decisiones válidas.", file=sys.stderr)
                sleep(3)
                continue
            _record_status("pending")
            print(f"::error::Correo Arena pendiente: fase={error.phase}; código={error.code}; HTTP={error.http_status or 'N/D'}. No se reenvían resúmenes de Stock Signal Lab.", file=sys.stderr)
            return 1
        except Exception:
            _record_status("pending")
            print("::error::Correo Arena pendiente: fallo no clasificado; no se publican detalles privados ni se reenvían otros correos.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
