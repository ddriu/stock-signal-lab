"""Read-only bridge from persisted import evidence to portfolio accounting.

This module neither imports a workbook nor refreshes market data. Persisted
financial columns remain authoritative; import metadata only restores the
controlled accounting-quality/cutoff fields that the legacy journal omits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
import math
import re

import pandas as pd

from src.instruments import resolve_analysis_ticker
from src.journal import OPERATION_COLUMNS
from src.market_data_quality import quote_freshness
from src.portfolio_reconciliation import ReconciledPortfolioResult, build_reconciled_portfolio


IMPORT_MARKER = "[reconciled-workbook:v1]"
COVERAGE_FLAGS = ("operations_complete", "capital_flows_complete", "income_complete", "costs_complete")
EXTRA_FIELDS = {
    "fee_quality", "fx_quality", "cost_quality", "phase", "after_snapshot",
    "raw_identifier", "identity_identifier", "source_sheet", "source_row",
    "reference_fx_rate", "residual_cost_local", "operation_id", "source_row_key",
}
AUTHORITATIVE_FIELDS = set(OPERATION_COLUMNS)


def _missing(value: object) -> bool:
    return value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value))


def _text(value: object) -> str:
    return "" if _missing(value) else str(value).strip()


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: object) -> pd.Timestamp | None:
    if _missing(value) or isinstance(value, (int, float)):
        return None
    day = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(day) else pd.Timestamp(day).tz_localize(None)


def _truth(value: object) -> bool:
    return value is True or _text(value).casefold() in {"true", "1", "yes", "si", "sí"}


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if _missing(value) or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(f"La metadata de {label} no contiene JSON válido.") from None
    if not isinstance(value, Mapping):
        raise ValueError(f"La metadata de {label} debe ser un objeto.")
    return dict(value)


def _list(value: object, *, label: str) -> list[dict[str, object]]:
    if _missing(value):
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"La metadata de {label} debe contener una lista de objetos.")
    return [dict(item) for item in value]


def _safe_code(value: object) -> str:
    code = _text(value)
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,100}", code) else "unclassified_import_issue"


def _batch_metadata(batches: pd.DataFrame) -> list[dict[str, object]]:
    ordered = batches.copy(deep=True)
    if "created_at" in ordered:
        ordered["_created"] = ordered["created_at"].map(_date)
        order = ["_created"]
        if "id" in ordered:
            order.append("id")
        ordered = ordered.sort_values(order, ascending=False, na_position="last", kind="stable")
    return [_mapping(row.get("metadata"), label="la importación") for row in ordered.to_dict("records")]


def _merge_coverage(metadata: list[dict[str, object]]) -> dict[str, object]:
    """Use latest evidence per explicitly covered platform; absent means unknown."""
    coverages = [_mapping(item.get("coverage"), label="cobertura") for item in metadata]
    newest = coverages[0] if coverages else {}
    result: dict[str, object] = {flag: _truth(newest.get(flag)) for flag in COVERAGE_FLAGS}
    per_platform: dict[str, dict[str, bool]] = {}
    for coverage in coverages if newest else []:
        platforms = _mapping(coverage.get("per_platform"), label="cobertura por plataforma")
        for platform, values in platforms.items():
            if platform in per_platform:
                continue
            local = _mapping(values, label="cobertura de plataforma")
            per_platform[str(platform)] = {
                flag: _truth(local.get(flag, coverage.get(flag))) for flag in COVERAGE_FLAGS
            }
    result["per_platform"] = per_platform
    return result


def _notes_metadata(value: object) -> dict[str, object]:
    note = _text(value)
    return _mapping(note.removeprefix(IMPORT_MARKER), label="trazabilidad de operación") if note.startswith(IMPORT_MARKER) else {}


def _operation_evidence(metadata: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    evidence = {}
    for item in metadata:
        provenance = _mapping(item.get("provenance"), label="procedencia")
        for row in _list(provenance.get("operations"), label="procedencia de operaciones"):
            source_id = _text(row.get("source_event_id"))
            if source_id and source_id not in evidence:
                evidence[source_id] = row
    return evidence


def _rehydrate_operations(operations: pd.DataFrame,
                           metadata: list[dict[str, object]]) -> pd.DataFrame:
    evidence = _operation_evidence(metadata)
    rows = []
    for original in operations.to_dict("records"):
        row = dict(original)
        notes = _notes_metadata(row.get("notes"))
        source_id = _text(row.get("source_event_id")) or _text(notes.get("source_event_id"))
        supplemental = evidence.get(source_id, {})
        # Notes bound to the persisted operation outrank a later import preview.
        for key in EXTRA_FIELDS - AUTHORITATIVE_FIELDS:
            if key in row and not _missing(row[key]):
                continue
            if key in notes:
                row[key] = notes[key]
            elif key in supplemental:
                row[key] = supplemental[key]
        if source_id:
            row.setdefault("operation_id", source_id)
            row.setdefault("source_row_key", source_id)
            if "source_event_id" not in row:
                row["source_event_id"] = source_id
        if "after_snapshot" not in row and _text(row.get("phase")) in {"historical", "post_snapshot"}:
            row["after_snapshot"] = row["phase"] == "post_snapshot"
        rows.append(row)
    return pd.DataFrame(rows) if rows else operations.copy(deep=True)


def _events(metadata: list[dict[str, object]], issues: list[str]) -> pd.DataFrame:
    selected: dict[str, dict[str, object]] = {}
    without_id = []
    conflicts: set[str] = set()
    def signature(row: dict[str, object]) -> tuple[object, ...]:
        day = next((_date(row.get(field)) for field in ("executed_at", "event_date", "date")
                    if _date(row.get(field)) is not None), None)
        return (
            _text(row.get("event_type")).casefold(), day.isoformat() if day is not None else "",
            _text(row.get("platform")) or _text(row.get("account_name")),
            _text(row.get("account_name")) or _text(row.get("platform")),
            _ticker(row), _currency(row.get("currency")),
            *(_number(row.get(field)) for field in ("amount", "amount_eur", "fx_rate_to_eur")),
            _text(row.get("phase")), _truth(row.get("after_snapshot")),
        )
    for item in metadata:
        for row in _list(item.get("events"), label="eventos"):
            source_id = next((_text(row.get(key)) for key in ("source_event_id", "event_id", "source_row_key")
                              if _text(row.get(key))), "")
            if not source_id:
                # Identical unnumbered dividends can be two legitimate events.
                without_id.append(row)
            elif source_id not in selected:
                selected[source_id] = row
            elif signature(selected[source_id]) != signature(row):
                conflicts.add(source_id)
    if conflicts:
        issues.append("Hay eventos importados con una identidad contradictoria; quedan pendientes y no se han aplicado.")
    return pd.DataFrame([row for key, row in selected.items() if key not in conflicts] + without_id)


def _import_issues(metadata: list[dict[str, object]]) -> list[str]:
    issues = []
    for item in metadata:
        for row in _list(item.get("issues"), label="incidencias"):
            code = _safe_code(row.get("code"))
            prefix = "Importación pendiente" if row.get("status") == "pending" else "Aviso de importación"
            issues.append(f"{prefix}: {code}.")
        for row in _list(item.get("pending_conflicts"), label="conflictos pendientes"):
            issues.append(f"Importación pendiente: {_safe_code(row.get('reason'))}.")
    return issues


def _ticker(row: Mapping[str, object]) -> str:
    raw = _text(row.get("analysis_ticker")) or _text(row.get("ticker"))
    return resolve_analysis_ticker(raw) if raw else ""


def _currency(value: object) -> str:
    text = _text(value).upper()
    return text if re.fullmatch("[A-Z]{3}", text) else ""


def _price_map(snapshots: pd.DataFrame, operations: pd.DataFrame,
               prepared: Mapping[str, pd.DataFrame] | None,
               rates: Mapping[str, object] | None, reference: pd.Timestamp | None,
               issues: list[str]) -> dict[object, object]:
    if not prepared:
        return {}
    snapshot_rows = snapshots.to_dict("records")
    # Historical quote/execution-currency labels must not contradict the current
    # account baseline merely because earlier photographs are retained.
    latest_account_dates: dict[tuple[str, str], pd.Timestamp] = {}
    for row in snapshot_rows:
        day = _date(row.get("snapshot_date"))
        account = (_text(row.get("platform")), _text(row.get("account_name")) or _text(row.get("platform")))
        if day is not None and (reference is None or day.normalize() <= reference.normalize()):
            latest_account_dates[account] = max(day, latest_account_dates.get(account, day))
    snapshot_rows = [row for row in snapshot_rows if _date(row.get("snapshot_date")) == latest_account_dates.get(
        (_text(row.get("platform")), _text(row.get("account_name")) or _text(row.get("platform"))))]
    operation_rows = operations.to_dict("records")
    snapshots_by_ticker: dict[str, set[str]] = {}
    operations_by_ticker: dict[str, set[str]] = {}
    for source, target in ((snapshot_rows, snapshots_by_ticker), (operation_rows, operations_by_ticker)):
        for row in source:
            ticker, currency = _ticker(row), _currency(row.get("currency"))
            if ticker and currency:
                target.setdefault(ticker, set()).add(currency)
    prices: dict[object, object] = {}
    for symbol, frame in prepared.items():
        ticker = resolve_analysis_ticker(symbol)
        if frame is None or frame.empty:
            continue
        close_column = "close" if "close" in frame else "Close" if "Close" in frame else ""
        if not close_column:
            continue
        # An explicit normalized quote-currency attribute can disambiguate two
        # accounting currencies. Otherwise source snapshot currency wins over
        # execution currency (TR can execute in EUR a USD-quoted instrument).
        currencies = snapshots_by_ticker.get(ticker) or operations_by_ticker.get(ticker, set())
        declared = _currency(frame.attrs.get("display_currency") or frame.attrs.get("quote_currency"))
        if declared and declared in currencies:
            currency = declared
        elif len(currencies) == 1 and not declared:
            currency = next(iter(currencies))
        else:
            issues.append("No se ha aplicado una cotización cuya moneda nativa falta o contradice la cartera.")
            continue
        dates = pd.Series([_date(value) for value in frame.index], index=frame.index)
        if dates.isna().all():
            for key in ("date", "Date", "datetime", "timestamp"):
                if key in frame:
                    dates = frame[key].map(_date)
                    break
        valid = dates.notna()
        if reference is not None:
            valid &= dates.map(lambda value: value is not None and not pd.isna(value)
                               and value.normalize() <= reference.normalize())
        if not valid.any():
            continue
        closes = pd.to_numeric(frame[close_column], errors="coerce")
        candidates = pd.DataFrame({"date": dates.to_list(), "close": closes.to_list(), "valid": valid.to_list()})
        latest = candidates.loc[candidates["valid"]].sort_values("date", kind="stable").iloc[-1]
        if _number(latest["close"]) is None or float(latest["close"]) <= 0:
            issues.append("El último cierre preparado no es válido; la cotización actual queda pendiente y no se ha sustituido por una anterior.")
            # A missing current quote must not reuse a prior historical close as
            # if current; an explicit unavailable quote invalidates valuation.
            quote = {"price": None, "currency": currency, "as_of": latest["date"].date().isoformat()}
            prices[ticker] = quote
            continue
        price, day = float(latest["close"]), latest["date"]
        baseline_dates = [
            observed for row in snapshot_rows
            if _ticker(row) == ticker and (observed := _date(row.get("snapshot_date"))) is not None
            and (reference is None or observed.normalize() <= reference.normalize())
        ]
        if not quote_freshness(day, reference_date=reference).fresh or (
            baseline_dates and day.normalize() < max(baseline_dates).normalize()
        ):
            issues.append("Cotización antigua o anterior a la fotografía: se conserva la valoración declarada, no un precio actual.")
            continue
        quote: dict[str, object] = {"price": price, "currency": currency,
                                    "as_of": day.date().isoformat()}
        rate = 1.0 if currency == "EUR" else _number((rates or {}).get(currency))
        if rate is not None and rate > 0:
            quote["fx_rate_to_eur"] = 1.0 / rate
        prices[ticker] = quote
        for row in snapshot_rows + operation_rows:
            if _ticker(row) == ticker:
                account = _text(row.get("account_name")) or _text(row.get("platform"))
                state_currency = _currency(row.get("currency"))
                if account and state_currency:
                    prices[(account, ticker, state_currency)] = quote
    return prices


def _valuation_history(snapshots: pd.DataFrame) -> pd.DataFrame:
    columns = ["date", "value_eur"]
    if snapshots.empty or not {"snapshot_date", "platform", "value_eur"}.issubset(snapshots.columns):
        return pd.DataFrame(columns=columns)
    frame = snapshots.copy(deep=True)
    frame["_date"] = frame["snapshot_date"].map(_date)
    scope = {_text(value) for value in frame["platform"] if _text(value)}
    rows = []
    for day, group in frame.dropna(subset=["_date"]).groupby("_date", sort=True):
        platforms = {_text(value) for value in group["platform"] if _text(value)}
        amounts = [_number(value) for value in group["value_eur"]]
        if scope and platforms == scope and all(_text(value) for value in group["platform"]) and all(value is not None for value in amounts):
            rows.append({"date": day.date().isoformat(), "value_eur": sum(amounts)})
    return pd.DataFrame(rows, columns=columns)


def load_reconciled_portfolio(journal: object, snapshots: pd.DataFrame, operations: pd.DataFrame, *,
                               prepared: Mapping[str, pd.DataFrame] | None = None,
                               rates_per_eur: Mapping[str, object] | None = None,
                               reference_date: object | None = None,
                               sector_by_ticker: Mapping[str, str] | None = None) -> ReconciledPortfolioResult | None:
    """Load existing import evidence and compute accounting without any writes.

    ``None`` means only that the journal has no import-reader capability or no
    saved batch. Read/validation/accounting errors intentionally propagate.
    Current quotes come solely from dated prepared OHLC closes, with explicit
    native currencies and positive supplied rates; current FX is never used to
    manufacture historical trade settlements or gains.
    """
    reader = getattr(journal, "list_portfolio_imports", None)
    if not callable(reader):
        return None
    batches = reader()  # Default public projection excludes private backups.
    if not isinstance(batches, pd.DataFrame):
        raise ValueError("La lista de importaciones debe ser una tabla.")
    if batches.empty:
        return None
    reference = _date(reference_date) if reference_date is not None else None
    if reference_date is not None and reference is None:
        raise ValueError("La fecha de reconciliación no es válida.")
    metadata = _batch_metadata(batches)
    issues = _import_issues(metadata)
    enriched = _rehydrate_operations(operations, metadata)
    events = _events(metadata, issues)
    prices = _price_map(snapshots, enriched, prepared, rates_per_eur, reference, issues)
    result = build_reconciled_portfolio(
        snapshots, enriched, events, as_of=reference_date, prices=prices,
        coverage=_merge_coverage(metadata), valuation_history=_valuation_history(snapshots),
    )
    sectors = {resolve_analysis_ticker(key): _text(value) for key, value in (sector_by_ticker or {}).items()}
    positions, assets = result.positions.copy(deep=True), result.metrics_by_asset.copy(deep=True)
    for frame in (positions, assets):
        if "ticker" in frame:
            # Presentation may label this 'Sin sector informado'; accounting and
            # review logic must still see a genuinely missing classification.
            frame["sector"] = frame["ticker"].map(lambda ticker: sectors.get(_text(ticker), ""))
    return replace(result, positions=positions, metrics_by_asset=assets,
                   issues=tuple(dict.fromkeys([*result.issues, *issues])))
