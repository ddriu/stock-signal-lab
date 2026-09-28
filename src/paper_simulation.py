"""Simulador diario prudente y sin efectos laterales.

El módulo mantiene una cartera *paper* separada de las operaciones reales. Una
señal conocida en ``T`` sólo puede ejecutarse con la primera apertura disponible
posterior a ``T``. Todas las cantidades monetarias del estado están expresadas
en euros y cada coste queda desglosado para que la comparación con mantener la
cartera y con el benchmark sea reproducible.

No hay funciones de persistencia en este módulo. El llamador decide si guarda
los estados paper en una tabla específica; nunca deben mezclarse con el diario
de operaciones real.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from hashlib import sha256
from math import sqrt
from typing import Iterable, Mapping, Sequence

import pandas as pd

from src.portfolio_rotation import (
    RotationDashboard,
    build_pair_correlations,
    build_rotation_dashboard,
)


PAPER_ENGINE_VERSION = "paper-v1"


def _as_date(value: object) -> date:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError("La simulación necesita una fecha válida.")
    return parsed.date()


def _ticker(value: object) -> str:
    return str(value or "").strip().upper()


def _number(value: object) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _value(row: Mapping[str, object], *keys: str) -> object | None:
    """Obtiene una clave tolerando mayúsculas y nombres de la interfaz."""

    normalized = {str(key).strip().casefold(): value for key, value in row.items()}
    for key in keys:
        if key in row:
            return row[key]
        found = normalized.get(key.casefold())
        if found is not None:
            return found
    return None


@dataclass(frozen=True)
class PaperAssumptions:
    base_currency: str = "EUR"
    benchmark_ticker: str = "SPY"
    buy_fee_eur: float = 1.0
    sell_fee_eur: float = 1.0
    spread_pct: float = 0.15
    slippage_pct: float = 0.10
    fx_cost_pct: float = 0.20
    tax_reserve_rate_pct: float = 20.0
    max_position_pct: float = 15.0
    max_sector_pct: float = 25.0
    max_daily_turnover_pct: float = 5.0
    minimum_cash_pct: float = 2.0
    max_order_age_days: int = 7

    def __post_init__(self) -> None:
        if self.base_currency.upper() != "EUR":
            raise ValueError("La versión paper v1 usa EUR como moneda base.")
        non_negative = {
            "comisión de compra": self.buy_fee_eur,
            "comisión de venta": self.sell_fee_eur,
            "spread": self.spread_pct,
            "slippage": self.slippage_pct,
            "coste de divisa": self.fx_cost_pct,
            "reserva fiscal": self.tax_reserve_rate_pct,
        }
        for label, value in non_negative.items():
            if value < 0:
                raise ValueError(f"El supuesto de {label} no puede ser negativo.")
        limits = {
            "peso por empresa": self.max_position_pct,
            "peso por sector": self.max_sector_pct,
            "rotación diaria": self.max_daily_turnover_pct,
            "efectivo mínimo": self.minimum_cash_pct,
        }
        for label, value in limits.items():
            if not 0 <= value <= 100:
                raise ValueError(f"El límite de {label} debe estar entre 0 y 100.")
        if self.max_position_pct > self.max_sector_pct:
            raise ValueError("El máximo por empresa no puede superar el máximo sectorial.")
        if self.max_order_age_days < 1:
            raise ValueError("Una orden paper debe conservar al menos un día de vigencia.")


@dataclass(frozen=True)
class PaperPosition:
    ticker: str
    quantity: float
    average_cost_eur: float
    last_price_eur: float
    sector: str = "Sin clasificar"
    valuation_mode: str = "market"

    def __post_init__(self) -> None:
        if not self.ticker or self.ticker != self.ticker.upper():
            raise ValueError("La posición paper necesita un ticker normalizado.")
        if self.quantity <= 0 or self.average_cost_eur <= 0 or self.last_price_eur <= 0:
            raise ValueError("Cantidad, coste y precio paper deben ser positivos.")
        if self.valuation_mode not in {"market", "frozen"}:
            raise ValueError("La valoración paper debe ser market o frozen.")

    @property
    def market_value_eur(self) -> float:
        return self.quantity * self.last_price_eur

    @property
    def cost_basis_eur(self) -> float:
        return self.quantity * self.average_cost_eur


@dataclass(frozen=True)
class PaperOrder:
    id: str
    portfolio_id: str
    signal_date: date
    effective_after: date
    ticker: str
    side: str
    target_value_eur: float
    reason: str
    engine_version: str = PAPER_ENGINE_VERSION
    status: str = "pending"
    scenario: str = "strict"
    pair_id: str = ""
    sector: str = "Sin clasificar"
    currency: str = "EUR"
    environment: str = "paper"

    def __post_init__(self) -> None:
        if self.environment != "paper":
            raise ValueError("Una orden del simulador debe permanecer en paper.")
        if self.side not in {"Compra", "Venta"}:
            raise ValueError("El lado paper debe ser Compra o Venta.")
        if self.scenario not in {"strict", "challenger"}:
            raise ValueError("El escenario debe ser strict o challenger.")
        if self.target_value_eur <= 0:
            raise ValueError("El importe objetivo debe ser positivo.")
        if self.effective_after <= self.signal_date:
            raise ValueError("Una orden paper sólo puede ser efectiva después de la señal.")


@dataclass(frozen=True)
class PaperTrade:
    ticker: str
    side: str
    quantity: float
    price: float
    currency: str
    fx_rate_to_eur: float
    gross_eur: float
    fee_eur: float
    spread_eur: float
    slippage_eur: float
    fx_cost_eur: float
    net_cash_eur: float
    realized_pnl_eur: float
    filled_at: date
    order_id: str
    scenario: str
    pair_id: str = ""
    environment: str = "paper"

    @property
    def total_cost_eur(self) -> float:
        return self.fee_eur + self.spread_eur + self.slippage_eur + self.fx_cost_eur


@dataclass(frozen=True)
class PaperState:
    portfolio_id: str
    as_of: date
    cash_eur: float
    positions: tuple[PaperPosition, ...]
    initial_positions: tuple[PaperPosition, ...]
    initial_cash_eur: float
    initial_nav_eur: float
    benchmark_initial_price_eur: float | None = None
    realized_pnl_ytd_eur: float = 0.0
    realized_pnl_year: int = 0
    prior_year_tax_reserve_eur: float = 0.0
    costs_cumulative_eur: float = 0.0
    filled_order_ids: tuple[str, ...] = ()
    trades: tuple[PaperTrade, ...] = ()
    engine_version: str = PAPER_ENGINE_VERSION


@dataclass(frozen=True)
class PaperSnapshot:
    as_of: date
    nav_gross_eur: float
    nav_net_eur: float
    cash_eur: float
    holdings_eur: float
    realized_pnl_ytd_eur: float
    unrealized_pnl_eur: float
    tax_reserve_eur: float
    liquidation_cost_eur: float
    costs_cumulative_eur: float
    benchmark_nav_eur: float | None
    buy_hold_nav_eur: float
    data_coverage_pct: float


@dataclass(frozen=True)
class PaperRunResult:
    run_id: str
    as_of: date
    orders: tuple[PaperOrder, ...]
    trades: tuple[PaperTrade, ...]
    snapshot: PaperSnapshot
    warnings: tuple[str, ...] = ()
    skipped_reasons: tuple[str, ...] = ()


def _position_map(state: PaperState) -> dict[str, PaperPosition]:
    return {position.ticker: position for position in state.positions}


def _state_nav(state: PaperState) -> float:
    return state.cash_eur + sum(position.market_value_eur for position in state.positions)


def _current_tax_reserve(
    state: PaperState,
    assumptions: PaperAssumptions,
) -> float:
    """Pasivo fiscal estimado que todavía no puede tratarse como efectivo libre."""

    current_year = (
        max(0.0, state.realized_pnl_ytd_eur)
        * assumptions.tax_reserve_rate_pct
        / 100.0
    )
    return max(0.0, state.prior_year_tax_reserve_eur) + current_year


def build_paper_rotation_dashboard(
    live_summary: Iterable[Mapping[str, object]],
    state: PaperState,
    favorite_tickers: Iterable[object],
    prepared: Mapping[str, pd.DataFrame],
    assumptions: PaperAssumptions | None = None,
    *,
    pair_correlations: Mapping[tuple[str, str], float] | None = None,
) -> RotationDashboard:
    """Construye el mapa semanal desde el libro paper, no desde la cartera real.

    Los pesos, bases fiscales y posiciones proceden exclusivamente de ``state``.
    Esto permite que una posición comprada por el simulador pueda convertirse en
    origen de una rotación posterior, aunque nunca haya existido en el bróker.
    """

    config = assumptions or PaperAssumptions()
    summary_rows = tuple(dict(row) for row in live_summary)
    frozen = {
        position.ticker
        for position in state.positions
        if position.valuation_mode == "frozen"
    }
    favorites = tuple(
        dict.fromkeys(
            _ticker(value)
            for value in favorite_tickers
            if _ticker(value) and _ticker(value) not in frozen
        )
    )
    held = tuple(
        position.ticker
        for position in state.positions
        if position.valuation_mode == "market"
    )
    nav = _state_nav(state)
    allocations = {
        position.ticker: position.market_value_eur / nav * 100.0
        for position in state.positions
        if nav > 0 and position.valuation_mode == "market"
    }

    currencies = {
        _ticker(row.get("Ticker")): str(row.get("Moneda") or "").strip().upper()
        for row in summary_rows
        if _ticker(row.get("Ticker"))
    }
    for ticker, frame in prepared.items():
        if frame is None:
            continue
        normalized = _ticker(ticker)
        currency = str(
            frame.attrs.get("display_currency")
            or frame.attrs.get("quote_currency")
            or ""
        ).strip().upper()
        if normalized and currency:
            currencies[normalized] = currency

    recovery_hurdles: dict[tuple[str, str], dict[str, object]] = {}
    for position in state.positions:
        if position.valuation_mode != "market":
            continue
        gross_value = position.market_value_eur
        origin_currency = currencies.get(position.ticker, "")
        if gross_value <= 0 or not origin_currency:
            continue
        sell_variable_rate = (
            config.spread_pct
            + config.slippage_pct
            + (config.fx_cost_pct if origin_currency != "EUR" else 0.0)
        ) / 100.0
        sale_cost = min(
            gross_value,
            config.sell_fee_eur + gross_value * sell_variable_rate,
        )
        broker_cash = max(0.0, gross_value - sale_cost)
        realized = broker_cash - position.cost_basis_eur
        tax_reserve = (
            max(0.0, realized) * config.tax_reserve_rate_pct / 100.0
        )
        net_available = max(0.0, broker_cash - tax_reserve)
        for candidate in favorites:
            candidate_currency = currencies.get(candidate, "")
            if not candidate_currency or net_available <= config.buy_fee_eur:
                continue
            buy_variable_rate = (
                config.spread_pct
                + config.slippage_pct
                + (config.fx_cost_pct if candidate_currency != "EUR" else 0.0)
            ) / 100.0
            purchasable = max(
                0.0,
                (net_available - config.buy_fee_eur) / (1.0 + buy_variable_rate),
            )
            if purchasable <= 0:
                continue
            recovery_hurdles[(position.ticker, candidate)] = {
                "Valor bruto": gross_value,
                "Efectivo bróker": broker_cash,
                "Reserva fiscal": tax_reserve,
                "Capital prudente": purchasable,
                "Fricción económica": sale_cost + net_available - purchasable,
                "Umbral recuperación": (gross_value / purchasable - 1.0) * 100.0,
                "Origen coste": "Libro paper",
            }

    correlations = (
        dict(pair_correlations)
        if pair_correlations is not None
        else build_pair_correlations(prepared, held, favorites)
    )
    return build_rotation_dashboard(
        summary_rows,
        held,
        favorites,
        allocations_pct=allocations,
        horizon="Semanal",
        recovery_hurdles=recovery_hurdles,
        pair_correlations=correlations,
        max_company_weight_pct=config.max_position_pct,
        max_sector_weight_pct=config.max_sector_pct,
        require_costs=True,
    )


def _iso_week(value: date) -> tuple[int, int]:
    calendar = value.isocalendar()
    return int(calendar.year), int(calendar.week)


def can_rebalance_on_date(
    state: PaperState,
    as_of: date | datetime | str,
    pending_orders: Iterable[PaperOrder] = (),
    assumptions: PaperAssumptions | None = None,
    *,
    scenario: str = "strict",
    pair_id: str = "",
) -> bool:
    """Indica si el escenario puede iniciar una rotación en esa fecha.

    El método estricto admite como máximo una pareja de rotación por semana
    ISO. Una pareja ya ejecutada se infiere de ``state.trades`` y una todavía
    no ejecutada puede reservar el hueco semanal mediante ``pending_orders``.
    Pasar ``pair_id`` permite completar la segunda pata de la misma rotación,
    pero nunca abrir una pareja distinta. El challenger no comparte este
    límite: es un experimento separado y no una decisión estricta adicional.
    """

    if scenario not in {"strict", "challenger"}:
        raise ValueError("El escenario debe ser strict o challenger.")
    if scenario != "strict":
        return True

    config = assumptions or PaperAssumptions()
    target_date = _as_date(as_of)
    target_week = _iso_week(target_date)
    requested_pair = str(pair_id or "").strip()
    reserved_pairs = {
        str(trade.pair_id or trade.order_id).strip()
        for trade in state.trades
        if trade.environment == "paper"
        and trade.scenario == "strict"
        and _iso_week(trade.filled_at) == target_week
    }
    reserved_pairs.update(
        str(order.pair_id or order.id).strip()
        for order in pending_orders
        if order.environment == "paper"
        and order.status == "pending"
        and order.scenario == "strict"
        and order.portfolio_id == state.portfolio_id
        and order.engine_version == state.engine_version
        and order.id not in state.filled_order_ids
        and order.signal_date <= target_date
        and (target_date - order.signal_date).days <= config.max_order_age_days
    )
    reserved_pairs.discard("")
    if not reserved_pairs:
        return True
    return bool(requested_pair) and reserved_pairs == {requested_pair}


def seed_paper_portfolio(
    positions: Iterable[Mapping[str, object]],
    prices_eur: Mapping[str, float] | None = None,
    *,
    cash_eur: float = 0.0,
    benchmark_price_eur: float | None = None,
    portfolio_id: str = "default",
    as_of: date | datetime | str,
    engine_version: str = PAPER_ENGINE_VERSION,
) -> PaperState:
    """Crea una semilla EUR independiente de la cartera/diario real.

    Cada fila debe contener ticker y cantidad. El precio puede venir en
    ``prices_eur`` o en la propia fila. Si sólo se conoce ``value_eur``, la
    cantidad se puede derivar del precio. Una fila marcada como ``frozen``
    puede usar una unidad sintética cuando sólo se conoce ese valor declarado;
    nunca se revaloriza ni se negocia hasta crear una semilla verificable nueva.
    El coste inicial usa ``cost_basis_eur`` cuando existe; de lo contrario parte
    del valor de mercado de la semilla.
    """

    if cash_eur < 0:
        raise ValueError("El efectivo paper no puede ser negativo.")
    if benchmark_price_eur is not None and benchmark_price_eur <= 0:
        raise ValueError("El precio inicial del benchmark debe ser positivo.")
    normalized_prices = {
        _ticker(ticker): float(price)
        for ticker, price in (prices_eur or {}).items()
        if _ticker(ticker) and _number(price) is not None
    }
    grouped: dict[str, dict[str, float | str]] = {}
    for source in positions:
        ticker = _ticker(_value(source, "ticker", "Ticker", "analysis_ticker"))
        if not ticker:
            continue
        currency = str(_value(source, "currency", "Moneda") or "EUR").upper()
        if currency != "EUR":
            raise ValueError(
                f"{ticker}: la semilla paper v1 necesita valor y precio convertidos a EUR."
            )
        valuation_mode = str(
            _value(source, "valuation_mode", "Modo valoración") or "market"
        ).strip().lower()
        if valuation_mode not in {"market", "frozen"}:
            raise ValueError(f"{ticker}: el modo de valoración paper no es válido.")
        quantity = _number(_value(source, "quantity", "Cantidad"))
        value_eur = _number(
            _value(source, "value_eur", "Valor EUR", "Ahora vale", "market_value_eur")
        )
        row_price = _number(
            _value(source, "price_eur", "current_price_eur", "Precio EUR", "current_price")
        )
        # Una línea ``frozen`` representa el último valor declarado por el
        # bróker cuando no conocemos suficientes unidades/precio para seguirla.
        # Una cotización externa posterior no debe reinterpretar esa unidad
        # sintética ni fabricar una ganancia o pérdida.
        price = (
            row_price
            if valuation_mode == "frozen"
            else normalized_prices.get(ticker, row_price)
        )
        if (
            valuation_mode == "frozen"
            and quantity is None
            and price is None
            and value_eur is not None
            and value_eur > 0
        ):
            quantity = 1.0
            price = value_eur
        if quantity is None and value_eur is not None and price is not None and price > 0:
            quantity = value_eur / price
        if price is None and quantity is not None and quantity > 0 and value_eur is not None:
            price = value_eur / quantity
        if quantity is None or price is None or quantity <= 0 or price <= 0:
            raise ValueError(f"{ticker}: faltan cantidad o precio EUR válidos para la semilla.")
        market_value = value_eur if value_eur is not None and value_eur > 0 else quantity * price
        cost_basis = _number(_value(source, "cost_basis_eur", "Coste EUR"))
        if cost_basis is None:
            average_cost = _number(_value(source, "average_cost_eur", "Precio medio EUR"))
            cost_basis = (
                quantity * average_cost
                if average_cost and average_cost > 0
                else market_value
            )
        if cost_basis <= 0:
            raise ValueError(f"{ticker}: el coste de la posición debe ser positivo.")
        sector = str(_value(source, "sector", "Sector") or "Sin clasificar")
        target = grouped.setdefault(
            ticker,
            {
                "quantity": 0.0,
                "cost_basis": 0.0,
                "value": 0.0,
                "sector": sector,
                "valuation_mode": valuation_mode,
            },
        )
        if target["valuation_mode"] != valuation_mode:
            raise ValueError(
                f"{ticker}: no se pueden mezclar lotes market y frozen en una posición."
            )
        target["quantity"] = float(target["quantity"]) + quantity
        target["cost_basis"] = float(target["cost_basis"]) + cost_basis
        target["value"] = float(target["value"]) + market_value

    paper_positions = tuple(
        PaperPosition(
            ticker=ticker,
            quantity=float(values["quantity"]),
            average_cost_eur=float(values["cost_basis"]) / float(values["quantity"]),
            last_price_eur=float(values["value"]) / float(values["quantity"]),
            sector=str(values["sector"]),
            valuation_mode=str(values["valuation_mode"]),
        )
        for ticker, values in sorted(grouped.items())
    )
    initial_nav = cash_eur + sum(item.market_value_eur for item in paper_positions)
    if initial_nav <= 0:
        raise ValueError("La cartera paper necesita algún valor inicial.")
    initial = tuple(replace(item) for item in paper_positions)
    return PaperState(
        portfolio_id=str(portfolio_id).strip() or "default",
        as_of=_as_date(as_of),
        cash_eur=float(cash_eur),
        positions=paper_positions,
        initial_positions=initial,
        initial_cash_eur=float(cash_eur),
        initial_nav_eur=float(initial_nav),
        benchmark_initial_price_eur=(
            float(benchmark_price_eur) if benchmark_price_eur is not None else None
        ),
        realized_pnl_year=_as_date(as_of).year,
        engine_version=str(engine_version).strip() or PAPER_ENGINE_VERSION,
    )


def _dashboard_rows(dashboard: object, name: str) -> tuple[Mapping[str, object], ...]:
    if isinstance(dashboard, Mapping):
        value = dashboard.get(name, ())
    else:
        value = getattr(dashboard, name, ())
    return tuple(value or ())


def _order_id(
    state: PaperState,
    signal_date: date,
    scenario: str,
    side: str,
    ticker: str,
    pair_id: str,
) -> str:
    raw = "|".join(
        (
            state.portfolio_id,
            state.engine_version,
            signal_date.isoformat(),
            scenario,
            pair_id,
            side,
            ticker,
        )
    )
    return sha256(raw.encode("utf-8")).hexdigest()[:24]


def _make_order(
    state: PaperState,
    *,
    signal_date: date,
    ticker: str,
    side: str,
    value_eur: float,
    reason: str,
    scenario: str,
    pair_id: str,
    sector: str,
) -> PaperOrder:
    return PaperOrder(
        id=_order_id(state, signal_date, scenario, side, ticker, pair_id),
        portfolio_id=state.portfolio_id,
        signal_date=signal_date,
        effective_after=signal_date + timedelta(days=1),
        ticker=ticker,
        side=side,
        target_value_eur=float(value_eur),
        reason=reason,
        engine_version=state.engine_version,
        scenario=scenario,
        pair_id=pair_id,
        sector=sector,
    )


def _capacity_for_candidate(
    state: PaperState,
    ticker: str,
    sector: str,
    assumptions: PaperAssumptions,
) -> float:
    nav = _state_nav(state)
    by_ticker = _position_map(state)
    current = by_ticker.get(ticker)
    current_value = current.market_value_eur if current else 0.0
    company_capacity = max(0.0, nav * assumptions.max_position_pct / 100.0 - current_value)
    sector_value = sum(
        position.market_value_eur
        for position in state.positions
        if position.sector == sector
    )
    sector_capacity = max(0.0, nav * assumptions.max_sector_pct / 100.0 - sector_value)
    return min(company_capacity, sector_capacity)


def _pair_orders(
    state: PaperState,
    *,
    origin: str,
    destination: str,
    destination_sector: str,
    signal_date: date,
    assumptions: PaperAssumptions,
    scenario: str,
    reason: str,
) -> tuple[PaperOrder, ...]:
    origin_position = _position_map(state).get(origin)
    destination_position = _position_map(state).get(destination)
    if (
        origin_position is None
        or origin_position.market_value_eur <= 0
        or origin_position.valuation_mode != "market"
        or (
            destination_position is not None
            and destination_position.valuation_mode != "market"
        )
    ):
        return ()
    nav = _state_nav(state)
    turnover_cap = nav * assumptions.max_daily_turnover_pct / 100.0
    destination_capacity = _capacity_for_candidate(
        state,
        destination,
        destination_sector,
        assumptions,
    )
    if origin_position.sector == destination_sector:
        # Al rotar dentro del mismo sector, la venta libera exactamente el peso
        # que la compra vuelve a ocupar. No debe bloquearse por medir sólo el
        # sector antes de la venta.
        destination_current = _position_map(state).get(destination)
        destination_value = (
            destination_current.market_value_eur if destination_current else 0.0
        )
        company_capacity = max(
            0.0,
            nav * assumptions.max_position_pct / 100.0 - destination_value,
        )
        destination_capacity = min(
            company_capacity,
            destination_capacity + min(origin_position.market_value_eur, turnover_cap),
        )
    moved = min(origin_position.market_value_eur, turnover_cap, destination_capacity)
    if moved <= assumptions.buy_fee_eur + assumptions.sell_fee_eur:
        return ()
    pair_id = sha256(
        f"{state.portfolio_id}|{signal_date}|{scenario}|{origin}|{destination}".encode()
    ).hexdigest()[:16]
    return (
        _make_order(
            state,
            signal_date=signal_date,
            ticker=origin,
            side="Venta",
            value_eur=moved,
            reason=reason,
            scenario=scenario,
            pair_id=pair_id,
            sector=origin_position.sector,
        ),
        _make_order(
            state,
            signal_date=signal_date,
            ticker=destination,
            side="Compra",
            value_eur=moved,
            reason=reason,
            scenario=scenario,
            pair_id=pair_id,
            sector=destination_sector,
        ),
    )


def propose_paper_orders(
    rotation_dashboard: object,
    state: PaperState,
    assumptions: PaperAssumptions | None = None,
    *,
    as_of: date | datetime | str,
    include_challenger: bool = True,
    pending_orders: Iterable[PaperOrder] = (),
) -> tuple[PaperOrder, ...]:
    """Propone dos experimentos separados, nunca órdenes reales.

    ``strict`` replica como máximo el primer salto ya validado por el motor de
    rotación. ``challenger`` toma una favorita no gris con datos suficientes y
    la enfrenta a la posición más débil. Ambos escenarios deben ejecutarse sobre
    copias independientes del mismo estado; ``fill_pending_orders`` permite
    seleccionar explícitamente cuál se rellena.
    """

    config = assumptions or PaperAssumptions()
    signal_date = _as_date(as_of)
    if signal_date < state.as_of:
        raise ValueError("No se pueden proponer órdenes anteriores al estado paper.")
    results: list[PaperOrder] = []
    switches = _dashboard_rows(rotation_dashboard, "switches")
    strict_destinations: set[str] = set()
    if can_rebalance_on_date(
        state,
        signal_date,
        pending_orders,
        config,
        scenario="strict",
    ):
        for switch in switches[:1]:
            origin = _ticker(_value(switch, "Origen", "origin"))
            destination = _ticker(_value(switch, "Alternativa", "destination"))
            if not origin or not destination:
                continue
            destination_sector = str(
                _value(switch, "Sector alternativa", "destination_sector")
                or "Sin clasificar"
            )
            pair = _pair_orders(
                state,
                origin=origin,
                destination=destination,
                destination_sector=destination_sector,
                signal_date=signal_date,
                assumptions=config,
                scenario="strict",
                reason=str(
                    _value(switch, "Lectura", "reason")
                    or "Salto validado por el mapa."
                ),
            )
            if pair:
                results.extend(pair)
                strict_destinations.add(destination)

    if include_challenger:
        candidates = [
            row
            for row in _dashboard_rows(rotation_dashboard, "candidates")
            if str(_value(row, "Color", "color") or "") in {"Azul", "Amarillo"}
            and _ticker(_value(row, "Ticker", "ticker")) not in strict_destinations
            and (_number(_value(row, "Cobertura", "coverage")) or 0.0) >= 80.0
            and (_number(_value(row, "Confianza", "confidence")) or 0.0) >= 60.0
        ]
        candidates.sort(
            key=lambda row: (
                str(_value(row, "Color", "color")) != "Azul",
                -(_number(_value(row, "Score horizonte", "score")) or 0.0),
                _ticker(_value(row, "Ticker", "ticker")),
            )
        )
        held = _position_map(state)
        position_rows = [
            row
            for row in _dashboard_rows(rotation_dashboard, "positions")
            if _ticker(_value(row, "Ticker", "ticker")) in held
        ]
        color_rank = {"Rojo": 0, "Naranja": 1, "Amarillo": 2, "Gris": 3, "Verde": 4, "Azul": 5}
        position_rows.sort(
            key=lambda row: (
                color_rank.get(str(_value(row, "Color", "color") or ""), 9),
                _number(_value(row, "Score horizonte", "score")) or 0.0,
            )
        )
        if candidates and position_rows:
            candidate = candidates[0]
            origin = _ticker(_value(position_rows[0], "Ticker", "ticker"))
            destination = _ticker(_value(candidate, "Ticker", "ticker"))
            sector = str(_value(candidate, "Sector", "sector") or "Sin clasificar")
            pair = _pair_orders(
                state,
                origin=origin,
                destination=destination,
                destination_sector=sector,
                signal_date=signal_date,
                assumptions=config,
                scenario="challenger",
                reason=(
                    "Hipótesis challenger para medir una alternativa; "
                    "no es una recomendación real."
                ),
            )
            results.extend(pair)
    return tuple(results)


def _next_open(
    source: object,
    *,
    after: date,
    through: date,
) -> tuple[date, float] | None:
    if isinstance(source, pd.DataFrame):
        if source.empty or "open" not in source.columns:
            return None
        values = pd.to_numeric(source["open"], errors="coerce").dropna()
    elif isinstance(source, pd.Series):
        values = pd.to_numeric(source, errors="coerce").dropna()
    else:
        return None
    if values.empty:
        return None
    index = pd.to_datetime(values.index, errors="coerce")
    valid = pd.Series(values.to_numpy(), index=index).dropna().sort_index()
    valid = valid.loc[
        (valid.index.date > after) & (valid.index.date <= through)
    ]
    if valid.empty:
        return None
    return valid.index[0].date(), float(valid.iloc[0])


def _fx_to_eur(
    currency: str,
    fx_rates_to_eur: Mapping[str, float] | None,
) -> float | None:
    if currency.upper() == "EUR":
        return 1.0
    value = _number((fx_rates_to_eur or {}).get(currency.upper()))
    return value if value is not None and value > 0 else None


def fill_pending_orders(
    state: PaperState,
    orders: Iterable[PaperOrder],
    bars: Mapping[str, object],
    fx_rates_to_eur: Mapping[str, float] | None = None,
    assumptions: PaperAssumptions | None = None,
    *,
    as_of: date | datetime | str,
    scenario: str = "strict",
) -> tuple[PaperState, tuple[PaperTrade, ...]]:
    """Rellena al primer ``open`` posterior a T, con costes explícitos.

    La misma orden no puede ejecutarse dos veces. Las ventas se procesan antes
    que las compras del mismo escenario para que una rotación pueda financiarse
    sin inventar efectivo.
    """

    if scenario not in {"strict", "challenger"}:
        raise ValueError("El escenario debe ser strict o challenger.")
    config = assumptions or PaperAssumptions()
    current_date = _as_date(as_of)
    if current_date < state.as_of:
        raise ValueError("No se puede valorar el estado paper hacia atrás.")
    selected_by_id: dict[str, PaperOrder] = {}
    filled_id_set = set(state.filled_order_ids)
    for order in orders:
        if not (
            order.environment == "paper"
            and order.status == "pending"
            and order.scenario == scenario
            and order.portfolio_id == state.portfolio_id
            and order.engine_version == state.engine_version
            and order.id not in filled_id_set
            and current_date >= order.effective_after
            and (current_date - order.signal_date).days <= config.max_order_age_days
        ):
            continue
        previous = selected_by_id.get(order.id)
        if previous is not None and previous != order:
            raise ValueError(
                f"El id de orden paper {order.id!r} identifica órdenes distintas."
            )
        selected_by_id.setdefault(order.id, order)
    selected = list(selected_by_id.values())
    position_map = _position_map(state)
    blocked_pairs = {
        order.pair_id or order.id
        for order in selected
        if (
            order.side == "Venta"
            and position_map.get(order.ticker) is not None
            and position_map[order.ticker].valuation_mode != "market"
        )
        or (
            order.side == "Compra"
            and position_map.get(order.ticker) is not None
            and position_map[order.ticker].valuation_mode != "market"
        )
    }
    selected = [
        order for order in selected if (order.pair_id or order.id) not in blocked_pairs
    ]
    selected.sort(
        key=lambda order: (
            order.signal_date,
            order.pair_id,
            order.side != "Venta",
            order.id,
        )
    )
    positions = _position_map(state)
    cash = float(state.cash_eur)
    realized = float(state.realized_pnl_ytd_eur)
    realized_year = state.realized_pnl_year or state.as_of.year
    prior_year_tax_reserve = max(0.0, state.prior_year_tax_reserve_eur)
    if current_date.year != realized_year:
        prior_year_tax_reserve += (
            max(0.0, realized) * config.tax_reserve_rate_pct / 100.0
        )
        realized = 0.0
        realized_year = current_date.year
    costs_cumulative = float(state.costs_cumulative_eur)
    filled_ids = list(dict.fromkeys(state.filled_order_ids))
    filled_id_set = set(filled_ids)
    trades: list[PaperTrade] = []
    nav_reference = max(_state_nav(state), 0.0)
    turnover_cap = nav_reference * config.max_daily_turnover_pct / 100.0
    turnover_by_pair: dict[str, float] = {}
    for trade in state.trades:
        if trade.filled_at != current_date or trade.scenario != scenario:
            continue
        key = trade.pair_id or trade.order_id
        turnover_by_pair[key] = max(turnover_by_pair.get(key, 0.0), trade.gross_eur)

    for order in selected:
        if order.id in filled_id_set:
            continue
        source = bars.get(order.ticker)
        source_currency = str(
            getattr(source, "attrs", {}).get("currency") or ""
        ).strip().upper()
        if source_currency and source_currency != order.currency.upper():
            # Una orden legacy con moneda asumida no puede reinterpretar una
            # apertura nativa como EUR. Se conserva pendiente hasta expirar.
            continue
        execution_cutoff = max(
            order.signal_date,
            state.as_of,
            order.effective_after - timedelta(days=1),
        )
        opening = _next_open(
            source,
            after=execution_cutoff,
            through=current_date,
        )
        fx_rate = _fx_to_eur(order.currency, fx_rates_to_eur)
        if opening is None or fx_rate is None:
            continue
        filled_at, native_price = opening
        price_eur = native_price * fx_rate
        if price_eur <= 0:
            continue
        turnover_key = order.pair_id or order.id
        if order.scenario == "strict" and not can_rebalance_on_date(
            replace(state, trades=state.trades + tuple(trades)),
            filled_at,
            assumptions=config,
            scenario="strict",
            pair_id=turnover_key,
        ):
            continue
        other_turnover = sum(
            value for key, value in turnover_by_pair.items() if key != turnover_key
        )
        turnover_room = max(0.0, turnover_cap - other_turnover)
        if turnover_room <= 0:
            continue
        gross_order_limit = min(order.target_value_eur, turnover_room)
        variable_rate = (config.spread_pct + config.slippage_pct) / 100.0
        fx_rate_cost = config.fx_cost_pct / 100.0 if order.currency != "EUR" else 0.0
        fee = config.sell_fee_eur if order.side == "Venta" else config.buy_fee_eur

        if order.side == "Venta":
            current = positions.get(order.ticker)
            if (
                current is None
                or current.quantity <= 0
                or current.valuation_mode != "market"
            ):
                continue
            gross_target = min(gross_order_limit, current.quantity * price_eur)
            quantity = min(current.quantity, gross_target / price_eur)
            gross = quantity * price_eur
            spread = gross * config.spread_pct / 100.0
            slippage = gross * config.slippage_pct / 100.0
            fx_cost = gross * fx_rate_cost
            total_cost = min(gross, fee + spread + slippage + fx_cost)
            cash_change = gross - total_cost
            allocated_cost = current.average_cost_eur * quantity
            trade_realized = cash_change - allocated_cost
            cash += cash_change
            realized += trade_realized
            remaining = current.quantity - quantity
            if remaining <= 1e-12:
                positions.pop(order.ticker, None)
            else:
                positions[order.ticker] = replace(
                    current,
                    quantity=remaining,
                    last_price_eur=price_eur,
                )
        else:
            current = positions.get(order.ticker)
            if current is not None and current.valuation_mode != "market":
                continue
            current_value = current.quantity * price_eur if current else 0.0
            company_room = max(
                0.0,
                nav_reference * config.max_position_pct / 100.0 - current_value,
            )
            sector_value = sum(
                position.quantity * (
                    price_eur if position.ticker == order.ticker else position.last_price_eur
                )
                for position in positions.values()
                if position.sector == order.sector
            )
            sector_room = max(
                0.0,
                nav_reference * config.max_sector_pct / 100.0 - sector_value,
            )
            minimum_cash = nav_reference * config.minimum_cash_pct / 100.0
            tax_reserve = prior_year_tax_reserve + (
                max(0.0, realized) * config.tax_reserve_rate_pct / 100.0
            )
            available_cash = max(0.0, cash - minimum_cash - tax_reserve)
            denominator = 1.0 + variable_rate + fx_rate_cost
            affordable_gross = max(0.0, (available_cash - fee) / denominator)
            gross = min(gross_order_limit, company_room, sector_room, affordable_gross)
            if gross <= 0:
                continue
            quantity = gross / price_eur
            spread = gross * config.spread_pct / 100.0
            slippage = gross * config.slippage_pct / 100.0
            fx_cost = gross * fx_rate_cost
            total_cost = fee + spread + slippage + fx_cost
            cash_change = -(gross + total_cost)
            trade_realized = 0.0
            cash += cash_change
            purchase_cost = gross + total_cost
            if current is None:
                positions[order.ticker] = PaperPosition(
                    ticker=order.ticker,
                    quantity=quantity,
                    average_cost_eur=purchase_cost / quantity,
                    last_price_eur=price_eur,
                    sector=order.sector,
                )
            else:
                combined_quantity = current.quantity + quantity
                combined_cost = current.cost_basis_eur + purchase_cost
                positions[order.ticker] = replace(
                    current,
                    quantity=combined_quantity,
                    average_cost_eur=combined_cost / combined_quantity,
                    last_price_eur=price_eur,
                )

        costs_cumulative += total_cost
        trade = PaperTrade(
            ticker=order.ticker,
            side=order.side,
            quantity=quantity,
            price=native_price,
            currency=order.currency,
            fx_rate_to_eur=fx_rate,
            gross_eur=gross,
            fee_eur=fee,
            spread_eur=spread,
            slippage_eur=slippage,
            fx_cost_eur=fx_cost,
            net_cash_eur=cash_change,
            realized_pnl_eur=trade_realized,
            filled_at=filled_at,
            order_id=order.id,
            scenario=order.scenario,
            pair_id=order.pair_id,
        )
        trades.append(trade)
        turnover_by_pair[turnover_key] = max(
            turnover_by_pair.get(turnover_key, 0.0), gross
        )
        filled_ids.append(order.id)
        filled_id_set.add(order.id)

    new_state = replace(
        state,
        as_of=current_date,
        cash_eur=max(0.0, cash),
        positions=tuple(sorted(positions.values(), key=lambda item: item.ticker)),
        realized_pnl_ytd_eur=realized,
        realized_pnl_year=realized_year,
        prior_year_tax_reserve_eur=prior_year_tax_reserve,
        costs_cumulative_eur=costs_cumulative,
        filled_order_ids=tuple(filled_ids),
        trades=state.trades + tuple(trades),
    )
    return new_state, tuple(trades)


def mark_to_market(
    state: PaperState,
    prices_eur: Mapping[str, float],
    benchmark_price_eur: float | None,
    assumptions: PaperAssumptions | None = None,
    *,
    as_of: date | datetime | str,
) -> PaperSnapshot:
    """Valora la estrategia y sus dos referencias con los mismos precios."""

    config = assumptions or PaperAssumptions()
    current_date = _as_date(as_of)
    normalized = {
        _ticker(ticker): float(value)
        for ticker, value in prices_eur.items()
        if _ticker(ticker) and _number(value) is not None and float(value) > 0
    }
    holdings = 0.0
    cost_basis = 0.0
    covered_value = 0.0
    liquidation_cost = 0.0
    for position in state.positions:
        has_market_price = (
            position.valuation_mode == "market" and position.ticker in normalized
        )
        price = (
            normalized[position.ticker]
            if has_market_price
            else position.last_price_eur
        )
        if has_market_price:
            covered_value += position.quantity * price
        value = position.quantity * price
        holdings += value
        cost_basis += position.cost_basis_eur
        if position.valuation_mode == "market":
            liquidation_cost += min(
                value,
                config.sell_fee_eur
                + value * (config.spread_pct + config.slippage_pct) / 100.0,
            )
    gross_nav = state.cash_eur + holdings
    tax_reserve = _current_tax_reserve(state, config)
    # Los costes ya ejecutados están descontados del efectivo. El coste de una
    # liquidación futura se muestra aparte, pero no se resta del NAV diario:
    # hacerlo penalizaría la estrategia frente a "mantener", que tampoco vende.
    net_nav = max(0.0, gross_nav - tax_reserve)

    buy_hold = state.initial_cash_eur
    for position in state.initial_positions:
        price = (
            normalized.get(position.ticker, position.last_price_eur)
            if position.valuation_mode == "market"
            else position.last_price_eur
        )
        buy_hold += position.quantity * price
    benchmark_nav: float | None = None
    if (
        benchmark_price_eur is not None
        and benchmark_price_eur > 0
        and state.benchmark_initial_price_eur is not None
        and state.benchmark_initial_price_eur > 0
    ):
        benchmark_nav = (
            state.initial_nav_eur
            * float(benchmark_price_eur)
            / state.benchmark_initial_price_eur
        )
    coverage = covered_value / holdings * 100.0 if holdings > 0 else 100.0
    return PaperSnapshot(
        as_of=current_date,
        nav_gross_eur=gross_nav,
        nav_net_eur=net_nav,
        cash_eur=state.cash_eur,
        holdings_eur=holdings,
        realized_pnl_ytd_eur=state.realized_pnl_ytd_eur,
        unrealized_pnl_eur=holdings - cost_basis,
        tax_reserve_eur=tax_reserve,
        liquidation_cost_eur=liquidation_cost,
        costs_cumulative_eur=state.costs_cumulative_eur,
        benchmark_nav_eur=benchmark_nav,
        buy_hold_nav_eur=buy_hold,
        data_coverage_pct=coverage,
    )


def _return_pct(final: float | None, initial: float | None) -> float | None:
    if final is None or initial is None or initial <= 0:
        return None
    return (final / initial - 1.0) * 100.0


def compute_paper_scorecard(
    snapshots: Sequence[PaperSnapshot] | Iterable[PaperSnapshot],
) -> dict[str, object]:
    """Compara resultado neto con mantener y SPY sin seleccionar ganadores."""

    ordered = sorted(tuple(snapshots), key=lambda item: item.as_of)
    if not ordered:
        return {
            "sessions": 0,
            "strategy_return_pct": None,
            "buy_hold_return_pct": None,
            "benchmark_return_pct": None,
            "excess_vs_hold_pct": None,
            "excess_vs_benchmark_pct": None,
            "maximum_drawdown_pct": None,
            "status": "Sin datos",
        }
    first, latest = ordered[0], ordered[-1]
    strategy_return = _return_pct(latest.nav_net_eur, first.nav_net_eur)
    hold_return = _return_pct(latest.buy_hold_nav_eur, first.buy_hold_nav_eur)
    benchmark_return = _return_pct(latest.benchmark_nav_eur, first.benchmark_nav_eur)
    peak = ordered[0].nav_net_eur
    maximum_drawdown = 0.0
    returns: list[float] = []
    previous = ordered[0].nav_net_eur
    for snapshot in ordered:
        peak = max(peak, snapshot.nav_net_eur)
        if peak > 0:
            maximum_drawdown = min(
                maximum_drawdown,
                (snapshot.nav_net_eur / peak - 1.0) * 100.0,
            )
        if previous > 0 and snapshot is not ordered[0]:
            returns.append(snapshot.nav_net_eur / previous - 1.0)
        previous = snapshot.nav_net_eur
    volatility = (
        float(pd.Series(returns).std(ddof=1) * sqrt(252) * 100.0)
        if len(returns) >= 2
        else None
    )
    excess_hold = (
        strategy_return - hold_return
        if strategy_return is not None and hold_return is not None
        else None
    )
    excess_benchmark = (
        strategy_return - benchmark_return
        if strategy_return is not None and benchmark_return is not None
        else None
    )
    comparisons = [value for value in (excess_hold, excess_benchmark) if value is not None]
    status = (
        "Mejora ambas referencias"
        if len(comparisons) == 2 and all(value > 0 for value in comparisons)
        else "No mejora ambas referencias"
        if comparisons
        else "Referencia incompleta"
    )
    return {
        "sessions": len(ordered),
        "from": first.as_of,
        "through": latest.as_of,
        "strategy_return_pct": strategy_return,
        "buy_hold_return_pct": hold_return,
        "benchmark_return_pct": benchmark_return,
        "excess_vs_hold_pct": excess_hold,
        "excess_vs_benchmark_pct": excess_benchmark,
        "maximum_drawdown_pct": maximum_drawdown,
        "annualized_volatility_pct": volatility,
        "costs_cumulative_eur": latest.costs_cumulative_eur,
        "data_coverage_pct": latest.data_coverage_pct,
        "status": status,
    }


compute_paper_metrics = compute_paper_scorecard
