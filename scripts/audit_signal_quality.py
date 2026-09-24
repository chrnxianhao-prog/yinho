"""信号质量与回测真实性体检（只读，不改任何数据和账本）。

用项目自己的评分引擎（quant_demo.indicator_engine）做三件事：
1. 评分分桶 + Spearman IC：看 entry_score - exit_score 对未来收益有没有区分度；
2. 按 run_replay 同样的规则（STRONG_ENTRY -> 多，STRONG_EXIT -> 空/平）重放，比较三种成交假设：
   (a) 信号K线收盘价成交、零成本（项目现状）
   (b) 下一根K线开盘成交、零成本
   (c) 下一根K线开盘成交、含现实成本
3. RB0 主力连续：统计换月日（持仓量单日变化 >= 30%）贡献的收益。

收益按 1 倍名义敞口计算，与下单数量无关。用法：
    .\\.venv\\Scripts\\python.exe scripts\\audit_signal_quality.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_demo.indicator_engine import SignalLevel, analyze_frame, merge_rule  # noqa: E402

# 单边成本假设（比例）。A股：佣金0.03% + 过户费0.001% + 滑点0.05%，卖出另收印花税0.05%。
STOCK_SIDE_COST = 0.0003 + 0.00001 + 0.0005
STOCK_SELL_TAX = 0.0005
# 螺纹钢：交易所手续费约成交额万分之一 + 1跳(1元/吨)滑点，按 3300 元/吨折算。
RB_SIDE_COST = 0.0001 + 1 / 3300
# 外汇/黄金：用 MT5 导出的逐根 spread 列，每换手一单位付半个点差（开平合计一个点差），未含佣金和隔夜利息。

CASES = [
    # 名称, market_id, raw_symbol, 数据文件, 成本类型, 点值, 每年K线数, 前瞻周期
    ("600000 日线", "stock_cn", "600000.SH", "data/ashare/600000_daily.csv", "stock", None, 244, (5, 10)),
    ("RB0 日线", "futures_cn", "RB0", "data/futures/RB0_daily.csv", "rb", None, 244, (5, 10)),
    ("EURUSD M1", "fx_gold", "EURUSD", "data/mt5/EURUSD_M1.csv", "spread", 0.00001, None, (15, 60)),
    ("XAUUSD M1", "fx_gold", "XAUUSD", "data/mt5/XAUUSD_M1.csv", "spread", 0.001, None, (15, 60)),
]


def load_rule(market_id: str, raw_symbol: str) -> tuple[dict, bool]:
    config = yaml.safe_load((ROOT / "configs" / "signal_rules.yaml").read_text(encoding="utf-8"))
    override = config.get("rules", {}).get(market_id, {}).get(raw_symbol, {})
    return merge_rule(config.get("defaults", {}), override), bool(override.get("allow_short", False))


def target_positions(levels: list[SignalLevel], allow_short: bool) -> np.ndarray:
    """复刻 run_replay：只在等级变化且非 WATCH 时动作。target[i] 是第 i 根收盘时决定的仓位。"""
    target = np.zeros(len(levels))
    current, previous = 0.0, None
    for index, level in enumerate(levels):
        if level != previous and level != SignalLevel.WATCH:
            if level == SignalLevel.STRONG_ENTRY:
                current = 1.0
            elif level == SignalLevel.STRONG_EXIT:
                current = -1.0 if allow_short else 0.0
        previous = level
        target[index] = current
    return target


def replay(frame: pd.DataFrame, target: np.ndarray, next_open: bool, cost_kind: str | None, point: float | None):
    o = frame["open"].to_numpy(float)
    c = frame["close"].to_numpy(float)
    spread = frame["spread"].to_numpy(float) * point if cost_kind == "spread" else None
    returns = np.zeros(len(frame))
    turnover_events = 0
    total_cost = 0.0
    for t in range(1, len(frame)):
        old = target[t - 2] if t >= 2 else 0.0
        new = target[t - 1]
        if next_open:
            returns[t] = old * (o[t] / c[t - 1] - 1) + new * (c[t] / o[t] - 1) * (o[t] / c[t - 1])
            fill_ratio = o[t] / c[t - 1]
        else:
            returns[t] = new * (c[t] / c[t - 1] - 1)
            fill_ratio = 1.0
        delta = new - old
        if delta == 0:
            continue
        turnover_events += 1
        if cost_kind == "stock":
            cost = abs(delta) * STOCK_SIDE_COST + max(-delta, 0.0) * STOCK_SELL_TAX
        elif cost_kind == "rb":
            cost = abs(delta) * RB_SIDE_COST
        elif cost_kind == "spread":
            cost = abs(delta) * (spread[t] / 2) / c[t - 1]
        else:
            cost = 0.0
        returns[t] -= cost * fill_ratio
        total_cost += cost
    return returns, turnover_events, total_cost


def summarize(returns: np.ndarray, bars_per_year: int | None) -> str:
    equity = np.cumprod(1 + returns)
    drawdown = equity / np.maximum.accumulate(equity) - 1
    parts = [f"总收益={equity[-1] - 1:+.2%}", f"最大回撤={drawdown.min():+.2%}"]
    if bars_per_year:
        years = len(returns) / bars_per_year
        parts.append(f"年化={equity[-1] ** (1 / years) - 1:+.2%}")
        std = returns.std()
        parts.append(f"夏普={returns.mean() / std * np.sqrt(bars_per_year):.2f}" if std > 0 else "夏普=nan")
    return "  ".join(parts)


def score_buckets(frame: pd.DataFrame, horizons: tuple[int, int]) -> None:
    data = frame.copy()
    data["net"] = data["entry_score"] - data["exit_score"]
    entry_price = data["open"].shift(-1)
    for horizon in horizons:
        data[f"fwd{horizon}"] = data["close"].shift(-horizon) / entry_price - 1
    data = data.dropna(subset=[f"fwd{horizons[-1]}"])
    data = data[(data["entry_score"] + data["exit_score"]) > 0]
    data["bucket"] = pd.cut(
        data["net"], bins=[-101, -80, -40, 0, 40, 80, 101],
        labels=["<=-80", "-80~-40", "-40~0", "0~40", "40~80", ">=80"],
    )
    table = data.groupby("bucket", observed=True).agg(
        样本=("net", "size"),
        **{f"平均fwd{h}(bp)": (f"fwd{h}", lambda s: round(s.mean() * 1e4, 1)) for h in horizons},
        **{f"上涨占比fwd{horizons[0]}": (f"fwd{horizons[0]}", lambda s: f"{(s > 0).mean():.1%}")},
    )
    ic = data[["net", f"fwd{horizons[0]}"]].corr(method="spearman").iloc[0, 1]
    print(f"评分分桶（下一根开盘入场，持有 {horizons} 根）  Spearman IC={ic:+.3f}")
    print(table.to_string())


def main() -> None:
    for name, market_id, raw_symbol, data_file, cost_kind, point, bars_per_year, horizons in CASES:
        path = ROOT / data_file
        if not path.is_file():
            print(f"\n==== {name}: 缺少数据 {path}")
            continue
        rule, allow_short = load_rule(market_id, raw_symbol)
        frame, points = analyze_frame(pd.read_csv(path), rule)
        print(f"\n==== {name}  bars={len(frame)}  {frame['timestamp'].iloc[0]} -> {frame['timestamp'].iloc[-1]} ====")
        if cost_kind == "spread":
            spread_bp = frame["spread"].median() * point / frame["close"].median() * 1e4
            print(f"中位点差≈{spread_bp:.2f} bp")
        score_buckets(frame, horizons)

        target = target_positions([point_.level for point_ in points], allow_short)
        print("重放（1倍名义敞口）：")
        close_returns = None
        for label, next_open, costs in (
            ("(a) 信号K线收盘成交·零成本[现状]", False, None),
            ("(b) 下一根开盘成交·零成本", True, None),
            ("(c) 下一根开盘成交·含成本", True, cost_kind),
        ):
            returns, events, total_cost = replay(frame, target, next_open, costs, point)
            close_returns = returns if close_returns is None else close_returns
            print(f"  {label:<24} 换手={events:<5} 成本合计={total_cost:.2%}  {summarize(returns, bars_per_year)}")
        closes = frame["close"].to_numpy(float)
        buy_hold = np.r_[0.0, closes[1:] / closes[:-1] - 1]
        print(f"  {'买入持有(同期)':<24} {'':<22}{summarize(buy_hold, bars_per_year)}")
        print(f"  持仓时间占比：多={np.mean(target > 0):.0%} 空={np.mean(target < 0):.0%} 空仓={np.mean(target == 0):.0%}")

        if "open_interest" in frame.columns:
            roll_mask = (frame["open_interest"].pct_change().abs() >= 0.30).to_numpy()
            print(
                f"  换月日({roll_mask.sum()}天)贡献收益(简单加总)={close_returns[roll_mask].sum():+.2%}，"
                f"其余{(~roll_mask).sum()}天={close_returns[~roll_mask].sum():+.2%}",
            )


if __name__ == "__main__":
    main()
