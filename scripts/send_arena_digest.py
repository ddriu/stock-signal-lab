"""Send the next Arena session after the daily market report has been saved."""

from datetime import date, datetime, timezone
from html import escape
from zoneinfo import ZoneInfo
import os

import exchange_calendars
import requests

from src.alerts import is_valid_email
from src.email_sender import load_email_config, send_email
from src.storage import create_journal

ARENA = "https://ddriu-arena-ia.gabledtapir.chatgpt.site"


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
        return
    journal = create_journal("ddriu")
    report = journal.get_latest_daily_score_report()
    if not report:
        return
    report_day = date.fromisoformat(report["as_of"])
    if expected_session is not None and report_day != expected_session:
        return
    calendar = exchange_calendars.get_calendar("XNYS")
    if not calendar.is_session(report_day):
        return
    session = calendar.next_session(report_day)
    opening, closing = calendar.session_open(session), calendar.session_close(session)
    now = datetime.now(timezone.utc)
    if now < calendar.session_close(report_day) or now >= opening:
        return

    config = load_email_config()
    if not config.configured or not config.sender_email:
        raise RuntimeError("El correo de la Arena necesita SMTP.")
    response = requests.post(
        f"{journal.url}/rest/v1/rpc/arena_mail_credentials",
        headers=journal.headers, json={}, timeout=12, allow_redirects=False,
    )
    if not response.ok:
        raise RuntimeError("No se pudo autorizar el correo de la Arena.")
    settings = response.json()
    if not isinstance(settings, dict) or not is_valid_email(settings.get("recipient", "")):
        raise RuntimeError("Falta el destinatario del correo de la Arena.")
    headers = {
        "OAI-Sites-Authorization": "Bearer " + settings["site_token"],
        "X-Arena-Mail-Token": settings["job_token"],
    }

    def arena(payload):
        res = requests.post(ARENA + "/api/arena/daily-mail", headers=headers, json=payload,
                            timeout=145, allow_redirects=False)
        if not res.ok:
            raise RuntimeError(f"La Arena no pudo preparar el correo (HTTP {res.status_code}).")
        return res.json()

    data = arena({
        "action": "prepare", "report_day": report_day.isoformat(), "report_id": report["report_id"],
        "session": session.date().isoformat(), "open_at": opening.isoformat(), "close_at": closing.isoformat(),
    })
    if data["status"] == "accepted":
        return
    if data["status"] != "ready":
        raise RuntimeError("El correo de la Arena tiene un intento pendiente de confirmación.")
    subject, plain, html = render_digest(data["digest"])
    try:
        receipt = send_email(settings["recipient"], subject, plain, html, config=config)
    except Exception as error:
        receipt = getattr(error, "smtp_receipt", None)
        if receipt is None:
            raise
    if receipt is None:
        raise RuntimeError("Gmail no confirmó la aceptación del correo.")
    arena({"action": "confirm", "session": session.date().isoformat(), "delivery_id": data["delivery_id"]})
    print("Correo de la Arena aceptado por Gmail.")
