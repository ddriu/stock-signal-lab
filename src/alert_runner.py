"""Proceso diario que analiza listas y envía un resumen por usuario."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
import hashlib
import json
from math import isfinite
import os
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from config import StrategyConfig
from src.alerts import (
    AlertCandidate,
    AlertPreferences,
    DailyOverviewRow,
    build_alert_candidate,
    build_alert_state,
    describe_state_change,
    filter_changed_candidates,
    is_valid_email,
    signal_signature,
)
from src.data_loader import (
    DataDownloadError,
    download_fundamental_snapshot,
    download_prices,
    resolve_analysis_ticker,
)
from src.data_sources import benchmark_for_ticker, sector_benchmark
from src.email_sender import send_email
from src.daily_digest import build_daily_score_report, render_daily_score_email, seal_daily_score_report
from src.entry_opportunity import STATUS_BUYABLE, evaluate_entry_opportunity
from src.fundamentals import evaluate_fundamentals
from src.fundamental_filter import evaluate_fundamental_filter
from src.growth_momentum import GrowthMomentumConfig, evaluate_growth_momentum
from src.indicators import add_indicators
from src.opportunity import (
    evaluate_relative_strength,
    evaluate_risk,
    evaluate_valuation,
)
from src.portfolio_snapshot import latest_portfolio_snapshot
from src.portfolio_decisions import build_portfolio_decision_rows
from src.market_data_quality import quote_freshness
from src.signal_engine import evaluate_latest_signal
from src.storage import GROUP_PORTFOLIO_OWNER, create_journal
from src.analysis_policy import (
    build_policy_snapshot,
    decode_policy_note,
    encode_policy_note,
    position_contexts_from_snapshot,
)


@dataclass(frozen=True)
class AlertRunSummary:
    users_checked: int
    tickers_checked: int
    emails_sent: int
    alerts_sent: int
    errors: tuple[str, ...]
    tickers_with_prices: int = 0
    tickers_with_fresh_prices: int = 0
    emails_skipped: int = 0


def _daily_digest_delivery_key(owner: str, recipient: str, market_date: str) -> str:
    """An owner/recipient/session key, independent of content or deployment.

    Reserving this identity before SMTP gives at-most-once attempts. It cannot
    prove delivery to a mailbox, and a failed/uncertain attempt is deliberately
    not released for an automatic retry of the same session.
    """

    identity = [owner.strip().lower(), recipient.strip().lower(), market_date, "daily_v1"]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _finish_daily_digest(journal: object, delivery_key: str, status: str, *,
                         errors: list[str], owner: str, error_type: str = "") -> None:
    try:
        journal.finish_daily_digest(delivery_key, status, error_type=error_type)
    except Exception as exc:
        # A post-SMTP persistence failure must never retry an accepted message.
        errors.append(f"{owner}: no se pudo completar el registro del resumen "
                      f"({type(exc).__name__}); no se reintentará automáticamente.")


def _model_email_delivery_id(owner: str, kind: str, day: date,
                             subject: str, plain_body: str, html_body: str) -> str:
    """Stable semantic identity; no raw address or email content is persisted."""
    return hashlib.sha256(json.dumps([owner, kind, day.isoformat(),
        subject, plain_body, html_body], ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _record_model_email(journal: object, receipt: object, delivery_id: str, *,
                        owner: str, errors: list[str]) -> None:
    """Optional evidence only; never activate a season or retry a message."""
    if os.getenv("MODEL_RESULTS_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    scoped_owner = os.getenv("MODEL_RESULTS_OWNER", "").strip().lower()
    if scoped_owner and (owner.strip().lower() != scoped_owner
            or str(getattr(journal, "owner", owner)).strip().lower() != scoped_owner):
        return
    try:
        from src.model_results_tracking import record_model_email_evidence

        record_model_email_evidence(journal, receipt, delivery_id)
    except Exception as exc:
        # Recording must not turn an accepted email into a delivery failure or
        # expose provider errors/credentials. Existing delivery semantics stay.
        errors.append(f"{owner}: no se pudo registrar la evidencia de correo del modelo ({type(exc).__name__}); no se reintentó el envío.")


def _virtual_report_summary(value: object) -> dict[str, object]:
    """Copy only the public tracking contract into the private sealed digest.

    Unknown counts remain unknown: a failed collection is never presented as
    zero operations or a successful virtual execution. Raw ledger/provider
    errors, credentials and owner identifiers do not belong in the email.
    """
    if not isinstance(value, Mapping) or not isinstance(value.get("status"), str):
        raise ValueError("A structured virtual tracking status is required")

    def count(raw):
        if isinstance(raw, bool):
            return None
        try:
            number = float(raw)
        except (ValueError, TypeError, OverflowError):
            return None
        return int(number) if isfinite(number) and number >= 0 and number.is_integer() else None

    available = pd.Timestamp(value.get("available_at"))
    if pd.isna(available) or available.tzinfo is None:
        raise ValueError("Virtual collection time must include its timezone")
    output = {"status": value["status"][:80], "available_at": available.tz_convert("UTC").isoformat()}
    for key in ("ready_experiments", "preserved_experiments", "runs", "issues", "ledger_incidents",
                "new_proposals", "new_fills", "pending_proposals", "cumulative_proposals", "cumulative_fills"):
        output[key] = count(value.get(key))
    experiments = []
    for item in value.get("experiments", ()) if isinstance(value.get("experiments"), (list, tuple)) else ():
        if not isinstance(item, Mapping):
            continue
        entry = {key: str(item[key])[:128] for key in ("source", "cohort", "status") if item.get(key) is not None}
        identity = item.get("experiment_id", item.get("id"))
        if identity is not None:
            entry["experiment_id"] = str(identity)[:128]
        for key in ("strategy_accounts", "runs"):
            entry[key] = count(item.get(key))
        if isinstance(item.get("requires_new_experiment"), bool):
            entry["requires_new_experiment"] = item["requires_new_experiment"]
        try:
            budget = float(item.get("budget_usd"))
            entry["budget_usd"] = budget if not isinstance(item.get("budget_usd"), bool) and isfinite(budget) and budget >= 0 else None
        except (ValueError, TypeError, OverflowError):
            entry["budget_usd"] = None
        experiments.append(entry)
    output["experiments"] = experiments
    operations = []
    raw_operations = value.get("operations")
    for item in raw_operations if isinstance(raw_operations, (list, tuple)) else ():
        if (not isinstance(item, Mapping) or item.get("event_type") not in {"order_proposed", "order_filled"}
                or item.get("side") not in {"buy", "sell"} or item.get("is_real_execution") is not False):
            continue
        entry = {key: str(item[key])[:80] for key in ("event_type", "ticker", "side", "strategy", "mode") if item.get(key) is not None}
        entry["is_real_execution"] = False
        for key in ("intended_open_at", "executed_at"):
            if item.get(key) is None:
                continue
            try:
                stamp = pd.Timestamp(item[key])
                if pd.notna(stamp) and stamp.tzinfo is not None:
                    entry[key] = stamp.tz_convert("UTC").isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        for key in ("notional_usd", "price_native"):
            try:
                number = float(item.get(key))
                if not isinstance(item.get(key), bool) and isfinite(number) and number > 0:
                    entry[key] = number
            except (ValueError, TypeError, OverflowError):
                pass
        operations.append(entry)
        if len(operations) == 5:
            break
    output["operations"] = operations
    output["operations_preview_truncated"] = value.get("operations_preview_truncated") is True
    output["operation_unit"] = "simulated_strategy_account_orders"
    return output


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


def _position_costs(journal: object) -> dict[str, float | None]:
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


def _snapshot_positions(journal: object) -> dict[str, str]:
    """Devuelve las posiciones de la última fotografía aptas para analizar.

    No se infiere un precio de entrada desde importes en euros: sólo se incorpora
    el ticker al seguimiento diario y se conserva el nombre para el correo.
    """

    if not hasattr(journal, "list_portfolio_snapshot_positions"):
        return {}
    try:
        stored = journal.list_portfolio_snapshot_positions()
        latest, _ = latest_portfolio_snapshot(stored)
    except (AttributeError, TypeError, ValueError):
        return {}
    if latest.empty or "analysis_ticker" not in latest:
        return {}
    positions: dict[str, str] = {}
    for _, row in latest.iterrows():
        raw_ticker = str(row.get("analysis_ticker") or "").strip()
        if not raw_ticker:
            continue
        try:
            ticker = resolve_analysis_ticker(raw_ticker)
        except ValueError:
            continue
        name = str(row.get("asset_name") or "").strip()
        positions[ticker] = name or ticker
    return positions


def _position_contexts(journal: object, *, errors: list[str] | None = None,
                       owner: str = "") -> dict[str, dict[str, object]]:
    """Lee planes ya guardados; no asigna una nueva estrategia por defecto."""
    if not hasattr(journal, "list_portfolio_snapshot_positions"):
        return {}
    try:
        latest, _ = latest_portfolio_snapshot(journal.list_portfolio_snapshot_positions())
        contexts = position_contexts_from_snapshot(latest.to_dict("records"))
        return {resolve_analysis_ticker(ticker): context for ticker, context in contexts.items()}
    except Exception as exc:
        # Un plan opcional no debe cancelar los avisos del resto del universo.
        if errors is not None:
            errors.append(f"{owner}: planes no disponibles; no se infiere una nueva estrategia ({exc}).")
        return {}


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
    owner: str | None = None,
    journal_factory: Callable[[str], object] = create_journal,
    downloader: Callable[..., pd.DataFrame] = download_prices,
    fundamental_downloader: Callable[[str], dict[str, object]] = (
        download_fundamental_snapshot
    ),
    sender: Callable[..., object] = send_email,
    today: date | None = None,
    require_current_session: bool = False,
    virtual_tracker: Callable[[object, pd.DataFrame], object] | None = None,
) -> AlertRunSummary:
    """Ejecuta el radar diario o una revisión manual de un solo propietario.

    Con ``owner`` sólo se consultan sus preferencias guardadas y se utiliza su
    dirección exacta: no se enumeran ni se modifican las de otros usuarios. Una
    revisión manual sin análisis frescos utilizables no envía ni marca estados.

    ``today`` fija la fecha objetivo de los precios y del informe, no la hora
    real de generación. El CLI conserva así la sesión del día anterior cuando
    GitHub arranca después de medianoche. Los cierres y ``created_at`` siguen
    usando el reloj real; nunca se retrodata una observación.

    ``virtual_tracker`` es una acción opcional explícita, después de guardar las
    señales y antes de sellar el informe/SMTP. Nunca activa temporadas por sí
    mismo este runner; el resultado y sus incidencias forman parte del informe.
    """

    resolved_today = today or datetime.now(ZoneInfo("Europe/Madrid")).date()
    manual_owner: str | None = None
    manual_journal: object | None = None
    if owner is not None:
        if not isinstance(owner, str):
            raise ValueError("La revisión manual necesita un usuario propietario válido.")
        manual_owner = owner.strip().lower()
        if not manual_owner or manual_owner == GROUP_PORTFOLIO_OWNER:
            raise ValueError("La revisión manual necesita un usuario privado propietario.")
        manual_journal = journal_factory(manual_owner)
        if not hasattr(manual_journal, "get_alert_preferences"):
            raise RuntimeError("No se pueden leer las preferencias del usuario.")
        preference = manual_journal.get_alert_preferences()
        if not isinstance(preference, AlertPreferences) or preference.owner != manual_owner:
            raise ValueError("Las preferencias no pertenecen al usuario solicitado.")
        if preference.enabled is not True:
            raise ValueError("Activa los avisos por correo antes de solicitar una revisión manual.")
        recipient = preference.email
        if (
            not isinstance(recipient, str)
            or not recipient
            or recipient != recipient.strip()
            or any(character in recipient for character in "\r\n")
            or not is_valid_email(recipient)
        ):
            raise ValueError("Guarda una única dirección de correo válida para tus avisos.")
        preferences = [preference]
        group_journal = (
            journal_factory(GROUP_PORTFOLIO_OWNER) if preference.include_group else None
        )
    else:
        group_journal = journal_factory(GROUP_PORTFOLIO_OWNER)
        if not hasattr(group_journal, "list_enabled_alert_preferences"):
            raise RuntimeError(
                "La ejecución automática requiere Supabase; SQLite sólo admite pruebas manuales."
            )
        preferences = group_journal.list_enabled_alert_preferences()
    if not preferences:
        return AlertRunSummary(0, 0, 0, 0, ())

    errors: list[str] = []
    group_names = _favorite_names(group_journal) if group_journal is not None else {}
    group_favorites = set(group_names)
    group_positions = _position_costs(group_journal) if group_journal is not None else {}
    group_snapshot_positions = _snapshot_positions(group_journal) if group_journal is not None else {}
    group_contexts = (
        _position_contexts(group_journal, errors=errors, owner=GROUP_PORTFOLIO_OWNER)
        if group_journal is not None else {}
    )
    user_contexts: dict[str, dict[str, dict[str, object]]] = {}
    for ticker, name in group_snapshot_positions.items():
        group_positions.setdefault(ticker, None)
        group_names.setdefault(ticker, name)
    scopes: dict[
        str,
        tuple[
            AlertPreferences,
            object,
            set[str],
            dict[str, float | None],
            dict[str, str],
        ],
    ] = {}
    all_tickers: set[str] = set()
    for preference in preferences:
        try:
            user_journal = (
                manual_journal if manual_owner is not None else journal_factory(preference.owner)
            )
            names = _favorite_names(user_journal)
            favorites = set(names)
            positions = _position_costs(user_journal)
            snapshot_positions = _snapshot_positions(user_journal)
            contexts = _position_contexts(user_journal, errors=errors, owner=preference.owner)
            for ticker, name in snapshot_positions.items():
                positions.setdefault(ticker, None)
                names.setdefault(ticker, name)
            if preference.include_group:
                favorites |= group_favorites
                for ticker, name in group_names.items():
                    names.setdefault(ticker, name)
                for ticker, average_cost in group_positions.items():
                    positions.setdefault(ticker, average_cost)
                for ticker, context in group_contexts.items():
                    contexts.setdefault(ticker, context)
            scope = favorites | set(positions)
            scopes[preference.owner] = (
                preference,
                user_journal,
                scope,
                positions,
                names,
            )
            all_tickers |= scope
            user_contexts[preference.owner] = contexts
        except Exception as exc:
            errors.append(f"{preference.owner}: no se pudo leer su seguimiento ({exc}).")

    frames, download_errors = _download_alert_frames(
        all_tickers,
        today=resolved_today,
        downloader=downloader,
    )
    errors.extend(download_errors)
    reference_frames: dict[str, pd.DataFrame | None] = dict(frames)
    reference_start = resolved_today - timedelta(days=550)

    def reference_frame(symbol: str | None) -> pd.DataFrame | None:
        """Descarga cada referencia una sola vez sin sumarla al universo analizado."""

        if not symbol:
            return None
        normalized = symbol.strip().upper()
        if normalized not in reference_frames:
            try:
                reference_frames[normalized] = downloader(
                    normalized,
                    reference_start,
                    resolved_today,
                    auto_adjust=True,
                )
            except Exception as exc:
                # La comparación es una dimensión opcional: su ausencia no debe
                # impedir que se calcule y envíe el análisis principal.
                reference_frames[normalized] = None
                errors.append(
                    f"{normalized}: no se pudo descargar la referencia ({exc})."
                )
        return reference_frames[normalized]

    config = StrategyConfig()
    growth_config = GrowthMomentumConfig()
    fundamental_cache: dict[str, dict[str, object]] = {}
    fundamental_failures: dict[str, str] = {}
    emails_sent = 0
    alerts_sent = 0
    emails_skipped = 0
    fresh_tickers: set[str] = set()

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
            saved_snapshot_keys: set[tuple[str, date]] = set()
            saved_policy_keys: set[tuple[str, date, str]] = set()
            saved_snapshots = pd.DataFrame()
            if hasattr(journal, "list_analysis_snapshots"):
                try:
                    saved_snapshots = journal.list_analysis_snapshots()
                    if (
                        not saved_snapshots.empty
                        and {"ticker", "analyzed_at"}.issubset(saved_snapshots.columns)
                    ):
                        saved_dates = pd.to_datetime(
                            saved_snapshots["analyzed_at"], errors="coerce"
                        ).dt.date
                        for saved in saved_snapshots.to_dict("records"):
                            policy = decode_policy_note(saved.get("note"))
                            saved_date = pd.to_datetime(saved.get("analyzed_at"), errors="coerce")
                            if policy and pd.notna(saved_date) and policy.get("snapshot_id"):
                                saved_policy_keys.add((str(saved.get("ticker") or "").strip().upper(),
                                                       saved_date.date(), str(policy["snapshot_id"])))
                        canonical_scores = {
                            "company_score",
                            "entry_score",
                            "valuation_score",
                            "relative_score",
                            "risk_score",
                        }
                        if canonical_scores.issubset(saved_snapshots.columns):
                            complete = pd.Series(True, index=saved_snapshots.index)
                            for column in canonical_scores:
                                complete &= pd.to_numeric(
                                    saved_snapshots[column], errors="coerce"
                                ).notna()
                        else:
                            complete = pd.Series(False, index=saved_snapshots.index)
                        saved_snapshot_keys = {
                            (str(ticker).strip().upper(), saved_date)
                            for ticker, saved_date, is_complete in zip(
                                saved_snapshots["ticker"], saved_dates, complete,
                                strict=True,
                            )
                            if (
                                bool(is_complete)
                                and str(ticker).strip()
                                and pd.notna(saved_date)
                            )
                        }
                except Exception as exc:
                    errors.append(f"{owner}: no se pudo leer el historial "
                                  f"({type(exc).__name__}); no se enviará un resumen sin verificarlo.")
                    continue
            else:
                errors.append(f"{owner}: el historial diario no está disponible; no se enviará el resumen.")
                continue
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
                    freshness = quote_freshness(signal.as_of, reference_date=resolved_today)
                    if not freshness.fresh:
                        overview_rows.append(DailyOverviewRow(
                            ticker=ticker,
                            company_name=names.get(ticker, ticker),
                            held=held,
                            price=float(frame["close"].iloc[-1]),
                            as_of=str(signal.as_of.date()),
                            technical_score=None,
                            technical_label="Datos antiguos",
                            position_label="Actualizar datos",
                            portfolio_decision="Actualizar datos",
                            data_note=freshness.status,
                        ))
                        errors.append(f"{owner} / {ticker}: {freshness.status}.")
                        continue
                    price = float(frame["close"].iloc[-1])
                    if manual_owner is not None and (not isfinite(price) or price <= 0):
                        raise ValueError("El precio reciente no es válido para una revisión manual.")
                    fresh_tickers.add(ticker)
                    state_signature = signal_signature(signal, held=held)
                    candidate = None
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
                    elif info.get("_warnings"):
                        data_notes.append("fundamentales parciales: alguna fuente no pudo verificar las cuentas")

                    quick_fundamental = None
                    fundamental_error = ""
                    try:
                        quick_fundamental = evaluate_fundamental_filter(info, ticker)
                        if quick_fundamental.score is None:
                            data_notes.append("calidad fundamental parcial")
                    except (KeyError, TypeError, ValueError) as exc:
                        fundamental_error = str(exc) or "No se pudo completar el filtro fundamental."
                        data_notes.append("calidad fundamental incompleta")

                    risk = None
                    try:
                        risk = evaluate_risk(ticker, frame)
                    except (KeyError, TypeError, ValueError):
                        data_notes.append("riesgo parcial")

                    broad_name = benchmark_for_ticker(ticker)
                    sector_name = sector_benchmark(
                        str(info.get("sector") or ""),
                        ticker,
                    )
                    broad_market = reference_frame(broad_name)
                    sector_market = reference_frame(sector_name)
                    relative = None
                    try:
                        relative = evaluate_relative_strength(
                            ticker,
                            frame,
                            broad_market,
                            broad_name=broad_name,
                            sector=sector_market,
                            sector_name=sector_name,
                        )
                        if relative.score is None:
                            data_notes.append("fuerza relativa parcial")
                    except (KeyError, TypeError, ValueError):
                        data_notes.append("fuerza relativa parcial")

                    growth = None
                    growth_error = ""
                    try:
                        growth = evaluate_growth_momentum(
                            ticker=ticker,
                            frame=frame,
                            info=info,
                            relative=relative,
                            risk=risk,
                            broad_market=broad_market,
                            config=growth_config,
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        growth_error = str(exc) or "No se pudo completar crecimiento y momentum."
                        data_notes.append("crecimiento parcial")

                    enhanced = None
                    fundamental = None
                    valuation = None
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
                                relative_score=(
                                    relative.score if relative is not None else None
                                ),
                                relative_coverage=(
                                    relative.coverage_pct
                                    if relative is not None
                                    else 0
                                ),
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

                    confidence = (
                        getattr(enhanced, "confidence_pct", None)
                        if enhanced is not None
                        else None
                    )
                    context = user_contexts.get(owner, {}).get(ticker, {})
                    policy_row = {
                        "Ticker": ticker, "Fecha": str(signal.as_of.date()),
                        "Fuente": "Yahoo Finance / yfinance · cierre diario",
                        "Precio": price,
                        "Moneda": info.get("currency") or frame.attrs.get("quote_currency") or "",
                        "Sector": info.get("sector") or "",
                        "Si ya la tienes": signal.position_label,
                        "Lectura entrada": signal.label,
                        "Calidad empresa": getattr(fundamental, "score", None),
                        "Valoración": getattr(valuation, "score", None),
                        "Riesgo controlado": getattr(risk, "score", None),
                        "Cobertura fundamental": getattr(fundamental, "coverage_pct", None),
                        "Incidencia principal": growth_error,
                        "Incidencia comparativa": fundamental_error,
                    }
                    policy = build_policy_snapshot(
                        policy_row, growth_result=growth,
                        fundamental_filter=quick_fundamental,
                        position_context=context, held=held,
                        config={"growth": asdict(growth_config), "technical": asdict(config)},
                        fundamental_info=info,
                    )
                    if not held:
                        state_signature = f"entry:growth_momentum:{policy['primary']['status']}"
                    position_decision = None
                    if held:
                        position_decision = build_portfolio_decision_rows(
                            [{
                                "Ticker": ticker,
                                "Si ya la tienes": signal.position_label,
                                "Lectura entrada": signal.label,
                                "Oportunidad": getattr(enhanced, "opportunity_score", None),
                                "Calidad empresa": getattr(fundamental, "score", None),
                                "Riesgo controlado": getattr(risk, "score", None),
                                "Confianza datos": confidence,
                                "Motivo posición": signal.explanation,
                                "Fecha": str(signal.as_of.date()),
                                "Tesis invalidada": context.get("invalidated", False),
                                "Motivo tesis": context.get("invalidation_reason") or "",
                            }],
                            [ticker],
                        )[0]
                        state_signature = f"position:{position_decision['Decisión']}"
                    candidate = build_alert_candidate(
                        signal,
                        price=price,
                        held=held,
                        preferences=preference,
                        company_name=company_name,
                        position_decision=position_decision,
                        policy_snapshot=policy,
                        strategy_id=(str(policy["position"]["strategy_id"]) if held else "growth_momentum"),
                        strategy_label=(str(policy["position"]["strategy_label"]) if held else "Crecimiento + momentum"),
                        horizon=str(policy["position"]["horizon"]),
                        data_source=str(policy["source"] or "Fuente no documentada"),
                        data_limitations="; ".join(dict.fromkeys(data_notes)),
                        next_step=(str(position_decision.get("Confirmar antes") or "Contrastar tesis y exposición.")
                                   if position_decision else "Confirmar precio, tamaño, liquidez y próximos resultados; no es una orden."),
                        previous_signature=previous_signatures.get(ticker),
                    )

                    if candidate is not None:
                        candidate = replace(candidate, company_name=company_name)
                    if candidate is not None and candidate.kind == "Compra":
                        if policy["primary"]["entry_candidate"]:
                            # El enfoque principal conserva sus propias reglas: una
                            # nota comparativa discrepante no obliga a un consenso.
                            candidate = replace(
                                candidate,
                                timing_score=(getattr(getattr(enhanced, "timing", None), "score", None)),
                                opportunity_score=getattr(enhanced, "opportunity_score", None),
                                opportunity_status=str(getattr(enhanced, "status_label", "") or ""),
                                preferred_entry=str(getattr(getattr(getattr(enhanced, "zones", None), "preferred_entry", None), "label", "") or ""),
                                event_label=str(getattr(getattr(enhanced, "event", None), "label", "") or ""),
                                fundamental_filter_score=getattr(quick_fundamental, "score", None),
                                fundamental_filter_label=str(getattr(quick_fundamental, "label", "") or ""),
                                signature=f"entry:growth_momentum:{policy['primary']['status']}",
                            )
                            state_signature = candidate.signature
                        elif enhanced is None:
                            state_signature = (
                                f"entry:{signal.label}:OPORTUNIDAD_INCOMPLETA"
                            )
                            candidate = None
                        else:
                            candidate = replace(
                                candidate,
                                timing_score=enhanced.timing.score,
                                opportunity_score=getattr(enhanced, "opportunity_score", None),
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
                            if (
                                enhanced.status_code != STATUS_BUYABLE
                                or confidence is None
                                or float(confidence) < 60.0
                            ):
                                candidate = None
                                if enhanced.status_code == STATUS_BUYABLE:
                                    state_signature = f"entry:{signal.label}:DATOS_INSUFICIENTES"
                    changed, change_kind, previous_state, current_state = (
                        describe_state_change(
                            previous_signatures.get(ticker),
                            state_signature,
                            held=held,
                        )
                    )
                    if candidate is not None:
                        candidate = replace(candidate, previous_state=previous_state,
                                            current_state=current_state,
                                            change_reason=(str(position_decision.get("Motivo") or "")
                                                           if position_decision else str(policy["primary"]["entry_reading"])))
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
                            portfolio_decision=(
                                str(position_decision["Decisión"])
                                if position_decision is not None else ""
                            ),
                            decision_reason=(
                                str(position_decision["Motivo"])
                                if position_decision is not None else ""
                            ),
                            confidence_pct=confidence,
                            policy_snapshot=policy,
                            strategy_id=(str(policy["position"]["strategy_id"]) if held else "growth_momentum"),
                            strategy_label=(str(policy["position"]["strategy_label"]) if held else "Crecimiento + momentum"),
                            horizon=str(policy["position"]["horizon"]),
                            data_source=str(policy["source"] or "Fuente no documentada"),
                            data_limitations="; ".join(dict.fromkeys(data_notes)),
                            next_step=(str(position_decision.get("Confirmar antes") or "Contrastar tesis y exposición.")
                                       if position_decision else str(policy["primary"]["entry_reading"])),
                        )
                    overview_rows.append(overview_row)
                    snapshot_key = (ticker, pd.Timestamp(signal.as_of).date())
                    policy_key = (*snapshot_key, str(policy["snapshot_id"]))
                    if (
                        policy_key not in saved_policy_keys
                        and enhanced is not None
                        and hasattr(journal, "add_analysis_snapshot")
                    ):
                        try:
                            journal.add_analysis_snapshot(
                                ticker=ticker,
                                analyzed_at=signal.as_of,
                                price=price,
                                opportunity_score=enhanced.opportunity_score,
                                confidence_pct=getattr(
                                    enhanced, "confidence_pct", None
                                ),
                                company_score=(
                                    fundamental.score
                                    if fundamental is not None
                                    else None
                                ),
                                entry_score=signal.score,
                                valuation_score=(
                                    valuation.score
                                    if valuation is not None
                                    else None
                                ),
                                relative_score=(
                                    relative.score if relative is not None else None
                                ),
                                risk_score=(risk.score if risk is not None else None),
                                opportunity_label=str(getattr(enhanced, "status_label", "") or ""),
                                entry_label=signal.label,
                                position_label=signal.position_label,
                                sector=str(
                                    (fundamental.sector if fundamental is not None else "")
                                    or info.get("sector")
                                    or ""
                                ),
                                explanation=str(getattr(enhanced, "explanation", "") or policy["primary"]["entry_reading"]),
                                note=encode_policy_note("Actualización automática del radar diario", policy),
                            )
                            saved_snapshot_keys.add(snapshot_key)
                            saved_policy_keys.add(policy_key)
                        except Exception:
                            data_notes.append("historial no guardado")
                            overview_row = replace(
                                overview_row,
                                data_note=", ".join(dict.fromkeys(data_notes)),
                            )
                            overview_rows[-1] = overview_row
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

            if manual_owner is not None and not evaluated:
                errors.append(
                    f"{owner}: no se envió la revisión manual porque no hay análisis "
                    "frescos utilizables; actualiza los datos e inténtalo de nuevo."
                )
                continue

            selected = filter_changed_candidates(
                candidates,
                previous_signatures,
                only_changes=preference.only_changes,
            )
            # Preserve raw engine scores, but do not promote an unverified
            # fundamental photograph into a new-purchase email. Holding-risk
            # reviews keep their independent rules and are never converted into
            # sells merely because company data is missing or outdated.
            blocked_entry_tickers = {candidate.ticker for candidate in candidates if (
                not candidate.held and str(candidate.kind).lower() in {"compra", "buy"}
                and (candidate.policy_snapshot or {}).get("data_quality", {}).get("block_entry") is not False
            )}
            selected = [candidate for candidate in selected if (
                candidate.held or str(candidate.kind).lower() not in {"compra", "buy"}
                or (candidate.policy_snapshot or {}).get("data_quality", {}).get("block_entry") is False
            )]
            pending_tickers = {candidate.ticker for candidate in selected} | blocked_entry_tickers
            selected_by_ticker: dict[str, AlertCandidate] = {}
            for candidate in sorted(selected,
                    key=lambda c: (not c.held, c.kind not in {"Vender", "Reducir", "Venta"}, c.ticker)):
                selected_by_ticker.setdefault(candidate.ticker, candidate)
            selected = list(selected_by_ticker.values())[:5]
            actionable_delivered = False
            if overview_rows:
                # Read what was actually saved, not a separate in-memory score.
                # The application opens this exact immutable report by its ID.
                try:
                    stored = journal.list_analysis_snapshots()
                    observed_dates = [pd.Timestamp(signal.as_of).date()
                                      for signal, *_ in evaluated]
                    report_date = max(observed_dates).isoformat() if observed_dates else resolved_today.isoformat()
                    if require_current_session and report_date != resolved_today.isoformat():
                        emails_skipped += 1
                        continue
                    if require_current_session and not evaluated:
                        emails_skipped += 1
                        continue
                    persisted_ids = {str(policy.get("snapshot_id"))
                        for saved in stored.to_dict("records")
                        if (policy := decode_policy_note(saved.get("note"))) and policy.get("snapshot_id")}
                    selected = [candidate for candidate in selected
                        if not (identity := str((candidate.policy_snapshot or {}).get("snapshot_id") or ""))
                        or identity in persisted_ids]
                    report = build_daily_score_report(
                        stored, tickers=scope, held_tickers=set(positions),
                        company_names=names, as_of=report_date,
                    )
                    report_date = str(report.get("as_of") or "")
                    current_sessions = {(str(row.get("ticker") or ""), str(row.get("session") or ""))
                                        for row in report.get("rows", []) if row.get("session")}
                    if (not report_date or evaluated and not any(
                            (str(signal.ticker), str(pd.Timestamp(signal.as_of).date())) in current_sessions
                            for signal, *_ in evaluated)):
                        emails_skipped += 1
                        if manual_owner is not None:
                            errors.append(f"{owner}: no hay un cierre nuevo verificable; "
                                          "las cotizaciones de una sesión abierta no se envían como resumen del cierre.")
                        continue
                    # A persisted policy may still be intraday, superseded or
                    # excluded by the completed-session gate. Only the exact
                    # accepted row may supply an alert in a closing report.
                    # Omitted candidates stay pending in notification state.
                    accepted_rows = {
                        (str(row.get("ticker") or ""),
                         str(row.get("snapshot_id") or ""),
                         str(row.get("session") or ""))
                        for row in report.get("rows", [])
                        if row.get("snapshot_id") and row.get("session")
                    }
                    if require_current_session and (
                            report_date != resolved_today.isoformat()
                            or not any(session == report_date for _, _, session in accepted_rows)):
                        emails_skipped += 1
                        continue
                    selected = [candidate for candidate in selected if (
                        candidate.ticker,
                        str((candidate.policy_snapshot or {}).get("snapshot_id") or ""),
                        str(candidate.as_of)[:10],
                    ) in accepted_rows]
                    report["incidents"] = [
                        {"ticker": item.ticker, "as_of": item.as_of,
                         "reason": item.data_note, "next_step": "Actualizar datos antes de tomar decisiones."}
                        for item in overview_rows if item.data_note
                    ]
                    report["positions"] = [
                        {"ticker": item.ticker, "reading": item.portfolio_decision,
                         "reason": item.decision_reason, "next_step": item.next_step}
                        for item in overview_rows if item.held and item.portfolio_decision and (
                            item.ticker,
                            str((item.policy_snapshot or {}).get("snapshot_id") or ""),
                            str(item.as_of)[:10],
                        ) in accepted_rows
                    ]
                    if require_current_session:
                        selected = [candidate for candidate in selected
                                    if str(candidate.as_of)[:10] == report_date]
                    report["alerts"] = [
                        {**{key: getattr(candidate, key) for key in (
                            "ticker", "company_name", "held", "kind", "title", "change_reason",
                            "next_step", "strategy_label", "horizon", "data_source", "data_limitations", "as_of",
                            "opportunity_status", "event_label",
                        )},
                         "positive_factors": list((candidate.policy_snapshot or {}).get("primary", {}).get("positive_factors", [])),
                         "risk_factors": list((candidate.policy_snapshot or {}).get("primary", {}).get("risk_factors", [])),
                         "policy_version": str((candidate.policy_snapshot or {}).get("policy_version") or "")}
                        for candidate in sorted(selected,
                            key=lambda c: (not c.held, c.kind not in {"Vender", "Reducir", "Venta"}, c.ticker))
                    ]
                    if virtual_tracker is not None:
                        try:
                            virtual = virtual_tracker(journal, stored.copy(deep=True))
                            # A configured owner-scoped callback may decline
                            # other owners. Their existing emails stay unchanged.
                            if virtual is not None:
                                report["virtual_tracking"] = _virtual_report_summary(virtual)
                                state = report["virtual_tracking"]
                                if state["status"] in {"error", "partial_error", "partial_failure"} or (state.get("issues") or 0) > 0:
                                    errors.append(f"{owner}: el seguimiento virtual comunica incidencias; "
                                                  "el correo conserva ese estado sin confirmar una actualización completa.")
                        except Exception as exc:
                            report["virtual_tracking"] = {
                                "status": "error", "available_at": pd.Timestamp.now(tz="UTC").isoformat(),
                                "experiments": [], "issues": 1, "new_proposals": None,
                                "new_fills": None, "pending_proposals": None,
                            }
                            errors.append(f"{owner}: actualización virtual incompleta ({type(exc).__name__}); "
                                          "el informe no confirma operaciones del laboratorio.")
                    report = seal_daily_score_report(report)
                    required_methods = ("save_daily_score_report", "claim_daily_digest", "finish_daily_digest")
                    if any(not callable(getattr(journal, name, None)) for name in required_methods):
                        raise RuntimeError("El almacenamiento aún no tiene el control de envíos diarios")
                    report_id = journal.save_daily_score_report(report)
                    if report_id != report["report_id"]:
                        raise RuntimeError("La identidad del informe guardado no coincide")
                    subject, plain_body, html_body = render_daily_score_email(
                        report, app_url=f"https://stock-signal-lab-ddriu.streamlit.app/?digest={report_id}",
                    )
                    delivery_key = _daily_digest_delivery_key(owner, preference.email, report_date)
                    claimed = journal.claim_daily_digest(delivery_key, report_date, report_id)
                except Exception as exc:
                    errors.append(f"{owner}: no se pudo preparar o reservar el resumen diario "
                                  f"({type(exc).__name__}); no se envió correo.")
                    claimed = False
                else:
                    if not claimed:
                        emails_skipped += 1
                if claimed:
                    delivery_id = _model_email_delivery_id(owner, "daily_v1", date.fromisoformat(report_date),
                        subject, plain_body, html_body)
                    try:
                        receipt = sender(preference.email, subject, plain_body, html_body)
                    except Exception as exc:
                        receipt = getattr(exc, "smtp_receipt", None)
                        evidence = receipt or {"status": "failed", "error_type": type(exc).__name__}
                        accepted = getattr(receipt, "status", None) == "smtp_accepted"
                        _finish_daily_digest(journal, delivery_key, "sent" if accepted else "uncertain",
                            errors=errors, owner=owner, error_type=type(exc).__name__)
                        # A transport error after DATA acceptance is not an
                        # invitation to send a second copy of the same session.
                        errors.append(f"{owner}: el envío del resumen requiere revisión "
                                      f"({type(exc).__name__}); no se reintentará automáticamente.")
                        if accepted:
                            actionable_delivered = True
                            emails_sent += 1
                            alerts_sent += len(selected)
                    else:
                        evidence = receipt
                        status = "sent" if getattr(receipt, "status", None) == "smtp_accepted" else "uncertain"
                        _finish_daily_digest(journal, delivery_key, status, errors=errors, owner=owner)
                        actionable_delivered = True
                        emails_sent += 1
                        alerts_sent += len(selected)
                    _record_model_email(journal,
                        evidence,
                        delivery_id, owner=owner, errors=errors)

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
                    position_label=overview.portfolio_decision or None,
                )
                for signal, price, held, signature, overview in evaluated
                if signal.ticker not in (pending_tickers - notified_tickers)
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
        tickers_with_fresh_prices=len(fresh_tickers),
        emails_skipped=emails_skipped,
    )
