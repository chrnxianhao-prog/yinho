from __future__ import annotations

import math
from typing import Any

import pandas as pd


def _quantiles(values: pd.Series) -> dict[str, float | None]:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return {"p25": None, "median": None, "p75": None, "p95": None}
    return {
        "p25": float(clean.quantile(0.25)),
        "median": float(clean.quantile(0.50)),
        "p75": float(clean.quantile(0.75)),
        "p95": float(clean.quantile(0.95)),
    }


def calculate_metrics(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    initial_equity: float,
    contract_size_oz: float | None,
    open_positions: int,
    rejected_signals: int,
    signal_checks: int,
    open_position_frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    monetary = contract_size_oz is not None
    if trades.empty:
        closed = pd.DataFrame(columns=[
            "close_reason", "net_pnl", "lots", "entry_mid", "exit_mid", "r_multiple",
            "gross_pnl", "spread_cost", "slippage_cost", "commission", "swap", "mae_usd", "mfe_usd",
        ])
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
        date_column = "date_utc" if "date_utc" in equity else "date_local"
        first_day = pd.Timestamp(equity.iloc[0][date_column])
        last_day = pd.Timestamp(equity.iloc[-1][date_column])
        initial_point = pd.Series([initial_equity], index=[first_day - pd.Timedelta(days=1)])
        full_curve = pd.concat([initial_point, pd.Series(values.to_numpy(), index=pd.to_datetime(equity[date_column]))])
        peak = full_curve.cummax().clip(lower=1e-12)
        max_drawdown = float((full_curve / peak - 1.0).min())
        elapsed_days = max(0, (last_day - first_day).days)
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
                if pd.notna(getattr(trade, "entry_mid", None)) and pd.notna(getattr(trade, "exit_mid", None)):
                    gross_notional += float(trade.lots) * contract_size_oz * (float(trade.entry_mid) + float(trade.exit_mid))
            if open_position_frame is not None and not open_position_frame.empty:
                for position in open_position_frame.itertuples(index=False):
                    gross_notional += float(position.lots) * contract_size_oz * float(position.entry_mid)
            turnover = gross_notional / avg_equity

    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss_abs = float(abs(losses.mean())) if len(losses) else None
    payoff = avg_win / avg_loss_abs if avg_win is not None and avg_loss_abs else None
    stop_count = int((closed["close_reason"] == "STOP_LOSS").sum()) if len(closed) else 0
    losing_stop_count = int(((closed["close_reason"] == "STOP_LOSS") & (pd.to_numeric(closed["net_pnl"], errors="coerce") < 0)).sum()) if len(closed) else 0
    session_close_count = int((closed["close_reason"] == "SESSION_CLOSE").sum()) if len(closed) else 0
    margin_stop_out_count = int((closed["close_reason"] == "MARGIN_STOP_OUT").sum()) if len(closed) else 0
    positive_total = float(wins.sum()) if len(wins) else 0.0
    top_five_positive = float(wins.nlargest(5).sum()) if len(wins) else 0.0
    net_total = float(pnl.sum()) if len(pnl) else 0.0
    r_values = pd.to_numeric(closed.get("r_multiple", pd.Series(dtype=float)), errors="coerce").dropna()
    total_swap = float(pd.to_numeric(closed.get("swap", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())

    return {
        "status": "MONETARY_WITH_CONFIGURED_COSTS" if monetary else "SIGNAL_AND_TIMING_ONLY",
        "net_pnl": net_total if monetary else None,
        "annualized_return": annual_return,
        "sharpe_daily_252": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": float(len(wins) / len(pnl)) if len(pnl) else None,
        "payoff_ratio_avg_win_over_avg_loss": payoff,
        "stop_loss_count": stop_count,
        "losing_stop_count": losing_stop_count,
        "session_close_count": session_close_count,
        "margin_stop_out_count": margin_stop_out_count,
        "closed_positions": int(len(closed)),
        "open_positions_at_end": int(open_positions),
        "turnover_notional_over_average_equity": turnover,
        "rejected_signals": int(rejected_signals),
        "signal_checks": int(signal_checks),
        "total_swap": total_swap,
        "mean_r_multiple": float(r_values.mean()) if len(r_values) else None,
        "median_r_multiple": float(r_values.median()) if len(r_values) else None,
        "r_multiple_distribution": _quantiles(r_values),
        "top_5_winners_over_total_net_pnl": top_five_positive / net_total if net_total else None,
        "top_5_winners_over_positive_pnl": top_five_positive / positive_total if positive_total else None,
        "cost_doubled_net_pnl_mechanical": (
            float(pd.to_numeric(closed.get("cost_doubled_net_pnl", pd.Series(dtype=float)), errors="coerce").dropna().sum())
            if monetary and len(closed) else None
        ),
        "mae_usd_distribution": _quantiles(closed.get("mae_usd", pd.Series(dtype=float))),
        "mfe_usd_distribution": _quantiles(closed.get("mfe_usd", pd.Series(dtype=float))),
        "mae_r_distribution": _quantiles(closed.get("mae_r", pd.Series(dtype=float))),
        "mfe_r_distribution": _quantiles(closed.get("mfe_r", pd.Series(dtype=float))),
        "risk_free_rate_assumption": "0; daily observations annualized with sqrt(252)",
        "swap_included": True,
        "performance_complete": True,
        "performance_warning": "Results depend on configured spread, slippage, commission, swap, leverage, and simplified stop-out assumptions.",
    }
