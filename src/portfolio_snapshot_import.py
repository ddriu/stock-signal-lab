"""Importación fiel de fotografías de cartera sin inventar operaciones."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO

import pandas as pd


PORTFOLIO_SHEET = "Cartera"
REQUIRED_COLUMNS = (
    "Fecha",
    "Plataforma",
    "Activo",
    "Ticker / ISIN",
    "Tipo",
    "Bloque",
    "Cantidad",
    "Precio actual",
    "Moneda",
    "Valor actual (€)",
    "Rentabilidad",
    "Coste estimado (€)",
    "Ganancia/Pérdida (€)",
    "Comentarios",
    "Fuente",
)
PORTFOLIO_IMPORT_NOTE_PREFIX = "Importado desde fotografía de cartera."
CIVISLEND_IMPORT_NOTE_PREFIX = "Importado desde fotografía de cartera de Civislend."

ANALYSIS_TICKER_OVERRIDES = {
    "6VO": "RDDT",
    "AMZ": "AMZN",
    "CEBS": "CEBS.DE",
    "NETFLIX": "NFLX",
    "7974 / NTDOY": "NTDOY",
    "KAP": "KAP.IL",
    "1801": "1801.HK",
    "05Y": "05Y.F",
}


@dataclass(frozen=True)
class PortfolioWorkbookSnapshot:
    positions: pd.DataFrame
    accounts: pd.DataFrame
    snapshot_date: str


@dataclass(frozen=True)
class PortfolioWorkbookImportResult:
    positions_saved: int
    civislend_created: int
    civislend_updated: int
    accounts_saved: int


def _optional_number(value: object, column: str) -> float | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"La columna {column} contiene un número no válido: {value!r}.") from exc


def _required_non_negative(value: object, column: str) -> float:
    number = _optional_number(value, column)
    if number is None or number < 0:
        raise ValueError(f"La columna {column} debe contener importes no negativos.")
    return number


def _analysis_ticker(raw_identifier: object) -> str:
    raw = str(raw_identifier or "").strip().upper()
    if not raw or (len(raw) == 12 and raw[:2].isalpha()):
        return ""
    return ANALYSIS_TICKER_OVERRIDES.get(raw, raw)


def _canonical_platform(platform: object, asset_type: object) -> str:
    if str(asset_type or "").strip().casefold() == "factoring":
        return "Segofactoring"
    return str(platform or "").strip()


def normalize_portfolio_snapshot_frame(source: pd.DataFrame) -> pd.DataFrame:
    """Normaliza la hoja Cartera manteniendo importes estimados como estimaciones."""

    frame = source.copy()
    frame.columns = [str(column).strip() for column in frame.columns]
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError("Faltan columnas en la hoja Cartera: " + ", ".join(missing))
    frame = frame.loc[:, REQUIRED_COLUMNS].dropna(how="all").reset_index(drop=True)
    if frame.empty:
        raise ValueError("La hoja Cartera no contiene posiciones.")

    dates = pd.to_datetime(frame["Fecha"], dayfirst=True, errors="coerce")
    if dates.isna().any():
        bad_rows = (dates[dates.isna()].index + 2).tolist()
        raise ValueError(f"Hay fechas no válidas en las filas: {bad_rows}.")
    unique_dates = dates.dt.date.unique()
    if len(unique_dates) != 1:
        raise ValueError("Cada archivo debe contener una sola fecha de valoración.")

    result = pd.DataFrame()
    result["snapshot_date"] = dates.dt.date.astype(str)
    result["asset_name"] = frame["Activo"].fillna("").astype(str).str.strip()
    if (result["asset_name"] == "").any():
        rows = (result.index[result["asset_name"] == ""] + 2).tolist()
        raise ValueError(f"Falta el nombre del activo en las filas: {rows}.")
    result["asset_type"] = frame["Tipo"].fillna("").astype(str).str.strip()
    result["platform"] = [
        _canonical_platform(platform, asset_type)
        for platform, asset_type in zip(frame["Plataforma"], frame["Tipo"])
    ]
    if (result["platform"] == "").any():
        raise ValueError("Todas las posiciones necesitan una plataforma.")
    result["raw_identifier"] = (
        frame["Ticker / ISIN"].fillna("").astype(str).str.strip()
    )
    result["analysis_ticker"] = result["raw_identifier"].map(_analysis_ticker)
    result["portfolio_block"] = frame["Bloque"].fillna("").astype(str).str.strip()
    result["quantity"] = frame["Cantidad"].map(
        lambda value: _optional_number(value, "Cantidad")
    )
    result["current_price"] = frame["Precio actual"].map(
        lambda value: _optional_number(value, "Precio actual")
    )
    result["currency"] = frame["Moneda"].fillna("EUR").astype(str).str.strip().str.upper()
    if (result["currency"].str.len() != 3).any():
        raise ValueError("La moneda debe tener tres letras, por ejemplo EUR o USD.")
    result["value_eur"] = frame["Valor actual (€)"].map(
        lambda value: _required_non_negative(value, "Valor actual (€)")
    )
    # El Excel expresa la rentabilidad como fracción (0,03 = 3%). La base usa puntos %.
    result["return_pct"] = frame["Rentabilidad"].map(
        lambda value: (
            None
            if (number := _optional_number(value, "Rentabilidad")) is None
            else number * 100.0
        )
    )
    result["cost_estimate_eur"] = frame["Coste estimado (€)"].map(
        lambda value: _optional_number(value, "Coste estimado (€)")
    )
    result["gain_loss_eur"] = frame["Ganancia/Pérdida (€)"].map(
        lambda value: _optional_number(value, "Ganancia/Pérdida (€)")
    )
    result["comments"] = frame["Comentarios"].fillna("").astype(str).str.strip()
    result["source"] = frame["Fuente"].fillna("").astype(str).str.strip()
    result["notes"] = (
        PORTFOLIO_IMPORT_NOTE_PREFIX
        + " Coste y rentabilidad conservados tal como figuran en el archivo; "
        + "no representan operaciones reconstruidas. "
        + result["comments"].where(result["comments"] != "", "Sin comentario adicional.")
    )
    return result


def account_summaries_from_positions(positions: pd.DataFrame) -> pd.DataFrame:
    """Separa inversiones y efectivo sin contar Segofactoring dos veces."""

    rows: list[dict[str, object]] = []
    for platform, group in positions.groupby("platform", sort=False):
        cash_mask = group["asset_type"].str.casefold() == "efectivo"
        rows.append(
            {
                "account_name": str(platform),
                "account_type": (
                    "Inversión alternativa"
                    if platform in {"Segofactoring", "Civislend"}
                    else "Bróker"
                ),
                "investments_value": float(group.loc[~cash_mask, "value_eur"].sum()),
                "cash_balance": float(group.loc[cash_mask, "value_eur"].sum()),
                "currency": "EUR",
                "status": "Pendiente de actualizar",
            }
        )
    return pd.DataFrame(rows)


def parse_portfolio_snapshot_excel(source: bytes | BinaryIO) -> PortfolioWorkbookSnapshot:
    """Lee la fotografía de cartera y calcula resúmenes reconciliados."""

    payload: bytes | BinaryIO = BytesIO(source) if isinstance(source, bytes) else source
    try:
        frame = pd.read_excel(payload, sheet_name=PORTFOLIO_SHEET, dtype=object)
    except ValueError as exc:
        raise ValueError(f"El Excel debe contener la hoja {PORTFOLIO_SHEET!r}.") from exc
    positions = normalize_portfolio_snapshot_frame(frame)
    return PortfolioWorkbookSnapshot(
        positions=positions,
        accounts=account_summaries_from_positions(positions),
        snapshot_date=str(positions["snapshot_date"].iloc[0]),
    )


def import_civislend_snapshot_rows(
    journal: object,
    positions: pd.DataFrame,
    *,
    recorded_by: str,
) -> tuple[int, int]:
    """Crea/actualiza los proyectos genéricos de Civislend sin tocar filas manuales."""

    rows = positions.loc[positions["platform"] == "Civislend"]
    if rows.empty:
        return 0, 0
    existing = journal.list_private_investments()
    if existing.empty or not {"platform", "notes"}.issubset(existing.columns):
        imported = existing.iloc[0:0]
    else:
        imported = existing.loc[
            (existing["platform"] == "Civislend")
            & existing["notes"].fillna("").astype(str).str.startswith(
                CIVISLEND_IMPORT_NOTE_PREFIX
            )
        ]
    imported_by_name = {
        str(row.project_name): int(row.id) for row in imported.itertuples(index=False)
    }
    existing_civislend_names = {
        str(row.project_name)
        for row in existing.itertuples(index=False)
        if str(row.platform) == "Civislend"
    }
    created = 0
    updated = 0
    for row in rows.itertuples(index=False):
        notes = (
            f"{CIVISLEND_IMPORT_NOTE_PREFIX} Fotografía {row.snapshot_date}; "
            "la fecha real de inversión, el vencimiento y el rendimiento esperado "
            "no aparecen en el archivo."
        )
        asset_name = str(row.asset_name)
        investment_id = imported_by_name.get(asset_name)
        if investment_id is not None:
            journal.update_private_investment(
                investment_id,
                current_value=float(row.value_eur),
                status="Activa",
                notes=notes,
            )
            updated += 1
        elif asset_name in existing_civislend_names:
            # La misma posición puede haberse registrado manualmente antes de
            # existir la importación por fotografías. Se conserva intacta y no
            # se crea una segunda copia sólo porque sus notas no lleven el
            # prefijo automático.
            continue
        else:
            journal.add_private_investment(
                platform="Civislend",
                project_name=asset_name,
                invested_amount=float(
                    row.cost_estimate_eur
                    if pd.notna(row.cost_estimate_eur)
                    else row.value_eur
                ),
                current_value=float(row.value_eur),
                expected_return_pct=0.0,
                start_date=str(row.snapshot_date),
                maturity_date=None,
                status="Activa",
                notes=notes,
                recorded_by=recorded_by,
            )
            created += 1
    return created, updated


def import_portfolio_workbook_snapshot(
    journal: object,
    snapshot: PortfolioWorkbookSnapshot,
    *,
    recorded_by: str,
) -> PortfolioWorkbookImportResult:
    """Guarda fotografía, cuentas agregadas y detalle genérico de Civislend."""

    positions_saved = journal.upsert_portfolio_snapshot_positions(
        snapshot.positions,
        recorded_by=recorded_by,
    )
    accounts_saved = 0
    for account in snapshot.accounts.itertuples(index=False):
        journal.upsert_portfolio_account(
            account_name=str(account.account_name),
            account_type=str(account.account_type),
            investments_value=float(account.investments_value),
            cash_balance=float(account.cash_balance),
            currency=str(account.currency),
            status="Pendiente de actualizar",
            notes=(
                f"Fotografía de cartera del {snapshot.snapshot_date}; "
                "pendiente de actualizar con datos actuales."
            ),
        )
        accounts_saved += 1
    civislend_created, civislend_updated = import_civislend_snapshot_rows(
        journal,
        snapshot.positions,
        recorded_by=recorded_by,
    )
    return PortfolioWorkbookImportResult(
        positions_saved=positions_saved,
        civislend_created=civislend_created,
        civislend_updated=civislend_updated,
        accounts_saved=accounts_saved,
    )
