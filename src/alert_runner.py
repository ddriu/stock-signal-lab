"""Proceso diario que analiza listas y envía un resumen por usuario."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from config import StrategyConfig
from src.alerts import (
    AlertCandidate,
    AlertPreferences,
    DailyOverviewRow,
    build_alert_candidate,
    build_alert_state,
    build_daily_overview_content,
    build_digest_content,
    describe_state_change,
    filter_changed_candidates,
    signal_signature,
)
from src.data_loader import (
    DataDownloadError,
    download_fundamental_snapshot,
    download_prices,
)
from src.email_sender import send_email
from src.entry_opportunity import STATUS_BUYABLE, evaluate_entry_opportunity
from src.fundamentals import evaluate_fundamentals
from src.fundamental_filter import evaluate_fundamental_filter
from src.growth_momentum import GrowthMomentumConfig, evaluate_growth_momentum
from src.indicators import add_indicators
from src.opportunity import evaluate_risk, evaluate_valuation
from src.signal_engine import evaluate_latest_signal
from src.storage import GROUP_PORTFOLIO_OWNER, create_journal


@dataclass(frozen=True)
class AlertRunSummary:
    users_checked: int
    tickers_checked: int
    emails_sent: int
    alerts_sent: int
    errors: tuple[str, ...]
    tickers_with_prices: int = 0


def _favorite_names(journal: object) -> dict[str, str]:
    favorites = journal.list_favorites()
    if favorites.empty:
        return {}
    names: dict[str, str] = {}
    has_name = "name" in favorites.columns
    for _, row in favorites.iterrows():
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        name = str(row.get("name") or "").strip() if has_name else ""
        names[ticker] = name or ticker
    return names


def _favorite_tickers(journal: object) -> set[str]:
    return set(_favorite_names(journal))


def _position_costs(journal: object) -> dict[str, float]:
    positions = journal.open_positions()
    if positions.empty:
        return {}
    weighted_costs: dict[str, tuple[float, float]] = {}
    for row in positions.itertuples(index=False):
        ticker = str(row.ticker).strip().upper()
        average_cost = float(row.average_cost)
        quantity = float(row.quantity)
        if ticker and average_cost > 0 and quantity > 0:
            current_cost, current_quantity = weighted_costs.get(ticker, (0.0, 0.0))
            weighted_costs[ticker] = (
                current_cost + average_cost * quantity,
                current_quantity + quantity,
            )
    return {
        ticker: total_cost / total_quantity
        for ticker, (total_cost, total_quantity) in weighted_costs.items()
        if total_quantity > 0
    }


def _download_alert_frames(
    tickers: set[str],
    *,
    today: date,
    downloader: Callable[..., pd.DataFrame],
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    frames: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    start = today - timedelta(days=550)
    config = StrategyConfig()
    for ticker in sorted(tickers):
        try:
            raw = downloader(ticker, start, today, auto_adjust=True)
            frames[ticker] = add_indicators(raw, config)
        except (DataDownloadError, ValueError, KeyError) as exc:
            errors.append(f"{ticker}: {exc}")
    return frames, errors


def run_daily_alerts(
    *,
    journal_factory: Callable[[str], object] = create_journal,
    downloader: Callable[..., pd.DataFrame] = download_prices,
    fundamental_downloader: Callable[[str], dict[str, object]] = (
        download_fundamental_snapshot
    ),
    sender: Callable[..., None] = send_email,
    today: date | None = None,
) -> AlertRunSummary:
    """Ejecuta el radar de todas las preferencias activas del backend."""

    resolved_today = today or date.today()
    group_journal = journal_factory(GROUP_PORTFOLIO_OWNER)
    if not hasattr(group_journal, "list_enabled_alert_preferences"):
        raise RuntimeError(
            "La ejecución automática requiere Supabase; SQLite sólo admite pruebas manuales."
        )
    preferences: list[AlertPreferences] = (
        group_journal.list_enabled_alert_preferences()
    )
    if not preferences:
        return AlertRunSummary(0, 0, 0, 0, ())

    group_names = _favorite_names(group_journal)
    group_favorites = set(group_names)
    group_positions = _position_costs(group_journal)
    scopes: dict[
        str,
        tuple[AlertPreferences, object, set[str], dict[str, float], dict[str, str]],
    ] = {}
    all_tickers: set[str] = set()
    errors: list[str] = []
    for preference in preferences:
        try:
            user_journal = journal_factory(preference.owner)
            names = _favorite_names(user_journal)
            favorites = set(names)
            positions = _position_costs(user_journal)
            if preference.include_group:
                favorites |= group_favorites
                for ticker, name in group_names.items():
                    names.setdefault(ticker, name)
                for ticker, average_cost in group_positions.items():
                    positions.setdefault(ticker, average_cost)
            scope = favorites | set(positions)
            scopes[preference.owner] = (
                preference,
                user_journal,
                scope,
                positions,
                names,
            )
            all_tickers |= scope
        except Exception as exc:
            errors.append(f"{preference.owner}: no se pudo leer su seguimiento ({exc}).")

    frames, download_errors = _download_alert_frames(
        all_tickers,
        today=resolved_today,
        downloader=downloader,
    )
    errors.extend(download_errors)
    config = StrategyConfig()
    growth_config = GrowthMomentumConfig()
    fundamental_cache: dict[str, dict[str, object]] = {}
    fundamental_failures: dict[str, str] = {}
    emails_sent = 0
    alerts_sent = 0

    for owner, (preference, journal, scope, positions, names) in scopes.items():
        try:
            previous = journal.list_alert_states()
            previous_signatures = (
                dict(zip(previous["ticker"], previous["signature"]))
                if not previous.empty
                else {}
            )
            previous_notified = (
                dict(zip(previous["ticker"], previous["notified_at"]))
                if not previous.empty and "notified_at" in previous.columns
                else {}
            )
            evaluated: list[tuple[object, float, bool, str, DailyOverviewRow]] = []
            candidates: list[AlertCandidate] = []
            overview_rows: list[DailyOverviewRow] = []
            for ticker in sorted(scope):
                frame = frames.get(ticker)
                if frame is None or frame.empty:
                    overview_row = DailyOverviewRow(
                            ticker=ticker,
                            company_name=names.get(ticker, ticker),
                            held=ticker in positions,
                            price=0.0,
                            as_of="",
                            technical_score=None,
                            technical_label="Sin datos",
                            position_label="Sin datos",
                            data_note="precios no disponibles",
                        )
                    overview_rows.append(overview_row)
                    continue
                try:
                    held = ticker in positions
                    signal = evaluate_latest_signal(
                        frame,
                        config,
                        ticker=ticker,
                        entry_price=positions.get(ticker),
                    )
                    price = float(frame["close"].iloc[-1])
                    state_signature = signal_signature(signal, held=held)
                    candidate = build_alert_candidate(
                        signal,
                        price=price,
                        held=held,
                        preferences=preference,
                        company_name=names.get(ticker, ""),
                    )
                    if ticker not in fundamental_cache:
                        try:
                            fundamental_cache[ticker] = fundamental_downloader(ticker)
                        except Exception as exc:
                            fundamental_cache[ticker] = {
                                "symbol": ticker,
                                "_quick_mode": True,
                            }
                            fundamental_failures[ticker] = str(exc)
                    info = fundamental_cache[ticker]
                    company_name = str(
                        info.get("longName")
                        or info.get("shortName")
                        or names.get(ticker)
                        or (candidate.company_name if candidate is not None else "")
                        or ticker
                    ).strip()
                    data_notes: list[str] = []
                    if ticker in fundamental_failures:
                        data_notes.append("fundamentales no disponibles")

                    quick_fundamental = None
                    try:
                        quick_fundamental = evaluate_fundamental_filter(info, ticker)
                        if quick_fundamental.score is None:
                            data_notes.append("calidad fundamental parcial")
                    except (KeyError, TypeError, ValueError):
                        data_notes.append("calidad fundamental incompleta")

                    risk = None
                    try:
                        risk = evaluate_risk(ticker, frame)
                    except (KeyError, TypeError, ValueError):
                        data_notes.append("riesgo parcial")

                    growth = None
                    try:
                        growth = evaluate_growth_momentum(
                            ticker=ticker,
                            frame=frame,
                            info=info,
                            relative=None,
                            risk=risk,
                            broad_market=None,
                            config=growth_config,
                        )
                    except (KeyError, TypeError, ValueError):
                        data_notes.append("crecimiento parcial")

                    enhanced = None
                    required_for_opportunity = {
                        "open",
                        "high",
                        "low",
                        "close",
                        "atr_14",
                    }
                    if required_for_opportunity.issubset(frame.columns):
                        try:
                            fundamental = evaluate_fundamentals(info, ticker)
                            valuation = evaluate_valuation(info, ticker)
                            if risk is None:
                                risk = evaluate_risk(ticker, frame)
                            enhanced = evaluate_entry_opportunity(
                                ticker=ticker,
                                company_name=company_name,
                                frame=frame,
                                signal=signal,
                                fundamental_score=fundamental.score,
                                fundamental_coverage=fundamental.coverage_pct,
                                valuation_score=valuation.score,
                                valuation_coverage=valuation.coverage_pct,
                                relative_score=None,
                                relative_coverage=0,
                                risk_score=risk.score,
                                risk_coverage=risk.coverage_pct,
                                info=info,
                                sector=str(
                                    info.get("industry")
                                    or fundamental.sector
                                    or ""
                                ),
                                market=fundamental.country or "",
                            )
                        except (KeyError, TypeError, ValueError):
                            data_notes.append("oportunidad parcial")
                    else:
                        data_notes.append("oportunidad sin histórico suficiente")

                    if candidate is not None:
                        candidate = replace(candidate, company_name=company_name)
                    if candidate is not None and candidate.kind == "Compra":
                        if enhanced is None:
                            state_signature = (
                                f"entry:{signal.label}:OPORTUNIDAD_INCOMPLETA"
                            )
                            candidate = None
                        else:
                            candidate = replace(
                                candidate,
                                timing_score=enhanced.timing.score,
                                opportunity_score=enhanced.opportunity_score,
                                opportunity_status=enhanced.status_label,
                                preferred_entry=enhanced.zones.preferred_entry.label,
                                event_label=enhanced.event.label,
                                fundamental_filter_score=(
                                    quick_fundamental.score
                                    if quick_fundamental is not None
                                    else None
                                ),
                                fundamental_filter_label=(
                                    quick_fundamental.label
                                    if quick_fundamental is not None
                                    else ""
                                ),
                                signature=(
                                    f"entry:{signal.label}:{enhanced.status_code}"
                                ),
                            )
                            state_signature = candidate.signature
                            if enhanced.status_code != STATUS_BUYABLE:
                                candidate = None
                    changed, change_kind, previous_state, current_state = (
                        describe_state_change(
                            previous_signatures.get(ticker),
                            state_signature,
                            held=held,
                        )
                    )
                    overview_row = DailyOverviewRow(
                            ticker=ticker,
                            company_name=company_name,
                            held=held,
                            price=price,
                            as_of=str(signal.as_of.date()),
                            technical_score=signal.score,
                            technical_label=signal.label,
                            position_label=signal.position_label,
                            growth_score=(growth.score if growth is not None else None),
                            growth_label=(growth.label if growth is not None else ""),
                            fundamental_score=(
                                quick_fundamental.score
                                if quick_fundamental is not None
                                else None
                            ),
                            fundamental_label=(
                                quick_fundamental.label
                                if quick_fundamental is not None
                                else ""
                            ),
                            opportunity_score=(
                                enhanced.opportunity_score
                                if enhanced is not None
                                else None
                            ),
                            opportunity_status=(
                                enhanced.status_label if enhanced is not None else ""
                            ),
                            changed=changed,
                            change_kind=change_kind,
                            previous_state=previous_state,
                            current_state=current_state,
                            data_note=", ".join(dict.fromkeys(data_notes)),
                        )
                    overview_rows.append(overview_row)
                    if candidate is not None:
                        candidates.append(candidate)
                    evaluated.append(
                        (signal, price, held, state_signature, overview_row)
                    )
                except Exception as exc:
                    # Un ticker con poco histórico o datos incompletos no debe
                    # cancelar el resumen de las demás empresas del usuario.
                    errors.append(
                        f"{owner} / {ticker}: no se pudo calcular la señal ({exc})."
                    )
                    overview_rows.append(
                        DailyOverviewRow(
                            ticker=ticker,
                            company_name=names.get(ticker, ticker),
                            held=ticker in positions,
                            price=(
                                float(frame["close"].iloc[-1])
                                if "close" in frame.columns and not frame.empty
                                else 0.0
                            ),
                            as_of="",
                            technical_score=None,
                            technical_label="Sin datos",
                            position_label="Sin datos",
                            data_note="no se pudo completar el análisis",
                        )
                    )

            selected = filter_changed_candidates(
                candidates,
                previous_signatures,
                only_changes=preference.only_changes,
            )
            actionable_delivered = False
            if selected:
                subject, plain_body, html_body = build_digest_content(
                    owner.capitalize(),
                    selected,
                )
                try:
                    sender(
                        preference.email,
                        subject,
                        plain_body,
                        html_body,
                    )
                except Exception as exc:
                    errors.append(
                        f"{owner}: no se pudo enviar el correo de alertas ({exc})."
                    )
                else:
                    actionable_delivered = True
                    emails_sent += 1
                    alerts_sent += len(selected)

            if overview_rows:
                subject, plain_body, html_body = build_daily_overview_content(
                    owner.capitalize(),
                    overview_rows,
                )
                try:
                    sender(
                        preference.email,
                        subject,
                        plain_body,
                        html_body,
                    )
                except Exception as exc:
                    errors.append(
                        f"{owner}: no se pudo enviar el resumen diario ({exc})."
                    )
                else:
                    emails_sent += 1

            notified_tickers = (
                {candidate.ticker for candidate in selected}
                if actionable_delivered
                else set()
            )
            states = [
                build_alert_state(
                    owner=owner,
                    signal=signal,
                    price=price,
                    held=held,
                    notified=signal.ticker in notified_tickers,
                    signature=signature,
                    previous_notified_at=(
                        None
                        if pd.isna(previous_notified.get(signal.ticker))
                        else str(previous_notified.get(signal.ticker))
                    ),
                    company_name=overview.company_name,
                    growth_score=overview.growth_score,
                    fundamental_score=overview.fundamental_score,
                    opportunity_score=overview.opportunity_score,
                    opportunity_status=overview.opportunity_status,
                    data_note=overview.data_note,
                )
                for signal, price, held, signature, overview in evaluated
            ]
            journal.upsert_alert_states(states)
        except Exception as exc:
            # Un destinatario no debe impedir que los demás reciban su resumen.
            errors.append(f"{owner}: no se pudo completar el aviso ({exc}).")

    return AlertRunSummary(
        users_checked=len(scopes),
        tickers_checked=len(all_tickers),
        emails_sent=emails_sent,
        alerts_sent=alerts_sent,
        errors=tuple(errors),
        tickers_with_prices=len(frames),
    )
