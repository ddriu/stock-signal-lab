"""La portada consume sólo cifras reconciliadas y no inventa cobertura."""

from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.portfolio_overview_ui import (
    build_overview_allocations,
    build_overview_movers,
    build_overview_position_rows,
)


def position(ticker="AAA", value=100, pnl=10, **changes):
    values = dict(platform="Broker A", account_name="Broker A", analysis_ticker=ticker,
                  asset_name=ticker, liquidity_class="liquid", value_eur=value,
                  value_complete=True, cost_basis_complete=True,
                  unrealized_pnl_eur=pnl, market_as_of="2026-10-14",
                  valuation_status="Precio actualizado")
    values.update(changes)
    return values


def test_liquid_concentration_excludes_cash_and_illiquid_wealth():
    positions = pd.DataFrame([
        position("AAA", 75), position("BBB", 25),
        position("", 900, liquidity_class="cash"),
        position("SEGO", 1000, platform="Segofactoring", liquidity_class="illiquid"),
    ])
    company = build_overview_allocations(positions, "company").set_index("Grupo")
    platform = build_overview_allocations(positions, "platform").set_index("Grupo")

    assert company["Peso %"].to_dict() == {"AAA": 75, "BBB": 25}
    assert platform.loc["Broker A", "Peso %"] == pytest.approx(50)
    assert platform.loc["Segofactoring", "Peso %"] == pytest.approx(50)


def test_duplicate_company_has_one_global_exposure_and_separate_platform_weights():
    positions = pd.DataFrame([
        position("AAA", 50), position("BBB", 150),
        position("AAA", 100, platform="Broker B"),
        position("", 100, platform="Broker B", liquidity_class="cash"),
    ])
    company = build_overview_allocations(positions, "company").set_index("Grupo")
    rows = build_overview_position_rows(positions)

    assert company.loc["AAA", "Valor EUR"] == 150
    assert company.loc["AAA", "Peso %"] == 50
    assert rows["Peso patrimonio %"].tolist() == [12.5, 37.5, 25, 25]
    assert rows["Peso plataforma %"].tolist() == [25, 75, 50, 50]


def test_unknown_value_does_not_become_zero_or_a_partial_denominator():
    positions = pd.DataFrame([
        position("AAA", 100),
        position("BBB", 100, value_complete=False, platform="Broker B"),
    ])
    distribution = build_overview_allocations(positions, "company")
    rows = build_overview_position_rows(positions)

    assert distribution["Peso %"].isna().all()
    assert pd.isna(distribution.set_index("Grupo").loc["BBB", "Valor EUR"])
    assert rows["Peso patrimonio %"].isna().all()
    assert rows.iloc[0]["Peso plataforma %"] == 100
    assert pd.isna(rows.iloc[1]["Peso plataforma %"])


def test_unknown_sector_remains_visible_without_guessing_from_ticker():
    distribution = build_overview_allocations(pd.DataFrame([
        position("AAA", 20, sector="Tecnología"), position("BBB", 80),
    ]), "sector").set_index("Grupo")

    assert distribution.loc["Sin sector informado", "Peso %"] == 80
    assert distribution.loc["Tecnología", "Peso %"] == 20


def test_top_five_uses_complete_liquid_company_results_not_partial_costs():
    positions = [position(f"UP{number}", 100, number) for number in range(1, 8)]
    positions += [position(f"DOWN{number}", 100, -number) for number in range(1, 8)]
    positions += [
        position("PARTIAL", 100, 10000),
        position("PARTIAL", 100, 999, cost_basis_complete=False, platform="Broker B"),
        position("PRIVATE", 100, 9999, liquidity_class="illiquid"),
        position("CASH", 100, 9999, liquidity_class="cash"),
    ]
    gains, losses = build_overview_movers(pd.DataFrame(positions))

    assert gains["Empresa"].tolist() == ["UP7", "UP6", "UP5", "UP4", "UP3"]
    assert losses["Empresa"].tolist() == ["DOWN7", "DOWN6", "DOWN5", "DOWN4", "DOWN3"]


def test_top_five_aggregates_pnl_without_aggregating_platform_cost_lots():
    gains, _ = build_overview_movers(pd.DataFrame([
        position("AAA", 100, 10), position("AAA", 100, -5, platform="Broker B"),
    ]))

    assert gains.to_dict("records") == [{"Empresa": "AAA", "Latente EUR": 5}]


@pytest.mark.parametrize("dimension", ["company", "platform", "sector"])
def test_empty_data_remains_empty(dimension):
    assert build_overview_allocations(pd.DataFrame(), dimension).empty
    assert build_overview_position_rows(pd.DataFrame()).empty
    assert all(frame.empty for frame in build_overview_movers(pd.DataFrame()))


def test_allocations_reject_unknown_dimension():
    with pytest.raises(ValueError):
        build_overview_allocations(pd.DataFrame(), "guess")


SCRIPT = '''
from types import SimpleNamespace
import pandas as pd
from src.portfolio_overview_ui import render_portfolio_overview

report = SimpleNamespace(
    positions=pd.DataFrame([dict(platform="Broker A", asset_name="Test", analysis_ticker="TEST",
        liquidity_class="liquid", value_eur=100, value_complete=True,
        cost_basis_complete=False, unrealized_pnl_eur=None)]),
    metrics_global=dict(value_eur=100, liquid_value_eur=100, cash_value_eur=0,
        illiquid_value_eur=0, unrealized_pnl_eur=None, unrealized_pnl_known_eur=0,
        realized_pnl_eur=None, total_pnl_eur=None, total_return_pct=None),
    monthly=pd.DataFrame([dict(period="2026-09", ending_value_eur=None,
        net_contributions_eur=None, total_return_pct=None),
        dict(period="2026-10", ending_value_eur=100,
        net_contributions_eur=None, total_return_pct=None)]),
    annual=pd.DataFrame([dict(period="2026", ending_value_eur=100,
        net_contributions_eur=None, total_return_pct=None)]),
    issues=("Falta coste documentado",),
)
render_portfolio_overview(report, source_label="Prueba sin conexiones",
    snapshot_date="2026-10-13", valuation_date="2026-10-14", key="test_overview")
'''


def test_ui_shows_unknown_results_and_dates_without_any_orders_or_writes():
    app = AppTest.from_string(SCRIPT, default_timeout=30).run()

    assert not app.exception
    assert not app.error
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Patrimonio financiero"] == "100.00 €"
    assert metrics["Resultado latente"] == "N/D"
    assert metrics["Resultado realizado"] == "N/D"
    assert metrics["Resultado total"] == "N/D"
    text = " ".join(item.value for item in app.caption)
    assert "Prueba sin conexiones" in text
    assert "2026-10-13" in text and "2026-10-14" in text
    assert "Rentabilidad total: N/D" in text
    assert "Civislend y Segofactoring" in text
    assert not app.button
    assert any("dos valoraciones" in info.value for info in app.info)
    assert app.warning
    assert all(not expander.proto.expanded for expander in app.expander)


def test_ui_frequency_switch_preserves_unknown_period_return():
    app = AppTest.from_string(SCRIPT, default_timeout=30).run()
    app.segmented_control(key="test_overview_frequency").set_value("Año").run()

    assert not app.exception
    history = app.dataframe[0].value
    assert history["Periodo"].tolist() == ["2026"]
    assert history["Rentabilidad %"].isna().all()


def test_ui_distributions_are_separate_and_have_no_fabricated_sector():
    app = AppTest.from_string(SCRIPT, default_timeout=30).run()
    app.radio(key="test_overview_distribution").set_value("Sector").run()

    assert not app.exception
    distribution = app.dataframe[1].value
    assert distribution["Grupo"].tolist() == ["Sin sector informado"]
    assert distribution["Peso %"].tolist() == [100]


def test_ui_draws_only_actual_wealth_marks_not_transaction_amounts():
    script = SCRIPT.replace('ending_value_eur=None,', 'ending_value_eur=90,')
    app = AppTest.from_string(script, default_timeout=30).run()

    assert not app.exception
    assert not any("dos valoraciones" in info.value for info in app.info)
    assert len(app.get("vega_lite_chart")) == 1


def test_ui_empty_report_is_safe_and_does_not_report_zero_as_verified():
    script = '''
from types import SimpleNamespace
import pandas as pd
from src.portfolio_overview_ui import render_portfolio_overview
render_portfolio_overview(SimpleNamespace(positions=pd.DataFrame(), metrics_global={},
    monthly=pd.DataFrame(), annual=pd.DataFrame(), issues=()))
'''
    app = AppTest.from_string(script, default_timeout=30).run()

    assert not app.exception
    assert {metric.value for metric in app.metric} == {"N/D"}
    assert not app.dataframe


def test_unrecorded_cash_does_not_allow_known_positions_to_claim_full_wealth_weights():
    script = SCRIPT.replace("metrics_global=dict(value_eur=100,", "metrics_global=dict(value_eur=None, value_complete=False,")
    app = AppTest.from_string(script, default_timeout=30).run()

    assert not app.exception
    assert app.metric[0].value == "N/D"
    assert app.dataframe[1].value["Peso %"].isna().all()
    assert app.dataframe[1].value["Valor EUR"].isna().all()
    positions = app.dataframe[-1].value
    assert positions["Valor EUR"].tolist() == [100]
    assert positions["Peso patrimonio %"].isna().all()
    assert positions["Peso plataforma %"].isna().all()
