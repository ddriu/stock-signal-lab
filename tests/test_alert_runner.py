from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from src.alerts import AlertPreferences, normalize_alert_preferences
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


class FakeManualJournal(FakeJournal):
    def __init__(
        self, preference: AlertPreferences, *, tickers: tuple[str, ...] = ()
    ) -> None:
        super().__init__(tickers=tickers)
        self.preference = preference
        self.preference_reads = 0
        self.state_writes = 0
        self.snapshots: list[dict[str, object]] = []

    def get_alert_preferences(self) -> AlertPreferences:
        self.preference_reads += 1
        return self.preference

    def list_enabled_alert_preferences(self) -> list[AlertPreferences]:
        raise AssertionError("Una revisión manual no enumera preferencias ajenas.")

    def save_alert_preferences(self, preference: AlertPreferences) -> None:
        raise AssertionError("Una revisión manual no modifica preferencias.")

    def upsert_alert_states(self, states: list[object]) -> None:
        self.state_writes += 1
        super().upsert_alert_states(states)

    def add_analysis_snapshot(self, **values: object) -> int:
        self.snapshots.append(values)
        return len(self.snapshots)


def _manual_test_frame(monkeypatch, *, as_of: str = "2026-10-14") -> pd.DataFrame:
    frame = pd.DataFrame({"close": [100.0]}, index=pd.DatetimeIndex([as_of]))
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    monkeypatch.setattr(
        "src.alert_runner.evaluate_latest_signal",
        lambda frame, config, *, ticker, entry_price=None: SignalResult(
            ticker=ticker, as_of=frame.index[-1], score=50, label="Esperar",
            position_label="Mantener", explanation="Análisis de prueba.",
            positive_factors=(), risk_factors=(),
        ),
    )
    return frame


def test_manual_review_reads_only_the_selected_owner_and_uses_the_stored_recipient(monkeypatch) -> None:
    preference = replace(
        normalize_alert_preferences(
            owner="ddriu", email="ddriu@example.com", enabled=True, include_group=False,
        ),
        email="Ddriu@Example.com",
    )
    user = FakeManualJournal(preference, tickers=("MINE",))
    other = FakeManualJournal(
        normalize_alert_preferences(owner="other", email="other@example.com", enabled=True),
        tickers=("OTHER",),
    )
    group = FakeGroupJournal(tickers=("GROUP",))
    group.list_enabled_alert_preferences = lambda: (_ for _ in ()).throw(
        AssertionError("No consultar la lista de usuarios activos.")
    )
    journals = {"ddriu": user, "other": other, GROUP_PORTFOLIO_OWNER: group}
    journal_reads: list[str] = []
    downloaded: list[str] = []
    sent: list[tuple[object, ...]] = []
    frame = _manual_test_frame(monkeypatch)

    def journal_factory(owner: str) -> FakeJournal:
        journal_reads.append(owner)
        return journals[owner]

    def downloader(ticker, *args, **kwargs) -> pd.DataFrame:
        downloaded.append(ticker)
        return frame

    summary = run_daily_alerts(
        owner=" Ddriu ", journal_factory=journal_factory, downloader=downloader,
        fundamental_downloader=lambda ticker: {}, sender=lambda *args: sent.append(args),
        today=date(2026, 10, 15),
    )

    assert summary.users_checked == 1
    assert summary.tickers_checked == 1
    assert summary.tickers_with_fresh_prices == 1
    assert summary.emails_sent == 1
    assert journal_reads == ["ddriu"]
    assert user.preference_reads == 1
    assert other.preference_reads == 0
    assert "MINE" in downloaded
    assert not {"GROUP", "OTHER"} & set(downloaded)
    assert [message[0] for message in sent] == [preference.email]
    assert "MINE" in sent[0][2]
    assert "OTHER" not in sent[0][2]
    assert "GROUP" not in sent[0][2]
    assert user.preference is preference
    assert user.state_writes == 1
    assert other.state_writes == 0


def test_manual_review_includes_group_only_when_the_saved_preference_allows_it(monkeypatch) -> None:
    user = FakeManualJournal(
        normalize_alert_preferences(
            owner="ddriu", email="ddriu@example.com", enabled=True, include_group=True,
        ),
        tickers=("MINE",),
    )
    group = FakeGroupJournal(tickers=("GROUP",))
    group.list_enabled_alert_preferences = lambda: (_ for _ in ()).throw(
        AssertionError("No enumerar preferencias para incluir favoritos compartidos.")
    )
    journal_reads: list[str] = []
    sent: list[tuple[object, ...]] = []
    frame = _manual_test_frame(monkeypatch)

    def journal_factory(owner: str) -> FakeJournal:
        journal_reads.append(owner)
        if owner == "ddriu":
            return user
        assert owner == GROUP_PORTFOLIO_OWNER
        return group

    summary = run_daily_alerts(
        owner="ddriu", journal_factory=journal_factory,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=lambda ticker: {}, sender=lambda *args: sent.append(args),
        today=date(2026, 10, 15),
    )

    assert journal_reads == ["ddriu", GROUP_PORTFOLIO_OWNER]
    assert summary.users_checked == 1
    assert summary.tickers_checked == 2
    assert summary.emails_sent == 1
    assert "MINE" in sent[0][2] and "GROUP" in sent[0][2]
    assert [message[0] for message in sent] == ["ddriu@example.com"]


@pytest.mark.parametrize("owner", ["", "  ", GROUP_PORTFOLIO_OWNER, " GRUPO_COMPARTIDO "])
def test_manual_review_rejects_empty_or_group_owners_before_reading_data(owner) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("Un propietario inválido no permite lecturas ni envíos.")

    with pytest.raises(ValueError, match="usuario privado"):
        run_daily_alerts(
            owner=owner, journal_factory=forbidden, downloader=forbidden,
            fundamental_downloader=forbidden, sender=forbidden,
        )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"owner": "other"}, "no pertenecen"),
        ({"owner": " Ddriu "}, "no pertenecen"),
        ({"enabled": False}, "Activa los avisos"),
        ({"email": ""}, "única dirección"),
        ({"email": "invalid"}, "única dirección"),
        ({"email": "ddriu@example.com,other@example.com"}, "única dirección"),
        ({"email": "Ddriu <ddriu@example.com>"}, "única dirección"),
        ({"email": " ddriu@example.com "}, "única dirección"),
        ({"email": "ddriu@example.com\r\nBcc: other@example.com"}, "única dirección"),
    ],
)
def test_manual_review_rejects_mismatched_disabled_or_invalid_stored_preferences(
    overrides, message,
) -> None:
    preference = replace(
        normalize_alert_preferences(owner="ddriu", email="ddriu@example.com", enabled=True),
        **overrides,
    )
    user = FakeManualJournal(preference, tickers=("MINE",))
    journal_reads: list[str] = []

    def journal_factory(owner: str) -> FakeManualJournal:
        journal_reads.append(owner)
        assert owner == "ddriu"
        return user

    def forbidden(*args, **kwargs):
        raise AssertionError("La validación debe impedir análisis, envíos y cambios.")

    user.list_favorites = forbidden
    with pytest.raises(ValueError, match=message):
        run_daily_alerts(
            owner="ddriu", journal_factory=journal_factory, downloader=forbidden,
            fundamental_downloader=forbidden, sender=forbidden,
        )
    assert journal_reads == ["ddriu"]
    assert user.preference_reads == 1
    assert user.preference is preference
    assert user.state_writes == 0
    assert user.snapshots == []


@pytest.mark.parametrize(
    "unusable", ["stale", "empty", "evaluation_failure", "no_tickers", "nan", "zero", "infinite"],
)
def test_manual_review_aborts_without_delivery_or_state_marking_when_no_symbols_are_usable(
    monkeypatch, unusable,
) -> None:
    user = FakeManualJournal(
        normalize_alert_preferences(
            owner="ddriu", email="ddriu@example.com", enabled=True, include_group=False,
        ),
        tickers=() if unusable == "no_tickers" else ("UNUSABLE",),
    )
    frame = _manual_test_frame(
        monkeypatch, as_of="2026-07-28" if unusable == "stale" else "2026-10-14",
    )
    if unusable == "empty":
        frame = pd.DataFrame()
    if unusable in {"nan", "zero", "infinite"}:
        frame["close"] = {"nan": float("nan"), "zero": 0.0, "infinite": float("inf")}[unusable]
    if unusable == "evaluation_failure":
        def broken_evaluation(*args, **kwargs):
            raise ValueError("No hay suficiente histórico.")

        monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", broken_evaluation)

    def forbidden(*args, **kwargs):
        raise AssertionError("No consultar fundamentales ni enviar sin análisis utilizables.")

    summary = run_daily_alerts(
        owner="ddriu", journal_factory=lambda owner: user,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=forbidden, sender=forbidden, today=date(2026, 10, 15),
    )

    assert summary.users_checked == 1
    assert summary.emails_sent == 0
    assert summary.alerts_sent == 0
    assert user.state_writes == 0
    assert user.saved_states == []
    assert user.snapshots == []
    assert any("no hay análisis frescos utilizables" in error for error in summary.errors)


def test_optional_group_plan_failure_does_not_cancel_private_daily_review() -> None:
    class BrokenGroup(FakeGroupJournal):
        def list_portfolio_snapshot_positions(self):
            raise ValueError("snapshot unavailable")

        def list_enabled_alert_preferences(self):
            return [normalize_alert_preferences(owner="ddriu", email="ddriu@example.com",
                                                enabled=True, include_group=False)]
    group, user = BrokenGroup(), FakeJournal(tickers=("GOOD",))
    sent = []
    result = run_daily_alerts(journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: pd.DataFrame(),
        fundamental_downloader=lambda ticker: {}, sender=lambda *args: sent.append(args), today=date(2026, 7, 29))
    assert result.users_checked == 1
    assert result.tickers_checked == 1
    assert result.emails_sent == 1
    assert result.alerts_sent == 0
    assert any("planes no disponibles" in error for error in result.errors)
    assert len(sent) == 1


def test_primary_candidate_preserves_auxiliary_event_warning_and_its_evidence(monkeypatch) -> None:
    from src.entry_opportunity import STATUS_EVENT
    group, user = FakeGroupJournal(), FakeJournal(tickers=("AAA",))
    frame = pd.DataFrame({"open": [99.0], "high": [101.0], "low": [98.0],
                          "close": [100.0], "atr_14": [2.0]}, index=pd.DatetimeIndex(["2026-07-28"]))
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    signal = SignalResult(ticker="AAA", as_of=pd.Timestamp("2026-07-28"), score=20,
                         label="No entrar", position_label="Mantener", explanation="Técnica auxiliar débil.",
                         positive_factors=(), risk_factors=())
    monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", lambda *args, **kwargs: signal)
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=85, confidence_pct=90, label="Entrada fuerte", growth_score=80, momentum_score=90,
        context_score=80, sector_key="technology", as_of=pd.Timestamp("2026-07-28"),
        positive_factors=("El flujo de caja crece.",), risk_factors=("Valoración exigente.",),
        manual_checks=("Contrastar próximos resultados.",)))
    monkeypatch.setattr("src.alert_runner.evaluate_fundamentals", lambda *args, **kwargs: SimpleNamespace(
        score=75, coverage_pct=80, sector="Technology", country="US"))
    monkeypatch.setattr("src.alert_runner.evaluate_fundamental_filter", lambda *args, **kwargs: SimpleNamespace(
        score=60, coverage_pct=80, label="Calidad mixta"))
    monkeypatch.setattr("src.alert_runner.evaluate_valuation", lambda *args, **kwargs: SimpleNamespace(score=40, coverage_pct=80))
    monkeypatch.setattr("src.alert_runner.evaluate_risk", lambda *args, **kwargs: SimpleNamespace(score=70, coverage_pct=100))
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        timing=SimpleNamespace(score=30), opportunity_score=45, confidence_pct=80,
        status_code=STATUS_EVENT, status_label="Evento próximo; no entrar",
        zones=SimpleNamespace(preferred_entry=SimpleNamespace(label="85–90")),
        event=SimpleNamespace(label="Resultados mañana")))
    sent = []
    result = run_daily_alerts(journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: frame, fundamental_downloader=lambda ticker: {"symbol": ticker},
        sender=lambda *args: sent.append(args), today=date(2026, 7, 29))
    assert result.alerts_sent == 1
    assert result.emails_sent == 2
    plain = sent[0][2]
    assert "Evento próximo; no entrar" in plain
    assert "Resultados mañana" in plain
    assert "El flujo de caja crece." in plain
    assert "Valoración exigente." in plain
    assert "dual_strategy_v1" in plain


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
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=85, confidence_pct=90, label="Entrada fuerte", growth_score=80,
        momentum_score=90, context_score=80, sector_key="technology", as_of=pd.Timestamp("2026-07-28")))
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
            confidence_pct=80,
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
    assert user.saved_states[0].signature == "entry:growth_momentum:Entrada fuerte"
    assert user.saved_states[0].company_name == "Empresa GOOD"
    assert user.saved_states[0].opportunity_score == 80


def test_buy_email_waits_when_the_primary_strategy_requires_a_better_price(monkeypatch) -> None:
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
    monkeypatch.setattr("src.alert_runner.evaluate_growth_momentum", lambda **kwargs: SimpleNamespace(
        score=80, confidence_pct=90, label="Esperar mejor precio", growth_score=80,
        momentum_score=90, context_score=80, sector_key="technology", as_of=pd.Timestamp("2026-07-28")))
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
            confidence_pct=80,
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
    assert user.saved_states[0].signature == "entry:growth_momentum:Esperar mejor precio"


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
            confidence_pct=80,
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


def test_stale_daily_prices_do_not_create_alerts_or_mutate_signal_states(monkeypatch) -> None:
    group = FakeGroupJournal()
    user = FakeJournal(tickers=("OLD",))
    sent: list[tuple[object, ...]] = []
    frame = pd.DataFrame({"close": [100]}, index=pd.DatetimeIndex(["2026-07-28"]))
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", lambda *args, **kwargs: SignalResult(
        ticker="OLD", as_of=frame.index[-1], score=5, label="Esperar",
        position_label="Vender", explanation="Precio antiguo.", positive_factors=(), risk_factors=(),
    ))
    summary = run_daily_alerts(
        journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=lambda ticker: (_ for _ in ()).throw(AssertionError("No consultar datos antiguos")),
        sender=lambda *args: sent.append(args), today=date(2026, 10, 3),
    )
    assert summary.alerts_sent == 0
    assert summary.tickers_with_fresh_prices == 0
    assert user.saved_states == []
    assert len(sent) == 1
    assert "Actualizar datos" in sent[0][2]
    assert "2026-07-28" in sent[0][3]
    assert summary.errors


def test_daily_holding_email_uses_confirmations_instead_of_raw_sell_label(monkeypatch) -> None:
    group = FakeGroupJournal()
    user = FakeJournal(tickers=("MCD",))
    user.open_positions = lambda: pd.DataFrame([{"ticker": "MCD", "average_cost": 100, "quantity": 1}])
    sent: list[tuple[object, ...]] = []
    frame = pd.DataFrame({
        "open": [100], "high": [101], "low": [99], "close": [100], "atr_14": [2],
    }, index=pd.DatetimeIndex(["2026-10-02"]))
    monkeypatch.setattr("src.alert_runner.add_indicators", lambda raw, config: raw)
    monkeypatch.setattr("src.alert_runner.evaluate_latest_signal", lambda *args, **kwargs: SignalResult(
        ticker="MCD", as_of=frame.index[-1], score=5, label="Esperar",
        position_label="Vender", explanation="Debilidad de precio aislada.", positive_factors=(), risk_factors=(),
    ))
    monkeypatch.setattr("src.alert_runner.evaluate_fundamentals", lambda *args: SimpleNamespace(score=90, coverage_pct=90, sector="", country="US"))
    monkeypatch.setattr("src.alert_runner.evaluate_valuation", lambda *args: SimpleNamespace(score=80, coverage_pct=90))
    monkeypatch.setattr("src.alert_runner.evaluate_risk", lambda *args: SimpleNamespace(score=90, coverage_pct=100))
    monkeypatch.setattr("src.alert_runner.evaluate_entry_opportunity", lambda **kwargs: SimpleNamespace(
        opportunity_score=80, confidence_pct=90, status_code=STATUS_WAIT_PRICE,
        status_label="ESPERAR PRECIO", explanation="Análisis conjunto.",
    ))
    summary = run_daily_alerts(
        journal_factory=lambda owner: group if owner == GROUP_PORTFOLIO_OWNER else user,
        downloader=lambda *args, **kwargs: frame,
        fundamental_downloader=lambda ticker: {"symbol": ticker},
        sender=lambda *args: sent.append(args), today=date(2026, 10, 3),
    )
    assert summary.alerts_sent == 0
    assert len(sent) == 1
    assert "Esperar confirmación" in sent[0][2]
    assert "90%" in sent[0][3]
    assert user.saved_states[0].signature == "position:Esperar confirmación"
