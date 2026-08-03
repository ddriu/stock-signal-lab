"""Resumen estable de fotografías de cartera para portada y exportaciones."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


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
