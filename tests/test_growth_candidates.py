"""Offline tests for the one-table primary candidate presentation model."""

from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.analysis_policy import build_policy_snapshot, encode_policy_note
from src.growth_candidates import GROWTH_CANDIDATE_GROUPS, build_growth_candidate_rows


REFERENCE_DATE = "2026-10-15"


def _result(**overrides: object) -> SimpleNamespace:
    return SimpleNamespace(**{
        "ticker": "TEST", "as_of": "2026-10-14", "label": "Entrada fuerte",
        "score": 85, "growth_score": 84, "momentum_score": 90, "context_score": 75,
        "confidence_pct": 100, "sector_key": "technology", "price": 100.0,
        "atr_stop_pct": 10.0, "positive_factors": ("Ingresos en crecimiento",),
        "risk_factors": ("Vigilar la concentración",), **overrides,
    })


def _rows(results: dict[str, object], **kwargs: object) -> list[dict[str, object]]:
    return build_growth_candidate_rows(results, reference_date=REFERENCE_DATE, **kwargs)


def _frame(*, periods: int = 220, indicators: bool = True, end: str = "2026-10-14") -> pd.DataFrame:
    frame = pd.DataFrame(
        {"close": np.full(periods, 100.0)}, index=pd.date_range(end=end, periods=periods, freq="B"),
    )
    if indicators:
        frame["sma_medium"] = 90.0
        frame["sma_long"] = 80.0
    return frame


def test_only_primary_entries_populate_their_group_and_keep_indicative_reference() -> None:
    rows = _rows(
        {"TEST": _result(), "WAIT": _result(ticker="WAIT", label="Esperar mejor precio")},
        comparison_rows=[{"Ticker": "WAIT", "Oportunidad": 100, "Lectura": "Comprar", "Calidad empresa": 100}],
        names={"TEST": "Test company"},
    )
    assert GROWTH_CANDIDATE_GROUPS == ("Mi cartera", "Entradas fuertes", "Candidatas", "Esperar")
    assert rows[0]["Empresa"] == "Test company · TEST"
    assert rows[0]["Grupo"] == "Entradas fuertes"
    assert rows[0]["Precio referencia"] == 100
    assert rows[0]["Stop orientativo"] == pytest.approx(90)
    assert "último cierre" in rows[0]["Entrada / stop orientativos"]
    assert "teórico" in rows[0]["Entrada / stop orientativos"]
    assert rows[0]["Fecha"] == "2026-10-14"
    assert rows[1]["Grupo"] == "Esperar"
    assert rows[1]["Qué revisar"] == "Esperar mejor precio"
    assert rows[1]["Precio referencia"] is None
    assert rows[1]["Stop orientativo"] is None


def test_held_entry_means_review_addition_and_held_wait_keeps_its_thesis() -> None:
    rows = _rows(
        {"TEST": _result(), "WAIT": _result(ticker="WAIT", label="No preparada", score=35)},
        held_tickers=["TEST", "WAIT"],
        comparison_rows=[{"Ticker": "WAIT", "Si ya la tienes": "Vender"}],
    )
    assert all(row["Grupo"] == "Mi cartera" and row["En cartera"] for row in rows)
    assert rows[0]["Estado"] == "Revisar ampliación"
    assert rows[1]["Estado"] == "Gestionar según tesis"
    assert "Vender" not in rows[1]["Qué revisar"]
    assert rows[1]["Stop orientativo"] is None
    assert all(row["Peso cartera %"] is None for row in rows)


def test_review_order_keeps_holdings_first_then_strong_entries_before_higher_scored_candidates() -> None:
    results = {
        "CANDIDATE": _result(ticker="CANDIDATE", label="Entrada candidata", score=99),
        "WAIT": _result(ticker="WAIT", label="Esperar mejor precio", score=100),
        "STRONG_LOW": _result(ticker="STRONG_LOW", score=80),
        "HELD": _result(ticker="HELD", label="No preparada", score=20),
        "STRONG_HIGH": _result(ticker="STRONG_HIGH", score=90),
        "CANDIDATE_LOW": _result(ticker="CANDIDATE_LOW", label="Entrada candidata", score=70),
    }
    originals = deepcopy(results)
    rows = _rows(results, held_tickers=["HELD"])
    assert [row["Ticker"] for row in rows] == [
        "HELD", "STRONG_HIGH", "STRONG_LOW", "CANDIDATE", "CANDIDATE_LOW", "WAIT",
    ]
    assert [row["Grupo"] for row in rows] == [
        "Mi cartera", "Entradas fuertes", "Entradas fuertes", "Candidatas", "Candidatas", "Esperar",
    ]
    assert {row["Ticker"]: (row["Lectura principal"], row["Nota principal"]) for row in rows} == {
        ticker: (result.label, result.score) for ticker, result in originals.items()
    }
    assert all(vars(results[ticker]) == vars(result) for ticker, result in originals.items())


@pytest.mark.parametrize("label", ["Entrada fuerte", "Entrada candidata", "No preparada"])
def test_held_rows_never_move_to_an_entry_or_wait_group(label: str) -> None:
    row = _rows({"TEST": _result(label=label)}, held_tickers=["TEST"])[0]
    assert row["Grupo"] == "Mi cartera"
    assert row["Lectura principal"] == label


def test_held_declared_thesis_risk_precedes_entries_without_resorting_other_holdings() -> None:
    results = {
        "HELD_WAIT": _result(ticker="HELD_WAIT", label="No preparada", score=20),
        "HELD_STRONG": _result(ticker="HELD_STRONG", score=99),
        "HELD_INVALID": _result(ticker="HELD_INVALID", score=80),
        "NEW": _result(ticker="NEW"),
    }
    rows = _rows(
        results, held_tickers=["HELD_WAIT", "HELD_STRONG", "HELD_INVALID"],
        comparison_rows=[{"Ticker": "HELD_INVALID", "Tesis invalidada": True}],
    )
    assert [row["Ticker"] for row in rows] == ["HELD_INVALID", "HELD_WAIT", "HELD_STRONG", "NEW"]
    assert rows[0]["Estado"] == "Revisión prioritaria de tesis"


@pytest.mark.parametrize("label, momentum, context, expected", [
    ("Entrada fuerte", 75, 50, "Entradas fuertes"),
    ("Entrada candidata", 65, 40, "Candidatas"),
])
def test_existing_entry_validity_boundaries_select_the_engine_label_group(
    label: str, momentum: float, context: float, expected: str,
) -> None:
    result = _result(label=label, momentum_score=momentum, context_score=context, confidence_pct=45)
    row = _rows({"TEST": result})[0]
    assert row["Grupo"] == expected
    assert row["Lectura principal"] == label
    assert row["Datos completos"]
    for overrides in ({"momentum_score": momentum - 0.1}, {"context_score": context - 0.1}, {"confidence_pct": 44.9}):
        invalid = _rows({"TEST": _result(**{**vars(result), **overrides})})[0]
        assert invalid["Grupo"] == "Esperar"
        assert invalid["Lectura principal"] == label
        assert not invalid["Datos completos"]


@pytest.mark.parametrize("label", ["Entrada fuerte", "Entrada candidata"])
@pytest.mark.parametrize("issue", ["stale", "missing"])
def test_unusable_entry_labels_do_not_populate_either_entry_group(label: str, issue: str) -> None:
    result = _result(label=label, **({"as_of": "2026-10-01"} if issue == "stale" else {}))
    rows = _rows(
        {} if issue == "missing" else {"TEST": result}, universe=["TEST"],
    )
    assert rows[0]["Grupo"] == "Esperar"
    assert rows[0]["Precio referencia"] is None
    assert rows[0]["Stop orientativo"] is None


def test_missing_results_and_unanalyzed_favorites_remain_visible_and_pending() -> None:
    rows = _rows(
        {}, universe=["NEW", "NEW", "", None, float("nan"), pd.NA],
        held_tickers=["HELD"], comparison_rows=[{"Ticker": "NEW", "Nota principal": 99, "Lectura principal": "Entrada fuerte"}],
    )
    assert {row["Ticker"] for row in rows} == {"NEW", "HELD"}
    held, new = rows
    assert new["Grupo"] == "Esperar"
    assert not new["Datos completos"]
    assert held["Grupo"] == "Mi cartera"
    assert held["Estado"] == "Gestionar según tesis"
    assert "Completar datos" in held["Qué revisar"]
    assert all(row["Fecha"] is None and row["Precio referencia"] is None for row in rows)


@pytest.mark.parametrize("overrides", [
    {"growth_score": None}, {"growth_score": float("nan")}, {"growth_score": True},
    {"score": float("inf")}, {"score": -1}, {"score": 101}, {"score": True},
    {"momentum_score": None}, {"context_score": pd.NA}, {"confidence_pct": 0},
    {"confidence_pct": 44}, {"confidence_pct": np.bool_(True)}, {"price": None},
    {"price": 0}, {"price": -10}, {"price": float("nan")}, {"price": float("inf")},
    {"price": "oops"}, {"price": True}, {"label": "Datos empresariales parciales"},
    {"label": "Pendiente de fundamentales"}, {"label": ""},
    {"as_of": None}, {"as_of": "not a date"}, {"as_of": "2026-10-01"},
    {"as_of": "2026-10-16"}, {"ticker": "OTHER"},
    {"score": 0}, {"momentum_score": 10}, {"context_score": 10},
])
def test_missing_invalid_or_old_primary_fields_cannot_create_entries(overrides: dict[str, object]) -> None:
    row = _rows({"TEST": _result(**overrides)})[0]
    assert row["Grupo"] == "Esperar"
    assert not row["Datos completos"]
    assert row["Precio referencia"] is None
    assert row["Stop orientativo"] is None


def test_valid_zero_scores_do_not_become_missing_data_for_a_nonentry() -> None:
    row = _rows({"TEST": _result(score=0, growth_score=0, momentum_score=0, context_score=0, label="No preparada")})[0]
    assert row["Datos completos"]
    assert row["Nota principal"] == 0
    assert row["Grupo"] == "Esperar"


def test_etf_can_use_its_existing_engine_result_without_company_growth_score() -> None:
    row = _rows({"TEST": _result(growth_score=None, sector_key="etf")})[0]
    assert row["Datos completos"]
    assert row["Grupo"] == "Entradas fuertes"


def test_plain_result_mapping_and_none_comparison_are_supported() -> None:
    row = _rows({"TEST": vars(_result())}, comparison_rows=None)[0]
    assert row["Grupo"] == "Entradas fuertes"
    assert row["Datos completos"]


@pytest.mark.parametrize("stop", [None, 0, -5, 100, 101, float("nan"), float("inf"), True, "bad"])
def test_invalid_stops_are_not_replaced_by_a_fabricated_stop_or_buy_zone(stop: object) -> None:
    row = _rows({"TEST": _result(atr_stop_pct=stop)})[0]
    assert row["Grupo"] == "Entradas fuertes"
    assert row["Stop orientativo"] is None
    assert row["Precio referencia"] == 100
    assert "stop pendiente" in row["Entrada / stop orientativos"]
    assert "moneda N/D" in row["Entrada / stop orientativos"]


@pytest.mark.parametrize("currency, expected", [("usd", "USD"), ("EUR", "EUR"), ("GBp", "GBp"), ("", "moneda N/D")])
def test_quote_currency_is_explicit_and_prices_are_not_converted(currency: str, expected: str) -> None:
    row = _rows({"TEST": _result()}, currencies={"TEST": currency})[0]
    assert row["Entrada / stop orientativos"].endswith(expected)
    assert row["Precio referencia"] == 100
    assert row["Stop orientativo"] == 90


def test_underflow_or_zero_effective_stop_distance_does_not_print_a_zero_or_entry_stop() -> None:
    for price, stop_pct in [(5e-324, 99), (100.0, 5e-324)]:
        row = _rows({"TEST": _result(price=price, atr_stop_pct=stop_pct)})[0]
        assert row["Precio referencia"] == price
        assert row["Stop orientativo"] is None


@pytest.mark.parametrize("frame", [
    None, pd.DataFrame(), _frame(periods=30, indicators=False),
    _frame().assign(sma_long=float("nan")), _frame().assign(sma_long=0),
    _frame().drop(columns="sma_medium"), _frame().assign(close=0),
    _frame().assign(close=float("nan")), _frame(end="2026-10-01"),
    _frame(end="2026-10-16"), _frame(end="2026-10-15"),
])
def test_missing_short_invalid_or_unsynchronized_prepared_history_is_pending(frame: object) -> None:
    row = _rows({"TEST": _result()}, prepared={"TEST": frame})[0]
    assert row["Grupo"] == "Esperar"
    assert not row["Datos completos"]
    assert row["Stop orientativo"] is None


def test_configurable_indicator_windows_are_not_replaced_with_a_fixed_200_day_rule() -> None:
    row = _rows({"TEST": _result()}, prepared={"TEST": _frame(periods=60)})[0]
    assert row["Grupo"] == "Entradas fuertes"
    assert row["Datos completos"]
    raw = _rows({"TEST": _result()}, prepared={"TEST": _frame(indicators=False)})[0]
    assert raw["Datos completos"]


def test_weights_use_latest_positive_quoted_eur_values_not_quantities_or_total_wealth() -> None:
    snapshot = pd.DataFrame([
        {"snapshot_date": "2026-10-01", "analysis_ticker": "TEST", "value_eur": 9000},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "TEST", "value_eur": 200, "quantity": 1},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "TEST", "value_eur": 100, "quantity": 5000},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "OTHER", "value_eur": 100},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "", "asset_name": "Efectivo", "value_eur": 700},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "CIVI", "platform": "Civislend", "value_eur": 6000},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "SEGO", "platform": "Segofactoring", "value_eur": 6000},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "NEGATIVE", "value_eur": -100},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "ZERO", "value_eur": 0},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "INVALID", "value_eur": float("nan")},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "INFINITE", "value_eur": float("inf")},
    ])
    rows = _rows({}, held_tickers=["TEST", "OTHER", "NEW", "ZERO"], portfolio_snapshot=snapshot)
    weights = {row["Ticker"]: row["Peso cartera %"] for row in rows}
    assert weights == {"TEST": 75.0, "OTHER": 25.0, "NEW": None, "ZERO": None}
    assert all(row["Fecha peso cartera"] == "2026-10-14" for row in rows)


@pytest.mark.parametrize("snapshot", [
    pd.DataFrame(), pd.DataFrame([{"analysis_ticker": "TEST", "quantity": 10}]),
    pd.DataFrame([{"analysis_ticker": "TEST", "value_eur": 0}]),
    pd.DataFrame([{"snapshot_date": "bad", "analysis_ticker": "TEST", "value_eur": 100}]),
])
def test_unavailable_snapshot_weights_remain_unknown_not_zero(snapshot: pd.DataFrame) -> None:
    row = _rows({"TEST": _result()}, held_tickers=["TEST"], portfolio_snapshot=snapshot)[0]
    assert row["Peso cartera %"] is None


@pytest.mark.parametrize("row", [
    {"Ticker": "TEST", "Tesis invalidada": True, "Motivo tesis": "Contrato rescindido"},
    {"Ticker": "TEST", "Política de análisis": {"position": {"invalidated": True, "invalidation_reason": "Contrato rescindido"}}},
    {"Ticker": "TEST", "analysis_policy": {"position": {"thesis_status": "invalidated", "invalidation_reason": "Contrato rescindido"}}},
    {"Ticker": "TEST", "comments": "[[SSL_TESIS_INVALIDADA]] Contrato rescindido"},
])
def test_explicit_invalidated_thesis_has_review_priority_without_inferring_a_sale(row: dict[str, object]) -> None:
    held = _rows({"TEST": _result()}, held_tickers=["TEST"], comparison_rows=[row])[0]
    assert held["Grupo"] == "Mi cartera"
    assert held["Estado"] == "Revisión prioritaria de tesis"
    assert held["Motivo"] == "Contrato rescindido"
    assert "sin venta automática" in held["Qué revisar"]
    assert held["Precio referencia"] is None
    assert held["Stop orientativo"] is None
    unheld = _rows({"TEST": _result()}, comparison_rows=[row])[0]
    assert unheld["Grupo"] == "Entradas fuertes"


def test_saved_policy_note_and_latest_snapshot_preserve_declared_thesis_invalidation() -> None:
    policy = build_policy_snapshot(
        {"Ticker": "TEST", "Tesis invalidada": True, "Motivo tesis": "Fraude declarado"},
        growth_result=_result(), held=True,
    )
    row = _rows(
        {"TEST": _result()}, held_tickers=["TEST"],
        comparison_rows=[{"Ticker": "TEST", "note": encode_policy_note("Nota", policy)}],
    )[0]
    assert row["Estado"] == "Revisión prioritaria de tesis"
    assert row["Motivo"] == "Fraude declarado"
    snapshot = pd.DataFrame([
        {"snapshot_date": "2026-10-01", "analysis_ticker": "TEST", "comments": "[[SSL_TESIS_INVALIDADA]] Viejo"},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "TEST", "comments": "Tesis conservada"},
    ])
    current = _rows({"TEST": _result()}, held_tickers=["TEST"], portfolio_snapshot=snapshot)[0]
    assert current["Estado"] == "Revisar ampliación"


def test_aliases_deduplicate_without_collapsing_distinct_listings_and_adr() -> None:
    rows = _rows(
        {"AMZ": _result(ticker="AMZN"), "AMZN": _result(ticker="AMZN", label="No preparada"),
         "SONY": _result(ticker="SONY"), "6758.T": _result(ticker="6758.T")},
        held_tickers=["amazon", "AMZN"], universe=["AMZ", "Sony", "6758.T", "NEW", "NEW"],
        names={"amazon": "Amazon"},
    )
    assert [row["Ticker"] for row in rows] == ["AMZN", "SONY", "6758.T", "NEW"]
    assert rows[0]["Estado"] == "Gestionar según tesis"
    assert rows[0]["Empresa"] == "Amazon · AMZN"
    assert rows[1]["Grupo"] == rows[2]["Grupo"] == "Entradas fuertes"


def test_alias_snapshot_values_aggregate_and_cash_in_a_quoted_fund_name_is_not_excluded() -> None:
    snapshot = pd.DataFrame([
        {"snapshot_date": "2026-10-14", "analysis_ticker": "AMZ", "value_eur": 100},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "AMZN", "value_eur": 200},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "FUND", "asset_type": "ETF", "asset_name": "Cash Flow UCITS", "value_eur": 100},
        {"snapshot_date": "2026-10-14", "analysis_ticker": "CASH1", "asset_type": "Efectivo", "value_eur": 900},
    ])
    rows = _rows({}, held_tickers=["AMZN", "FUND"], portfolio_snapshot=snapshot)
    assert {row["Ticker"]: row["Peso cartera %"] for row in rows} == {"AMZN": 75.0, "FUND": 25.0}


def test_model_does_not_mutate_results_frames_names_comparison_rows_or_snapshot() -> None:
    result = _result()
    original_result = vars(result).copy()
    frame = _frame()
    original_frame = frame.copy(deep=True)
    snapshot = pd.DataFrame([{"snapshot_date": "2026-10-14", "analysis_ticker": "TEST", "value_eur": 100}])
    original_snapshot = snapshot.copy(deep=True)
    names = {"TEST": {"shortName": "Test name"}}
    comparisons = [{"Ticker": pd.NA, "analysis_ticker": "TEST", "Política de análisis": {"position": {"invalidated": False}}}]
    originals = deepcopy((names, comparisons))
    _rows({"TEST": result}, held_tickers=["TEST"], prepared={"TEST": frame},
          portfolio_snapshot=snapshot, names=names, comparison_rows=comparisons)
    assert vars(result) == original_result
    pd.testing.assert_frame_equal(frame, original_frame)
    pd.testing.assert_frame_equal(snapshot, original_snapshot)
    assert names == originals[0]
    assert comparisons == originals[1]
