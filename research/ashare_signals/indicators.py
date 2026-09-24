"""指标计算和信号事件定义。

研究（analyze.py）和每日扫描（scan.py）共用这一份代码，保证回测里的信号和推送给你的信号是同一套逻辑。
所有指标都只用当日及以前的数据（rolling / ewm / shift(1)），不含未来函数；analyze.py 里有截断数据的复核。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from common import price_limit


def _sma_cn(series: pd.Series, n: int, m: int = 1) -> pd.Series:
    """通达信 SMA(X, N, M)：权重 M/N 的递推平均，用于 KDJ。"""
    return series.ewm(alpha=m / n, adjust=False).mean()


def add_indicators(frame: pd.DataFrame, code: str) -> pd.DataFrame:
    """输入单只股票按日期升序的后复权日线，返回附加了指标列的新表。"""
    out = frame.sort_values("date").reset_index(drop=True).copy()
    o, h, l, c, v = out["open"], out["high"], out["low"], out["close"], out["volume"]
    prev_close = c.shift(1)

    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = c.rolling(n, min_periods=n).mean()

    out["dif"] = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    out["dea"] = out["dif"].ewm(span=9, adjust=False).mean()
    out["macd"] = 2 * (out["dif"] - out["dea"])

    delta = c.diff()
    for n in (6, 14):
        gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        out[f"rsi{n}"] = rsi.where(loss != 0, 100.0).where(loss.notna())

    low9 = l.rolling(9, min_periods=9).min()
    high9 = h.rolling(9, min_periods=9).max()
    rsv = ((c - low9) / (high9 - low9).replace(0, np.nan) * 100).fillna(50)
    out["kdj_k"] = _sma_cn(rsv, 3)
    out["kdj_d"] = _sma_cn(out["kdj_k"], 3)
    out["kdj_j"] = 3 * out["kdj_k"] - 2 * out["kdj_d"]

    std20 = c.rolling(20, min_periods=20).std(ddof=0)
    out["boll_up"] = out["ma20"] + 2 * std20
    out["boll_dn"] = out["ma20"] - 2 * std20
    out["boll_pctb"] = (c - out["boll_dn"]) / (out["boll_up"] - out["boll_dn"]).replace(0, np.nan)

    true_range = pd.concat([h - l, (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
    out["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    out["atr_pct"] = out["atr14"] / c

    # 量比近似：当日成交量 / 前 5 日平均成交量（不含当日）
    out["vol_ratio"] = v / v.shift(1).rolling(5, min_periods=5).mean().replace(0, np.nan)
    for n in (20, 60, 250):
        out[f"hh{n}"] = h.shift(1).rolling(n, min_periods=n).max()   # 前 n 日最高（不含当日）
        out[f"ll{n}"] = l.shift(1).rolling(n, min_periods=n).min()
    out["dist_hh60"] = c / out["hh60"] - 1

    out["ret1"] = c.pct_change()
    for n in (5, 20, 60):
        out[f"ret{n}"] = c.pct_change(n)
    out["bias20"] = c / out["ma20"] - 1
    out["vol20"] = out["ret1"].rolling(20, min_periods=20).std()
    out["amount_ma20"] = out["amount"].rolling(20, min_periods=20).mean()
    out["log_amount"] = np.log(out["amount_ma20"].where(out["amount_ma20"] > 0))
    out["turnover_rel"] = out["turnover_rate"] / out["turnover_rate"].rolling(60, min_periods=20).mean().replace(0, np.nan)
    out["macd_norm"] = out["macd"] / c

    body_top = pd.concat([o, c], axis=1).max(axis=1)
    body_bottom = pd.concat([o, c], axis=1).min(axis=1)
    bar_range = (h - l).replace(0, np.nan)
    out["upper_shadow"] = (h - body_top) / bar_range
    out["lower_shadow"] = (body_bottom - l) / bar_range
    out["gap"] = o / prev_close - 1

    limit = price_limit(code, out["date"])
    out["limit"] = limit
    # 后复权价格有取整误差，留 0.2 个百分点容差
    out["limit_up"] = c >= prev_close * (1 + limit - 0.002)
    out["limit_down"] = c <= prev_close * (1 - limit + 0.002)
    out["one_word"] = h <= l
    return out


def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def _onset(condition: pd.Series) -> pd.Series:
    """条件从不成立变为成立的那一天。"""
    condition = condition.fillna(False).astype(bool)
    return condition & ~condition.shift(1, fill_value=False)


@dataclass(frozen=True)
class EventDef:
    name: str
    label: str
    hint: str            # 传统解读：buy / sell（真实方向以统计结果为准）
    rule: Callable[[pd.DataFrame], pd.Series]


EVENTS: list[EventDef] = [
    EventDef("ma5_x_ma20_up", "MA5 上穿 MA20（金叉）", "buy", lambda d: _cross_up(d.ma5, d.ma20)),
    EventDef("ma5_x_ma20_dn", "MA5 下穿 MA20（死叉）", "sell", lambda d: _cross_down(d.ma5, d.ma20)),
    EventDef("close_x_ma60_up", "收盘站上 MA60", "buy", lambda d: _cross_up(d.close, d.ma60)),
    EventDef("close_x_ma60_dn", "收盘跌破 MA60", "sell", lambda d: _cross_down(d.close, d.ma60)),
    EventDef("ma_bull_align", "均线多头排列形成（5>10>20>60）", "buy",
             lambda d: _onset((d.ma5 > d.ma10) & (d.ma10 > d.ma20) & (d.ma20 > d.ma60))),
    EventDef("ma_bear_align", "均线空头排列形成（5<10<20<60）", "sell",
             lambda d: _onset((d.ma5 < d.ma10) & (d.ma10 < d.ma20) & (d.ma20 < d.ma60))),
    EventDef("macd_golden", "MACD 金叉", "buy", lambda d: _cross_up(d.dif, d.dea)),
    EventDef("macd_golden_below0", "MACD 零轴下金叉", "buy", lambda d: _cross_up(d.dif, d.dea) & (d.dif < 0)),
    EventDef("macd_golden_above0", "MACD 零轴上金叉", "buy", lambda d: _cross_up(d.dif, d.dea) & (d.dif > 0)),
    EventDef("macd_dead", "MACD 死叉", "sell", lambda d: _cross_down(d.dif, d.dea)),
    EventDef("macd_dead_above0", "MACD 零轴上死叉", "sell", lambda d: _cross_down(d.dif, d.dea) & (d.dif > 0)),
    EventDef("kdj_golden_low", "KDJ 低位金叉（K<25）", "buy", lambda d: _cross_up(d.kdj_k, d.kdj_d) & (d.kdj_k < 25)),
    EventDef("kdj_dead_high", "KDJ 高位死叉（K>75）", "sell", lambda d: _cross_down(d.kdj_k, d.kdj_d) & (d.kdj_k > 75)),
    EventDef("kdj_j_below0", "KDJ 的 J 值跌破 0", "buy", lambda d: _onset(d.kdj_j < 0)),
    EventDef("rsi6_rebound_20", "RSI6 从 20 以下回升", "buy", lambda d: _cross_up(d.rsi6, pd.Series(20.0, index=d.index))),
    EventDef("rsi14_below30", "RSI14 跌破 30（超卖）", "buy", lambda d: _onset(d.rsi14 < 30)),
    EventDef("rsi14_above70", "RSI14 升破 70（超买）", "sell", lambda d: _onset(d.rsi14 > 70)),
    EventDef("boll_break_dn", "收盘跌破布林下轨", "buy", lambda d: _onset(d.close < d.boll_dn)),
    EventDef("boll_break_up", "收盘突破布林上轨", "buy", lambda d: _onset(d.close > d.boll_up)),
    EventDef("break_hh20", "收盘创 20 日新高", "buy", lambda d: _onset(d.close > d.hh20)),
    EventDef("break_hh60_vol", "放量创 60 日新高（量比>1.5）", "buy", lambda d: _onset(d.close > d.hh60) & (d.vol_ratio > 1.5)),
    EventDef("new_high_250", "创 250 日新高", "buy", lambda d: _onset(d.close > d.hh250)),
    EventDef("break_ll20", "收盘创 20 日新低", "sell", lambda d: _onset(d.close < d.ll20)),
    EventDef("break_ll60", "收盘创 60 日新低", "sell", lambda d: _onset(d.close < d.ll60)),
    EventDef("vol_surge_up", "放量大涨（量比>2.5 且涨幅>3%）", "buy", lambda d: (d.vol_ratio > 2.5) & (d.ret1 > 0.03)),
    EventDef("vol_surge_dn", "放量大跌（量比>2.5 且跌幅>3%）", "sell", lambda d: (d.vol_ratio > 2.5) & (d.ret1 < -0.03)),
    EventDef("shrink_pullback", "上升趋势中缩量回调", "buy",
             lambda d: (d.ma20 > d.ma60) & (d.close > d.ma60) & (d.close < d.ma10) & (d.vol_ratio < 0.6)),
    EventDef("drop5_12", "5 日累计跌幅超过 12%", "buy", lambda d: _onset(d.ret5 < -0.12)),
    EventDef("rally5_15", "5 日累计涨幅超过 15%", "sell", lambda d: _onset(d.ret5 > 0.15)),
    EventDef("hammer_low", "20 日新低处长下影线", "buy", lambda d: (d.lower_shadow > 0.6) & (d.low < d.ll20)),
    EventDef("shooting_high", "20 日新高处长上影线", "sell", lambda d: (d.upper_shadow > 0.6) & (d.high > d.hh20)),
    EventDef("limit_up_normal", "涨停（非一字板）", "buy", lambda d: d.limit_up & ~d.one_word),
    EventDef("limit_down", "跌停", "sell", lambda d: d.limit_down),
    EventDef("gap_up_hold", "高开 3% 以上且收阳", "buy", lambda d: (d.gap > 0.03) & (d.close > d.open)),
]

EVENT_BY_NAME = {event.name: event for event in EVENTS}

# 图表和推送里用的短名称
SHORT_LABELS: dict[str, str] = {
    "ma5_x_ma20_up": "MA5 金叉 MA20", "ma5_x_ma20_dn": "MA5 死叉 MA20",
    "close_x_ma60_up": "站上 MA60", "close_x_ma60_dn": "跌破 MA60",
    "ma_bull_align": "均线多头排列", "ma_bear_align": "均线空头排列",
    "macd_golden": "MACD 金叉", "macd_golden_below0": "MACD 零轴下金叉", "macd_golden_above0": "MACD 零轴上金叉",
    "macd_dead": "MACD 死叉", "macd_dead_above0": "MACD 零轴上死叉",
    "kdj_golden_low": "KDJ 低位金叉", "kdj_dead_high": "KDJ 高位死叉", "kdj_j_below0": "KDJ J 值 < 0",
    "rsi6_rebound_20": "RSI6 回升过 20", "rsi14_below30": "RSI14 跌破 30", "rsi14_above70": "RSI14 升破 70",
    "boll_break_dn": "跌破布林下轨", "boll_break_up": "突破布林上轨",
    "break_hh20": "20 日新高", "break_hh60_vol": "放量 60 日新高", "new_high_250": "250 日新高",
    "break_ll20": "20 日新低", "break_ll60": "60 日新低",
    "vol_surge_up": "放量大涨", "vol_surge_dn": "放量大跌", "shrink_pullback": "缩量回调",
    "drop5_12": "5 日跌超 12%", "rally5_15": "5 日涨超 15%",
    "hammer_low": "低位长下影", "shooting_high": "高位长上影",
    "limit_up_normal": "涨停（非一字）", "limit_down": "跌停", "gap_up_hold": "高开 3% 收阳",
}

# 截面因子（连续值），用于 IC 分析：看“数值越大，未来超额收益越高还是越低”
FACTORS: dict[str, str] = {
    "ret5": "5 日涨跌幅（短期反转）",
    "ret20": "20 日涨跌幅",
    "ret60": "60 日涨跌幅（中期动量）",
    "bias20": "偏离 MA20 的幅度",
    "rsi14": "RSI14",
    "kdj_j": "KDJ 的 J 值",
    "boll_pctb": "布林带位置 %b",
    "macd_norm": "MACD 柱 / 股价",
    "dist_hh60": "距 60 日最高点的距离",
    "vol_ratio": "量比（当日量 / 前 5 日均量）",
    "turnover_rel": "换手率 / 60 日平均换手率",
    "turnover_rate": "换手率",
    "vol20": "20 日波动率",
    "atr_pct": "ATR / 股价",
    "log_amount": "20 日平均成交额（对数，规模代理）",
    "upper_shadow": "上影线占比",
    "lower_shadow": "下影线占比",
    "gap": "开盘跳空幅度",
}


def add_events(frame: pd.DataFrame, names: list[str] | None = None) -> pd.DataFrame:
    """在指标表上追加事件布尔列（列名 ev_<name>）。"""
    out = frame
    selected = [EVENT_BY_NAME[name] for name in names] if names else EVENTS
    for event in selected:
        out[f"ev_{event.name}"] = event.rule(out).fillna(False).astype(bool)
    return out
