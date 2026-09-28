"""Persistencia local del diario de operaciones en SQLite."""

from __future__ import annotations

import json
import math
import sqlite3
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from src.alerts import (
    ALERT_STATE_COLUMNS,
    AlertPreferences,
    AlertState,
    normalize_alert_preferences,
    preferences_from_mapping,
)
from src.favorite_tags import serialize_favorite_tags


OPERATION_COLUMNS = [
    "id",
    "ticker",
    "account_name",
    "side",
    "quantity",
    "price",
    "fees",
    "settlement_amount_eur",
    "fee_eur",
    "fx_rate_to_eur",
    "executed_at",
    "notes",
    "currency",
    "recorded_by",
    "created_at",
]

FAVORITE_COLUMNS = [
    "id",
    "ticker",
    "name",
    "exchange",
    "tags",
    "recorded_by",
    "created_at",
]
ANALYSIS_SNAPSHOT_COLUMNS = [
    "id",
    "ticker",
    "analyzed_at",
    "price",
    "opportunity_score",
    "confidence_pct",
    "company_score",
    "entry_score",
    "valuation_score",
    "relative_score",
    "risk_score",
    "opportunity_label",
    "entry_label",
    "position_label",
    "expected_return_pct",
    "positive_rate_pct",
    "expected_price",
    "horizon_days",
    "sector",
    "explanation",
    "note",
    "created_at",
]
PRIVATE_INVESTMENT_COLUMNS = [
    "id",
    "platform",
    "project_name",
    "invested_amount",
    "current_value",
    "expected_return_pct",
    "start_date",
    "maturity_date",
    "status",
    "notes",
    "recorded_by",
    "created_at",
]
PRIVATE_INVESTMENT_PLATFORMS = ("Civislend", "Segofactoring")
PRIVATE_INVESTMENT_STATUSES = ("Activa", "Finalizada", "Retrasada", "Impagada")
PORTFOLIO_ACCOUNT_COLUMNS = [
    "id",
    "account_name",
    "account_type",
    "investments_value",
    "cash_balance",
    "currency",
    "status",
    "notes",
    "updated_at",
    "created_at",
]
PORTFOLIO_ACCOUNT_TYPES = ("Bróker", "Inversión alternativa")
PORTFOLIO_ACCOUNT_STATUSES = ("Pendiente de actualizar", "Actualizada", "Inactiva")
PORTFOLIO_SNAPSHOT_COLUMNS = [
    "id",
    "snapshot_date",
    "platform",
    "asset_name",
    "raw_identifier",
    "analysis_ticker",
    "asset_type",
    "portfolio_block",
    "quantity",
    "current_price",
    "currency",
    "value_eur",
    "return_pct",
    "cost_estimate_eur",
    "gain_loss_eur",
    "comments",
    "source",
    "notes",
    "recorded_by",
    "created_at",
    "updated_at",
]
PAPER_SIMULATION_COLUMNS = [
    "id",
    "name",
    "status",
    "base_currency",
    "benchmark_ticker",
    "strategy_key",
    "engine_version",
    "start_date",
    "initial_nav_eur",
    "initial_cash_eur",
    "source_snapshot_date",
    "assumptions_json",
    "initial_positions_json",
    "recorded_by",
    "created_at",
    "updated_at",
]
PAPER_DAILY_RUN_COLUMNS = [
    "id",
    "simulation_id",
    "market_date",
    "signal_as_of",
    "input_hash",
    "status",
    "proposed_actions_json",
    "executed_actions_json",
    "positions_after_json",
    "cash_eur",
    "net_nav_eur",
    "hold_nav_eur",
    "benchmark_nav_eur",
    "realized_pnl_eur",
    "unrealized_pnl_eur",
    "cumulative_costs_eur",
    "tax_reserve_eur",
    "coverage_pct",
    "warnings_json",
    "rejected_json",
    "engine_version",
    "created_at",
    "updated_at",
]
PAPER_SIMULATION_STATUSES = ("active", "paused", "archived")
PAPER_DAILY_RUN_STATUSES = ("complete", "partial", "failed")
PAPER_SIMULATION_JSON_COLUMNS = (
    "assumptions_json",
    "initial_positions_json",
)
PAPER_DAILY_RUN_JSON_COLUMNS = (
    "proposed_actions_json",
    "executed_actions_json",
    "positions_after_json",
    "warnings_json",
    "rejected_json",
)
DEFAULT_DDRIU_ACCOUNTS = (
    ("MyInvestor", "Bróker"),
    ("Trade Republic", "Bróker"),
    ("Revolut", "Bróker"),
    ("Segofactoring", "Inversión alternativa"),
    ("Civislend", "Inversión alternativa"),
)
MAX_FAVORITES = 300


def default_database_path() -> Path:
    """Usa el proyecto en desarrollo y una carpeta privada al estar instalado."""

    configured = os.environ.get("STOCK_SIGNAL_LAB_DATA_DIR")
    if configured:
        return Path(configured).expanduser() / "trading_journal.db"
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
            return base / "StockSignalLab" / "trading_journal.db"
        return Path.home() / ".stock_signal_lab" / "trading_journal.db"
    return Path(__file__).resolve().parents[1] / "data" / "trading_journal.db"


DEFAULT_DATABASE = default_database_path()


def _paper_json_default(value: object) -> object:
    """Convierte sólo tipos escalares reproducibles para el diario paper."""

    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    raise TypeError(f"{type(value).__name__} no se puede guardar como JSON paper.")


def serialize_stable_json(
    value: object,
    *,
    expected: str,
) -> str:
    """Serializa JSON de forma canónica para hashes, SQLite y Supabase.

    También acepta una cadena JSON ya serializada, pero la vuelve a codificar
    para eliminar diferencias de espacios u orden de claves.
    """

    if expected not in {"object", "array"}:
        raise ValueError("El tipo JSON esperado debe ser object o array.")
    if value is None or (isinstance(value, str) and not value.strip()):
        parsed: object = {} if expected == "object" else []
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("El contenido paper no es JSON válido.") from exc
    else:
        parsed = value

    if expected == "object":
        if not isinstance(parsed, Mapping):
            raise ValueError("El contenido paper debe ser un objeto JSON.")
        normalized: object = dict(parsed)
    else:
        if isinstance(parsed, (str, bytes)) or not isinstance(parsed, Sequence):
            raise ValueError("El contenido paper debe ser una lista JSON.")
        normalized = list(parsed)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_paper_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("El contenido paper no se puede serializar de forma estable.") from exc


def _paper_date(value: date | datetime | str, label: str) -> str:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"La {label} paper no es válida.") from exc
    if pd.isna(parsed):
        raise ValueError(f"La {label} paper no es válida.")
    return parsed.date().isoformat()


def _paper_datetime(value: date | datetime | str | None, label: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"La {label} paper no es válida.") from exc
    if pd.isna(parsed):
        raise ValueError(f"La {label} paper no es válida.")
    return parsed.isoformat()


def _paper_number(
    value: object,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"El valor paper de {label} no es válido.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"El valor paper de {label} debe ser finito.")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"El valor paper de {label} no puede ser menor que {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"El valor paper de {label} no puede superar {maximum}.")
    return parsed


def normalize_paper_simulation(
    *,
    name: str,
    start_date: date | datetime | str,
    initial_nav_eur: float,
    initial_positions: object,
    assumptions: object | None = None,
    initial_cash_eur: float = 0.0,
    source_snapshot_date: date | datetime | str | None = None,
    status: str = "active",
    base_currency: str = "EUR",
    benchmark_ticker: str = "SPY",
    strategy_key: str = "strict_rotation_v1",
    engine_version: str = "paper-v1",
) -> dict[str, object]:
    """Valida la cabecera inmutable de una simulación aislada."""

    normalized_name = str(name or "").strip()[:160]
    if not normalized_name:
        raise ValueError("La simulación paper necesita un nombre.")
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in PAPER_SIMULATION_STATUSES:
        raise ValueError("El estado de la simulación paper no es válido.")
    currency = str(base_currency or "").strip().upper()
    if len(currency) != 3:
        raise ValueError("La moneda base paper debe tener tres letras.")
    benchmark = str(benchmark_ticker or "").strip().upper()
    strategy = str(strategy_key or "").strip()[:120]
    version = str(engine_version or "").strip()[:120]
    if not benchmark or not strategy or not version:
        raise ValueError("Benchmark, estrategia y versión paper son obligatorios.")
    nav = _paper_number(initial_nav_eur, "NAV inicial", minimum=0.0000001)
    cash = _paper_number(initial_cash_eur, "efectivo inicial", minimum=0.0)
    if cash > nav + 1e-9:
        raise ValueError("El efectivo inicial no puede superar el NAV inicial.")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "name": normalized_name,
        "status": normalized_status,
        "base_currency": currency,
        "benchmark_ticker": benchmark,
        "strategy_key": strategy,
        "engine_version": version,
        "start_date": _paper_date(start_date, "fecha inicial"),
        "initial_nav_eur": nav,
        "initial_cash_eur": cash,
        "source_snapshot_date": (
            _paper_date(source_snapshot_date, "fecha de fotografía")
            if source_snapshot_date not in {None, ""}
            else None
        ),
        "assumptions_json": serialize_stable_json(
            assumptions, expected="object"
        ),
        "initial_positions_json": serialize_stable_json(
            initial_positions, expected="array"
        ),
        "created_at": now,
        "updated_at": now,
    }


def normalize_paper_daily_run(
    *,
    simulation_id: int,
    market_date: date | datetime | str,
    signal_as_of: date | datetime | str | None,
    input_hash: str,
    positions_after: object,
    proposed_actions: object | None = None,
    executed_actions: object | None = None,
    warnings: object | None = None,
    rejected: object | None = None,
    cash_eur: float,
    net_nav_eur: float,
    hold_nav_eur: float,
    benchmark_nav_eur: float,
    realized_pnl_eur: float = 0.0,
    unrealized_pnl_eur: float = 0.0,
    cumulative_costs_eur: float = 0.0,
    tax_reserve_eur: float = 0.0,
    coverage_pct: float = 0.0,
    status: str = "complete",
    engine_version: str = "paper-v1",
) -> dict[str, object]:
    """Valida una fotografía diaria paper, idempotente por sesión y fecha."""

    try:
        normalized_simulation_id = int(simulation_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("La ejecución paper necesita una simulación válida.") from exc
    if normalized_simulation_id <= 0:
        raise ValueError("La ejecución paper necesita una simulación válida.")
    normalized_hash = str(input_hash or "").strip()[:256]
    version = str(engine_version or "").strip()[:120]
    normalized_status = str(status or "").strip().lower()
    if not normalized_hash or not version:
        raise ValueError("El hash de entrada y la versión paper son obligatorios.")
    if normalized_status not in PAPER_DAILY_RUN_STATUSES:
        raise ValueError("El estado de la ejecución paper no es válido.")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "simulation_id": normalized_simulation_id,
        "market_date": _paper_date(market_date, "fecha de mercado"),
        "signal_as_of": _paper_datetime(signal_as_of, "fecha de señal"),
        "input_hash": normalized_hash,
        "status": normalized_status,
        "proposed_actions_json": serialize_stable_json(
            proposed_actions, expected="array"
        ),
        "executed_actions_json": serialize_stable_json(
            executed_actions, expected="array"
        ),
        "positions_after_json": serialize_stable_json(
            positions_after, expected="array"
        ),
        "cash_eur": _paper_number(cash_eur, "efectivo", minimum=0.0),
        "net_nav_eur": _paper_number(net_nav_eur, "NAV neto", minimum=0.0),
        "hold_nav_eur": _paper_number(hold_nav_eur, "NAV mantener", minimum=0.0),
        "benchmark_nav_eur": _paper_number(
            benchmark_nav_eur, "NAV del benchmark", minimum=0.0
        ),
        "realized_pnl_eur": _paper_number(realized_pnl_eur, "resultado realizado"),
        "unrealized_pnl_eur": _paper_number(
            unrealized_pnl_eur, "resultado latente"
        ),
        "cumulative_costs_eur": _paper_number(
            cumulative_costs_eur, "costes acumulados", minimum=0.0
        ),
        "tax_reserve_eur": _paper_number(
            tax_reserve_eur, "reserva fiscal", minimum=0.0
        ),
        "coverage_pct": _paper_number(
            coverage_pct, "cobertura", minimum=0.0, maximum=100.0
        ),
        "warnings_json": serialize_stable_json(warnings, expected="array"),
        "rejected_json": serialize_stable_json(rejected, expected="array"),
        "engine_version": version,
        "created_at": now,
        "updated_at": now,
    }


def normalize_operation(
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    fees: float,
    executed_at: date | datetime | str,
    notes: str = "",
    currency: str = "EUR",
    account_name: str = "",
    settlement_amount_eur: float | None = None,
    fee_eur: float | None = None,
    fx_rate_to_eur: float | None = None,
) -> dict[str, object]:
    """Valida una operación y devuelve valores normalizados para cualquier backend."""

    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("La operación necesita un ticker.")
    if side not in {"Compra", "Venta"}:
        raise ValueError("El tipo debe ser Compra o Venta.")
    if quantity <= 0 or price <= 0 or fees < 0:
        raise ValueError("Cantidad/precio deben ser positivos y las comisiones no negativas.")
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3:
        raise ValueError("La moneda debe tener tres letras, por ejemplo EUR o USD.")
    normalized_account = account_name.strip()[:120]
    normalized_settlement = (
        float(settlement_amount_eur)
        if settlement_amount_eur is not None and pd.notna(settlement_amount_eur)
        else None
    )
    normalized_fee_eur = (
        float(fee_eur) if fee_eur is not None and pd.notna(fee_eur) else None
    )
    normalized_fx = (
        float(fx_rate_to_eur)
        if fx_rate_to_eur is not None and pd.notna(fx_rate_to_eur)
        else None
    )
    if normalized_settlement is not None and normalized_settlement <= 0:
        raise ValueError("El importe liquidado en euros debe ser positivo.")
    if normalized_fee_eur is not None and normalized_fee_eur < 0:
        raise ValueError("La comisión en euros no puede ser negativa.")
    if normalized_fx is not None and normalized_fx <= 0:
        raise ValueError("El tipo de cambio a euros debe ser positivo.")

    local_settlement = (
        float(quantity) * float(price) + float(fees)
        if side == "Compra"
        else float(quantity) * float(price) - float(fees)
    )
    if local_settlement <= 0:
        raise ValueError("La liquidación de la operación debe ser positiva.")
    if normalized_currency == "EUR":
        normalized_settlement = normalized_settlement or local_settlement
        normalized_fee_eur = (
            float(fees) if normalized_fee_eur is None else normalized_fee_eur
        )
        normalized_fx = 1.0
    elif normalized_settlement is None and normalized_fx is not None:
        normalized_settlement = local_settlement * normalized_fx
    elif normalized_settlement is not None and normalized_fx is None:
        normalized_fx = normalized_settlement / local_settlement

    return {
        "ticker": normalized_ticker,
        "account_name": normalized_account,
        "side": side,
        "quantity": float(quantity),
        "price": float(price),
        "fees": float(fees),
        "settlement_amount_eur": normalized_settlement,
        "fee_eur": normalized_fee_eur,
        "fx_rate_to_eur": normalized_fx,
        "executed_at": pd.Timestamp(executed_at).isoformat(),
        "notes": notes.strip(),
        "currency": normalized_currency,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_favorite(
    ticker: str,
    name: str = "",
    exchange: str = "",
    tags: object = "",
) -> dict[str, str]:
    """Valida una empresa favorita para cualquier backend de almacenamiento."""

    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("La empresa favorita necesita un ticker.")
    return {
        "ticker": normalized_ticker,
        "name": name.strip() or normalized_ticker,
        "exchange": exchange.strip(),
        "tags": serialize_favorite_tags(tags),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_analysis_snapshot(
    *,
    ticker: str,
    analyzed_at: date | datetime | str,
    price: float,
    opportunity_score: int,
    confidence_pct: int | float | None = None,
    company_score: int | None,
    entry_score: int,
    valuation_score: int | None,
    relative_score: int | None,
    risk_score: int | None,
    opportunity_label: str,
    entry_label: str,
    position_label: str,
    expected_return_pct: float | None = None,
    positive_rate_pct: float | None = None,
    expected_price: float | None = None,
    horizon_days: int | None = None,
    sector: str = "",
    explanation: str = "",
    note: str = "",
) -> dict[str, object]:
    """Valida una fotografía resumida del análisis para cualquier backend."""

    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("El análisis necesita un ticker.")
    if price <= 0:
        raise ValueError("El precio analizado debe ser positivo.")

    def checked_score(value: int | None, name: str) -> int | None:
        if value is None:
            return None
        normalized = int(value)
        if not 0 <= normalized <= 100:
            raise ValueError(f"La nota de {name} debe estar entre 0 y 100.")
        return normalized

    normalized_expected_price = (
        float(expected_price) if expected_price is not None else None
    )
    if normalized_expected_price is not None and normalized_expected_price <= 0:
        raise ValueError("El precio esperado debe ser positivo.")
    normalized_horizon = int(horizon_days) if horizon_days is not None else None
    if normalized_horizon is not None and normalized_horizon <= 0:
        raise ValueError("El horizonte del análisis debe ser positivo.")
    normalized_positive_rate = (
        float(positive_rate_pct) if positive_rate_pct is not None else None
    )
    if normalized_positive_rate is not None and not 0 <= normalized_positive_rate <= 100:
        raise ValueError("El porcentaje de casos positivos debe estar entre 0 y 100.")

    return {
        "ticker": normalized_ticker,
        "analyzed_at": pd.Timestamp(analyzed_at).isoformat(),
        "price": float(price),
        "opportunity_score": checked_score(opportunity_score, "oportunidad"),
        "confidence_pct": checked_score(
            int(round(float(confidence_pct))) if confidence_pct is not None else None,
            "confianza",
        ),
        "company_score": checked_score(company_score, "empresa"),
        "entry_score": checked_score(entry_score, "entrada"),
        "valuation_score": checked_score(valuation_score, "valoración"),
        "relative_score": checked_score(relative_score, "fortaleza relativa"),
        "risk_score": checked_score(risk_score, "riesgo"),
        "opportunity_label": opportunity_label.strip(),
        "entry_label": entry_label.strip(),
        "position_label": position_label.strip(),
        "expected_return_pct": (
            float(expected_return_pct) if expected_return_pct is not None else None
        ),
        "positive_rate_pct": normalized_positive_rate,
        "expected_price": normalized_expected_price,
        "horizon_days": normalized_horizon,
        "sector": sector.strip(),
        "explanation": explanation.strip(),
        "note": note.strip()[:1_000],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_private_investment(
    *,
    platform: str,
    project_name: str,
    invested_amount: float,
    current_value: float,
    expected_return_pct: float,
    start_date: date | datetime | str,
    maturity_date: date | datetime | str | None = None,
    status: str = "Activa",
    notes: str = "",
) -> dict[str, object]:
    """Valida una inversión manual de Civislend o Segofactoring."""

    normalized_platform = platform.strip()
    if normalized_platform not in PRIVATE_INVESTMENT_PLATFORMS:
        raise ValueError("La plataforma debe ser Civislend o Segofactoring.")
    normalized_project = project_name.strip()
    if not normalized_project:
        raise ValueError("Indica el nombre o referencia del proyecto.")
    if invested_amount <= 0:
        raise ValueError("El importe invertido debe ser positivo.")
    if current_value < 0:
        raise ValueError("El valor actual no puede ser negativo.")
    if not -100 <= expected_return_pct <= 1_000:
        raise ValueError("La rentabilidad esperada debe estar entre -100% y 1.000%.")
    if status not in PRIVATE_INVESTMENT_STATUSES:
        raise ValueError("El estado de la inversión no es válido.")

    normalized_start = pd.Timestamp(start_date)
    normalized_maturity = (
        pd.Timestamp(maturity_date) if maturity_date not in (None, "") else None
    )
    if normalized_maturity is not None and normalized_maturity < normalized_start:
        raise ValueError("El vencimiento no puede ser anterior a la inversión.")
    return {
        "platform": normalized_platform,
        "project_name": normalized_project[:200],
        "invested_amount": float(invested_amount),
        "current_value": float(current_value),
        "expected_return_pct": float(expected_return_pct),
        "start_date": normalized_start.isoformat(),
        "maturity_date": (
            normalized_maturity.isoformat() if normalized_maturity is not None else None
        ),
        "status": status,
        "notes": notes.strip()[:1_000],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_portfolio_account(
    *,
    account_name: str,
    account_type: str,
    investments_value: float = 0.0,
    cash_balance: float = 0.0,
    currency: str = "EUR",
    status: str = "Pendiente de actualizar",
    notes: str = "",
) -> dict[str, object]:
    """Valida una cuenta de inversión o plataforma agregada."""

    normalized_name = account_name.strip()
    if not normalized_name:
        raise ValueError("La cuenta necesita un nombre.")
    if account_type not in PORTFOLIO_ACCOUNT_TYPES:
        raise ValueError("El tipo de cuenta no es válido.")
    if investments_value < 0 or cash_balance < 0:
        raise ValueError("El valor de inversiones y el efectivo no pueden ser negativos.")
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3:
        raise ValueError("La moneda debe tener tres letras, por ejemplo EUR o USD.")
    if status not in PORTFOLIO_ACCOUNT_STATUSES:
        raise ValueError("El estado de actualización no es válido.")
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "account_name": normalized_name[:100],
        "account_type": account_type,
        "investments_value": float(investments_value),
        "cash_balance": float(cash_balance),
        "currency": normalized_currency,
        "status": status,
        "notes": notes.strip()[:1_000],
        "updated_at": now,
        "created_at": now,
    }


def normalize_portfolio_snapshot_position(
    *,
    snapshot_date: date | datetime | str,
    platform: str,
    asset_name: str,
    raw_identifier: str = "",
    analysis_ticker: str = "",
    asset_type: str = "",
    portfolio_block: str = "",
    quantity: float | None = None,
    current_price: float | None = None,
    currency: str = "EUR",
    value_eur: float,
    return_pct: float | None = None,
    cost_estimate_eur: float | None = None,
    gain_loss_eur: float | None = None,
    comments: str = "",
    source: str = "",
    notes: str = "",
) -> dict[str, object]:
    """Valida una posición de una fotografía, no una operación de compraventa."""

    normalized_platform = platform.strip()
    normalized_asset = asset_name.strip()
    if not normalized_platform or not normalized_asset:
        raise ValueError("La fotografía necesita plataforma y nombre del activo.")
    if value_eur < 0:
        raise ValueError("El valor de la posición no puede ser negativo.")
    for number, label in (
        (quantity, "cantidad"),
        (current_price, "precio actual"),
        (cost_estimate_eur, "coste estimado"),
    ):
        if number is not None and pd.notna(number) and float(number) < 0:
            raise ValueError(f"El {label} no puede ser negativo.")
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3:
        raise ValueError("La moneda debe tener tres letras, por ejemplo EUR o USD.")
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "snapshot_date": pd.Timestamp(snapshot_date).date().isoformat(),
        "platform": normalized_platform[:100],
        "asset_name": normalized_asset[:200],
        "raw_identifier": raw_identifier.strip()[:100],
        "analysis_ticker": analysis_ticker.strip().upper()[:30],
        "asset_type": asset_type.strip()[:100],
        "portfolio_block": portfolio_block.strip()[:100],
        "quantity": float(quantity) if quantity is not None and pd.notna(quantity) else None,
        "current_price": (
            float(current_price)
            if current_price is not None and pd.notna(current_price)
            else None
        ),
        "currency": normalized_currency,
        "value_eur": float(value_eur),
        "return_pct": (
            float(return_pct) if return_pct is not None and pd.notna(return_pct) else None
        ),
        "cost_estimate_eur": (
            float(cost_estimate_eur)
            if cost_estimate_eur is not None and pd.notna(cost_estimate_eur)
            else None
        ),
        "gain_loss_eur": (
            float(gain_loss_eur)
            if gain_loss_eur is not None and pd.notna(gain_loss_eur)
            else None
        ),
        "comments": comments.strip()[:1_000],
        "source": source.strip()[:500],
        "notes": notes.strip()[:1_000],
        "created_at": now,
        "updated_at": now,
    }


def calculate_position_states(
    operations: pd.DataFrame,
    *,
    include_closed: bool = True,
) -> pd.DataFrame:
    """Reconstruye el estado de cada posición, incluidas las ya cerradas."""

    columns = [
        "ticker",
        "account_name",
        "currency",
        "quantity",
        "average_cost",
        "cost_basis",
        "realized_pnl",
        "paid_fees",
        "cost_basis_eur",
        "realized_pnl_eur",
        "paid_fees_eur",
        "eur_values_complete",
    ]
    if operations.empty:
        return pd.DataFrame(columns=columns)

    operations = operations.sort_values(["executed_at", "id"])
    states: dict[tuple[str, str, str], dict[str, float | str | bool | None]] = {}
    for operation in operations.itertuples(index=False):
        currency = str(getattr(operation, "currency", "EUR") or "EUR").upper()
        account_name = str(getattr(operation, "account_name", "") or "").strip()
        key = (str(operation.ticker), currency, account_name)
        state = states.setdefault(
            key,
            {
                "ticker": key[0],
                "currency": key[1],
                "account_name": key[2],
                "quantity": 0.0,
                "cost_basis": 0.0,
                "realized_pnl": 0.0,
                "paid_fees": 0.0,
                "cost_basis_eur": 0.0,
                "realized_pnl_eur": 0.0,
                "paid_fees_eur": 0.0,
            },
        )
        quantity = float(operation.quantity)
        price = float(operation.price)
        fee = float(operation.fees)
        settlement_eur = pd.to_numeric(
            getattr(operation, "settlement_amount_eur", None), errors="coerce"
        )
        fee_eur = pd.to_numeric(
            getattr(operation, "fee_eur", None), errors="coerce"
        )
        if pd.isna(settlement_eur) and currency == "EUR":
            settlement_eur = (
                quantity * price + fee
                if operation.side == "Compra"
                else quantity * price - fee
            )
        if pd.isna(fee_eur) and currency == "EUR":
            fee_eur = fee
        state["paid_fees"] = float(state["paid_fees"]) + fee
        if pd.isna(fee_eur):
            state["paid_fees_eur"] = None
        elif state["paid_fees_eur"] is not None:
            state["paid_fees_eur"] = float(state["paid_fees_eur"]) + float(fee_eur)
        if operation.side == "Compra":
            state["quantity"] = float(state["quantity"]) + quantity
            state["cost_basis"] = float(state["cost_basis"]) + quantity * price + fee
            if pd.isna(settlement_eur):
                state["cost_basis_eur"] = None
            elif state["cost_basis_eur"] is not None:
                state["cost_basis_eur"] = float(state["cost_basis_eur"]) + float(
                    settlement_eur
                )
            continue

        available = float(state["quantity"])
        sold = min(quantity, available)
        if sold <= 0:
            continue
        average_cost = float(state["cost_basis"]) / available
        allocated_fee = fee * (sold / quantity)
        proceeds = sold * price - allocated_fee
        removed_cost = average_cost * sold
        removed_cost_eur: float | None = None
        if state["cost_basis_eur"] is not None:
            removed_cost_eur = float(state["cost_basis_eur"]) / available * sold
        state["realized_pnl"] = float(state["realized_pnl"]) + proceeds - removed_cost
        if (
            removed_cost_eur is None
            or pd.isna(settlement_eur)
            or state["realized_pnl_eur"] is None
        ):
            state["realized_pnl_eur"] = None
        else:
            state["realized_pnl_eur"] = float(state["realized_pnl_eur"]) + float(
                settlement_eur
            ) - removed_cost_eur
        state["quantity"] = available - sold
        state["cost_basis"] = max(0.0, float(state["cost_basis"]) - removed_cost)
        if removed_cost_eur is not None:
            state["cost_basis_eur"] = max(
                0.0, float(state["cost_basis_eur"]) - removed_cost_eur
            )

    rows: list[dict[str, object]] = []
    for state in states.values():
        quantity = float(state["quantity"])
        if not include_closed and quantity <= 1e-9:
            continue
        cost_basis = float(state["cost_basis"])
        rows.append(
            {
                **state,
                "average_cost": cost_basis / quantity if quantity > 1e-9 else 0.0,
                "eur_values_complete": all(
                    state.get(field) is not None
                    for field in (
                        "cost_basis_eur",
                        "realized_pnl_eur",
                        "paid_fees_eur",
                    )
                ),
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["ticker", "account_name", "currency"], ignore_index=True
    )


def calculate_open_positions(operations: pd.DataFrame) -> pd.DataFrame:
    """Reconstruye únicamente las posiciones todavía abiertas."""

    return calculate_position_states(operations, include_closed=False)


class TradingJournal:
    backend_name = "SQLite local"

    def __init__(
        self,
        database_path: str | Path = DEFAULT_DATABASE,
        *,
        owner: str = "",
    ) -> None:
        self.database_path = Path(database_path)
        self.owner = owner.strip().lower() or self.database_path.stem
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    account_name TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL CHECK (side IN ('Compra', 'Venta')),
                    quantity REAL NOT NULL CHECK (quantity > 0),
                    price REAL NOT NULL CHECK (price > 0),
                    fees REAL NOT NULL DEFAULT 0 CHECK (fees >= 0),
                    settlement_amount_eur REAL,
                    fee_eur REAL,
                    fx_rate_to_eur REAL,
                    executed_at TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(operations)").fetchall()
            }
            if "currency" not in columns:
                connection.execute(
                    "ALTER TABLE operations ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'"
                )
            if "recorded_by" not in columns:
                connection.execute(
                    "ALTER TABLE operations ADD COLUMN recorded_by TEXT NOT NULL DEFAULT ''"
                )
            for column, definition in (
                ("account_name", "TEXT NOT NULL DEFAULT ''"),
                ("settlement_amount_eur", "REAL"),
                ("fee_eur", "REAL"),
                ("fx_rate_to_eur", "REAL"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE operations ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    exchange TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            favorite_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(favorites)").fetchall()
            }
            if "tags" not in favorite_columns:
                connection.execute(
                    "ALTER TABLE favorites ADD COLUMN tags TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    analyzed_at TEXT NOT NULL,
                    price REAL NOT NULL CHECK (price > 0),
                    opportunity_score INTEGER NOT NULL
                        CHECK (opportunity_score BETWEEN 0 AND 100),
                    confidence_pct INTEGER
                        CHECK (confidence_pct BETWEEN 0 AND 100),
                    company_score INTEGER,
                    entry_score INTEGER NOT NULL
                        CHECK (entry_score BETWEEN 0 AND 100),
                    valuation_score INTEGER,
                    relative_score INTEGER,
                    risk_score INTEGER,
                    opportunity_label TEXT NOT NULL DEFAULT '',
                    entry_label TEXT NOT NULL DEFAULT '',
                    position_label TEXT NOT NULL DEFAULT '',
                    expected_return_pct REAL,
                    positive_rate_pct REAL,
                    expected_price REAL,
                    horizon_days INTEGER,
                    sector TEXT NOT NULL DEFAULT '',
                    explanation TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            analysis_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(analysis_snapshots)"
                ).fetchall()
            }
            if "confidence_pct" not in analysis_columns:
                connection.execute(
                    "ALTER TABLE analysis_snapshots ADD COLUMN confidence_pct INTEGER "
                    "CHECK (confidence_pct BETWEEN 0 AND 100)"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS analysis_snapshots_ticker_date_idx
                ON analysis_snapshots (ticker, analyzed_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS email_alert_preferences (
                    owner TEXT PRIMARY KEY,
                    email TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    alert_buy INTEGER NOT NULL DEFAULT 1,
                    alert_reduce INTEGER NOT NULL DEFAULT 1,
                    alert_sell INTEGER NOT NULL DEFAULT 1,
                    include_group INTEGER NOT NULL DEFAULT 1,
                    minimum_buy_score INTEGER NOT NULL DEFAULT 65
                        CHECK (minimum_buy_score BETWEEN 55 AND 100),
                    only_changes INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS email_alert_states (
                    owner TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    entry_score INTEGER NOT NULL,
                    entry_label TEXT NOT NULL DEFAULT '',
                    position_label TEXT NOT NULL DEFAULT '',
                    price REAL NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    notified_at TEXT,
                    company_name TEXT NOT NULL DEFAULT '',
                    growth_score INTEGER,
                    fundamental_score INTEGER,
                    opportunity_score INTEGER,
                    opportunity_status TEXT NOT NULL DEFAULT '',
                    data_note TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (owner, ticker)
                )
                """
            )
            alert_state_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(email_alert_states)"
                ).fetchall()
            }
            for column, definition in (
                ("company_name", "TEXT NOT NULL DEFAULT ''"),
                ("growth_score", "INTEGER"),
                ("fundamental_score", "INTEGER"),
                ("opportunity_score", "INTEGER"),
                ("opportunity_status", "TEXT NOT NULL DEFAULT ''"),
                ("data_note", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in alert_state_columns:
                    connection.execute(
                        f"ALTER TABLE email_alert_states ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS private_investments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL
                        CHECK (platform IN ('Civislend', 'Segofactoring')),
                    project_name TEXT NOT NULL,
                    invested_amount REAL NOT NULL CHECK (invested_amount > 0),
                    current_value REAL NOT NULL CHECK (current_value >= 0),
                    expected_return_pct REAL NOT NULL DEFAULT 0,
                    start_date TEXT NOT NULL,
                    maturity_date TEXT,
                    status TEXT NOT NULL DEFAULT 'Activa'
                        CHECK (status IN ('Activa', 'Finalizada', 'Retrasada', 'Impagada')),
                    notes TEXT NOT NULL DEFAULT '',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL UNIQUE,
                    account_type TEXT NOT NULL
                        CHECK (account_type IN ('Bróker', 'Inversión alternativa')),
                    investments_value REAL NOT NULL DEFAULT 0
                        CHECK (investments_value >= 0),
                    cash_balance REAL NOT NULL DEFAULT 0 CHECK (cash_balance >= 0),
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    status TEXT NOT NULL DEFAULT 'Pendiente de actualizar'
                        CHECK (status IN ('Pendiente de actualizar', 'Actualizada', 'Inactiva')),
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_date TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    asset_name TEXT NOT NULL,
                    raw_identifier TEXT NOT NULL DEFAULT '',
                    analysis_ticker TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL DEFAULT '',
                    portfolio_block TEXT NOT NULL DEFAULT '',
                    quantity REAL,
                    current_price REAL,
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    value_eur REAL NOT NULL CHECK (value_eur >= 0),
                    return_pct REAL,
                    cost_estimate_eur REAL,
                    gain_loss_eur REAL,
                    comments TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (snapshot_date, platform, asset_name)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS portfolio_snapshots_date_platform_idx
                ON portfolio_snapshots (snapshot_date DESC, platform, asset_name)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_simulations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'paused', 'archived')),
                    base_currency TEXT NOT NULL DEFAULT 'EUR'
                        CHECK (length(base_currency) = 3),
                    benchmark_ticker TEXT NOT NULL DEFAULT 'SPY',
                    strategy_key TEXT NOT NULL,
                    engine_version TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    initial_nav_eur REAL NOT NULL CHECK (initial_nav_eur > 0),
                    initial_cash_eur REAL NOT NULL DEFAULT 0
                        CHECK (initial_cash_eur >= 0),
                    source_snapshot_date TEXT,
                    assumptions_json TEXT NOT NULL DEFAULT '{}',
                    initial_positions_json TEXT NOT NULL DEFAULT '[]',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (initial_cash_eur <= initial_nav_eur),
                    UNIQUE (owner, id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS paper_simulations_status_date_idx
                ON paper_simulations (owner, status, start_date DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_daily_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    simulation_id INTEGER NOT NULL,
                    market_date TEXT NOT NULL,
                    signal_as_of TEXT,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'complete'
                        CHECK (status IN ('complete', 'partial', 'failed')),
                    proposed_actions_json TEXT NOT NULL DEFAULT '[]',
                    executed_actions_json TEXT NOT NULL DEFAULT '[]',
                    positions_after_json TEXT NOT NULL DEFAULT '[]',
                    cash_eur REAL NOT NULL CHECK (cash_eur >= 0),
                    net_nav_eur REAL NOT NULL CHECK (net_nav_eur >= 0),
                    hold_nav_eur REAL NOT NULL CHECK (hold_nav_eur >= 0),
                    benchmark_nav_eur REAL NOT NULL CHECK (benchmark_nav_eur >= 0),
                    realized_pnl_eur REAL NOT NULL DEFAULT 0,
                    unrealized_pnl_eur REAL NOT NULL DEFAULT 0,
                    cumulative_costs_eur REAL NOT NULL DEFAULT 0
                        CHECK (cumulative_costs_eur >= 0),
                    tax_reserve_eur REAL NOT NULL DEFAULT 0
                        CHECK (tax_reserve_eur >= 0),
                    coverage_pct REAL NOT NULL DEFAULT 0
                        CHECK (coverage_pct >= 0 AND coverage_pct <= 100),
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    rejected_json TEXT NOT NULL DEFAULT '[]',
                    engine_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (owner, simulation_id)
                        REFERENCES paper_simulations(owner, id)
                        ON DELETE CASCADE,
                    UNIQUE (owner, simulation_id, market_date)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS paper_daily_runs_simulation_date_idx
                ON paper_daily_runs (owner, simulation_id, market_date DESC, id DESC)
                """
            )

    def create_paper_simulation(
        self,
        *,
        name: str,
        start_date: date | datetime | str,
        initial_nav_eur: float,
        initial_positions: object,
        assumptions: object | None = None,
        initial_cash_eur: float = 0.0,
        source_snapshot_date: date | datetime | str | None = None,
        status: str = "active",
        base_currency: str = "EUR",
        benchmark_ticker: str = "SPY",
        strategy_key: str = "strict_rotation_v1",
        engine_version: str = "paper-v1",
        recorded_by: str = "",
    ) -> int:
        """Crea una temporada paper sin modificar la cartera ni sus operaciones."""

        simulation = normalize_paper_simulation(
            name=name,
            start_date=start_date,
            initial_nav_eur=initial_nav_eur,
            initial_positions=initial_positions,
            assumptions=assumptions,
            initial_cash_eur=initial_cash_eur,
            source_snapshot_date=source_snapshot_date,
            status=status,
            base_currency=base_currency,
            benchmark_ticker=benchmark_ticker,
            strategy_key=strategy_key,
            engine_version=engine_version,
        )
        values = {
            "owner": self.owner,
            **simulation,
            "recorded_by": recorded_by.strip().lower(),
        }
        columns = [
            "owner",
            *(column for column in PAPER_SIMULATION_COLUMNS if column != "id"),
        ]
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO paper_simulations ({', '.join(columns)})
                VALUES ({', '.join('?' for _ in columns)})
                """,
                tuple(values[column] for column in columns),
            )
            return int(cursor.lastrowid)

    def list_paper_simulations(self, status: str | None = None) -> pd.DataFrame:
        """Lista sólo las simulaciones del diario local de este usuario."""

        where = " WHERE owner = ?"
        parameters: tuple[object, ...] = (self.owner,)
        if status is not None:
            normalized_status = str(status).strip().lower()
            if normalized_status not in PAPER_SIMULATION_STATUSES:
                raise ValueError("El estado de la simulación paper no es válido.")
            where += " AND status = ?"
            parameters = (self.owner, normalized_status)
        with self._connect() as connection:
            return pd.read_sql_query(
                f"""
                SELECT {', '.join(PAPER_SIMULATION_COLUMNS)}
                FROM paper_simulations{where}
                ORDER BY start_date DESC, id DESC
                """,
                connection,
                params=parameters,
            )

    def set_paper_simulation_status(self, simulation_id: int, status: str) -> None:
        """Cambia sólo el estado de una temporada paper del propietario actual."""

        normalized_status = str(status or "").strip().lower()
        if normalized_status not in PAPER_SIMULATION_STATUSES:
            raise ValueError("El estado de la simulación paper no es válido.")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE paper_simulations
                SET status = ?, updated_at = ?
                WHERE owner = ? AND id = ?
                """,
                (normalized_status, now, self.owner, int(simulation_id)),
            )
        if cursor.rowcount != 1:
            raise ValueError("La simulación paper indicada no existe.")

    def upsert_paper_daily_run(
        self,
        *,
        simulation_id: int,
        market_date: date | datetime | str,
        signal_as_of: date | datetime | str | None,
        input_hash: str,
        positions_after: object,
        proposed_actions: object | None = None,
        executed_actions: object | None = None,
        warnings: object | None = None,
        rejected: object | None = None,
        cash_eur: float,
        net_nav_eur: float,
        hold_nav_eur: float,
        benchmark_nav_eur: float,
        realized_pnl_eur: float = 0.0,
        unrealized_pnl_eur: float = 0.0,
        cumulative_costs_eur: float = 0.0,
        tax_reserve_eur: float = 0.0,
        coverage_pct: float = 0.0,
        status: str = "complete",
        engine_version: str = "paper-v1",
    ) -> int:
        """Inserta o reemplaza el único cierre paper de una sesión y fecha."""

        run = normalize_paper_daily_run(
            simulation_id=simulation_id,
            market_date=market_date,
            signal_as_of=signal_as_of,
            input_hash=input_hash,
            positions_after=positions_after,
            proposed_actions=proposed_actions,
            executed_actions=executed_actions,
            warnings=warnings,
            rejected=rejected,
            cash_eur=cash_eur,
            net_nav_eur=net_nav_eur,
            hold_nav_eur=hold_nav_eur,
            benchmark_nav_eur=benchmark_nav_eur,
            realized_pnl_eur=realized_pnl_eur,
            unrealized_pnl_eur=unrealized_pnl_eur,
            cumulative_costs_eur=cumulative_costs_eur,
            tax_reserve_eur=tax_reserve_eur,
            coverage_pct=coverage_pct,
            status=status,
            engine_version=engine_version,
        )
        values = {"owner": self.owner, **run}
        columns = [
            "owner",
            *(column for column in PAPER_DAILY_RUN_COLUMNS if column != "id"),
        ]
        mutable_columns = [
            column
            for column in columns
            if column not in {"owner", "simulation_id", "market_date", "created_at"}
        ]
        with self._connect() as connection:
            simulation = connection.execute(
                "SELECT id FROM paper_simulations WHERE owner = ? AND id = ?",
                (self.owner, int(run["simulation_id"])),
            ).fetchone()
            if simulation is None:
                raise ValueError("La simulación paper indicada no existe.")
            connection.execute(
                f"""
                INSERT INTO paper_daily_runs ({', '.join(columns)})
                VALUES ({', '.join('?' for _ in columns)})
                ON CONFLICT(owner, simulation_id, market_date) DO UPDATE SET
                    {', '.join(f'{column} = excluded.{column}' for column in mutable_columns)}
                """,
                tuple(values[column] for column in columns),
            )
            row = connection.execute(
                """
                SELECT id FROM paper_daily_runs
                WHERE owner = ? AND simulation_id = ? AND market_date = ?
                """,
                (self.owner, int(run["simulation_id"]), str(run["market_date"])),
            ).fetchone()
        if row is None:
            raise RuntimeError("SQLite no devolvió la ejecución paper guardada.")
        return int(row["id"])

    def list_paper_daily_runs(
        self,
        simulation_id: int | None = None,
    ) -> pd.DataFrame:
        """Devuelve el historial paper sin mezclarlo con movimientos reales."""

        where = " WHERE owner = ?"
        parameters: tuple[object, ...] = (self.owner,)
        if simulation_id is not None:
            normalized_id = int(simulation_id)
            if normalized_id <= 0:
                raise ValueError("La simulación paper indicada no es válida.")
            where += " AND simulation_id = ?"
            parameters = (self.owner, normalized_id)
        with self._connect() as connection:
            return pd.read_sql_query(
                f"""
                SELECT {', '.join(PAPER_DAILY_RUN_COLUMNS)}
                FROM paper_daily_runs{where}
                ORDER BY market_date DESC, id DESC
                """,
                connection,
                params=parameters,
            )

    def add_operation(
        self,
        ticker: str,
        side: str,
        quantity: float,
        price: float,
        fees: float,
        executed_at: date | datetime | str,
        notes: str = "",
        currency: str = "EUR",
        recorded_by: str = "",
        account_name: str = "",
        settlement_amount_eur: float | None = None,
        fee_eur: float | None = None,
        fx_rate_to_eur: float | None = None,
    ) -> int:
        operation = normalize_operation(
            ticker,
            side,
            quantity,
            price,
            fees,
            executed_at,
            notes,
            currency,
            account_name,
            settlement_amount_eur,
            fee_eur,
            fx_rate_to_eur,
        )
        if side == "Venta":
            positions = self.open_positions()
            available = positions.loc[
                (positions["ticker"] == operation["ticker"])
                & (positions["currency"] == operation["currency"])
                & (positions["account_name"] == operation["account_name"]),
                "quantity",
            ]
            if available.empty or float(available.iloc[0]) + 1e-9 < quantity:
                raise ValueError("La venta supera la cantidad registrada en cartera.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operations
                    (
                        ticker, account_name, side, quantity, price, fees,
                        settlement_amount_eur, fee_eur, fx_rate_to_eur,
                        executed_at, notes, currency, recorded_by, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation["ticker"],
                    operation["account_name"],
                    operation["side"],
                    operation["quantity"],
                    operation["price"],
                    operation["fees"],
                    operation["settlement_amount_eur"],
                    operation["fee_eur"],
                    operation["fx_rate_to_eur"],
                    operation["executed_at"],
                    operation["notes"],
                    operation["currency"],
                    recorded_by.strip().lower(),
                    operation["created_at"],
                ),
            )
            return int(cursor.lastrowid)

    def list_operations(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                "SELECT * FROM operations ORDER BY executed_at DESC, id DESC", connection
            )

    def delete_operation(self, operation_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM operations WHERE id = ?", (int(operation_id),))

    def add_private_investment(
        self,
        *,
        platform: str,
        project_name: str,
        invested_amount: float,
        current_value: float,
        expected_return_pct: float,
        start_date: date | datetime | str,
        maturity_date: date | datetime | str | None = None,
        status: str = "Activa",
        notes: str = "",
        recorded_by: str = "",
    ) -> int:
        investment = normalize_private_investment(
            platform=platform,
            project_name=project_name,
            invested_amount=invested_amount,
            current_value=current_value,
            expected_return_pct=expected_return_pct,
            start_date=start_date,
            maturity_date=maturity_date,
            status=status,
            notes=notes,
        )
        columns = [column for column in PRIVATE_INVESTMENT_COLUMNS if column != "id"]
        values = {**investment, "recorded_by": recorded_by.strip().lower()}
        with self._connect() as connection:
            cursor = connection.execute(
                f"INSERT INTO private_investments ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            return int(cursor.lastrowid)

    def list_private_investments(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT * FROM private_investments
                ORDER BY start_date DESC, id DESC
                """,
                connection,
            )

    def update_private_investment(
        self,
        investment_id: int,
        *,
        current_value: float,
        status: str,
        notes: str,
    ) -> None:
        if current_value < 0:
            raise ValueError("El valor actual no puede ser negativo.")
        if status not in PRIVATE_INVESTMENT_STATUSES:
            raise ValueError("El estado de la inversión no es válido.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE private_investments
                SET current_value = ?, status = ?, notes = ?
                WHERE id = ?
                """,
                (float(current_value), status, notes.strip()[:1_000], int(investment_id)),
            )
            if cursor.rowcount == 0:
                raise ValueError("La inversión privada no existe.")

    def delete_private_investment(self, investment_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM private_investments WHERE id = ?",
                (int(investment_id),),
            )

    def upsert_portfolio_account(
        self,
        *,
        account_name: str,
        account_type: str,
        investments_value: float = 0.0,
        cash_balance: float = 0.0,
        currency: str = "EUR",
        status: str = "Pendiente de actualizar",
        notes: str = "",
    ) -> int:
        account = normalize_portfolio_account(
            account_name=account_name,
            account_type=account_type,
            investments_value=investments_value,
            cash_balance=cash_balance,
            currency=currency,
            status=status,
            notes=notes,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO portfolio_accounts (
                    account_name, account_type, investments_value, cash_balance,
                    currency, status, notes, updated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_name) DO UPDATE SET
                    account_type = excluded.account_type,
                    investments_value = excluded.investments_value,
                    cash_balance = excluded.cash_balance,
                    currency = excluded.currency,
                    status = excluded.status,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                tuple(account[column] for column in PORTFOLIO_ACCOUNT_COLUMNS if column != "id"),
            )
            row = connection.execute(
                "SELECT id FROM portfolio_accounts WHERE account_name = ?",
                (account["account_name"],),
            ).fetchone()
            return int(row["id"])

    def list_portfolio_accounts(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT * FROM portfolio_accounts
                ORDER BY CASE account_name
                    WHEN 'MyInvestor' THEN 1
                    WHEN 'Trade Republic' THEN 2
                    WHEN 'Revolut' THEN 3
                    WHEN 'Segofactoring' THEN 4
                    WHEN 'Civislend' THEN 5
                    ELSE 6 END, account_name
                """,
                connection,
            )

    def upsert_portfolio_snapshot_positions(
        self,
        positions: pd.DataFrame,
        *,
        recorded_by: str = "",
    ) -> int:
        """Guarda una fotografía completa sin convertirla en compraventas."""

        payloads = [
            normalize_portfolio_snapshot_position(
                **{
                    column: row[column]
                    for column in PORTFOLIO_SNAPSHOT_COLUMNS
                    if column in row
                    and column
                    not in {"id", "recorded_by", "created_at", "updated_at"}
                }
            )
            for row in positions.to_dict("records")
        ]
        columns = [column for column in PORTFOLIO_SNAPSHOT_COLUMNS if column != "id"]
        with self._connect() as connection:
            for position in payloads:
                values = {**position, "recorded_by": recorded_by.strip().lower()}
                connection.execute(
                    f"""
                    INSERT INTO portfolio_snapshots ({', '.join(columns)})
                    VALUES ({', '.join('?' for _ in columns)})
                    ON CONFLICT(snapshot_date, platform, asset_name) DO UPDATE SET
                        raw_identifier = excluded.raw_identifier,
                        analysis_ticker = excluded.analysis_ticker,
                        asset_type = excluded.asset_type,
                        portfolio_block = excluded.portfolio_block,
                        quantity = excluded.quantity,
                        current_price = excluded.current_price,
                        currency = excluded.currency,
                        value_eur = excluded.value_eur,
                        return_pct = excluded.return_pct,
                        cost_estimate_eur = excluded.cost_estimate_eur,
                        gain_loss_eur = excluded.gain_loss_eur,
                        comments = excluded.comments,
                        source = excluded.source,
                        notes = excluded.notes,
                        recorded_by = excluded.recorded_by,
                        updated_at = excluded.updated_at
                    """,
                    tuple(values[column] for column in columns),
                )
        return len(payloads)

    def replace_portfolio_snapshot_positions(
        self,
        positions: pd.DataFrame,
        *,
        snapshot_date: date | datetime | str,
        recorded_by: str = "",
    ) -> int:
        """Sustituye atómicamente una foto completa, incluidas las bajas."""

        target = pd.Timestamp(snapshot_date).date().isoformat()
        replacement = positions.copy()
        if replacement.empty:
            raise ValueError("La fotografía de cartera no puede quedar vacía.")
        replacement["snapshot_date"] = target
        payloads = [
            normalize_portfolio_snapshot_position(
                **{
                    column: row[column]
                    for column in PORTFOLIO_SNAPSHOT_COLUMNS
                    if column in row
                    and column
                    not in {"id", "recorded_by", "created_at", "updated_at"}
                }
            )
            for row in replacement.to_dict("records")
        ]
        columns = [column for column in PORTFOLIO_SNAPSHOT_COLUMNS if column != "id"]
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM portfolio_snapshots WHERE snapshot_date = ?",
                (target,),
            )
            for position in payloads:
                values = {**position, "recorded_by": recorded_by.strip().lower()}
                connection.execute(
                    f"""
                    INSERT INTO portfolio_snapshots ({', '.join(columns)})
                    VALUES ({', '.join('?' for _ in columns)})
                    """,
                    tuple(values[column] for column in columns),
                )
        return len(payloads)

    def list_portfolio_snapshot_positions(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT * FROM portfolio_snapshots
                ORDER BY snapshot_date DESC, platform, value_eur DESC, asset_name
                """,
                connection,
            )

    def add_favorite(
        self,
        ticker: str,
        name: str = "",
        exchange: str = "",
        tags: object = "",
        recorded_by: str = "",
    ) -> int:
        favorite = normalize_favorite(ticker, name, exchange, tags)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM favorites WHERE ticker = ?",
                (favorite["ticker"],),
            ).fetchone()
            if existing:
                if favorite["tags"]:
                    connection.execute(
                        "UPDATE favorites SET tags = ? WHERE ticker = ?",
                        (favorite["tags"], favorite["ticker"]),
                    )
                return int(existing["id"])
            total = int(
                connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
            )
            if total >= MAX_FAVORITES:
                raise ValueError(f"Cada lista admite hasta {MAX_FAVORITES} favoritos.")
            cursor = connection.execute(
                """
                INSERT INTO favorites
                    (ticker, name, exchange, tags, recorded_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    favorite["ticker"],
                    favorite["name"],
                    favorite["exchange"],
                    favorite["tags"],
                    recorded_by.strip().lower(),
                    favorite["created_at"],
                ),
            )
            return int(cursor.lastrowid)

    def list_favorites(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT id, ticker, name, exchange, tags, recorded_by, created_at
                FROM favorites
                ORDER BY name COLLATE NOCASE, ticker
                """,
                connection,
            )

    def update_favorite_tags(self, ticker: str, tags: object) -> None:
        normalized_ticker = ticker.strip().upper()
        normalized_tags = serialize_favorite_tags(tags)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE favorites SET tags = ? WHERE ticker = ?",
                (normalized_tags, normalized_ticker),
            )
            if cursor.rowcount == 0:
                raise ValueError("La empresa favorita no existe.")

    def delete_favorite(self, ticker: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM favorites WHERE ticker = ?",
                (ticker.strip().upper(),),
            )

    def add_analysis_snapshot(self, **values: object) -> int:
        """Guarda una fotografía ligera de una señal y sus notas."""

        snapshot = normalize_analysis_snapshot(**values)  # type: ignore[arg-type]
        columns = [column for column in ANALYSIS_SNAPSHOT_COLUMNS if column != "id"]
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO analysis_snapshots ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                tuple(snapshot[column] for column in columns),
            )
            return int(cursor.lastrowid)

    def list_analysis_snapshots(self, ticker: str | None = None) -> pd.DataFrame:
        """Devuelve el seguimiento más reciente, opcionalmente de una empresa."""

        query = "SELECT * FROM analysis_snapshots"
        parameters: tuple[object, ...] = ()
        if ticker:
            query += " WHERE ticker = ?"
            parameters = (ticker.strip().upper(),)
        query += " ORDER BY analyzed_at DESC, id DESC"
        with self._connect() as connection:
            return pd.read_sql_query(query, connection, params=parameters)

    def delete_analysis_snapshot(self, snapshot_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM analysis_snapshots WHERE id = ?",
                (int(snapshot_id),),
            )

    def get_alert_preferences(self) -> AlertPreferences:
        """Devuelve valores seguros aunque el usuario aún no los haya guardado."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM email_alert_preferences WHERE owner = ?",
                (self.owner,),
            ).fetchone()
        if row is None:
            return normalize_alert_preferences(owner=self.owner)
        return preferences_from_mapping(dict(row), owner=self.owner)

    def save_alert_preferences(
        self,
        preferences: AlertPreferences,
    ) -> None:
        values = normalize_alert_preferences(**preferences.__dict__)
        if values.owner != self.owner:
            raise ValueError("No se pueden modificar las alertas de otro usuario.")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO email_alert_preferences (
                    owner, email, enabled, alert_buy, alert_reduce, alert_sell,
                    include_group, minimum_buy_score, only_changes, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner) DO UPDATE SET
                    email = excluded.email,
                    enabled = excluded.enabled,
                    alert_buy = excluded.alert_buy,
                    alert_reduce = excluded.alert_reduce,
                    alert_sell = excluded.alert_sell,
                    include_group = excluded.include_group,
                    minimum_buy_score = excluded.minimum_buy_score,
                    only_changes = excluded.only_changes,
                    updated_at = excluded.updated_at
                """,
                (
                    values.owner,
                    values.email,
                    int(values.enabled),
                    int(values.alert_buy),
                    int(values.alert_reduce),
                    int(values.alert_sell),
                    int(values.include_group),
                    values.minimum_buy_score,
                    int(values.only_changes),
                    values.updated_at,
                ),
            )

    def list_alert_states(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT owner, ticker, signature, entry_score, entry_label,
                       position_label, price, evaluated_at, notified_at,
                       company_name, growth_score, fundamental_score,
                       opportunity_score, opportunity_status, data_note
                FROM email_alert_states
                ORDER BY ticker
                """,
                connection,
            )

    def upsert_alert_states(self, states: list[AlertState]) -> None:
        if not states:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO email_alert_states (
                    owner, ticker, signature, entry_score, entry_label,
                    position_label, price, evaluated_at, notified_at,
                    company_name, growth_score, fundamental_score,
                    opportunity_score, opportunity_status, data_note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner, ticker) DO UPDATE SET
                    signature = excluded.signature,
                    entry_score = excluded.entry_score,
                    entry_label = excluded.entry_label,
                    position_label = excluded.position_label,
                    price = excluded.price,
                    evaluated_at = excluded.evaluated_at,
                    company_name = excluded.company_name,
                    growth_score = excluded.growth_score,
                    fundamental_score = excluded.fundamental_score,
                    opportunity_score = excluded.opportunity_score,
                    opportunity_status = excluded.opportunity_status,
                    data_note = excluded.data_note,
                    notified_at = COALESCE(
                        excluded.notified_at,
                        email_alert_states.notified_at
                    )
                """,
                [
                    tuple(getattr(state, column) for column in ALERT_STATE_COLUMNS)
                    for state in states
                ],
            )

    def open_positions(self) -> pd.DataFrame:
        """Reconstruye posiciones mediante coste medio, incluidas comisiones pagadas."""

        return calculate_open_positions(self.list_operations())

    def portfolio_summary(self) -> pd.DataFrame:
        """Compatibilidad: devuelve ahora las posiciones abiertas calculadas."""

        return self.open_positions()
