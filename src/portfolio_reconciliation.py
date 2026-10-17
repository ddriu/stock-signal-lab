"""Read-only, snapshot-anchored portfolio accounting.

A snapshot is an opening balance, never another purchase. Only transactions
strictly after its account cutoff change current holdings. Historical operations
are retained for results, but seed opening FIFO lots only with an explicit
``operations_complete`` coverage assertion and matching quantity/cost.

Amounts without sufficient evidence are ``None`` (N/D), not zero. ``*_known_eur``
fields are documented subtotals, not estimates of the missing remainder. No
network, persistence, present-day FX fallback, synthetic trades, or assumed tax
rates are used here. DataFrames supplied by callers are never mutated.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

import pandas as pd

from src.instruments import resolve_analysis_ticker
from src.journal import consume_fifo_lots


EPSILON = 1e-8
EVENT_TYPES = {
    "contribution": "contribution", "deposit": "contribution", "aportacion": "contribution",
    "aportación": "contribution", "withdrawal": "withdrawal", "retirada": "withdrawal",
    "dividend": "dividend", "dividendo": "dividend", "interest": "interest",
    "interes": "interest", "interés": "interest", "tax": "tax", "withholding": "tax",
    "retencion": "tax", "retención": "tax", "fee": "fee", "commission": "fee",
    "comision": "fee", "comisión": "fee", "fx_cost": "fx_cost",
    "principal_return": "principal_return", "repayment": "principal_return",
    "transfer_in": "transfer_in", "transfer_out": "transfer_out",
}


@dataclass(frozen=True)
class ReconciledPortfolioResult:
    positions: pd.DataFrame
    realized_trades: pd.DataFrame
    cash_flows: pd.DataFrame
    metrics_global: dict[str, object]
    metrics_by_platform: pd.DataFrame
    metrics_by_asset: pd.DataFrame
    monthly: pd.DataFrame
    annual: pd.DataFrame
    issues: tuple[str, ...] = ()


def _text(value: object, default: str = "") -> str:
    return default if value is None or pd.isna(value) else str(value).strip()


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(parsed) else pd.Timestamp(parsed).tz_localize(None)


def _truth(value: object) -> bool:
    return value is True or _text(value).casefold() in {"true", "1", "yes", "si", "sí"}


def _covered(coverage: Mapping[str, object], flag: str, platform: str = "") -> bool:
    platforms = coverage.get("per_platform", {})
    if isinstance(platforms, Mapping) and platform in platforms:
        local = platforms[platform]
        if isinstance(local, Mapping) and flag in local:
            return _truth(local[flag])
    return _truth(coverage.get(flag, False))


def _key(row: Mapping[str, object]) -> tuple[str, str, str, str]:
    platform = _text(row.get("platform")) or _text(row.get("account_name"), "Sin especificar")
    account = _text(row.get("account_name")) or platform
    ticker = _text(row.get("analysis_ticker")) or _text(row.get("ticker"))
    raw_identifier = _text(row.get("raw_identifier")).upper()
    if not ticker and _is_isin(raw_identifier):
        ticker = raw_identifier
    ticker = resolve_analysis_ticker(ticker.upper()) if ticker else ""
    identity = ticker or "manual:" + (
        _text(row.get("raw_identifier")) or _text(row.get("asset_name"), "Sin identificar")
    )
    return platform, account, identity, _text(row.get("currency"), "EUR").upper()


def _is_isin(value: str) -> bool:
    return len(value) == 12 and value[:2].isalpha() and value[2:].isalnum() and value[-1].isdigit()


def _liquidity(row: Mapping[str, object]) -> str:
    kind = _text(row.get("asset_type")).casefold()
    name = _text(row.get("asset_name")).casefold()
    # Cash is tested first: repaid Sego principal must never remain invested.
    if kind in {"cash", "efectivo", "liquidez", "saldo"} or name in {"efectivo", "cash", "saldo disponible"}:
        return "cash"
    if _text(row.get("platform")) in {"Civislend", "Segofactoring"} or kind in {"factoring", "crowdlending", "préstamo", "prestamo"}:
        return "illiquid"
    return "liquid"


def _eur(row: Mapping[str, object], field: str, *, native: str | None = None) -> float | None:
    amount = _number(row.get(field))
    if amount is not None:
        return amount
    local = _number(row.get(native)) if native else None
    if local is None:
        return None
    if _text(row.get("currency"), "EUR").upper() == "EUR":
        return local
    fx = _number(row.get("fx_rate_to_eur"))
    quality = _text(row.get("fx_quality")).casefold()
    return local * fx if fx is not None and fx > 0 and quality not in {"reference", "estimated", "unknown", "missing"} else None


def _settlement(row: Mapping[str, object]) -> float | None:
    recorded = _number(row.get("settlement_amount_eur"))
    if recorded is not None:
        return abs(recorded)
    local = _local_settlement(row)
    return _eur({**row, "local_settlement": local}, "__none", native="local_settlement")


def _local_settlement(row: Mapping[str, object]) -> float | None:
    recorded = _number(row.get("settlement_amount_local"))
    if recorded is not None:
        return abs(recorded)
    if _text(row.get("currency"), "EUR").upper() == "EUR":
        recorded = _number(row.get("settlement_amount_eur"))
        if recorded is not None:
            return abs(recorded)
    quantity, price = _number(row.get("quantity")), _number(row.get("price"))
    fee = _number(row.get("fees"))
    if quantity is None or price is None or fee is None:
        return None
    if _text(row.get("fee_quality")).casefold() in {"missing", "unknown", "incomplete"}:
        return None
    return quantity * price + (fee if _side(row) == "buy" else -fee)


def _side(row: Mapping[str, object]) -> str:
    return {"compra": "buy", "buy": "buy", "venta": "sell", "sell": "sell"}.get(_text(row.get("side")).casefold(), "")


def _records(frame: pd.DataFrame | None, *, dates: tuple[str, ...], as_of: pd.Timestamp | None, issues: list[str]) -> list[dict[str, object]]:
    if frame is None or frame.empty:
        return []
    result: list[dict[str, object]] = []
    seen: dict[tuple[str, str, str], dict[str, object]] = {}
    for index, original in enumerate(frame.to_dict("records")):
        row = dict(original)
        executed = next((_date(row.get(field)) for field in dates if _date(row.get(field)) is not None), None)
        if executed is None:
            issues.append("Movimiento sin fecha válida: no se ha aplicado.")
            continue
        if as_of is not None and executed.normalize() > as_of.normalize():
            continue
        identifier = next((_text(row.get(field)) for field in ("source_event_id", "operation_id", "event_id", "source_row_key", "id") if _text(row.get(field))), "")
        if identifier:
            dedup_key = (_key(row)[0], _key(row)[1], identifier)
            if dedup_key in seen:
                # Stable source IDs provide idempotence; content-only deduplication
                # would silently discard two legitimate identical fills.
                comparable = {key: _text(value) for key, value in row.items() if key not in {"id", "created_at", "updated_at"}}
                previous = {key: _text(value) for key, value in seen[dedup_key].items() if key not in {"id", "created_at", "updated_at"}}
                if comparable != previous:
                    issues.append(f"Identificador duplicado con datos distintos: {identifier}; se conserva la primera fila.")
                continue
            seen[dedup_key] = row.copy()
        row["_date"] = executed
        source_order = _number(row.get("source_row"))
        # Database reads commonly return newest IDs first. Preserve the source
        # statement order for dated imported fills instead of reversing their
        # FIFO sequence when several executions share a day-only timestamp.
        documented_order = source_order is not None and source_order >= 2 and _text(row.get("source_sheet")) and identifier
        row["_order"] = source_order if documented_order else index
        result.append(row)
    return sorted(result, key=lambda row: (row["_date"], row["_order"]))


def _new_state(row: Mapping[str, object], *, opening: bool = False) -> dict[str, object]:
    platform, account, identity, currency = _key(row)
    quantity = _number(row.get("quantity")) if opening else 0.0
    cost = _number(row.get("cost_basis_eur"))
    if cost is None:
        cost = _number(row.get("cost_estimate_eur"))
    state: dict[str, object] = {
        "row": dict(row), "key": (platform, account, identity, currency),
        "quantity": quantity, "quantity_complete": quantity is not None,
        "lots": [], "basis_quality": "Sin coste documentado", "last_operation_date": "",
        "opening": opening, "has_trades": False,
    }
    if opening and quantity is not None and quantity > EPSILON:
        local_cost = _number(row.get("cost_basis_local"))
        if local_cost is None and currency == "EUR":
            local_cost = cost
        state["lots"] = [{"quantity": quantity, "cost_basis_eur": cost, "cost_basis_local": local_cost,
                          "currency": currency, "aggregate": True}]
        state["basis_quality"] = "Coste agregado de la fotografía; FIFO de origen no acreditado"
    return state


def _apply_trade(state: dict[str, object], row: Mapping[str, object], realized: list[dict[str, object]], issues: list[str]) -> None:
    state["has_trades"] = True
    quantity = _number(row.get("quantity"))
    side = _side(row)
    platform, account, identity, currency = state["key"]
    execution_currency = _text(row.get("currency"), currency).upper()
    if quantity is None or quantity <= 0 or not side:
        state["quantity_complete"] = False
        issues.append(f"Operación inválida en {platform}/{identity}: cantidad o tipo no acreditados.")
        return
    state["last_operation_date"] = row["_date"].date().isoformat()
    lots = state["lots"]
    assert isinstance(lots, list)
    current = state["quantity"]
    if current is None:
        state["quantity_complete"] = False
        current = 0.0
    settlement = _settlement(row)
    settlement_local = _local_settlement(row)
    if side == "buy":
        lots.append({"quantity": quantity, "cost_basis_eur": settlement, "cost_basis_local": settlement_local,
                     "currency": execution_currency, "aggregate": False})
        state["quantity"] = float(current) + quantity
        if not any(lot.get("aggregate") for lot in lots) and "histórico reconciliado" not in str(state["basis_quality"]):
            state["basis_quality"] = "FIFO de operaciones documentadas"
        return
    available = sum(float(lot["quantity"]) for lot in lots)
    missing_inventory = quantity > available + EPSILON
    if missing_inventory:
        state["quantity_complete"] = False
        issues.append(f"Venta sin inventario suficiente en {platform}/{identity}; resultado N/D.")
    remaining = quantity
    for lot in lots:
        consumed = min(remaining, float(lot["quantity"]))
        if consumed <= EPSILON:
            break
        if lot.get("aggregate") and consumed < float(lot["quantity"]) - EPSILON:
            lot["cost_basis_eur"] = None
            lot["cost_basis_local"] = None
            state["basis_quality"] = "FIFO incompleto tras venta parcial de un saldo agregado"
        remaining -= consumed
    # A security's quote currency can differ from the broker execution currency.
    # Local proceeds cannot be compared with a lot denominated in another unit.
    # Keep the original lot cost for its remainder, while allocating an optional
    # same-execution-currency cost for this disposal only.
    for lot in lots:
        lot["_sale_cost_basis_local"] = lot.get("cost_basis_local") if _text(lot.get("currency"), currency) == execution_currency else None
    removed, _ = consume_fifo_lots(lots, quantity,
        cost_fields=("cost_basis_eur", "cost_basis_local", "_sale_cost_basis_local"))
    for lot in lots:
        lot.pop("_sale_cost_basis_local", None)
    removed_cost = None if missing_inventory else removed["cost_basis_eur"]
    pnl = settlement - removed_cost if settlement is not None and removed_cost is not None else None
    removed_local = None if missing_inventory else removed["_sale_cost_basis_local"]
    pnl_local = settlement_local - removed_local if settlement_local is not None and removed_local is not None else None
    realized.append({
        "executed_at": row["_date"].date().isoformat(), "platform": platform,
        "account_name": account, "ticker": identity, "currency": execution_currency,
        "inventory_currency": currency,
        "quantity": quantity, "proceeds_eur": settlement, "cost_basis_eur": removed_cost,
        "realized_pnl_eur": pnl, "complete": pnl is not None,
        "proceeds_local": settlement_local, "cost_basis_local": removed_local,
        "realized_pnl_local": pnl_local,
        "source_row_key": row.get("source_row_key", row.get("operation_id", row.get("id", ""))),
    })
    state["quantity"] = float(current) - quantity


def _known_sum(rows: list[dict[str, object]], field: str) -> tuple[float, bool]:
    values = [_number(row.get(field)) for row in rows]
    return sum(value for value in values if value is not None), all(value is not None for value in values)


def _realized_inventory_key(row: Mapping[str, object]) -> tuple[str, str, str, str]:
    return (_text(row.get("platform")), _text(row.get("account_name")), _text(row.get("ticker")),
            _text(row.get("inventory_currency")) or _text(row.get("currency")))


def _position(state: dict[str, object], prices: Mapping[object, object], realized: list[dict[str, object]]) -> dict[str, object]:
    row = dict(state["row"])
    platform, account, identity, currency = state["key"]
    ticker = "" if identity.startswith("manual:") else identity
    analysis_ticker = "" if _is_isin(ticker) else ticker
    row.update(platform=platform, account_name=account, ticker=ticker, analysis_ticker=analysis_ticker, currency=currency)
    liquidity = _liquidity(row)
    quantity = state["quantity"] if state["quantity_complete"] else None
    lots = state["lots"]
    cost, complete_cost = _known_sum(lots, "cost_basis_eur")
    local_cost, complete_local_cost = _known_sum(lots, "cost_basis_local")
    local_currencies = {_text(lot.get("currency"), currency) for lot in lots}
    complete_local_cost = complete_local_cost and len(local_currencies) <= 1
    preserve_opening_cost = state["opening"] and not state["has_trades"] and not lots
    if (liquidity in {"illiquid", "cash"} or preserve_opening_cost) and not lots:
        cost = _number(row.get("cost_basis_eur"))
        if cost is None:
            cost = _number(row.get("cost_estimate_eur"))
        if liquidity == "cash":
            cost = _number(row.get("value_eur"))
        complete_cost = cost is not None
    cost = cost if complete_cost and (state["quantity_complete"] or liquidity in {"cash", "illiquid"} or preserve_opening_cost) else None
    value = _number(row.get("value_eur"))
    quote = prices.get((account, ticker, currency), prices.get((platform, ticker, currency), prices.get(ticker))) if ticker else None
    old_quantity = _number(row.get("quantity"))
    status = _text(row.get("valuation_status")) or "Valoración declarada de la fotografía"
    market_as_of = _text(row.get("market_as_of")) or _text(row.get("snapshot_date"))
    if ticker and quantity is None and state["has_trades"]:
        value = None
    elif ticker and quote is not None and quantity is not None:
        quote_row = dict(quote) if isinstance(quote, Mapping) else {"price": quote, "currency": currency}
        quote_row.setdefault("currency", currency)
        price = _number(quote_row.get("price", quote_row.get("current_price")))
        unit_eur = _eur({**quote_row, "unit": price}, "price_eur", native="unit")
        value = quantity * unit_eur if unit_eur is not None else None
        row["current_price"] = price
        status = "Precio aportado" if value is not None else "Precio pendiente" if price is None else "Cambio de moneda pendiente"
        market_as_of = _text(quote_row.get("as_of", quote_row.get("market_as_of")))
    elif ticker and old_quantity is not None and old_quantity > EPSILON and value is not None and quantity is not None:
        value *= quantity / old_quantity
        if state["last_operation_date"]:
            status = "Último precio de fotografía; cantidad reconciliada"
    elif ticker and quantity is not None and quantity > EPSILON:
        value = None
        status = "Precio pendiente"
    unrealized = value - cost if value is not None and cost is not None else None
    if liquidity == "cash":
        unrealized = 0.0 if value is not None else None
    sales = [trade for trade in realized if _realized_inventory_key(trade) == state["key"]]
    realized_known, realized_complete = _known_sum(sales, "realized_pnl_eur")
    row.update(
        quantity=quantity, quantity_complete=state["quantity_complete"],
        value_eur=value, value_complete=value is not None, cost_basis_eur=cost,
        cost_estimate_eur=cost, cost_basis_complete=cost is not None,
        cost_basis_local=local_cost if complete_local_cost else None,
        cost_basis_local_currency=next(iter(local_currencies)) if complete_local_cost and local_currencies else "",
        unrealized_pnl_eur=unrealized, unrealized_pnl_complete=unrealized is not None,
        return_pct=unrealized / cost * 100 if unrealized is not None and cost is not None and cost > 0 else None,
        gain_loss_eur=unrealized, realized_pnl_eur=realized_known if realized_complete else None,
        realized_pnl_known_eur=realized_known, realized_pnl_complete=realized_complete,
        liquidity_class=liquidity, basis_quality=state["basis_quality"],
        valuation_status=status, market_as_of=market_as_of,
        operation_as_of=state["last_operation_date"],
    )
    return row


def _metrics(positions: list[dict[str, object]], sales: list[dict[str, object]], events: list[dict[str, object]], operations: list[dict[str, object]], coverage: Mapping[str, object], platform: str = "") -> dict[str, object]:
    platforms = set(_text(row.get("platform")) for row in positions + sales + events + operations)
    def covered(flag: str) -> bool:
        return _covered(coverage, flag, platform) if platform else bool(platforms) and all(_covered(coverage, flag, item) for item in platforms)
    result: dict[str, object] = {}
    for field, stem in (("value_eur", "value"), ("cost_basis_eur", "cost_basis"), ("unrealized_pnl_eur", "unrealized_pnl")):
        known, complete = _known_sum(positions, field)
        result[f"{stem}_eur"] = known if complete else None
        result[f"{stem}_known_eur"] = known
        result[f"{stem}_complete"] = complete
    for liquidity in ("liquid", "illiquid", "cash"):
        known, complete = _known_sum([row for row in positions if row["liquidity_class"] == liquidity], "value_eur")
        result[f"{liquidity}_value_eur"] = known if complete else None
    known, complete = _known_sum(sales, "realized_pnl_eur")
    result.update(realized_pnl_known_eur=known, realized_pnl_complete=complete and covered("operations_complete"))
    result["realized_pnl_eur"] = known if result["realized_pnl_complete"] else None
    components: dict[str, tuple[float, bool]] = {}
    for kind, stem in (("contribution", "contributed_capital"), ("withdrawal", "withdrawn_capital"), ("dividend", "dividends"), ("interest", "interest"), ("tax", "tax"), ("fee", "fees"), ("fx_cost", "fx_cost")):
        known, complete = _known_sum([row for row in events if row["event_type"] == kind], "amount_eur")
        if kind == "fee":
            fees = [_eur(row, "fee_eur", native="fees") for row in operations]
            known += sum(value for value in fees if value is not None)
            complete = complete and all(value is not None for value in fees) and all(_text(row.get("fee_quality")).casefold() not in {"unknown", "missing", "incomplete"} for row in operations)
        if kind == "fx_cost":
            costs = [_number(row.get("fx_cost_eur")) for row in operations]
            known += sum(value for value in costs if value is not None)
            complete = complete and all(value is not None for value in costs)
        flag = "capital_flows_complete" if kind in {"contribution", "withdrawal"} else "income_complete" if kind in {"dividend", "interest"} else "costs_complete"
        components[stem] = known, complete and covered(flag)
        result[f"{stem}_known_eur"] = known
        result[f"{stem}_eur"] = known if components[stem][1] else None
    result["capital_flows_complete"] = components["contributed_capital"][1] and components["withdrawn_capital"][1]
    result["net_contributions_eur"] = components["contributed_capital"][0] - components["withdrawn_capital"][0] if result["capital_flows_complete"] else None
    result["income_complete"] = components["dividends"][1] and components["interest"][1]
    result["costs_complete"] = all(components[item][1] for item in ("tax", "fees", "fx_cost"))
    # Trade P/L already includes embedded settlement fees/FX. Separately recorded
    # tax/fee/FX events are deducted; operation fee disclosures are never twice.
    external_costs = [row for row in events if row["event_type"] in {"tax", "fee", "fx_cost"}]
    external_known, external_complete = _known_sum(external_costs, "amount_eur")
    pnl_complete = bool(result["realized_pnl_complete"] and result["unrealized_pnl_complete"] and result["income_complete"] and result["costs_complete"] and external_complete)
    pnl_known = result["realized_pnl_known_eur"] + result["unrealized_pnl_known_eur"] + components["dividends"][0] + components["interest"][0] - external_known
    result["total_pnl_known_eur"] = pnl_known
    result["total_pnl_eur"] = pnl_known if pnl_complete else None
    # A deposit is not necessarily the capital exposed for the entire period.
    # Time-weighted / money-weighted returns require dated valuation/flow coverage.
    result["total_return_pct"] = None
    return result


def _history(snapshot: pd.DataFrame, sales: list[dict[str, object]], events: list[dict[str, object]], operations: list[dict[str, object]], coverage: Mapping[str, object], valuations: pd.DataFrame | None, frequency: str) -> pd.DataFrame:
    """Activity by period plus actual wealth marks, never cumulative purchases."""
    activity: list[dict[str, object]] = []
    scope = {_key(row)[0] for row in snapshot.to_dict("records") + sales + events + operations}
    def complete_coverage(flag: str) -> bool:
        return bool(scope) and all(_covered(coverage, flag, platform) for platform in scope)
    for trade in sales:
        activity.append({"date": trade["executed_at"], "realized_pnl_eur": trade["realized_pnl_eur"]})
    for operation in operations:
        fee = _eur(operation, "fee_eur", native="fees")
        if _text(operation.get("fee_quality")).casefold() in {"unknown", "missing", "incomplete"}:
            fee = None
        activity.append({"date": operation["_date"], "fees_eur": fee, "fx_cost_eur": _number(operation.get("fx_cost_eur"))})
    event_fields = {"contribution": "contributed_capital_eur", "withdrawal": "withdrawn_capital_eur", "dividend": "dividends_eur", "interest": "interest_eur", "tax": "tax_eur", "fee": "fees_eur", "fx_cost": "fx_cost_eur"}
    for event in events:
        field = event_fields.get(str(event["event_type"]))
        if field:
            activity.append({"date": event["executed_at"], field: event["amount_eur"]})
    marks: list[dict[str, object]] = []
    if not snapshot.empty and "snapshot_date" in snapshot:
        accounts = {_key(row)[:2] for row in snapshot.to_dict("records")}
        for day, rows in snapshot.groupby("snapshot_date"):
            values = [_number(row.get("value_eur")) for row in rows.to_dict("records")]
            full_scope = {_key(row)[:2] for row in rows.to_dict("records")} == accounts
            marks.append({"date": day, "value_eur": sum(values) if full_scope and all(value is not None for value in values) else None})
    if valuations is not None and not valuations.empty:
        marks.extend(valuations.to_dict("records"))
    dates = [_date(row.get("date", row.get("valuation_date"))) for row in activity + marks]
    periods = sorted({day.to_period(frequency) for day in dates if day is not None})
    rows = []
    for period in periods:
        row: dict[str, object] = {"period": str(period), "total_return_pct": None}
        matching = [item for item in activity if _date(item["date"]).to_period(frequency) == period]
        for field in ["realized_pnl_eur", *event_fields.values()]:
            entries = [item for item in matching if field in item]
            known, complete = _known_sum(entries, field)
            flag = "operations_complete" if field == "realized_pnl_eur" else "capital_flows_complete" if field in {"contributed_capital_eur", "withdrawn_capital_eur"} else "income_complete" if field in {"dividends_eur", "interest_eur"} else "costs_complete"
            row[field] = known if complete and complete_coverage(flag) else None
            row[field.replace("_eur", "_known_eur")] = known
        period_marks = [item for item in marks if (day := _date(item.get("date", item.get("valuation_date")))) is not None and day.to_period(frequency) == period]
        latest = max(period_marks, key=lambda item: _date(item.get("date", item.get("valuation_date")))) if period_marks else None
        row["net_contributions_eur"] = row["contributed_capital_eur"] - row["withdrawn_capital_eur"] if row["contributed_capital_eur"] is not None and row["withdrawn_capital_eur"] is not None else None
        row["ending_value_eur"] = _number(latest.get("value_eur")) if latest else None
        row["valuation_date"] = _date(latest.get("date", latest.get("valuation_date"))).date().isoformat() if latest else ""
        row["valuation_is_period_end"] = bool(latest and _date(row["valuation_date"]).normalize() == period.end_time.normalize())
        rows.append(row)
    return pd.DataFrame(rows, columns=["period", "ending_value_eur", "valuation_date", "valuation_is_period_end", "realized_pnl_eur", "realized_pnl_known_eur", "contributed_capital_eur", "contributed_capital_known_eur", "withdrawn_capital_eur", "withdrawn_capital_known_eur", "net_contributions_eur", "dividends_eur", "dividends_known_eur", "interest_eur", "interest_known_eur", "tax_eur", "tax_known_eur", "fees_eur", "fees_known_eur", "fx_cost_eur", "fx_cost_known_eur", "total_return_pct"])


def build_reconciled_portfolio(
    snapshot: pd.DataFrame,
    operations: pd.DataFrame,
    events: pd.DataFrame | None = None,
    *,
    as_of: object | None = None,
    prices: Mapping[object, object] | None = None,
    coverage: Mapping[str, object] | None = None,
    valuation_history: pd.DataFrame | None = None,
) -> ReconciledPortfolioResult:
    """Reconcile normalized imported records without any persistence side effect.

    ``coverage`` flags default False: operations_complete, capital_flows_complete,
    income_complete, costs_complete; per_platform may override these. Operation
    keys are platform/account_name/ticker/currency. Events use event_type,
    executed_at (or event_date/date), amount_eur (or amount with recorded FX).
    A source_row_key/operation_id/event_id deduplicates repeated imports.
    Same-day trades require after_snapshot=True or phase='post_snapshot'.
    ``prices`` accepts ticker or (account,ticker,currency) -> native price or
    {price, currency, fx_rate_to_eur, as_of}; no FX is inferred. Valuation history
    accepts actual whole-portfolio {date,value_eur} marks, not cumulative flows.
    """
    issues: list[str] = []
    coverage = coverage or {}
    prices = prices or {}
    cutoff_date = _date(as_of) if as_of is not None else None
    if as_of is not None and cutoff_date is None:
        raise ValueError("La fecha de reconciliación no es válida.")
    if cutoff_date is not None:
        safe_prices = {}
        for key, quote in prices.items():
            quote_date = _date(quote.get("as_of", quote.get("market_as_of"))) if isinstance(quote, Mapping) else None
            if quote_date is not None and quote_date.normalize() > cutoff_date.normalize():
                issues.append("Cotización posterior a la fecha solicitada: no se ha utilizado.")
            else:
                safe_prices[key] = quote
        prices = safe_prices
        if valuation_history is not None and not valuation_history.empty:
            valuation_history = valuation_history.loc[valuation_history.apply(lambda row: (day := _date(row.get("date", row.get("valuation_date")))) is not None and day.normalize() <= cutoff_date.normalize(), axis=1)].copy()
    snapshot = snapshot.copy(deep=True)
    if not snapshot.empty:
        snapshot["_date"] = snapshot.get("snapshot_date", pd.Series(index=snapshot.index, dtype=object)).map(_date)
        if snapshot["_date"].isna().any():
            raise ValueError("La fotografía necesita una fecha válida en todas las posiciones.")
        if cutoff_date is not None:
            snapshot = snapshot.loc[snapshot["_date"].map(lambda value: value.normalize() <= cutoff_date.normalize())].copy()
    ops = _records(operations, dates=("executed_at", "date"), as_of=cutoff_date, issues=issues)
    event_rows = _records(events, dates=("executed_at", "event_date", "date"), as_of=cutoff_date, issues=issues)
    account_dates: dict[tuple[str, str], pd.Timestamp] = {}
    for row in snapshot.to_dict("records"):
        key = _key(row)
        account_dates[key[:2]] = max(account_dates.get(key[:2], row["_date"]), row["_date"])
    latest_rows = [row for row in snapshot.to_dict("records") if row["_date"] == account_dates[_key(row)[:2]]]
    # Inventory identifies the security held by an account, not the currency in
    # which that broker executed it. A unique snapshot currency is sufficient
    # evidence to join EUR executions to a USD-quoted position. Without that
    # evidence (or with multiple declared currencies), preserve separate lots.
    snapshot_currencies: dict[tuple[str, str, str], set[str]] = {}
    for row in latest_rows:
        key = _key(row)
        if not key[2].startswith("manual:") and _liquidity(row) == "liquid":
            snapshot_currencies.setdefault(key[:3], set()).add(key[3])
    def inventory_key(row: Mapping[str, object]) -> tuple[str, str, str, str]:
        key = _key(row)
        currencies = snapshot_currencies.get(key[:3], set())
        return (*key[:3], next(iter(currencies))) if len(currencies) == 1 else key
    def trade_state(row: Mapping[str, object], key: tuple[str, str, str, str]) -> dict[str, object]:
        state = _new_state(row)
        state["key"] = key
        return state
    default_cutoff = max(account_dates.values()) if account_dates else None
    def post_snapshot(row: Mapping[str, object]) -> bool:
        cutoff = account_dates.get(_key(row)[:2], default_cutoff)
        if cutoff is None:
            return True
        day = row["_date"].normalize()
        if _text(row.get("phase")) == "historical":
            return False
        return day > cutoff.normalize() or day == cutoff.normalize() and (_truth(row.get("after_snapshot")) or _text(row.get("phase")) == "post_snapshot")
    historical: dict[tuple[str, str, str, str], dict[str, object]] = {}
    historical_sales: list[dict[str, object]] = []
    for operation in ops:
        if not post_snapshot(operation):
            key = inventory_key(operation)
            state = historical.setdefault(key, trade_state(operation, key))
            _apply_trade(state, operation, historical_sales, issues)
    states: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for row in latest_rows:
        key = _key(row)
        if key in states:
            raise ValueError(f"Posición duplicada en la fotografía: {key[0]}/{key[2]}/{key[3]}.")
        state = _new_state(row, opening=True)
        old = historical.get(key)
        if old is not None and _covered(coverage, "operations_complete", key[0]) and old["quantity_complete"] and state["quantity_complete"]:
            old_cost, old_complete = _known_sum(old["lots"], "cost_basis_eur")
            snapshot_cost = _number(row.get("cost_estimate_eur", row.get("cost_basis_eur")))
            if abs(float(old["quantity"]) - float(state["quantity"])) <= EPSILON and (snapshot_cost is None or old_complete and math.isclose(old_cost, snapshot_cost, abs_tol=0.02)):
                state["lots"] = [dict(lot) for lot in old["lots"]]
                state["basis_quality"] = "FIFO histórico reconciliado con la fotografía"
            else:
                issues.append(f"FIFO histórico no reconciliado con la fotografía en {key[0]}/{key[2]}; se conserva el saldo declarado.")
        states[key] = state
    realized = list(historical_sales)
    post_ops = [row for row in ops if post_snapshot(row)]
    for operation in post_ops:
        key = inventory_key(operation)
        state = states.setdefault(key, trade_state(operation, key))
        _apply_trade(state, operation, realized, issues)
    normalized_events: list[dict[str, object]] = []
    for event in event_rows:
        kind = EVENT_TYPES.get(_text(event.get("event_type")).casefold())
        if not kind:
            issues.append(f"Evento no clasificado: {_text(event.get('event_type'), 'sin tipo')}; no se ha supuesto una aportación.")
            continue
        key = _key(event)
        amount = _eur(event, "amount_eur", native="amount")
        normalized_events.append({**event, "executed_at": event["_date"].date().isoformat(), "platform": key[0], "account_name": key[1], "ticker": "" if key[2].startswith("manual:") else key[2], "currency": key[3], "event_type": kind, "amount_eur": abs(amount) if amount is not None else None})
    # Cash is not an independent stale total after buys/sales. A missing cash
    # opening balance is flagged rather than fabricated as zero.
    cash_states: dict[tuple[str, str, str], dict[str, object]] = {}
    for state in states.values():
        if _liquidity(state["row"]) == "cash":
            key = state["key"]
            cash_states[(key[0], key[1], key[3])] = state
    cash_changes: list[tuple[Mapping[str, object], float | None]] = [(row, (1 if _side(row) == "sell" else -1) * amount if (amount := _settlement(row)) is not None else None) for row in post_ops]
    event_sign = {"contribution": 1, "withdrawal": -1, "dividend": 1, "interest": 1, "tax": -1, "fee": -1, "fx_cost": -1, "principal_return": 1, "transfer_in": 1, "transfer_out": -1}
    for event in normalized_events:
        if post_snapshot(event):
            cash_changes.append((event, event_sign[event["event_type"]] * event["amount_eur"] if event["amount_eur"] is not None else None))
            if event["event_type"] == "principal_return":
                issues.append(f"Devolución de principal en {event['platform']}: comprobar saldo ilíquido; no es una aportación ni un interés.")
    missing_cash_platforms: set[str] = set()
    # Daily snapshots do not reveal intraday ordering. Net all documented cash
    # events for a day before checking financing; a same-day documented deposit
    # must not be ignored merely because it came from a different sheet.
    daily_cash: dict[tuple[tuple[str, str, str], pd.Timestamp], tuple[Mapping[str, object], float | None]] = {}
    for row, change in cash_changes:
        key = _key(row)
        day_key = ((key[0], key[1], key[3]), row["_date"].normalize())
        previous = daily_cash.get(day_key)
        total = change if previous is None else previous[1] + change if previous[1] is not None and change is not None else None
        daily_cash[day_key] = row, total
    for _, (row, change) in sorted(daily_cash.items(), key=lambda item: item[0][1]):
        key = _key(row)
        state = cash_states.get((key[0], key[1], key[3]))
        if state is None:
            issues.append(f"Saldo de efectivo inicial no documentado para {key[0]}/{key[3]}; el patrimonio total puede estar incompleto.")
            missing_cash_platforms.add(key[0])
            continue
        opening = _number(state["row"].get("value_eur"))
        updated = opening + change if opening is not None and change is not None else None
        if updated is not None and updated < -EPSILON:
            issues.append(f"Efectivo insuficiente en {key[0]}/{key[3]}: faltan movimientos de financiación; saldo actual N/D.")
            updated = None
        if change is None:
            issues.append(f"Liquidación o cambio no documentado en {key[0]}/{key[3]}: efectivo actual N/D.")
        state["row"]["value_eur"] = updated
    position_rows = [_position(state, prices, realized) for state in states.values()]
    position_rows = [row for row in position_rows if row.get("quantity") is None or row["quantity"] > EPSILON or row["liquidity_class"] in {"illiquid", "cash"}]
    if not all(_covered(coverage, "capital_flows_complete", _text(row.get("platform"))) for row in position_rows) or not position_rows:
        issues.append("Aportaciones y retiradas externas incompletas: rentabilidad total N/D; compras y ventas no se consideran aportaciones.")
    if not all(_covered(coverage, "operations_complete", _text(row.get("platform"))) for row in position_rows) or not position_rows:
        issues.append("Histórico de operaciones no acreditado como completo: resultado realizado documentado parcial.")
    positions = pd.DataFrame(position_rows)
    if positions.empty:
        positions = pd.DataFrame(columns=["platform", "account_name", "ticker", "analysis_ticker", "currency", "asset_name", "asset_type", "quantity", "value_eur", "cost_basis_eur", "cost_estimate_eur", "unrealized_pnl_eur", "liquidity_class", "snapshot_date"])
    global_metrics = _metrics(position_rows, realized, normalized_events, ops, coverage)
    if not position_rows:
        global_metrics["value_complete"] = False
        global_metrics["value_eur"] = None
    if any("Saldo de efectivo inicial" in issue for issue in issues):
        global_metrics["value_complete"] = False
        global_metrics["value_eur"] = None
    if any("Devolución de principal" in issue for issue in issues):
        global_metrics["value_complete"] = False
        global_metrics["value_eur"] = None
    platform_rows = []
    for platform in sorted({_text(row.get("platform")) for row in position_rows + realized + normalized_events + ops}):
        selects = [[row for row in rows if _text(row.get("platform")) == platform] for rows in (position_rows, realized, normalized_events, ops)]
        platform_metrics = _metrics(*selects, coverage, platform)
        if platform in missing_cash_platforms:
            platform_metrics["value_complete"] = False
            platform_metrics["value_eur"] = None
        platform_rows.append({"platform": platform, **platform_metrics})
    asset_rows = []
    for key in sorted(states):
        selected_positions = [row for row in position_rows if _key(row) == key]
        selected_sales = [row for row in realized if _realized_inventory_key(row) == key]
        selected_events = [row for row in normalized_events if _key(row) == key]
        selected_ops = [row for row in ops if _key(row) == key]
        asset_rows.append({"platform": key[0], "account_name": key[1], "ticker": key[2], "currency": key[3], **_metrics(selected_positions, selected_sales, selected_events, selected_ops, coverage, key[0])})
    clean_snapshot = snapshot.drop(columns=["_date"], errors="ignore")
    clean_events = pd.DataFrame(normalized_events).drop(columns=["_date", "_order"], errors="ignore")
    return ReconciledPortfolioResult(
        positions=positions.drop(columns=["_date"], errors="ignore"),
        realized_trades=pd.DataFrame(realized, columns=["executed_at", "platform", "account_name", "ticker", "currency", "inventory_currency", "quantity", "proceeds_eur", "cost_basis_eur", "realized_pnl_eur", "proceeds_local", "cost_basis_local", "realized_pnl_local", "complete", "source_row_key"]),
        cash_flows=clean_events, metrics_global=global_metrics,
        metrics_by_platform=pd.DataFrame(platform_rows), metrics_by_asset=pd.DataFrame(asset_rows),
        monthly=_history(clean_snapshot, realized, normalized_events, ops, coverage, valuation_history, "M"),
        annual=_history(clean_snapshot, realized, normalized_events, ops, coverage, valuation_history, "Y"),
        issues=tuple(dict.fromkeys(issues)),
    )
