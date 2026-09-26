from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from src.alerts import normalize_alert_preferences
from src.alert_runner import _snapshot_positions, run_daily_alerts
from src.entry_opportunity import STATUS_BUYABLE, STATUS_WAIT_PRICE
from src.opportunity import evaluate_relative_strength
from src.signal_engine import SignalResult
from src.storage import GROUP_PORTFOLIO_OWNER


class FakeJournal:
    def __init__(self, *, tickers: tuple[str, ...] = ()) -> None:
        self.tickers = tickers
        self.saved_states: list[object] = []

    def list_favorites(self) -> pd.DataFrame:
        return pd.DataFrame({"ticker": list(self.tickers)})

    def open_positions(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["ticker", "average_cost"])

    def list_alert_states(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["ticker", "signature"])

    def upsert_alert_states(self, states: list[object]) -> None:
        self.saved_states.extend(states)


def test_latest_snapshot_positions_are_included_in_the_daily_scope() -> None:
    class SnapshotJournal(FakeJournal):
        def list_portfolio_snapshot_positions(self) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "snapshot_date": "2026-09-20",
                        "platform": "Broker",
                        "asset_name": "Posición antigua",
                        "asset_type": "Acción",
                        "analysis_ticker": "OLD",
                        "value_eur": 100.0,
                    },
                    {
                        "snapshot_date": "2026-09-25",
                        "platform": "Broker",
                        "asset_name": "AST SpaceMobile",
                        "asset_type": "Acción",
                        "analysis_ticker": "ASTS",
                        "value_eur": 320.0,
                    },
                    {
                        "snapshot_date": "2026-09-25",
                        "platform": "Broker",
                        "asset_name": "Cobre",
                        "asset_type": "ETF",
                        "analysis_ticker": "CEBS",
                        "value_eur": 150.0,
                    },
                ]
            )

    assert _snapshot_positions(SnapshotJournal()) == {
        "ASTS": "AST SpaceMobile",
        "CEBS.DE": "Cobre",
    }


class FakeGroupJournal(FakeJournal):
    def list_enabled_alert_preferences(self) -> list[object]:
        return [
            normalize_alert_preferences(
                owner="ddriu",
                email="ddriu@example.com",
                enabled=True,
            )
        ]


def test_one_invalid_ticker_does_not_cancel_the_user_digest(monkeypatch) -> None:
    group = FakeGroupJournal()
    user = FakeJournal(tickers=("BAD", "GOOD"))
    sent: list[tuple[object, ...]] = []

    def journal_factory(owner: str) -> FakeJournal:
        return group if owner == GROUP_PORTFOLIO_OWNER else user

    def downloader(
        ticker: str,
        start: date,
        end: date,
        *,
        auto_adjust: bool,
    ) -> pd.DataFrame:
        del ticker, start, end, auto_adjust
        return pd.DataFrame(
            {
                "open": [99.0],
                "high": [101.0],
                "low": [98.0],
                "close": [100.0],
                "atr_14": [2.0],
            },
            index=pd.DatetimeIndex(["2026-07-28"]),
        )

    def evaluate(frame, config, *, ticker: str, entry_price=None) -> SignalResult:
        del frame, config, entry_price
        if ticker == "BAD":
            raise ValueError("No hay suficiente histórico.")
        return SignalResult(
            ticker=ticker,
            as_of=pd.Timestamp("2026-07-28"),
            score=80,
            label="Entrada fuerte",
            position_label="Mantener",
            explanation="Señal de prueba.",
            positive_factors=("tendencia positiva",),
            risk_factors=(),
        )

    monkeypatch.setattr("src.alert_runner.add_indicators", lambda frame, config: frame)
    monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", evaluate)
    monkeypatch.setattr(
        "src.alert_runner.evaluate_fundamentals",
        lambda info, ticker: SimpleNamespace(
            score=75, coverage_pct=80, sector="Technology", country="US"
        ),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_valuation",
        lambda info, ticker: SimpleNamespace(score=70, coverage_pct=80),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_risk",
        lambda ticker, frame: SimpleNamespace(score=70, coverage_pct=100),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_entry_opportunity",
        lambda **kwargs: SimpleNamespace(
            timing=SimpleNamespace(score=75),
            opportunity_score=80,
            status_code=STATUS_BUYABLE,
            status_label="🟢 COMPRABLE",
            zones=SimpleNamespace(preferred_entry=SimpleNamespace(label="98–100")),
            event=SimpleNamespace(label="Sin evento próximo"),
        ),
    )

    summary = run_daily_alerts(
        journal_factory=journal_factory,
        downloader=downloader,
        fundamental_downloader=lambda ticker: {
            "symbol": ticker,
            "longName": f"Empresa {ticker}",
        },
        sender=lambda *args: sent.append(args),
        today=date(2026, 7, 29),
    )

    assert summary.users_checked == 1
    assert summary.tickers_checked == 2
    assert summary.tickers_with_prices == 2
    assert summary.emails_sent == 2
    assert summary.alerts_sent == 1
    assert any("ddriu / BAD" in error for error in summary.errors)
    assert len(sent) == 2
    assert "1 alerta" in sent[0][1]
    assert "resumen diario" in sent[1][1]
    assert [state.ticker for state in user.saved_states] == ["GOOD"]
    assert user.saved_states[0].signature.endswith(STATUS_BUYABLE)
    assert user.saved_states[0].company_name == "Empresa GOOD"
    assert user.saved_states[0].opportunity_score == 80


def test_buy_email_waits_until_the_full_opportunity_is_buyable(monkeypatch) -> None:
    group = FakeGroupJournal()
    user = FakeJournal(tickers=("WAIT",))
    sent: list[tuple[object, ...]] = []

    def journal_factory(owner: str) -> FakeJournal:
        return group if owner == GROUP_PORTFOLIO_OWNER else user

    frame = pd.DataFrame(
        {
            "open": [99.0],
            "high": [101.0],
            "low": [98.0],
            "close": [100.0],
            "atr_14": [2.0],
        },
        index=pd.DatetimeIndex(["2026-07-28"]),
    )
    signal = SignalResult(
        ticker="WAIT",
        as_of=pd.Timestamp("2026-07-28"),
        score=82,
        label="Entrada fuerte",
        position_label="Mantener",
        explanation="Señal fuerte, pero el precio debe esperar.",
        positive_factors=(),
        risk_factors=(),
    )
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    monkeypatch.setattr(
        "src.alert_runner.evaluate_latest_signal", lambda *args, **kwargs: signal
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_fundamentals",
        lambda *args, **kwargs: SimpleNamespace(
            score=75, coverage_pct=80, sector="Technology", country="US"
        ),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_valuation",
        lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=80),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_risk",
        lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=100),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_entry_opportunity",
        lambda **kwargs: SimpleNamespace(
            timing=SimpleNamespace(score=45),
            opportunity_score=60,
            status_code=STATUS_WAIT_PRICE,
            status_label="🟡 ESPERAR PRECIO",
            zones=SimpleNamespace(preferred_entry=SimpleNamespace(label="85–90")),
            event=SimpleNamespace(label="Sin evento próximo"),
        ),
    )

    summary = run_daily_alerts(
        journal_factory=journal_factory,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=lambda ticker: {
            "symbol": ticker,
            "longName": f"Empresa {ticker}",
        },
        sender=lambda *args: sent.append(args),
        today=date(2026, 7, 29),
    )

    assert summary.emails_sent == 1
    assert len(sent) == 1
    assert "resumen diario" in sent[0][1]
    assert user.saved_states[0].signature.endswith(STATUS_WAIT_PRICE)


def test_daily_snapshots_use_cached_broad_and_sector_relative_strength(
    monkeypatch,
) -> None:
    class SnapshotJournal(FakeJournal):
        def __init__(self, *, tickers: tuple[str, ...] = ()) -> None:
            super().__init__(tickers=tickers)
            self.snapshots: list[dict[str, object]] = []

        def list_analysis_snapshots(self) -> pd.DataFrame:
            return pd.DataFrame(columns=["ticker", "analyzed_at"])

        def add_analysis_snapshot(self, **values: object) -> int:
            self.snapshots.append(values)
            return len(self.snapshots)

    group = FakeGroupJournal()
    user = SnapshotJournal(tickers=("AAA", "BBB"))

    def journal_factory(owner: str) -> FakeJournal:
        return group if owner == GROUP_PORTFOLIO_OWNER else user

    index = pd.date_range(end="2026-07-28", periods=253, freq="B")

    def price_frame(final_price: float) -> pd.DataFrame:
        close = [100.0 + (final_price - 100.0) * step / 252 for step in range(253)]
        return pd.DataFrame(
            {
                "open": close,
                "high": [value * 1.01 for value in close],
                "low": [value * 0.99 for value in close],
                "close": close,
                "volume": [1_000_000.0] * len(close),
                "atr_14": [2.0] * len(close),
            },
            index=index,
        )

    downloaded = {
        "AAA": price_frame(160.0),
        "BBB": price_frame(140.0),
        "SPY": price_frame(105.0),
        "XLK": price_frame(110.0),
    }
    download_calls: list[str] = []

    def downloader(
        ticker: str,
        start: date,
        end: date,
        *,
        auto_adjust: bool,
    ) -> pd.DataFrame:
        del start, end, auto_adjust
        download_calls.append(ticker)
        return downloaded[ticker]

    monkeypatch.setattr("src.alert_runner.add_indicators", lambda frame, config: frame)
    monkeypatch.setattr(
        "src.alert_runner.evaluate_latest_signal",
        lambda frame, config, *, ticker, entry_price=None: SignalResult(
            ticker=ticker,
            as_of=frame.index[-1],
            score=82,
            label="Entrada fuerte",
            position_label="Mantener",
            explanation="Señal de prueba.",
            positive_factors=(),
            risk_factors=(),
        ),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_fundamental_filter",
        lambda *args, **kwargs: SimpleNamespace(score=75, label="Sólida"),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_fundamentals",
        lambda info, ticker: SimpleNamespace(
            score=75, coverage_pct=80, sector="Technology", country="US"
        ),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_valuation",
        lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=80),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_risk",
        lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=100),
    )
    monkeypatch.setattr(
        "src.alert_runner.evaluate_growth_momentum",
        lambda **kwargs: SimpleNamespace(score=75, label="Fuerte"),
    )
    opportunity_inputs: list[tuple[int | None, int]] = []

    def entry_opportunity(**kwargs):
        opportunity_inputs.append(
            (kwargs["relative_score"], kwargs["relative_coverage"])
        )
        return SimpleNamespace(
            timing=SimpleNamespace(score=65),
            opportunity_score=72,
            status_code=STATUS_WAIT_PRICE,
            status_label="🟡 ESPERAR PRECIO",
            zones=SimpleNamespace(preferred_entry=SimpleNamespace(label="95–100")),
            event=SimpleNamespace(label="Sin evento próximo"),
            explanation="Análisis automático.",
        )

    monkeypatch.setattr(
        "src.alert_runner.evaluate_entry_opportunity",
        entry_opportunity,
    )

    summary = run_daily_alerts(
        journal_factory=journal_factory,
        downloader=downloader,
        fundamental_downloader=lambda ticker: {
            "symbol": ticker,
            "longName": f"Empresa {ticker}",
            "sector": "Technology",
        },
        sender=lambda *args: None,
        today=date(2026, 7, 29),
    )

    expected_scores = {
        ticker: evaluate_relative_strength(
            ticker,
            downloaded[ticker],
            downloaded["SPY"],
            broad_name="SPY",
            sector=downloaded["XLK"],
            sector_name="XLK",
        ).score
        for ticker in ("AAA", "BBB")
    }
    saved_scores = {
        str(snapshot["ticker"]): snapshot["relative_score"]
        for snapshot in user.snapshots
    }

    assert summary.tickers_checked == 2
    assert summary.tickers_with_prices == 2
    assert download_calls.count("SPY") == 1
    assert download_calls.count("XLK") == 1
    assert saved_scores == expected_scores
    assert opportunity_inputs == [
        (expected_scores["AAA"], 100),
        (expected_scores["BBB"], 100),
    ]
