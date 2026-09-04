from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from src.alerts import normalize_alert_preferences
from src.alert_runner import run_daily_alerts
from src.entry_opportunity import STATUS_BUYABLE, STATUS_WAIT_PRICE
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
