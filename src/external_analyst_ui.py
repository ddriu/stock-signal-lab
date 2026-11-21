"""A compact, owner-private screen. Reading it never polls a provider or sends mail."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import unicodedata

import pandas as pd
import streamlit as st

from src.external_analysts import (ACTION_LABELS, STATE_LABELS, historical_model_reading,
                                   public_source_url, stable_id, utc_timestamp)
from src.external_analyst_service import (activate_comparison, build_external_report, effective_call, import_csv_bytes,
                                          render_external_report, save_research_candidate)
from src.external_analyst_store import ExternalAnalystStore


def _records(value):
    return value.to_dict("records") if hasattr(value, "to_dict") else list(value or [])


def _time(value):
    if not value:
        return "N/D"
    try:
        return datetime.fromisoformat(utc_timestamp(value)).strftime("%d/%m %H:%M UTC")
    except ValueError:
        return "N/D"


def _value(value, suffix=""):
    return "N/D" if value is None else f"{value:g}{suffix}" if isinstance(value, (int, float)) else str(value)


def _publication(call):
    if call.get("published_at"):
        return _time(call["published_at"])
    day = (call.get("evidence") or {}).get("published_date")
    return f"{day} (sin hora)" if day else "N/D"


def _state(call):
    return call.get("tracking_status") or call.get("state") or "not_verifiable"


def build_call_rows(calls, *, favorites=(), held=(), analyst=None, ticker=None, company=None, status=None,
                    scope="Todas", since=None, until=None):
    """Pure projection; no zero substitution, score blending or source inference."""
    favorites, held = set(favorites), set(held)
    filtered = []
    for call in calls:
        symbol = call.get("ticker")
        stamp = datetime.fromisoformat(utc_timestamp(call["observed_at"])).date()
        if (analyst and call.get("analyst") != analyst or ticker and symbol != ticker
                or company is not None and (call.get("company_name") or "", symbol or "") != company
                or status and _state(call) != status or since and stamp < since or until and stamp > until
                or scope == "Mis favoritas" and symbol not in favorites
                or scope == "Mi cartera" and symbol not in held):
            continue
        filtered.append(call)
    filtered.sort(key=lambda call: (call.get("ticker") not in held, call.get("ticker") not in favorites,
                                    not call.get("executable", False), call["observed_at"], call["id"]))
    return [{"id": call["id"], "Empresa": call.get("company_name") or "Nombre sin documentar",
             "Ticker": call.get("ticker") or "N/D", "Analista": call.get("analyst") or "N/D",
             "Dónde": "Cartera" if call.get("ticker") in held else "Favoritas" if call.get("ticker") in favorites else "Otra",
             "Lectura": ACTION_LABELS.get(call.get("action"), "Sin indicación"),
             "Estado": STATE_LABELS.get(_state(call), _state(call)),
             "Entrada": _value(call.get("entry")), "Stop": _value(call.get("stop")),
             "Objetivo": _value(call.get("target")), "Divisa": call.get("currency") or "N/D",
             "R/B": _value(call.get("reward_risk")), "Observada": _time(call["observed_at"])}
            for call in filtered]


def _journal_rows(journal, method):
    reader = getattr(journal, method, None)
    return _records(reader()) if callable(reader) else []


def _compact_table(rows):
    """Keep the first reading small; analyst, dates and ratios are in the ficha."""
    return pd.DataFrame([{**{key: row[key] for key in ("Estado", "Entrada", "Stop", "Objetivo", "Divisa")},
                          "Empresa": f"{row['Empresa']} ({row['Ticker']})"} for row in rows],
                        columns=["Empresa", "Estado", "Entrada", "Stop", "Objetivo", "Divisa"])


def _csv_cell(value):
    """Keep spreadsheet formula prefixes inert, including after whitespace."""
    text = "" if value is None else str(value)
    if text and (text[0].isspace() or unicodedata.category(text[0]) in {"Cc", "Cf"}
                 or text[0] in "=+-@"):
        return "'" + text
    return text


def filtered_calls_csv(calls, rows, snapshots=()):
    """Export only the selected structured facts, never the original letter."""
    by_id = {call["id"]: call for call in calls}
    fields = ["Empresa", "Ticker", "Analista", "Dónde", "Lectura", "Estado", "Entrada", "Stop", "Objetivo", "Divisa",
              "Potencial %", "Riesgo %", "R/B", "Publicada", "Recibida", "Observada", "Fuente",
              "Técnica histórica", "Crecimiento histórico", "Radar histórico", "Fecha del modelo"]
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        call = by_id[row["id"]]
        try:
            source = public_source_url(call.get("source_url"), provider=call.get("provider", ""))
        except (ValueError, TypeError):
            source = None
        model = historical_model_reading(call, snapshots)
        values = {**row, "Potencial %": call.get("potential_return_pct"), "Riesgo %": call.get("risk_pct"),
                  "Publicada": call.get("published_at") or (call.get("evidence") or {}).get("published_date"),
                  "Recibida": call.get("received_at"), "Observada": call["observed_at"], "Fuente": source,
                  "Técnica histórica": model["technical"], "Crecimiento histórico": model["growth"],
                  "Radar histórico": model["radar"], "Fecha del modelo": model["as_of"]}
        writer.writerow({key: _csv_cell(values.get(key)) for key in fields})
    return output.getvalue().encode("utf-8-sig")


def _source_link(call):
    try:
        url = public_source_url(call.get("source_url"), provider=call.get("provider", ""))
    except (ValueError, TypeError):
        url = None
    if url:
        st.link_button("Abrir fuente original", url)
    else:
        st.caption("Fuente original no verificada; no se habilita un enlace.")


def _render_detail(store, calls, rows, snapshots):
    if not rows:
        st.info("No hay recomendaciones con estos filtros. No significa que no existan oportunidades.")
        return
    labels = {row["id"]: f"{row['Empresa']} ({row['Ticker']}) · {row['Analista']}" for row in rows}
    requested = st.query_params.get("signal")
    ids = list(labels)
    index = ids.index(requested) if requested in ids else 0
    if st.session_state.get("external_call_detail") not in ids:
        st.session_state.pop("external_call_detail", None)
    if requested in ids and st.session_state.get("external_open_signal") != requested:
        st.session_state["external_call_detail"] = requested
        st.session_state["external_open_signal"] = requested
    identity = st.selectbox("Abrir ficha", ids, index=index, format_func=labels.get, key="external_call_detail")
    call = next(call for call in calls if call["id"] == identity)
    with st.expander("Fuente, fechas y lectura del modelo", expanded=False):
        st.write(f"{labels[identity]} · {STATE_LABELS.get(_state(call), _state(call))}")
        st.caption(f"Publicada: {_publication(call)} · Recibida: {_time(call['received_at'])} · Observada: {_time(call['observed_at'])}")
        st.caption("Publicación, recepción y observación son distintas. Solo se simulan precios posteriores a la observación efectiva.")
        _source_link(call)
        st.write(f"Entrada {_value(call.get('entry'))} · Stop {_value(call.get('stop'))} · Objetivo {_value(call.get('target'))} {call.get('currency') or 'N/D'}")
        st.write(f"Beneficio/riesgo {_value(call.get('reward_risk'))} · Riesgo hasta stop {_value(call.get('risk_pct'), '%')} · Potencial teórico {_value(call.get('potential_return_pct'), '%')}")
        reading = historical_model_reading(call, snapshots)
        st.write("Modelo ya guardado al recibirla: "
                 f"técnica {_value(reading.get('technical'))} · crecimiento {_value(reading.get('growth'))} · radar {_value(reading.get('radar'))}.")
        st.caption(f"{reading['status']}. Fecha del análisis: {reading.get('as_of') or 'N/D'}. No se mezcla con la opinión externa.")
        if call.get("issues"):
            for issue in call["issues"]:
                st.caption("• " + str(issue))
        revisions = store.list_revisions(identity)
        st.caption(f"{len(revisions)} versión(es) conservada(s); las revisiones no reescriben decisiones virtuales anteriores.")
        st.dataframe(pd.DataFrame([{"Observada": _time(row["observed_at"]),
                                   "Entrada": _value(row["call"].get("entry")),
                                   "Stop": _value(row["call"].get("stop")),
                                   "Objetivo": _value(row["call"].get("target")),
                                   "Revisión": row["revision_id"][:12]} for row in revisions]),
                     hide_index=True, use_container_width=True)
        events = [row for row in store.list_events() if row["call_id"] == identity]
        if events:
            st.write("Seguimiento virtual guardado")
            st.dataframe(pd.DataFrame([{"Fecha": _time(row["occurred_at"]),
                                       "Estado": STATE_LABELS.get(row["payload"].get("status"), row["payload"].get("status", "Evento")),
                                       "Origen": row["payload"].get("origin", "Documentado")} for row in events]),
                         hide_index=True, use_container_width=True)


def _render_import(store):
    from src.external_analyst_ingest import parse_email_bytes, parse_msg_bytes
    with st.expander("Importar un correo o CSV", expanded=False):
        st.caption("Vista previa primero, guardado después. El archivo original permanece en memoria y no se almacena. La importación manual no verifica DKIM.")
        upload = st.file_uploader("Archivo .eml, .msg o .csv (máximo 2 MB)", type=["eml", "msg", "csv"], key="external_upload")
        receipt = st.text_input("Fecha real de recepción (ISO con zona horaria)",
                                value=datetime.now(timezone.utc).isoformat(timespec="seconds"), key="external_receipt")
        senders = st.text_input("Remitente exacto permitido (solo para .eml o .msg)", key="external_sender")
        lawful = st.checkbox("Tengo derecho a usar este contenido y autorizo su tratamiento privado, sin redistribuir el correo original.", key="external_lawful_import")
        if st.button("Preparar vista previa", disabled=upload is None or not lawful, key="external_preview"):
            try:
                raw = upload.getvalue()
                if not raw or len(raw) > 2_000_000:
                    raise ValueError("El archivo debe tener entre 1 byte y 2 MB.")
                now = datetime.now(timezone.utc)
                if upload.name.lower().endswith(".csv"):
                    parsed = import_csv_bytes(raw, observed_at=now)
                else:
                    parser = parse_msg_bytes if upload.name.lower().endswith(".msg") else parse_email_bytes
                    parsed = parser(raw, received_at=utc_timestamp(receipt), observed_at=now,
                                    allowed_senders=senders, require_gmail_auth=False)
                st.session_state["external_preview_data"] = {"owner": store.owner, "hash": sha256(raw).hexdigest(), "parsed": parsed}
            except Exception:
                st.session_state.pop("external_preview_data", None)
                st.error("No se pudo preparar el archivo. Revisa formato, fecha con zona horaria, remitente y límite de 2 MB. No se ha guardado nada.")
        preview = st.session_state.get("external_preview_data")
        if preview and preview.get("owner") == store.owner and upload is not None and preview.get("hash") == sha256(upload.getvalue()).hexdigest():
            parsed = preview["parsed"]
            announced = parsed.get("announced_count")
            st.write(f"Anunciadas: {announced if announced is not None else 'N/D'} · Extraídas: {len(parsed['calls'])}")
            st.dataframe(_compact_table(build_call_rows(parsed["calls"])), hide_index=True, use_container_width=True)
            if parsed.get("issues"):
                st.warning("Hay datos no verificables o recuentos distintos. Se conservarán como incidencias, no como operaciones inventadas.")
                for issue in parsed["issues"][:10]:
                    st.caption("• " + str(issue)[:200])
            if st.button("Confirmar importación privada", key="external_confirm_import", disabled=not lawful):
                try:
                    result = store.import_message({**parsed, "observed_at": datetime.now(timezone.utc).isoformat()})
                    st.session_state.pop("external_preview_data", None)
                    # An old date/ticker filter must not hide a just-confirmed
                    # import on the refreshed screen.
                    st.session_state["external_reset_filters"] = True
                    st.session_state["external_notice"] = f"Guardadas {result['inserted']} · revisiones {result['revised']} · duplicadas {result['duplicates']}."
                except Exception:
                    st.error("El almacenamiento no confirmó la importación. Se conserva la vista previa para revisar o reintentar; no se afirma que esté guardada.")
                else:
                    st.rerun()


def _render_comparison(store, runs):
    from src.external_analyst_simulation import STRATEGIES, SimulationCosts
    activations = [row["payload"] for row in runs if row["payload"].get("kind") == "activation"]
    comparisons = [row["payload"] for row in runs if row["payload"].get("kind") == "comparison"]
    with st.expander("Comparación virtual · cuatro cuentas de 100 €", expanded=False):
        st.caption("A: resultados · B: beneficiarios indirectos · C: rebote/ruptura técnica · D: analistas externos. Presupuestos separados; ni notas combinadas ni operaciones reales.")
        if not activations:
            commission = st.number_input("Comisión estimada por operación (€)", min_value=0.0, max_value=100.0, value=1.0, step=0.25, key="external_commission")
            st.caption("Otros costes estimados: spread 10 pb, deslizamiento 5 pb y cambio de divisa 15 pb. No son tarifas confirmadas de tu bróker.")
            if st.button("Activar comparación desde ahora", key="external_activate"):
                try:
                    activate_comparison(store, now=datetime.now(timezone.utc), costs=SimulationCosts(commission_eur=commission))
                    st.success("Inicio registrado. Solo contará evidencia y precios posteriores; no se han realizado compras.")
                except Exception:
                    st.error("No se pudo confirmar la activación. Recarga para comprobar si ya quedó registrada antes de reintentar.")
            return
        activation = min(activations, key=lambda item: item["observed_at"])
        st.caption(f"Inicio congelado: {_time(activation['observed_at'])}. 100 € por cuenta; costes y versión guardados. Reabrir no reinicia pérdidas.")
        if not comparisons:
            st.info("Comparación activada; aún no hay una sesión de valoración guardada. No hay rendimientos verificables que mostrar.")
            return
        current = comparisons[-1]
        accounts = current["result"]["accounts"]
        rows = []
        for strategy, title in STRATEGIES.items():
            account = accounts[strategy]
            metric = account["metrics"]
            awaiting = account["status"] == "awaiting_evidence"
            rows.append({"Cuenta": strategy, "Enfoque": title,
                         "Estado": "Pendiente de evidencia" if awaiting else "Virtual; revisar cobertura",
                         "Patrimonio €": None if awaiting else account["nav_eur"],
                         "Neto %": None if awaiting else metric["net_return_pct"],
                         "Drawdown %": None if awaiting else metric["max_drawdown_pct"],
                         "Cerradas": metric["closed_trades"], "Abiertas": len(account["open_positions"]),
                         "Costes €": metric["total_estimated_costs_eur"],
                         "Ejecuciones verificadas": metric["verified_closed_trades"]})
        st.caption(f"Valoración guardada: {_time(current['observed_at'])}. No se consulta el mercado al abrir esta pantalla.")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("Una muestra pequeña, incluso con operaciones verificadas, no demuestra capacidad predictiva ni rentabilidad futura.")
        curves = {key: pd.Series({row["at"]: row["nav_eur"] for row in account["equity_curve"]})
                  for key, account in accounts.items() if account["status"] != "awaiting_evidence"}
        if curves:
            st.line_chart(pd.DataFrame(curves))
        details = st.selectbox("Operaciones de la cuenta", list(STRATEGIES), format_func=lambda key: f"{key} · {STRATEGIES[key]}", key="external_comparison_account")
        account = accounts[details]
        analyst_rows = []
        for analyst, metric in account.get("by_analyst", {}).items():
            analyst_rows.append({"Analista": analyst, "Observadas": metric["N_observed"],
                "Ejecutables": metric["N_executable"], "Cerradas verificadas": metric["verified_closed_trades"],
                "Aciertos %": metric["hit_rate_pct"], "Ganancia media €": metric["mean_gain_eur"],
                "Pérdida media €": metric["mean_loss_eur"], "Neto €": metric["net_profit_eur"],
                "Drawdown %": metric["max_drawdown_pct"], "Cobertura %": metric["verification_coverage_pct"]})
        if analyst_rows:
            st.write("Seguimiento por analista")
            st.caption("Cada analista usa una cuenta virtual independiente de 100 €. No se suman resultados ni se elige retrospectivamente al ganador. N/D: sin muestra verificable.")
            st.dataframe(pd.DataFrame(analyst_rows), hide_index=True, use_container_width=True)
        if account["trades"]:
            st.dataframe(pd.DataFrame(account["trades"]), hide_index=True, use_container_width=True)
        if account["incidents"]:
            st.caption(f"{len(account['incidents'])} incidencia(s): faltan precios, evidencia o hay ambigüedad de velas; no se ocultan para mejorar resultados.")


def _render_research(store, snapshots):
    with st.expander("Registrar una idea propia con evidencia (A, B o C)", expanded=False):
        st.caption("Registro prospectivo, no recomendación automática. A/B necesitan fuente pública contemporánea. C exige un análisis original guardado de esta cuenta.")
        with st.form("external_research_form"):
            strategy = st.selectbox("Enfoque", ["A", "B", "C"], format_func=lambda key: {"A": "A · Resultados empresariales", "B": "B · Beneficiario indirecto", "C": "C · Rebote o ruptura técnica"}[key])
            ticker = st.text_input("Ticker exacto", key="external_research_ticker")
            company = st.text_input("Nombre documentado", key="external_research_company")
            currency = st.text_input("Divisa explícita (EUR, USD…)", key="external_research_currency")
            source = st.text_input("Fuente pública HTTPS", key="external_research_source")
            evidence_at = st.text_input("Hora publicada de la evidencia (ISO y zona)", key="external_research_evidence_at")
            relation = st.text_input("Relación documentada con el beneficiario (B)", key="external_research_relation")
            known = {str(row["id"]): row for row in snapshots if row.get("id") is not None}
            snapshot_id = st.selectbox("Análisis técnico guardado (C)", [""] + list(known),
                format_func=lambda key: "Selecciona solo para C" if not key else f"{known[key].get('ticker')} · {known[key].get('created_at')} · #{key}", key="external_research_snapshot")
            rule = st.selectbox("Regla documentada de entrada", ["limit", "breakout", "market"],
                                format_func=lambda key: {"limit": "Precio límite", "breakout": "Ruptura del nivel", "market": "Siguiente apertura"}[key])
            entry = st.number_input("Nivel de entrada", min_value=0.0, value=0.0, key="external_research_entry")
            stop = st.number_input("Stop documentado", min_value=0.0, value=0.0, key="external_research_stop")
            target = st.number_input("Objetivo documentado", min_value=0.0, value=0.0, key="external_research_target")
            confirmed = st.checkbox("He comprobado la fuente y estos niveles; no los añado a una fecha pasada.", key="external_research_confirm")
            submitted = st.form_submit_button("Guardar idea virtual")
        if submitted:
            if not confirmed:
                st.warning("Confirma primero la evidencia contemporánea.")
                return
            try:
                now = datetime.now(timezone.utc)
                evidence = ({"engine_snapshot_id": snapshot_id, "signal_known_at": known[snapshot_id]["created_at"]}
                            if strategy == "C" else {"evidence_url": public_source_url(source),
                                                      "evidence_at": utc_timestamp(evidence_at), "beneficiary_relation": relation})
                facts = {"source_id": stable_id(strategy, ticker, source, evidence_at, entry, stop, target),
                         "ticker": ticker, "company_name": company, "currency": currency,
                         "source_url": source, "entry": entry, "stop": stop, "target": target,
                         "direction": "long", "action": "buy", "entry_rule": rule}
                save_research_candidate(store, facts, strategy=strategy, now=now, evidence=evidence)
                st.success("Idea guardada desde ahora; sin operaciones reales.")
            except Exception:
                st.error("No se pudo confirmar la idea. Revisa niveles positivos, ticker, divisa, fuente y evidencia anterior a la observación.")


def render_external_analysts(journal, *, favorites=None, held=None):
    """Render private saved observations only. Explicit buttons own all writes."""
    st.subheader("Analistas externos")
    st.caption("Fuente independiente del motor. Primero datos y evidencia; después seguimiento virtual. Sin órdenes reales.")
    try:
        store = ExternalAnalystStore(journal)
        if st.session_state.get("external_ui_owner") != store.owner:
            for key in list(st.session_state):
                if key.startswith("external_"):
                    del st.session_state[key]
            st.session_state["external_ui_owner"] = store.owner
        now = datetime.now(timezone.utc)
        calls = [effective_call(call, at=now) for call in store.list_calls()]
        messages, deliveries, runs = store.list_messages(), store.list_deliveries(), store.list_simulation_runs()
    except Exception:
        st.error("No se puede leer el almacenamiento privado de analistas. La migración o conexión puede estar pendiente. No es una lista vacía ni se ha modificado tu cartera.")
        return
    notice = st.session_state.pop("external_notice", None)
    if notice:
        st.success(notice)
    if st.session_state.pop("external_reset_filters", False):
        for key in list(st.session_state):
            if key.startswith("external_filter_"):
                del st.session_state[key]
    try:
        snapshots = _journal_rows(journal, "list_analysis_snapshots")
    except Exception:
        snapshots = []
        st.warning("No se pudo consultar el historial del modelo. La comparación histórica quedará como N/D; las recomendaciones externas siguen visibles.")
    try:
        favorites = set(favorites if favorites is not None else [row["ticker"] for row in _journal_rows(journal, "list_favorites")])
        held = set(held if held is not None else [row["ticker"] for row in _journal_rows(journal, "open_positions")])
    except Exception:
        favorites, held = set(), set()
        st.warning("No se pudo cruzar favoritas o cartera; no se han cambiado. La lista de analistas permanece independiente.")
    total = len(calls)
    executable = sum(bool(call.get("executable")) for call in calls)
    for column, label, number in zip(st.columns(3), ["Recomendaciones guardadas", "Con niveles verificables", "Solo seguimiento / revisar"], [total, executable, total-executable]):
        column.metric(label, number)
    if messages:
        latest = messages[-1]
        st.caption(f"Última importación: {_time(latest['observed_at'])} · anunciadas {latest.get('announced_count') if latest.get('announced_count') is not None else 'N/D'} · extraídas {latest['extracted_count']}. No se inventan las que faltan.")
    if calls:
        cols = st.columns(3)
        analyst = cols[0].selectbox("Analista", [None] + sorted({call["analyst"] for call in calls if call.get("analyst")}), format_func=lambda value: value or "Todos", key="external_filter_analyst")
        companies = sorted({(call.get("company_name") or "", call.get("ticker") or "") for call in calls})
        company = cols[1].selectbox("Empresa", [None] + companies,
            format_func=lambda value: "Todas" if value is None else
                f"{value[0] or 'Nombre sin documentar'} ({value[1] or 'Ticker N/D'})", key="external_filter_company")
        status = cols[2].selectbox("Estado", [None] + sorted({_state(call) for call in calls}), format_func=lambda value: STATE_LABELS.get(value, value) if value else "Todos", key="external_filter_status")
        scope = st.radio("Lista", ["Todas", "Mi cartera", "Mis favoritas"], horizontal=True, key="external_filter_scope")
        dates = [datetime.fromisoformat(utc_timestamp(call["observed_at"])).date() for call in calls]
        interval = st.date_input("Observadas entre", value=(min(dates), max(dates)), key="external_filter_date")
        since, until = (interval[0], interval[-1]) if isinstance(interval, (tuple, list)) and interval else (None, None)
        rows = build_call_rows(calls, favorites=favorites, held=held, analyst=analyst, company=company,
                               status=status, scope=scope, since=since, until=until)
        st.caption(f"{len(rows)} de {len(calls)}. N/D significa no documentado; nunca cero ni venta automática.")
        st.dataframe(_compact_table(rows), hide_index=True, use_container_width=True)
        st.download_button("Descargar selección en CSV", data=filtered_calls_csv(calls, rows, snapshots),
                           file_name="analistas_externos.csv", mime="text/csv", key="external_download_csv",
                           disabled=not rows)
        _render_detail(store, calls, rows, snapshots)
    else:
        st.info("Todavía no hay recomendaciones importadas. No se ha accedido al buzón al abrir esta vista.")
    _render_comparison(store, runs)
    _render_import(store)
    _render_research(store, snapshots)
    with st.expander("Resumen independiente y estado de envíos", expanded=False):
        st.caption("Correo separado de tus alertas actuales, máximo cinco novedades. Usa el remitente y destino ya configurados, sin cambiarlos aquí. No se envía nada al guardar estas preferencias.")
        try:
            preferences = store.get_preferences()
        except Exception:
            st.error("No se pudo consultar la autorización de este resumen. No se cambia ni se envía nada.")
            return
        enabled = st.checkbox("Autorizar el resumen de analistas externos", value=preferences.get("enabled", False), key="external_summary_enabled")
        if st.button("Guardar preferencia", key="external_save_preferences"):
            try:
                store.save_preferences({**preferences, "enabled": enabled})
                st.success("Preferencia guardada. No se ha enviado ningún correo.")
            except Exception:
                st.error("No se pudo confirmar la preferencia. No se ha enviado ningún correo.")
        if deliveries:
            states = {"reserved": "Reservado", "sent": "Aceptado por SMTP", "failed": "Fallido", "uncertain": "Incierto; no reenviar"}
            st.dataframe(pd.DataFrame([{"Reserva": row["delivery_key"][:12], "Estado": states.get(row["status"], "Desconocido"), "Fecha": _time(row["created_at"]),
                                       "Novedades": len(row["report"].get("item_ids", []))} for row in deliveries[-10:]]), hide_index=True, use_container_width=True)
            st.caption("Reservado o incierto no equivale a entregado. Un envío incierto no se repite automáticamente.")
        else:
            st.caption("Sin reservas ni envíos registrados para este resumen.")
        ingestion = [row["payload"] for row in runs if row["payload"].get("kind") == "mail_ingestion"]
        if ingestion:
            last = ingestion[-1]
            st.caption(f"Última recepción: {_time(last['observed_at'])} · estado {last['status']} · revisados {last.get('scanned', 0)} · pendientes {last.get('remaining', 0)} · con incidencias {len(last.get('failures', []))}.")
            for issue in last.get("issues", [])[:10]:
                st.caption("Recepción: " + str(issue)[:200])
            if last.get("failures"):
                st.warning("Hay correos que no se pudieron verificar o importar. No se han contado como recomendaciones ni se borraron del buzón.")
                st.dataframe(pd.DataFrame([{"Id privado": item.get("message_id", "")[:12], "Incidencia": item.get("code", "Sin confirmar")} for item in last["failures"]]), hide_index=True, use_container_width=True)
        try:
            report = build_external_report(store, now=datetime.now(timezone.utc), favorites=favorites, held=held)
            plain, _ = render_external_report(report)
            st.write("Vista previa del próximo resumen (no enviado)")
            st.text(plain)
            if report.get("waiting"):
                st.caption(f"{report['waiting']} cambios adicionales esperan otro resumen; máximo cinco por mensaje.")
        except Exception:
            st.caption("No se pudo preparar la vista previa del resumen. No se ha enviado ningún correo.")
