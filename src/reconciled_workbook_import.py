"""Read-only, provenance-preserving import plans for reconciled workbooks.

The valuation sheet is a historical observation, never an opening purchase.
Supplementary sale/position sheets are controls, never a second trade source.
Nothing in this module writes a journal, workbook, network resource, or log.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
import json
import math
import re
from typing import BinaryIO

import pandas as pd

from src.instruments import resolve_analysis_ticker
from src.journal import normalize_operation
from src.portfolio_snapshot_import import (
    PortfolioWorkbookSnapshot,
    account_summaries_from_positions,
    normalize_portfolio_snapshot_frame,
)


IMPORT_MARKER = "[reconciled-workbook:v1]"
OPERATION_EXTRA_COLUMNS = [
    "source_event_id", "operation_id", "source_row_key", "platform",
    "raw_identifier", "analysis_ticker", "settlement_amount_local",
    "identity_identifier",
    "residual_cost_local", "fee_quality", "fx_quality", "cost_quality",
    "reference_fx_rate", "phase", "after_snapshot", "source_sheet",
    "source_row", "provenance_json",
]
ISSUE_COLUMNS = ["code", "severity", "sheet", "row", "status", "message"]
EVENT_COLUMNS = [
    "source_event_id", "event_id", "source_row_key", "event_type",
    "executed_at", "platform", "account_name", "ticker", "amount",
    "amount_eur", "currency", "fx_rate_to_eur", "reference_fx_rate",
    "fx_quality", "phase", "after_snapshot", "source_sheet", "source_row",
    "provenance_json",
]
SHEET_ROLES = {
    "Cartera": "historical_snapshot",
    "Operaciones TR": "operations",
    "Operaciones Revolut": "operations_and_income",
    "Compras TR 15-10": "supplementary_post_snapshot_operations",
    "Ventas TR": "fifo_sales_control",
    "Ventas Revolut": "fifo_sales_control",
    "Posiciones TR": "quantity_control",
    "Posiciones Revolut": "quantity_control",
    "Referencias de tickers": "identifier_reference",
    "Resumen": "summary_not_operations",
    "Fuentes y límites": "source_limits",
    "Foto bursátil 13-10": "valuation_control",
    "Conciliación 13-10": "reconciliation_control",
    "LEER ANTES DE IMPORTAR": "import_instructions",
}


@dataclass(frozen=True)
class ReconciledWorkbookPlan:
    snapshot: PortfolioWorkbookSnapshot
    operations: pd.DataFrame
    events: pd.DataFrame
    issues: pd.DataFrame
    inventory: pd.DataFrame
    sales_references: pd.DataFrame
    position_references: pd.DataFrame
    fifo_sales: pd.DataFrame
    checks: pd.DataFrame
    coverage: dict[str, object]
    source_sha256: str

    @property
    def snapshot_date(self) -> str:
        return self.snapshot.snapshot_date

    @property
    def historical_operations(self) -> pd.DataFrame:
        return self.operations.loc[self.operations["phase"] == "historical"].copy()

    @property
    def post_snapshot_operations(self) -> pd.DataFrame:
        return self.operations.loc[self.operations["phase"] == "post_snapshot"].copy()

    @property
    def pending_rows(self) -> pd.DataFrame:
        return self.issues.loc[self.issues["status"] == "pending"].copy()

    @property
    def sheet_inventory(self) -> pd.DataFrame:
        return self.inventory


def _text(value: object) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


def _number(value: object) -> float | None:
    if value is None or pd.isna(value) or _text(value) == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).date().isoformat()
    text = _text(value)
    # Do not interpret totals, prose, serial numbers, or a missing reporting year.
    formats = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(row: dict[str, object], *, event_type: str = "operation",
              use_source_identifier: bool = True) -> str:
    """Economic identity excludes file layout/hash, fee guesses and row numbers.

    Conflicting amounts for this identity are quarantined, not silently imported
    as a new operation. Identical same-sheet rows are ambiguous without trade IDs.
    """
    payload = {
        "account": _text(row.get("account_name") or row.get("platform")).casefold(),
        "ticker": _text((row.get("identity_identifier") if use_source_identifier else None)
                        or row.get("ticker")).upper(),
        "date": _text(row.get("executed_at"))[:10],
        "side": _text(row.get("side") or event_type),
        "currency": _text(row.get("currency")).upper(),
        "quantity": round(float(row.get("quantity") or 0), 8),
        "price": round(float(row.get("price") or 0), 6),
    }
    if event_type != "operation":
        payload["amount"] = round(float(row.get("amount") or 0), 8)
    return "rw1:" + sha256(_canonical_json(payload).encode()).hexdigest()


def _economic_amounts(row: dict[str, object]) -> tuple[object, ...]:
    return tuple(
        None if _number(row.get(key)) is None else round(float(row[key]), 6)
        for key in ("fees", "settlement_amount_local", "settlement_amount_eur")
    )


def _issue(issues: list[dict[str, object]], code: str, sheet: str = "", row: int | None = None,
           *, pending: bool = True, message: str = "") -> None:
    # Messages are controlled strings. Never interpolate source-cell values.
    issues.append({"code": code, "severity": "warning", "sheet": sheet,
                   "row": row, "status": "pending" if pending else "notice",
                   "message": message or code})


def _records(sheet: list[tuple[object, ...]]) -> list[tuple[int, dict[str, object]]]:
    if not sheet:
        return []
    headers = [_text(value) for value in sheet[0]]
    return [(index, dict(zip(headers, row))) for index, row in enumerate(sheet[1:], 2)
            if any(value is not None for value in row)]


def _references(sheets: dict[str, list[tuple[object, ...]]], positions: pd.DataFrame,
                issues: list[dict[str, object]]) -> dict[str, str]:
    mappings: dict[str, set[str]] = {}
    for row in positions.itertuples(index=False):
        if row.raw_identifier and row.analysis_ticker:
            mappings.setdefault(_text(row.raw_identifier).upper(), set()).add(row.analysis_ticker)
    sheet = "Referencias de tickers"
    for index, row in _records(sheets.get(sheet, [])):
        isin, ticker = _text(row.get("ISIN fuente")).upper(), _text(row.get("Ticker en importación"))
        if not isin or not ticker:
            _issue(issues, "incomplete_identifier_reference", sheet, index)
            continue
        mappings.setdefault(isin, set()).add(resolve_analysis_ticker(ticker))
    result = {}
    for identifier, symbols in mappings.items():
        if len(symbols) == 1:
            result[identifier] = next(iter(symbols))
        else:
            _issue(issues, "contradictory_identifier_reference", sheet)
    return result


def _attach_provenance(row: dict[str, object], digest: str, sheet: str, index: int,
                       snapshot_date: str, *, event_type: str = "operation") -> dict[str, object]:
    event_id = _identity(row, event_type=event_type)
    phase = "post_snapshot" if str(row["executed_at"])[:10] > snapshot_date else "historical"
    provenance = [{"source_sha256": digest, "sheet": sheet, "row": index}]
    row.update(source_event_id=event_id, source_row_key=event_id,
               operation_id=event_id, phase=phase, after_snapshot=phase == "post_snapshot",
               source_sheet=sheet, source_row=index, provenance_json=_canonical_json(provenance))
    if event_type != "operation":
        row["event_id"] = event_id
    else:
        # Stored notes carry only controlled metadata, not workbook free text.
        row["notes"] = IMPORT_MARKER + _canonical_json({
            "source_event_id": event_id, "phase": phase,
            "fee_quality": row["fee_quality"], "fx_quality": row["fx_quality"],
            "source_sheet": sheet, "source_row": index,
        })
    return row


def _parse_operation(row: dict[str, object], sheet: str, index: int,
                     identifiers: dict[str, str], issues: list[dict[str, object]]) -> dict[str, object] | None:
    is_tr = sheet in {"Operaciones TR", "Compras TR 15-10"}
    supplementary = sheet == "Compras TR 15-10"
    if supplementary and _text(row.get("Plataforma")) not in {"", "Trade Republic"}:
        _issue(issues, "contradictory_supplementary_platform", sheet, index)
        return None
    executed = _date(row.get("Fecha"))
    if supplementary and executed is None:
        # Two well-known presentation rows, explicitly not financial events.
        if _text(row.get("Fecha")) in {
            "Importe total de compras posteriores",
            "No sumar estas compras otra vez al importar el extracto completo en el diario.",
        }:
            return None
    kind = "BUY" if supplementary else _text(row.get("Tipo")).upper()
    side = "Compra" if kind.startswith("BUY") else "Venta" if kind.startswith("SELL") else ""
    if not side:
        _issue(issues, "unsupported_operation_type", sheet, index)
        return None
    quantity_key = "Cantidad" if not is_tr or supplementary else "Participaciones (+/-)"
    price_key = "Precio ejecución (€)" if supplementary else "Precio" if is_tr else "Precio unitario"
    raw_identifier = _text(row.get("Ticker / ISIN") if supplementary else row.get("ISIN") if is_tr else row.get("Ticker")).upper()
    quantity, price = _number(row.get(quantity_key)), _number(row.get(price_key))
    currency = "EUR" if is_tr else _text(row.get("Moneda")).upper()
    if (not executed or not raw_identifier or quantity is None or quantity == 0 or price is None
            or price <= 0 or not re.fullmatch("[A-Z]{3}", currency)):
        _issue(issues, "incomplete_operation", sheet, index)
        return None
    if is_tr and not supplementary and ((side == "Compra" and quantity < 0) or (side == "Venta" and quantity > 0)):
        _issue(issues, "contradictory_quantity_sign", sheet, index)
        return None
    if not is_tr and quantity < 0:
        _issue(issues, "contradictory_quantity_sign", sheet, index)
        return None
    quantity = abs(quantity)
    ticker = identifiers.get(raw_identifier, resolve_analysis_ticker(raw_identifier))
    # TR source ISIN remains stable if a later workbook adds a quote symbol.
    # A supplementary ticker can use that ISIN only when the mapping is unique.
    identity_identifier = raw_identifier
    if is_tr and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", raw_identifier):
        source_isins = [key for key, value in identifiers.items() if value == ticker
                        and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", key)]
        if len(source_isins) == 1:
            identity_identifier = source_isins[0]
    is_isin = bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", ticker))
    if is_isin:
        _issue(issues, "analysis_ticker_unmapped", sheet, index, pending=False,
               message="Se conserva el ISIN sin inventar un símbolo de mercado.")
    local_amount = _number(row.get("Desembolso (€)" if supplementary else "Flujo bróker €" if is_tr else "Importe original"))
    fee = _number(row.get("Comisión (€)" if supplementary else "Comisión €")) if is_tr else None
    if local_amount is None or local_amount == 0 or (is_tr and (fee is None or fee < 0)):
        _issue(issues, "incomplete_settlement_or_fee", sheet, index)
        return None
    gross = quantity * price
    if is_tr:
        if not supplementary and ((side == "Compra" and local_amount >= 0) or (side == "Venta" and local_amount <= 0)):
            _issue(issues, "contradictory_cashflow_sign", sheet, index)
            return None
        # Source TR flow is gross, with fees in a separate column.
        expected_gross = local_amount - fee if supplementary else abs(local_amount)
        if abs(expected_gross - gross) > 0.025:
            _issue(issues, "contradictory_execution_total", sheet, index)
            return None
        settlement = local_amount if supplementary else abs(local_amount) + (fee if side == "Compra" else -fee)
        if settlement <= 0:
            _issue(issues, "nonpositive_settlement", sheet, index)
            return None
        fee_quality, fx_quality = "documented", "exact"
        residual = None
    else:
        if local_amount <= 0:
            _issue(issues, "contradictory_settlement_sign", sheet, index)
            return None
        settlement = local_amount
        residual = settlement - gross if side == "Compra" else gross - settlement
        # A residual is a diagnostic, not evidence that it is a commission.
        fee_quality, fx_quality = "unknown", "exact" if currency == "EUR" else "reference"
        fee = 0.0  # Existing journal API requires a number; quality remains unknown.
    try:
        result = normalize_operation(
            ticker=ticker, side=side, quantity=quantity, price=price, fees=fee,
            executed_at=executed, currency=currency,
            account_name="Trade Republic" if is_tr else "Revolut",
            settlement_amount_eur=settlement if currency == "EUR" else None,
            fee_eur=fee if is_tr else None,
        )
    except (ValueError, TypeError):
        _issue(issues, "invalid_normalized_operation", sheet, index)
        return None
    # Reproducible import plans must not contain normalization wall-clock values.
    result.pop("created_at", None)
    if not is_tr:
        result["fee_eur"] = None
    result.update(
        platform=result["account_name"], raw_identifier=raw_identifier,
        identity_identifier=identity_identifier,
        analysis_ticker="" if is_isin else ticker,
        settlement_amount_local=settlement, residual_cost_local=residual,
        fee_quality=fee_quality, fx_quality=fx_quality, cost_quality="documented_settlement",
        reference_fx_rate=_number(row.get("FX referencia")),
    )
    return result


def _deduplicate(rows: list[dict[str, object]], issues: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["source_event_id"]), []).append(row)
    result = []
    for group in grouped.values():
        if len(group) == 1:
            result.append(group[0])
            continue
        sheets = [row["source_sheet"] for row in group]
        same_sheet = len(sheets) != len(set(sheets))
        conflicting = len({_economic_amounts(row) for row in group}) != 1
        if same_sheet or conflicting:
            code = "ambiguous_same_source_duplicate" if same_sheet else "contradictory_duplicate"
            for row in group:
                _issue(issues, code, str(row["source_sheet"]), int(row["source_row"]))
            continue
        first = group[0].copy()
        first["provenance_json"] = _canonical_json([
            ref for row in group for ref in json.loads(str(row["provenance_json"]))
        ])
        result.append(first)
        _issue(issues, "supplementary_duplicate_not_reimported", str(first["source_sheet"]),
               int(first["source_row"]), pending=False)
    return result


def _fifo_controls(operations: pd.DataFrame, positions: pd.DataFrame,
                   sales_controls: pd.DataFrame, issues: list[dict[str, object]],
                   snapshot_date: str) -> tuple[pd.DataFrame, pd.DataFrame, set[str]]:
    """Reconstruct only documented lots, using recorded local net settlements."""
    sales, checks = [], []
    incomplete: set[str] = set()
    if operations.empty:
        return pd.DataFrame(), pd.DataFrame(), incomplete
    ordered = operations.sort_values(["executed_at", "source_row"], kind="stable")
    lots: dict[tuple[str, str, str], deque[list[float]]] = {}
    for row in ordered.to_dict("records"):
        platform, ticker, currency = str(row["platform"]), str(row["ticker"]), str(row["currency"])
        group = lots.setdefault((platform, ticker, currency), deque())
        quantity, settlement = float(row["quantity"]), float(row["settlement_amount_local"])
        if row["side"] == "Compra":
            group.append([quantity, settlement / quantity])
            continue
        remaining, cost = quantity, 0.0
        while group and remaining > 1e-8:
            lot = group[0]
            used = min(remaining, lot[0])
            cost += used * lot[1]
            remaining -= used
            lot[0] -= used
            if lot[0] <= 1e-8:
                group.popleft()
        complete = remaining <= 1e-6
        if not complete:
            incomplete.add(platform)
            _issue(issues, "sale_without_documented_opening_lot", str(row["source_sheet"]), int(row["source_row"]))
        sales.append({"source_event_id": row["source_event_id"], "platform": platform,
                      "ticker": ticker, "sold_at": str(row["executed_at"])[:10],
                      "quantity": quantity, "currency": currency,
                      "cost_basis_local": cost if complete else None,
                      "net_proceeds_local": settlement,
                      "realized_pnl_local": settlement - cost if complete else None,
                      "cost_basis_eur": cost if complete and currency == "EUR" else None,
                      "realized_pnl_eur": settlement - cost if complete and currency == "EUR" else None,
                      "status": "documented_local_settlement" if complete else "pending_opening_lots"})
    sale_frame = pd.DataFrame(sales)
    for platform, source in positions.groupby("platform", sort=False):
        relevant = source.loc[source["quantity"].notna() & (source["asset_type"].str.casefold() != "efectivo")]
        comparison_count, mismatches = 0, 0
        for position in relevant.itertuples(index=False):
            ticker = _text(position.analysis_ticker) or _text(position.raw_identifier).upper()
            if not ticker:
                continue
            historical = operations.loc[(operations["platform"] == platform)
                & (operations["ticker"] == ticker) & (operations["phase"] == "historical")]
            if historical.empty:
                continue
            comparison_count += 1
            balance = sum(float(row.quantity) * (1 if row.side == "Compra" else -1)
                          for row in historical.itertuples(index=False))
            if abs(balance - float(position.quantity)) > 1e-5:
                mismatches += 1
        checks.append({"check": "historical_snapshot_quantity", "platform": platform,
                       "compared_rows": comparison_count, "mismatched_rows": mismatches,
                       "status": "pending" if mismatches else "matched" if comparison_count else "unavailable"})
        if mismatches:
            incomplete.add(str(platform))
            _issue(issues, "snapshot_quantity_mismatch", "Cartera",
                   message="Hay cantidades históricas que no coinciden con el diario documentado.")
    if not sale_frame.empty and not sales_controls.empty:
        for platform, source in sales_controls.groupby("platform", sort=False):
            mismatch = matched = 0
            for (ticker, sold_at), controls in source.groupby(["ticker", "sold_at"], sort=False):
                reconstructed = sale_frame.loc[(sale_frame["platform"] == platform)
                    & (sale_frame["ticker"] == ticker) & (sale_frame["sold_at"] == sold_at)]
                if reconstructed.empty or abs(reconstructed["quantity"].sum() - controls["quantity"].sum()) > 1e-5:
                    mismatch += 1
                    continue
                if platform == "Trade Republic":
                    pnl = reconstructed["realized_pnl_local"]
                    reported = pd.to_numeric(controls["reported_pnl_local"], errors="coerce")
                    if pnl.isna().any() or reported.isna().any() or abs(pnl.sum() - reported.sum()) > 0.03:
                        mismatch += 1
                        continue
                matched += 1
            checks.append({"check": "reported_fifo_sales", "platform": platform,
                           "compared_rows": matched + mismatch, "mismatched_rows": mismatch,
                           "status": "pending" if mismatch else "matched"})
            if mismatch:
                incomplete.add(str(platform))
                _issue(issues, "reported_fifo_sales_mismatch",
                       "Ventas TR" if platform == "Trade Republic" else "Ventas Revolut",
                       message="El control de ventas no coincide con las operaciones documentadas.")
    return sale_frame, pd.DataFrame(checks), incomplete


def _controls(sheets: dict[str, list[tuple[object, ...]]], identifiers: dict[str, str],
              issues: list[dict[str, object]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    sales, positions = [], []
    for sheet, platform in (("Ventas TR", "Trade Republic"), ("Ventas Revolut", "Revolut")):
        for index, row in _records(sheets.get(sheet, [])):
            tr = platform == "Trade Republic"
            identifier = _text(row.get("ISIN") if tr else row.get("Ticker")).upper()
            sold_at = _date(row.get("Fecha") if tr else row.get("Fecha venta"))
            quantity = _number(row.get("Acciones vendidas") if tr else row.get("Cantidad"))
            if not identifier or not sold_at or quantity is None or quantity <= 0:
                _issue(issues, "incomplete_sales_control", sheet, index)
                continue
            sales.append({"platform": platform, "ticker": identifiers.get(identifier, resolve_analysis_ticker(identifier)),
                          "raw_identifier": identifier, "sold_at": sold_at,
                          "bought_at": _date(row.get("Fecha compra")), "quantity": quantity,
                          "currency": "EUR" if tr else _text(row.get("Moneda")).upper(),
                          "cost_basis_local": _number(row.get("Coste FIFO €") if tr else row.get("Coste bruto")),
                          "gross_proceeds_local": _number(row.get("Venta bruta €") if tr else row.get("Venta bruta")),
                          "fee_local": _number(row.get("Comisión venta €")) if tr else None,
                          "reported_pnl_local": _number(row.get("Beneficio neto €") if tr else row.get("PnL bruto")),
                          "quality": "reported_fifo_net" if tr else "reported_gross_not_net",
                          "source_sheet": sheet, "source_row": index})
    for sheet, platform in (("Posiciones TR", "Trade Republic"), ("Posiciones Revolut", "Revolut")):
        for index, row in _records(sheets.get(sheet, [])):
            tr = platform == "Trade Republic"
            identifier = _text(row.get("ISIN") if tr else row.get("Ticker")).upper()
            qty = _number(row.get("Participaciones al 15/10") if tr else row.get("Participaciones al 08/10"))
            if not identifier or qty is None:
                _issue(issues, "incomplete_position_control", sheet, index)
                continue
            positions.append({"platform": platform, "ticker": identifiers.get(identifier, resolve_analysis_ticker(identifier)),
                              "quantity": qty, "cost_basis_eur": _number(row.get("Coste FIFO €")) if tr else None,
                              "as_of": _date(row.get("Fecha cálculo")) if tr else None,
                              "source_sheet": sheet, "source_row": index})
    return pd.DataFrame(sales), pd.DataFrame(positions)


def parse_reconciled_workbook_excel(source: bytes | BinaryIO, *,
                                    expected_snapshot_value_eur: float | None = None) -> ReconciledWorkbookPlan:
    """Build a deterministic plan; never infer deposits or purchases from a photo.

    Unknown source sheets are inventoried only. Workbook free-text instructions
    are data and are never executed. Invalid rows expose coordinates and issue
    codes, never private source values in exceptions or diagnostics.
    """
    from openpyxl import load_workbook

    payload = source if isinstance(source, bytes) else source.read()
    if not isinstance(payload, bytes):
        raise ValueError("El importador necesita un archivo Excel binario.")
    digest = sha256(payload).hexdigest()
    try:
        workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
        try:
            sheets = {sheet.title: list(sheet.values) for sheet in workbook}
        finally:
            workbook.close()
    except Exception:
        raise ValueError("No se ha podido leer el archivo Excel.") from None
    if not sheets.get("Cartera"):
        raise ValueError("El Excel debe contener la hoja Cartera.")
    issues: list[dict[str, object]] = []
    source_rows = _records(sheets["Cartera"])
    try:
        snapshot_source = pd.DataFrame([row for _, row in source_rows])
        snapshot_dates = [_date(value) for value in snapshot_source["Fecha"]]
        if any(value is None for value in snapshot_dates):
            raise ValueError("Invalid snapshot date")
        # Typed dates keep the shared normalizer independent of locale guessing.
        snapshot_source["Fecha"] = pd.to_datetime(snapshot_dates, format="%Y-%m-%d")
        positions = normalize_portfolio_snapshot_frame(snapshot_source)
        for column in ("quantity", "current_price", "value_eur", "return_pct", "cost_estimate_eur", "gain_loss_eur"):
            values = positions[column].dropna()
            if any(not math.isfinite(float(value)) for value in values):
                raise ValueError("Non-finite snapshot number")
        if (positions["quantity"].dropna() < 0).any():
            raise ValueError("Negative snapshot quantity")
    except (ValueError, TypeError, KeyError):
        # The shared normalizer may include raw invalid values in its exception.
        raise ValueError("La fotografía Cartera no supera la validación de fechas, columnas o importes.") from None
    snapshot_date = str(positions["snapshot_date"].iloc[0])
    snapshot = PortfolioWorkbookSnapshot(positions, account_summaries_from_positions(positions), snapshot_date)
    identifiers = _references(sheets, positions, issues)
    inventory = pd.DataFrame([
        {"sheet": title, "role": SHEET_ROLES.get(title, "unrecognized_not_imported"),
         "nonempty_data_rows": len(_records(rows)), "column_count": len(rows[0]) if rows else 0,
         "headers": tuple(_text(value) for value in rows[0]) if rows else ()}
        for title, rows in sheets.items()
    ])
    operations: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    for sheet in ("Operaciones TR", "Operaciones Revolut", "Compras TR 15-10"):
        if sheet not in sheets:
            _issue(issues, "missing_operation_sheet", sheet)
            continue
        for index, row in _records(sheets[sheet]):
            if sheet == "Operaciones Revolut" and _text(row.get("Tipo")).upper() == "DIVIDEND":
                amount, executed = _number(row.get("Importe original")), _date(row.get("Fecha"))
                currency, ticker = _text(row.get("Moneda")).upper(), _text(row.get("Ticker")).upper()
                if amount is None or amount <= 0 or not executed or not ticker or not re.fullmatch("[A-Z]{3}", currency):
                    _issue(issues, "incomplete_dividend", sheet, index)
                    continue
                event = {"event_type": "dividend", "executed_at": executed,
                         "platform": "Revolut", "account_name": "Revolut",
                         "ticker": resolve_analysis_ticker(ticker), "amount": amount,
                         "amount_eur": amount if currency == "EUR" else None,
                         "currency": currency, "fx_rate_to_eur": 1.0 if currency == "EUR" else None,
                         "reference_fx_rate": _number(row.get("FX referencia")),
                         "fx_quality": "exact" if currency == "EUR" else "reference"}
                events.append(_attach_provenance(event, digest, sheet, index, snapshot_date, event_type="dividend"))
                continue
            operation = _parse_operation(row, sheet, index, identifiers, issues)
            if operation is not None:
                operations.append(_attach_provenance(operation, digest, sheet, index, snapshot_date))
    operations = _deduplicate(operations, issues)
    events = _deduplicate(events, issues)
    op_columns = list(dict.fromkeys(["ticker", "account_name", "side", "quantity", "price", "fees",
        "settlement_amount_eur", "fee_eur", "fx_rate_to_eur", "executed_at", "notes", "currency"] + OPERATION_EXTRA_COLUMNS))
    operation_frame = pd.DataFrame(operations).reindex(columns=op_columns)
    event_frame = pd.DataFrame(events).reindex(columns=EVENT_COLUMNS)
    sales, position_controls = _controls(sheets, identifiers, issues)
    fifo_sales, checks, incomplete_platforms = _fifo_controls(operation_frame, positions, sales, issues, snapshot_date)
    snapshot_value = round(float(positions["value_eur"].sum()), 2)
    if expected_snapshot_value_eur is not None:
        expected = _number(expected_snapshot_value_eur)
        if expected is None or expected < 0:
            raise ValueError("La referencia patrimonial debe ser un importe finito no negativo.")
        if abs(snapshot_value - expected) > 0.01:
            _issue(issues, "snapshot_wealth_reference_mismatch", "Cartera",
                   message="El total documentado difiere de la referencia patrimonial indicada.")
    # A transaction list alone does not prove bank deposits, historical taxes,
    # income completeness, or the absence of omitted earlier transactions.
    coverage: dict[str, object] = {
        "operations_complete": False, "capital_flows_complete": False,
        "income_complete": False, "costs_complete": False,
        "per_platform": {
            platform: {"operations_complete": False, "capital_flows_complete": False,
                       "income_complete": False, "costs_complete": False}
            for platform in positions["platform"].unique()
        },
        "snapshot_is_historical": True,
        "snapshot_date": snapshot_date,
        "snapshot_value_eur": snapshot_value,
        "expected_snapshot_value_eur": expected_snapshot_value_eur,
        "snapshot_reference_difference_eur": round(snapshot_value - expected_snapshot_value_eur, 2)
            if expected_snapshot_value_eur is not None else None,
        "source_operations": len(operation_frame), "source_income_events": len(event_frame),
        "platforms_with_control_mismatches": sorted(incomplete_platforms),
    }
    return ReconciledWorkbookPlan(
        snapshot=snapshot, operations=operation_frame, events=event_frame,
        issues=pd.DataFrame(issues, columns=ISSUE_COLUMNS), inventory=inventory,
        sales_references=sales, position_references=position_controls,
        fifo_sales=fifo_sales, checks=checks,
        coverage=coverage, source_sha256=digest,
    )


def plan_reconciled_operation_reimport(operations: pd.DataFrame,
                                      existing: pd.DataFrame | None = None) -> pd.DataFrame:
    """Classify insert/skip/pending without writing or guessing a manual match.

    A source ID is strong evidence. A single identical manual economic event can
    be reused, while multiple matches, absent accounts, or corrected amounts
    require review. This also works before journal source-ID migrations exist.
    """
    existing_rows = [] if existing is None else existing.to_dict("records")
    decisions = []
    for operation in operations.to_dict("records"):
        event_id = str(operation["source_event_id"])
        matches = []
        ambiguous_account = False
        for saved in existing_rows:
            saved_id = _text(saved.get("source_event_id"))
            if not saved_id and IMPORT_MARKER in _text(saved.get("notes")):
                try:
                    saved_id = _text(json.loads(_text(saved["notes"]).split(IMPORT_MARKER, 1)[1]).get("source_event_id"))
                except (ValueError, TypeError, AttributeError):
                    pass
            if saved_id == event_id:
                matches.append(saved)
                continue
            semantic_id = _identity(operation, use_source_identifier=False)
            if _text(saved.get("account_name")) and _identity(saved, use_source_identifier=False) == semantic_id:
                matches.append(saved)
                continue
            probe = dict(saved, account_name=operation["account_name"])
            if not _text(saved.get("account_name")) and _identity(probe, use_source_identifier=False) == semantic_id:
                ambiguous_account = True
        action, reason = "insert", "new_economic_event"
        if ambiguous_account or len(matches) > 1:
            action, reason = "pending", "ambiguous_existing_match"
        elif matches:
            saved = matches[0]
            conflict = any(
                _number(saved.get(key)) is not None and _number(operation.get(key)) is not None
                and abs(float(saved[key]) - float(operation[key])) > 0.025
                for key in ("fees", "settlement_amount_local", "settlement_amount_eur")
            )
            action, reason = ("pending", "contradictory_existing_amount") if conflict else ("skip", "existing_economic_event")
        decisions.append({"source_event_id": event_id, "action": action, "reason": reason,
                          "existing_id": matches[0].get("id") if len(matches) == 1 else None})
    return pd.DataFrame(decisions, columns=["source_event_id", "action", "reason", "existing_id"])
