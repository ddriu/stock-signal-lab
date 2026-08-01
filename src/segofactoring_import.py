"""Importación explicable e idempotente del resumen Excel de Segofactoring."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO
import unicodedata

import pandas as pd


SEGOFACTORING_SHEET = "Resumenes Operaciones"
SEGOFACTORING_REQUIRED_COLUMNS = (
    "NombreOperacion",
    "Estado",
    "Fecha Inversion",
    "Fecha Vencimiento",
    "Inversion Realizada",
    "Ganancias Ordinarias",
    "Ganancias ExtraOrdinarias",
    "Comisiones",
    "Retenciones",
)
SEGOFACTORING_IMPORT_NOTE_PREFIX = "Importado desde resumen de Segofactoring."


@dataclass(frozen=True)
class SegofactoringImportResult:
    created: int
    updated: int


def _plain_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _spanish_number(value: object, column: str) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("€", "").replace(" ", "")
    if not text:
        return 0.0
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"La columna {column} contiene un importe no válido: {value!r}.") from exc


def _mapped_status(value: object) -> str:
    source = _plain_text(value).strip().casefold()
    if "esperando" in source or "pendiente" in source:
        return "Activa"
    if "cobrado" in source or "pagado" in source:
        return "Finalizada"
    if "retras" in source or "vencido" in source:
        return "Retrasada"
    if "impagad" in source:
        return "Impagada"
    raise ValueError(f"Estado de Segofactoring no reconocido: {value!r}.")


def _iso_dates(values: pd.Series, column: str) -> pd.Series:
    parsed = pd.to_datetime(values, dayfirst=True, errors="coerce")
    if parsed.isna().any():
        bad_rows = (parsed[parsed.isna()].index + 2).tolist()
        raise ValueError(f"Fechas no válidas en {column}, filas: {bad_rows}.")
    return parsed.dt.date.astype(str)


def normalize_segofactoring_frame(source: pd.DataFrame) -> pd.DataFrame:
    """Convierte la exportación española al contrato de inversiones privadas."""

    frame = source.copy()
    frame.columns = [str(column).strip() for column in frame.columns]
    missing = [column for column in SEGOFACTORING_REQUIRED_COLUMNS if column not in frame]
    if missing:
        raise ValueError("Faltan columnas del resumen de Segofactoring: " + ", ".join(missing))
    frame = frame.loc[:, SEGOFACTORING_REQUIRED_COLUMNS].dropna(how="all").reset_index(drop=True)
    if frame.empty:
        raise ValueError("El resumen de Segofactoring no contiene operaciones.")

    result = pd.DataFrame()
    result["project_name"] = frame["NombreOperacion"].fillna("").astype(str).str.strip()
    if (result["project_name"] == "").any():
        rows = (result.index[result["project_name"] == ""] + 2).tolist()
        raise ValueError(f"Falta el nombre de la operación en las filas: {rows}.")
    result["source_status"] = frame["Estado"].fillna("").astype(str).str.strip()
    result["status"] = result["source_status"].map(_mapped_status)
    result["start_date"] = _iso_dates(frame["Fecha Inversion"], "Fecha Inversion")
    result["maturity_date"] = _iso_dates(
        frame["Fecha Vencimiento"], "Fecha Vencimiento"
    )

    numeric_mapping = {
        "invested_amount": "Inversion Realizada",
        "gross_profit": "Ganancias Ordinarias",
        "extraordinary_profit": "Ganancias ExtraOrdinarias",
        "fees": "Comisiones",
        "withholding": "Retenciones",
    }
    for target, source_column in numeric_mapping.items():
        result[target] = frame[source_column].map(
            lambda value, column=source_column: _spanish_number(value, column)
        )
    if (result["invested_amount"] <= 0).any():
        rows = (result.index[result["invested_amount"] <= 0] + 2).tolist()
        raise ValueError(f"La inversión debe ser positiva en las filas: {rows}.")

    result["net_profit"] = (
        result["gross_profit"]
        + result["extraordinary_profit"]
        - result["fees"]
        - result["withholding"]
    )
    result["current_value"] = result["invested_amount"].where(
        result["status"] != "Finalizada", 0.0
    )
    # La exportación no contiene la rentabilidad prevista; cero significa dato ausente,
    # no una previsión de rendimiento nulo.
    result["expected_return_pct"] = 0.0
    identity_columns = ["project_name", "start_date", "maturity_date", "invested_amount"]
    result["duplicate_number"] = result.groupby(identity_columns, dropna=False).cumcount() + 1
    result["notes"] = result.apply(
        lambda row: (
            f"{SEGOFACTORING_IMPORT_NOTE_PREFIX} Estado original: {row['source_status']}. "
            f"Ganancia bruta: {row['gross_profit']:.2f} €; extraordinaria: "
            f"{row['extraordinary_profit']:.2f} €; comisiones: {row['fees']:.2f} €; "
            f"retenciones: {row['withholding']:.2f} €; neta después de impuestos: "
            f"{row['net_profit']:.2f} €. Participación {int(row['duplicate_number'])}."
        ),
        axis=1,
    )
    return result


def parse_segofactoring_excel(source: bytes | BinaryIO) -> pd.DataFrame:
    """Lee únicamente la hoja de operaciones del fichero XLSX."""

    payload: bytes | BinaryIO = BytesIO(source) if isinstance(source, bytes) else source
    try:
        frame = pd.read_excel(payload, sheet_name=SEGOFACTORING_SHEET, dtype=object)
    except ValueError as exc:
        raise ValueError(
            f"El Excel debe contener la hoja {SEGOFACTORING_SHEET!r}."
        ) from exc
    return normalize_segofactoring_frame(frame)


def private_investment_identity(
    project_name: object,
    start_date: object,
    maturity_date: object,
    invested_amount: object,
) -> tuple[str, str, str, float]:
    """Identidad estable que conserva participaciones duplicadas por orden."""

    start = pd.Timestamp(start_date).date().isoformat()
    maturity = pd.Timestamp(maturity_date).date().isoformat()
    return (
        str(project_name).strip(),
        start,
        maturity,
        round(float(invested_amount), 2),
    )


def import_segofactoring_rows(
    journal: object,
    rows: pd.DataFrame,
    *,
    recorded_by: str,
) -> SegofactoringImportResult:
    """Crea o actualiza filas previamente importadas sin borrar datos manuales."""

    existing = journal.list_private_investments()
    if existing.empty or not {"platform", "notes"}.issubset(existing.columns):
        imported_existing = existing.iloc[0:0].copy()
    else:
        imported_existing = existing.loc[
            (existing["platform"] == "Segofactoring")
            & existing["notes"].fillna("").astype(str).str.startswith(
                SEGOFACTORING_IMPORT_NOTE_PREFIX
            )
        ].copy()
    buckets: dict[tuple[str, str, str, float], list[int]] = {}
    for row in imported_existing.sort_values("id").itertuples(index=False):
        key = private_investment_identity(
            row.project_name,
            row.start_date,
            row.maturity_date,
            row.invested_amount,
        )
        buckets.setdefault(key, []).append(int(row.id))

    created = 0
    updated = 0
    for row in rows.itertuples(index=False):
        key = private_investment_identity(
            row.project_name,
            row.start_date,
            row.maturity_date,
            row.invested_amount,
        )
        candidates = buckets.get(key, [])
        if candidates:
            journal.update_private_investment(
                candidates.pop(0),
                current_value=float(row.current_value),
                status=str(row.status),
                notes=str(row.notes),
            )
            updated += 1
            continue
        journal.add_private_investment(
            platform="Segofactoring",
            project_name=str(row.project_name),
            invested_amount=float(row.invested_amount),
            current_value=float(row.current_value),
            expected_return_pct=float(row.expected_return_pct),
            start_date=str(row.start_date),
            maturity_date=str(row.maturity_date),
            status=str(row.status),
            notes=str(row.notes),
            recorded_by=recorded_by,
        )
        created += 1
    return SegofactoringImportResult(created=created, updated=updated)
