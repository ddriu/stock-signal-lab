import pandas as pd
import pytest

from src.journal import TradingJournal
from src.segofactoring_import import (
    import_segofactoring_rows,
    normalize_segofactoring_frame,
)


def sample_segofactoring_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "NombreOperacion": "FACTURA 10",
                "Estado": "Esperando vencimiento",
                "Fecha Inversion": "01/07/2026",
                "Fecha Vencimiento": "01/09/2026",
                "Inversion Realizada": "50,00",
                "Ganancias Ordinarias": "0,00",
                "Ganancias ExtraOrdinarias": "0,00",
                "Comisiones": "0,00",
                "Retenciones": "0,00",
            },
            {
                "NombreOperacion": "FACTURA 10",
                "Estado": "Esperando vencimiento",
                "Fecha Inversion": "01/07/2026",
                "Fecha Vencimiento": "01/09/2026",
                "Inversion Realizada": "50,00",
                "Ganancias Ordinarias": "0,00",
                "Ganancias ExtraOrdinarias": "0,00",
                "Comisiones": "0,00",
                "Retenciones": "0,00",
            },
            {
                "NombreOperacion": "FACTURA COBRADA",
                "Estado": "Cobrado",
                "Fecha Inversion": "01/06/2026",
                "Fecha Vencimiento": "01/07/2026",
                "Inversion Realizada": "100,00",
                "Ganancias Ordinarias": "1,20",
                "Ganancias ExtraOrdinarias": "0,10",
                "Comisiones": "0,20",
                "Retenciones": "0,21",
            },
        ]
    )


def test_segofactoring_normalization_preserves_duplicates_and_net_profit() -> None:
    result = normalize_segofactoring_frame(sample_segofactoring_frame())

    assert len(result) == 3
    assert result.loc[0, "duplicate_number"] == 1
    assert result.loc[1, "duplicate_number"] == 2
    assert result.loc[2, "status"] == "Finalizada"
    assert result.loc[2, "current_value"] == 0
    assert result.loc[2, "net_profit"] == pytest.approx(0.89)
    assert result.loc[:1, "current_value"].sum() == 100


def test_segofactoring_import_is_idempotent_and_keeps_duplicate_participations(tmp_path) -> None:
    journal = TradingJournal(tmp_path / "journal.db", owner="ddriu")
    rows = normalize_segofactoring_frame(sample_segofactoring_frame())

    first = import_segofactoring_rows(journal, rows, recorded_by="ddriu")
    second = import_segofactoring_rows(journal, rows, recorded_by="ddriu")
    stored = journal.list_private_investments()

    assert first.created == 3
    assert first.updated == 0
    assert second.created == 0
    assert second.updated == 3
    assert len(stored) == 3
    assert (stored["project_name"] == "FACTURA 10").sum() == 2
    assert stored.loc[stored["status"] == "Finalizada", "current_value"].iloc[0] == 0


def test_segofactoring_unknown_status_is_rejected() -> None:
    source = sample_segofactoring_frame().iloc[:1].copy()
    source.loc[0, "Estado"] = "Estado misterioso"

    with pytest.raises(ValueError, match="no reconocido"):
        normalize_segofactoring_frame(source)
