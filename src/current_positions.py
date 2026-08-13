"""Alta sencilla de posiciones actuales sin inventar operaciones históricas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


REFERENCE_GAIN = "Ganancia/pérdida (€)"
REFERENCE_RETURN = "Rentabilidad (%)"
REFERENCE_COST = "Dinero invertido (€)"
REFERENCE_ENTRY = "Cantidad y precio medio de compra"
REFERENCE_OPTIONS = (
    REFERENCE_GAIN,
    REFERENCE_RETURN,
    REFERENCE_COST,
    REFERENCE_ENTRY,
)


@dataclass(frozen=True)
class CurrentPositionEstimate:
    """Valores reconciliados de una posición mostrada por un bróker."""

    current_value_eur: float
    cost_estimate_eur: float
    gain_loss_eur: float
    return_pct: float
    quantity: float | None
    average_entry_price: float | None
    current_price: float | None
    is_estimated: bool
    warning: str = ""


def _optional_positive(value: float | None, label: str) -> float | None:
    if value is None or pd.isna(value) or float(value) == 0:
        return None
    number = float(value)
    if number < 0:
        raise ValueError(f"{label} no puede ser negativo.")
    return number


def estimate_current_position(
    *,
    current_value_eur: float,
    reference_kind: str,
    reference_value: float | None = None,
    quantity: float | None = None,
    average_entry_price: float | None = None,
    buy_fee_eur: float = 0.0,
) -> CurrentPositionEstimate:
    """Calcula coste, resultado y rentabilidad con el dato que aporte el usuario."""

    value = float(current_value_eur)
    fee = float(buy_fee_eur)
    if value <= 0:
        raise ValueError("El valor actual debe ser mayor que cero.")
    if fee < 0:
        raise ValueError("La comisión no puede ser negativa.")
    if reference_kind not in REFERENCE_OPTIONS:
        raise ValueError("Elige qué dato está mostrando tu bróker.")

    known_quantity = _optional_positive(quantity, "La cantidad")
    known_entry = _optional_positive(average_entry_price, "El precio de entrada")

    if reference_kind == REFERENCE_GAIN:
        if reference_value is None:
            raise ValueError("Introduce la ganancia o pérdida en euros.")
        gain = float(reference_value)
        cost = value - gain
    elif reference_kind == REFERENCE_RETURN:
        if reference_value is None:
            raise ValueError("Introduce la rentabilidad mostrada por el bróker.")
        reported_return = float(reference_value)
        if reported_return <= -100:
            raise ValueError("La rentabilidad debe ser mayor que -100%.")
        cost = value / (1.0 + reported_return / 100.0)
        gain = value - cost
    elif reference_kind == REFERENCE_COST:
        if reference_value is None or float(reference_value) <= 0:
            raise ValueError("El dinero invertido debe ser mayor que cero.")
        cost = float(reference_value)
        gain = value - cost
    else:
        if known_quantity is None or known_entry is None:
            raise ValueError("Introduce cantidad y precio medio de compra.")
        cost = known_quantity * known_entry + fee
        gain = value - cost

    if cost <= 0:
        raise ValueError("Los datos producen un coste de compra no válido.")

    warning = ""
    if known_quantity is None and known_entry is not None:
        quantity_without_fee = cost - fee
        if quantity_without_fee > 0:
            known_quantity = quantity_without_fee / known_entry
    elif known_entry is None and known_quantity is not None:
        cost_without_fee = cost - fee
        if cost_without_fee > 0:
            known_entry = cost_without_fee / known_quantity
    elif known_quantity is not None and known_entry is not None:
        detailed_cost = known_quantity * known_entry + fee
        difference_pct = abs(detailed_cost - cost) / cost * 100.0
        if reference_kind != REFERENCE_ENTRY and difference_pct > 1.0:
            warning = (
                "La cantidad y el precio de entrada no cuadran con el resultado "
                "mostrado por el bróker; se conservará como estimación."
            )

    current_price = value / known_quantity if known_quantity else None
    return CurrentPositionEstimate(
        current_value_eur=value,
        cost_estimate_eur=cost,
        gain_loss_eur=gain,
        return_pct=gain / cost * 100.0,
        quantity=known_quantity,
        average_entry_price=known_entry,
        current_price=current_price,
        is_estimated=reference_kind != REFERENCE_ENTRY,
        warning=warning,
    )


def snapshot_with_current_position(
    existing: pd.DataFrame,
    *,
    snapshot_date: date | str,
    position: dict[str, object],
) -> pd.DataFrame:
    """Copia la última foto a la nueva fecha y añade o reemplaza una posición."""

    target_date = pd.Timestamp(snapshot_date).normalize()
    if pd.isna(target_date):
        raise ValueError("La fecha de valoración no es válida.")
    target = target_date.date().isoformat()

    if existing.empty:
        base = pd.DataFrame()
    else:
        frame = existing.copy()
        parsed = pd.to_datetime(frame["snapshot_date"], errors="coerce").dt.normalize()
        if parsed.isna().all():
            raise ValueError("Las posiciones guardadas no tienen una fecha válida.")
        latest_date = parsed.max()
        if target_date < latest_date:
            raise ValueError(
                "La fecha no puede ser anterior a la última valoración guardada."
            )
        base = frame.loc[parsed == latest_date].copy()
        base["snapshot_date"] = target

    new_row = dict(position)
    new_row["snapshot_date"] = target
    platform = str(new_row.get("platform") or "").strip().casefold()
    asset_name = str(new_row.get("asset_name") or "").strip().casefold()
    ticker = str(new_row.get("analysis_ticker") or "").strip().upper()
    if not platform or not asset_name:
        raise ValueError("La posición necesita empresa y plataforma.")

    if not base.empty:
        same_platform = base["platform"].fillna("").astype(str).str.casefold() == platform
        same_name = base["asset_name"].fillna("").astype(str).str.casefold() == asset_name
        saved_tickers = (
            base.get("analysis_ticker", pd.Series("", index=base.index))
            .fillna("")
            .astype(str)
            .str.upper()
        )
        same_ticker = (saved_tickers == ticker) if ticker else pd.Series(False, index=base.index)
        base = base.loc[~(same_platform & (same_name | same_ticker))].copy()

    return pd.concat([base, pd.DataFrame([new_row])], ignore_index=True, sort=False)


def snapshot_without_positions(
    existing: pd.DataFrame,
    *,
    snapshot_date: date | str,
    removed: list[tuple[str, str]],
) -> pd.DataFrame:
    """Crea una foto nueva omitiendo las posiciones que el usuario ya no tiene.

    Las claves son ``(plataforma, activo)`` porque una misma empresa puede estar en
    dos brókeres. No se altera la fotografía histórica de origen.
    """

    if existing.empty:
        raise ValueError("No hay una cartera guardada que se pueda revisar.")

    target_date = pd.Timestamp(snapshot_date).normalize()
    if pd.isna(target_date):
        raise ValueError("La fecha de valoración no es válida.")

    frame = existing.copy()
    parsed = pd.to_datetime(frame["snapshot_date"], errors="coerce").dt.normalize()
    if parsed.isna().all():
        raise ValueError("Las posiciones guardadas no tienen una fecha válida.")
    latest_date = parsed.max()
    if target_date < latest_date:
        raise ValueError(
            "La fecha no puede ser anterior a la última valoración guardada."
        )

    base = frame.loc[parsed == latest_date].copy()
    base["snapshot_date"] = target_date.date().isoformat()
    removed_keys = {
        (str(platform).strip().casefold(), str(asset).strip().casefold())
        for platform, asset in removed
        if str(platform).strip() and str(asset).strip()
    }
    if not removed_keys:
        raise ValueError("Marca al menos una posición que ya no tengas.")

    row_keys = pd.Series(
        [
            (
                str(row.platform).strip().casefold(),
                str(row.asset_name).strip().casefold(),
            )
            for row in base.itertuples(index=False)
        ],
        index=base.index,
    )
    updated = base.loc[~row_keys.isin(removed_keys)].copy()
    removed_count = len(base) - len(updated)
    if removed_count == 0:
        raise ValueError("Las posiciones marcadas ya no aparecen en la cartera actual.")
    if updated.empty:
        raise ValueError(
            "No se puede dejar la fotografía completamente vacía desde esta pantalla."
        )
    return updated.reset_index(drop=True)
