"""Ex-post diagnostics, never rules that choose yesterday's winning strategy.

Recovery dates and avoided losses describe what subsequently happened. They
are not information that was available to make an earlier trading decision.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd


def sector_results(lots: pd.DataFrame) -> pd.DataFrame:
    """Keep incomplete sectors visible; never turn a missing valuation into zero."""
    rows = []
    if lots.empty:
        return pd.DataFrame(rows)
    data = lots.copy(deep=True)
    data["sector"] = data["sector"].fillna("Sin sector histórico")
    for sector, group in data.groupby("sector", dropna=False, sort=True):
        closed = group.loc[group.status.eq("closed")]
        opened = group.loc[~group.status.eq("closed")]
        complete = closed.proceeds_usd.notna().all() and opened.value_usd.notna().all()
        contributed = float(group.contribution_usd.sum())
        terminal = float(closed.proceeds_usd.sum() + opened.value_usd.sum()) if complete else None
        wins = closed.realized_pnl_usd.gt(0).sum()
        rows.append(dict(sector=sector, lots=len(group), closed=len(closed), open=len(opened),
                         contributions_usd=contributed, value_usd=terminal,
                         profit_usd=terminal - contributed if terminal is not None else None,
                         roi_pct=(terminal / contributed - 1) * 100 if terminal is not None and contributed else None,
                         win_rate_closed_pct=wins / len(closed) * 100 if len(closed) and closed.realized_pnl_usd.notna().all() else None,
                         data_complete=bool(complete)))
    return pd.DataFrame(rows)


def exit_counterfactuals(report, hold, bars: Mapping[str, pd.DataFrame], *, end: object) -> pd.DataFrame:
    """Compare each actual virtual exit with holding that SAME entry and amount.

    Sale proceeds are kept as zero-yield cash, not reinvested. Missing terminal
    hold prices stay unknown. Breakeven is measured on USD closes, including FX,
    and only if the post-exit session path is present in the supplied schedule.
    """
    rows = []
    if report.trades.empty:
        return pd.DataFrame(rows)
    hold_lots = {row["lot_id"]: row for row in hold.lots.to_dict("records")}
    cutoff = pd.Timestamp(end)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    if len(str(end)) == 10:
        cutoff += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    for trade in report.trades.to_dict("records"):
        terminal = hold_lots.get(trade["lot_id"], {}).get("value_usd")
        proceeds = trade.get("proceeds_usd")
        complete = pd.notna(terminal) and pd.notna(proceeds)
        difference = terminal - proceeds if complete else None
        frame = bars.get(trade["ticker"], pd.DataFrame())
        intraday = trade.get("fill_timing") == "intraday_unknown_before_close"
        close_times = pd.to_datetime(frame.close_at, utc=True) if not frame.empty else pd.Series(dtype="datetime64[ns, UTC]")
        after = frame.loc[((close_times >= trade["exit_at"]) if intraday else (close_times > trade["exit_at"]))
                          & (pd.to_datetime(frame.close_at, utc=True) <= cutoff)] if not frame.empty else frame
        breakeven, sessions, path_complete = None, None, None
        scheduled = frame.attrs.get("session_schedule", [])
        expected = [pd.Timestamp(row["close_at"]) for row in scheduled
                    if (trade["exit_at"] <= pd.Timestamp(row["close_at"]) if intraday else
                        trade["exit_at"] < pd.Timestamp(row["close_at"])) and pd.Timestamp(row["close_at"]) <= cutoff]
        if scheduled:
            path_complete = True
        seen = []
        for number, bar in enumerate(after.itertuples(), 1):
            seen.append(pd.Timestamp(bar.close_at))
            if expected and seen != expected[:len(seen)]:
                path_complete = False
                break
            if pd.isna(bar.close) or pd.isna(bar.fx_close):
                path_complete = False
                break
            wealth = trade["quantity"] * bar.close * bar.fx_close
            if wealth >= trade["contribution_usd"]:
                breakeven, sessions = bar.close_at, number
                break
        if breakeven is None and expected and len(after) < len(expected):
            path_complete = False
        rows.append(dict(lot_id=trade["lot_id"], ticker=trade["ticker"], exit_at=trade["exit_at"],
                         exit_reason=trade["exit_reason"], proceeds_usd=proceeds,
                         same_lot_hold_value_usd=terminal,
                         missed_terminal_gain_usd=max(difference, 0) if difference is not None else None,
                         avoided_terminal_loss_usd=max(-difference, 0) if difference is not None else None,
                         first_post_exit_breakeven_at=breakeven, sessions_to_breakeven=sessions,
                         recovery_timing="same-session close; intraday stop time unknown" if intraday and breakeven == trade["exit_at"] else "subsequent session close",
                         recovery_path_complete=path_complete,
                         comparison_complete=bool(complete),
                         interpretation="Ex post; not a prediction or an entry/exit rule"))
    return pd.DataFrame(rows)


def period_slices(daily: pd.DataFrame, boundaries: tuple[object, object]) -> pd.DataFrame:
    """Before/during/after a USER-defined correction, keeping the full path primary.

    Period results are changes in investor wealth minus external contributions;
    withdrawals remain segregated cash. No negative return day is deleted.
    """
    if daily.empty:
        return pd.DataFrame()
    start, end = (pd.Timestamp(value).date().isoformat() for value in boundaries)
    if start > end:
        raise ValueError("El inicio de la corrección no puede ser posterior al final")
    frame = (daily.reset_index() if "date" not in daily.columns else daily).sort_values("date").reset_index(drop=True)
    prior = frame.value_usd.shift(1).fillna(0)
    frame["daily_profit_usd"] = frame.value_usd - prior - frame.contribution_usd
    frame.loc[frame.value_usd.isna() | frame.value_usd.shift(1).isna() & frame.index.to_series().ne(frame.index[0]), "daily_profit_usd"] = float("nan")
    rows = []
    masks = {"Antes": frame.date.lt(start), "Durante": frame.date.between(start, end), "Después": frame.date.gt(end)}
    for name, mask in masks.items():
        period = frame.loc[mask]
        if period.empty:
            continue
        complete = bool(period.daily_profit_usd.notna().all())
        rows.append(dict(period=name, start=period.date.iloc[0], end=period.date.iloc[-1],
                         contributions_usd=float(period.contribution_usd.sum()),
                         profit_usd=float(period.daily_profit_usd.sum()) if complete else None,
                         data_complete=complete,
                         definition="User-defined descriptive windows; entire history retained"))
    return pd.DataFrame(rows)
