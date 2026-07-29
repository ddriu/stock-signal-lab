from __future__ import annotations

from datetime import date

import pandas as pd

from src.alerts import normalize_alert_preferences
from src.alert_runner import run_daily_alerts
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
            {"close": [100.0]},
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

    summary = run_daily_alerts(
        journal_factory=journal_factory,
        downloader=downloader,
        sender=lambda *args: sent.append(args),
        today=date(2026, 7, 29),
    )

    assert summary.users_checked == 1
    assert summary.tickers_checked == 2
    assert summary.emails_sent == 1
    assert summary.alerts_sent == 1
    assert any("ddriu / BAD" in error for error in summary.errors)
    assert len(sent) == 1
    assert [state.ticker for state in user.saved_states] == ["GOOD"]
