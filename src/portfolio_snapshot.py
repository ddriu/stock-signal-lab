"""Resumen estable de fotografías de cartera para portada y exportaciones."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from src.data_sources import convert_currency
from src.data_loader import resolve_analysis_ticker


HOME_GROUPED_PLATFORMS = ("Civislend", "Segofactoring")


@dataclass(frozen=True)
class PortfolioSnapshotSummary:
    """Cifras reconciliadas de la última fotografía disponible."""

    snapshot_date: str
    line_count: int
    investment_count: int
    platform_count: int
    analyzable_count: int
    value_eur: float
    cost_estimate_eur: float | None
    gain_loss_eur: float | None
    return_pct: float | None


@dataclass(frozen=True)
class PortfolioRefreshSummary:
    """Procedencia de los valores mostrados para una fotografía de cartera."""

    market_priced_count: int
    manual_count: int
    pending_count: int
    market_as_of: str | None


def refresh_portfolio_snapshot_prices(
    positions: pd.DataFrame,
    latest_prices: dict[str, float],
    rates_per_eur: dict[str, float],
    *,
    price_dates: dict[str, object] | None = None,
) -> tuple[pd.DataFrame, PortfolioRefreshSummary]:
    """Revaloriza sólo las partidas con ticker y cantidad comprobables.

    La fotografía sigue siendo la fuente de cantidades y costes. Los activos sin
    ticker (efectivo, Civislend, Segofactoring...) conservan su último valor manual
    para no inventar una cotización. El resultado es sólo de presentación y no
    modifica la fotografía persistida.
    """

    if positions.empty:
        return positions.copy(), PortfolioRefreshSummary(0, 0, 0, None)

    refreshed = positions.copy()
    if "valuation_status" not in refreshed.columns:
        refreshed["valuation_status"] = ""
    if "market_as_of" not in refreshed.columns:
        refreshed["market_as_of"] = ""

    market_priced = 0
    manual = 0
    pending = 0
    observed_dates: list[pd.Timestamp] = []
    price_dates = price_dates or {}

    for index, row in refreshed.iterrows():
        stored_ticker = str(row.get("analysis_ticker") or "").strip().upper()
        ticker = resolve_analysis_ticker(stored_ticker) if stored_ticker else ""
        quantity = pd.to_numeric(row.get("quantity"), errors="coerce")
        currency = str(row.get("currency") or "EUR").strip().upper()
        current_price = latest_prices.get(ticker)

        if not ticker:
            refreshed.at[index, "valuation_status"] = "Dato manual"
            manual += 1
            continue
        if pd.isna(quantity) or float(quantity) <= 0:
            # Una valoración introducida por importe sigue siendo válida aunque no
            # permita recalcularla con una cotización. No debe presentarse como un
            # error ni desaparecer de los totales.
            refreshed.at[index, "valuation_status"] = "Dato manual (sin cantidad)"
            manual += 1
            continue
        if current_price is None or float(current_price) <= 0:
            refreshed.at[index, "valuation_status"] = "Precio pendiente"
            pending += 1
            continue

        try:
            value_eur = float(
                convert_currency(
                    float(quantity) * float(current_price),
                    currency,
                    "EUR",
                    rates_per_eur,
                )
            )
        except (TypeError, ValueError):
            refreshed.at[index, "valuation_status"] = "Cambio de moneda pendiente"
            pending += 1
            continue

        refreshed.at[index, "current_price"] = float(current_price)
        refreshed.at[index, "value_eur"] = value_eur
        cost = pd.to_numeric(row.get("cost_estimate_eur"), errors="coerce")
        if pd.notna(cost):
            gain = value_eur - float(cost)
            refreshed.at[index, "gain_loss_eur"] = gain
            refreshed.at[index, "return_pct"] = (
                gain / float(cost) * 100.0 if float(cost) > 0 else None
            )
        refreshed.at[index, "valuation_status"] = "Precio actualizado"
        raw_date = price_dates.get(ticker)
        parsed_date = pd.to_datetime(raw_date, errors="coerce")
        if pd.notna(parsed_date):
            normalized_date = pd.Timestamp(parsed_date).date().isoformat()
            refreshed.at[index, "market_as_of"] = normalized_date
            observed_dates.append(pd.Timestamp(parsed_date))
        market_priced += 1

    market_as_of = (
        max(observed_dates).date().isoformat() if observed_dates else None
    )
    return refreshed, PortfolioRefreshSummary(
        market_priced_count=market_priced,
        manual_count=manual,
        pending_count=pending,
        market_as_of=market_as_of,
    )


def reconcile_current_portfolio(
    snapshot: pd.DataFrame,
    operations: pd.DataFrame,
    positions_dashboard: pd.DataFrame,
) -> pd.DataFrame:
    """Construye una sola vista actual a partir de fotografía y diario.

    La fotografía conserva fondos, efectivo e inversiones alternativas. Cuando un
    ticker tiene movimientos posteriores a la foto, su posición abierta reconstruida
    pasa a ser la fuente prioritaria. Una foto completa reciente prevalece sobre
    compras antiguas del diario: así una venta declarada desde el bróker no reaparece.
    La función es sólo de presentación: no borra el histórico.
    """

    if operations.empty or "ticker" not in operations.columns:
        return snapshot.copy()

    frame = snapshot.copy()
    relevant_operations = operations
    if not frame.empty and "executed_at" in operations.columns:
        snapshot_dates = pd.to_datetime(
            frame.get("snapshot_date", pd.Series(dtype=object)), errors="coerce"
        )
        operation_dates = pd.to_datetime(operations["executed_at"], errors="coerce")
        if snapshot_dates.notna().any() and operation_dates.notna().any():
            latest_snapshot_date = snapshot_dates.max().normalize()
            relevant_operations = operations.loc[
                operation_dates.dt.normalize() > latest_snapshot_date
            ]

    operated_tickers = {
        resolve_analysis_ticker(str(value).strip().upper())
        for value in relevant_operations["ticker"].dropna().tolist()
        if str(value).strip()
    }
    if not operated_tickers:
        return frame

    if frame.empty:
        frame = pd.DataFrame(
            columns=[
                "snapshot_date", "platform", "asset_name", "raw_identifier",
                "analysis_ticker", "asset_type", "portfolio_block", "quantity",
                "current_price", "currency", "value_eur", "return_pct",
                "cost_estimate_eur", "gain_loss_eur", "comments", "source",
                "notes", "valuation_status", "market_as_of",
            ]
        )

    resolved_snapshot = frame.get(
        "analysis_ticker", pd.Series("", index=frame.index)
    ).fillna("").astype(str).map(
        lambda value: resolve_analysis_ticker(value.strip().upper()) if value.strip() else ""
    )
    templates = frame.loc[resolved_snapshot.isin(operated_tickers)].copy()
    reconciled = frame.loc[~resolved_snapshot.isin(operated_tickers)].copy()

    replacement_rows: list[dict[str, object]] = []
    if not positions_dashboard.empty:
        for position in positions_dashboard.to_dict("records"):
            raw_ticker = str(position.get("ticker") or "").strip().upper()
            ticker = resolve_analysis_ticker(raw_ticker) if raw_ticker else ""
            if not ticker or ticker not in operated_tickers:
                continue

            matching = templates.loc[
                templates.get(
                    "analysis_ticker", pd.Series("", index=templates.index)
                ).fillna("").astype(str).map(
                    lambda value: (
                        resolve_analysis_ticker(value.strip().upper())
                        if value.strip()
                        else ""
                    )
                )
                == ticker
            ]
            row = matching.iloc[0].to_dict() if not matching.empty else {}
            if len(matching) > 1:
                row["platform"] = "Varias cuentas"
            row.setdefault("platform", "Diario de operaciones")
            row.setdefault("asset_name", raw_ticker or ticker)
            row.setdefault("asset_type", "Acción / ETF")
            row.setdefault("portfolio_block", "Cartera actual")
            row["snapshot_date"] = date.today().isoformat()
            row["raw_identifier"] = raw_ticker or ticker
            row["analysis_ticker"] = ticker
            row["quantity"] = position.get("quantity")
            row["current_price"] = position.get("current_price")
            row["currency"] = str(position.get("currency") or "EUR").upper()

            cost = pd.to_numeric(position.get("cost_basis_eur"), errors="coerce")
            value = pd.to_numeric(position.get("net_value_eur"), errors="coerce")
            gain = pd.to_numeric(position.get("net_pnl_eur"), errors="coerce")
            return_pct = pd.to_numeric(position.get("net_return_pct"), errors="coerce")
            if pd.notna(cost):
                row["cost_estimate_eur"] = float(cost)
            if pd.notna(value):
                row["value_eur"] = float(value)
                row["gain_loss_eur"] = float(gain) if pd.notna(gain) else None
                row["return_pct"] = (
                    float(return_pct) if pd.notna(return_pct) else None
                )
                row["valuation_status"] = "Precio actualizado desde el diario"
                row["source"] = "Diario de operaciones + último precio"
            else:
                # Si hoy no hay precio, mantenemos el último valor manual visible,
                # pero la cantidad y el coste proceden ya del diario.
                fallback_values = pd.to_numeric(
                    matching.get("value_eur", pd.Series(dtype=float)), errors="coerce"
                )
                fallback_value = (
                    float(fallback_values.sum()) if fallback_values.notna().any() else None
                )
                row["value_eur"] = fallback_value
                if fallback_value is not None and pd.notna(cost):
                    row["gain_loss_eur"] = fallback_value - float(cost)
                    row["return_pct"] = (
                        (fallback_value - float(cost)) / float(cost) * 100.0
                        if float(cost) > 0
                        else None
                    )
                row["valuation_status"] = "Último valor manual; precio pendiente"
                row["source"] = "Diario de operaciones + último valor manual"
            row["notes"] = (
                "Vista reconciliada: cantidades y coste del diario; la fotografía "
                "histórica original no se ha modificado."
            )
            replacement_rows.append(row)

    if replacement_rows:
        replacements = pd.DataFrame(replacement_rows)
        all_columns = list(dict.fromkeys([*frame.columns, *replacements.columns]))
        reconciled = reconciled.reindex(columns=all_columns)
        replacements = replacements.reindex(columns=all_columns)
        reconciled = (
            replacements.copy()
            if reconciled.empty
            else pd.concat([reconciled, replacements], ignore_index=True)
        )
    return reconciled.reset_index(drop=True)


def group_portfolio_snapshot_for_home(positions: pd.DataFrame) -> pd.DataFrame:
    """Agrupa inversiones alternativas en Inicio sin alterar su detalle guardado.

    Civislend y Segofactoring pueden contener muchos proyectos. En la portada interesa
    ver cuánto capital representan en conjunto; la pestaña de cartera conserva cada
    proyecto por separado.
    """

    if positions.empty or "platform" not in positions.columns:
        return positions.copy()

    frame = positions.copy()
    frame["platform"] = frame["platform"].fillna("").astype(str)
    regular = frame.loc[~frame["platform"].isin(HOME_GROUPED_PLATFORMS)].copy()
    grouped_rows: list[pd.Series] = []

    for platform in HOME_GROUPED_PLATFORMS:
        platform_rows = frame.loc[frame["platform"] == platform].copy()
        if platform_rows.empty:
            continue

        grouped = platform_rows.iloc[0].copy()
        project_count = len(platform_rows)
        grouped["asset_name"] = (
            f"{platform} · total invertido"
            + (f" ({project_count} proyectos)" if project_count > 1 else "")
        )
        grouped["asset_type"] = "Inversión alternativa"
        for column in ("analysis_ticker", "raw_identifier"):
            if column in grouped.index:
                grouped[column] = ""
        for column in ("quantity", "current_price"):
            if column in grouped.index:
                grouped[column] = None

        values = pd.to_numeric(platform_rows.get("value_eur"), errors="coerce")
        grouped["value_eur"] = float(values.fillna(0.0).sum())

        cost_values = pd.to_numeric(
            platform_rows.get("cost_estimate_eur", pd.Series(dtype=float)),
            errors="coerce",
        )
        gain_values = pd.to_numeric(
            platform_rows.get("gain_loss_eur", pd.Series(dtype=float)),
            errors="coerce",
        )
        cost = float(cost_values.sum()) if cost_values.notna().any() else None
        gain = float(gain_values.sum()) if gain_values.notna().any() else None
        if "cost_estimate_eur" in grouped.index:
            grouped["cost_estimate_eur"] = cost
        if "gain_loss_eur" in grouped.index:
            grouped["gain_loss_eur"] = gain
        if "return_pct" in grouped.index:
            grouped["return_pct"] = (
                gain / cost * 100.0
                if cost is not None and cost > 0 and gain is not None
                else None
            )
        grouped_rows.append(grouped)

    if not grouped_rows:
        return regular.reset_index(drop=True)
    rows = regular.to_dict("records") + [row.to_dict() for row in grouped_rows]
    return pd.DataFrame(rows, columns=frame.columns)


def latest_portfolio_snapshot(
    positions: pd.DataFrame,
) -> tuple[pd.DataFrame, PortfolioSnapshotSummary | None]:
    """Selecciona la fecha más reciente y calcula cifras sin mezclar fotografías."""

    if positions.empty:
        return positions.copy(), None

    required = {"snapshot_date", "platform", "asset_name", "asset_type", "value_eur"}
    missing = sorted(required.difference(positions.columns))
    if missing:
        raise ValueError(
            "La fotografía de cartera no contiene: " + ", ".join(missing)
        )

    frame = positions.copy()
    frame["_parsed_snapshot_date"] = pd.to_datetime(
        frame["snapshot_date"], errors="coerce"
    )
    frame = frame.dropna(subset=["_parsed_snapshot_date"])
    if frame.empty:
        raise ValueError("Las fotografías guardadas no contienen una fecha válida.")

    latest_date = frame["_parsed_snapshot_date"].max().normalize()
    latest = frame.loc[
        frame["_parsed_snapshot_date"].dt.normalize() == latest_date
    ].copy()
    latest["value_eur"] = pd.to_numeric(latest["value_eur"], errors="coerce").fillna(0.0)

    cost_values = pd.to_numeric(
        latest.get("cost_estimate_eur", pd.Series(dtype=float)), errors="coerce"
    )
    gain_values = pd.to_numeric(
        latest.get("gain_loss_eur", pd.Series(dtype=float)), errors="coerce"
    )
    cost = float(cost_values.sum()) if cost_values.notna().any() else None
    gain = float(gain_values.sum()) if gain_values.notna().any() else None
    return_pct = (
        gain / cost * 100.0
        if cost is not None and cost > 0 and gain is not None
        else None
    )
    asset_types = latest["asset_type"].fillna("").astype(str).str.casefold()
    analyzable = latest.get(
        "analysis_ticker", pd.Series("", index=latest.index)
    ).fillna("").astype(str).str.strip()

    latest = latest.drop(columns=["_parsed_snapshot_date"])
    return latest, PortfolioSnapshotSummary(
        snapshot_date=latest_date.date().isoformat(),
        line_count=len(latest),
        investment_count=int((asset_types != "efectivo").sum()),
        platform_count=int(latest["platform"].fillna("").astype(str).nunique()),
        analyzable_count=int((analyzable != "").sum()),
        value_eur=float(latest["value_eur"].sum()),
        cost_estimate_eur=cost,
        gain_loss_eur=gain,
        return_pct=return_pct,
    )
