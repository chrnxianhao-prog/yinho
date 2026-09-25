from __future__ import annotations

import math

import pandas as pd


def calculate_metrics(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    initial_equity: float,
    contract_size_oz: float | None,
    open_positions: int,
    rejected_signals: int,
    signal_checks: int,
    open_position_frame: pd.DataFrame | None = None,
) -> dict[str, float | int | str | None]:
    monetary = contract_size_oz is not None
    if trades.empty:
        closed = pd.DataFrame(columns=["close_reason", "net_pnl", "lots", "entry_mid", "exit_mid"])
    else:
        closed = trades
    pnl = pd.to_numeric(closed.get("net_pnl", pd.Series(dtype=float)), errors="coerce").dropna()
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    annual_return: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None
    turnover: float | None = None
    if monetary and not equity.empty:
        values = equity["equity"].astype(float)
        initial_point = pd.Series([initial_equity], index=[pd.Timestamp(equity.iloc[0]["date_local"]) - pd.Timedelta(days=1)])
        full_curve = pd.concat([initial_point, pd.Series(values.to_numpy(), index=pd.to_datetime(equity["date_local"]))])
        peak = full_curve.cummax().clip(lower=1e-12)
        max_drawdown = float((full_curve / peak - 1.0).min())
        first = pd.Timestamp(equity.iloc[0]["date_local"])
        last = pd.Timestamp(equity.iloc[-1]["date_local"])
        elapsed_days = max(0, (last - first).days)
        final_value = float(values.iloc[-1])
        if elapsed_days > 0 and final_value > 0:
            annual_return = (final_value / initial_equity) ** (365.25 / elapsed_days) - 1.0
        returns = values.pct_change()
        first_return = values.iloc[0] / initial_equity - 1.0
        daily_returns = pd.concat([pd.Series([first_return]), returns.iloc[1:]], ignore_index=True).dropna()
        if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0:
            sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252.0))
        avg_equity = float(values.mean())
        if avg_equity > 0:
            gross_notional = 0.0
            for trade in closed.itertuples(index=False):
                if pd.notna(trade.entry_mid) and pd.notna(trade.exit_mid):
                    gross_notional += float(trade.lots) * contract_size_oz * (float(trade.entry_mid) + float(trade.exit_mid))
            if open_position_frame is not None and not open_position_frame.empty:
                for position in open_position_frame.itertuples(index=False):
                    gross_notional += float(position.lots) * contract_size_oz * float(position.entry_mid)
            turnover = gross_notional / avg_equity

    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss_abs = float(abs(losses.mean())) if len(losses) else None
    payoff = avg_win / avg_loss_abs if avg_win is not None and avg_loss_abs else None
    stop_count = int((closed["close_reason"] == "STOP_LOSS").sum()) if len(closed) else 0
    forced_count = int((closed["close_reason"] == "FORCED_WEEKEND_CLOSE").sum()) if len(closed) else 0
    return {
        "status": "MONETARY_EXCLUDING_SWAP" if monetary else "SIGNAL_AND_TIMING_ONLY",
        "annualized_return": annual_return,
        "sharpe_daily_252": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": float(len(wins) / len(pnl)) if len(pnl) else None,
        "payoff_ratio_avg_win_over_avg_loss": payoff,
        "stop_loss_count": stop_count,
        "forced_weekend_close_count": forced_count,
        "closed_positions": int(len(closed)),
        "open_positions_at_end": int(open_positions),
        "turnover_notional_over_average_equity": turnover,
        "rejected_signals": int(rejected_signals),
        "signal_checks": int(signal_checks),
        "risk_free_rate_assumption": "0; daily observations annualized with sqrt(252)",
        "swap_included": False,
        "performance_complete": False,
        "performance_warning": "Historical swap is not included; monetary results are pre-financing.",
    }
