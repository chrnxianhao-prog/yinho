"""A股技术指标信号研究：事件研究 + 截面因子 IC 分析，筛出“强信号”。

口径（和手动执行方式一致）：
- 信号日 T 收盘后出信号，T+1 开盘买入，再过 H 个交易日开盘卖出。
  T+1 停牌、或 T+1 开盘就涨停/跌停（买卖不进）的事件不计入。
- 收益扣除佣金、滑点、印花税（按卖出日期取 0.1% 或 0.05%）和过户费。
- 超额收益 = 个股收益 − 同一天股票池等权平均收益，剔除大盘涨跌的影响。
- 只统计当时在沪深300/中证500 里的股票（时点一致），且 20 日平均成交额 ≥ config 里的下限。
- t 值先按入场日期聚类（同一天多只股票同时出信号并不独立），再做 Newey-West 调整（持有期重叠）。

输出到 results/：event_stats.csv、factor_ic.csv、strong_signals.yaml、report.md。
用法：..\\..\\.venv\\Scripts\\python.exe analyze.py
"""
from __future__ import annotations

import math
import time
from datetime import datetime

import numpy as np
import pandas as pd
import yaml

from common import INDEX_DIR, PRICE_DIR, RESULT_DIR, load_config, load_membership, round_trip_cost
from indicators import EVENTS, FACTORS, EVENT_BY_NAME, add_events, add_indicators

REGIMES = {"any": "不限", "bull": "沪深300在60日线上方", "bear": "沪深300在60日线下方"}


def nw_tstat(values: np.ndarray, lags: int) -> float:
    """均值的 Newey-West t 值（Bartlett 核）。"""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 20:
        return float("nan")
    e = x - x.mean()
    long_run = e @ e / n
    for lag in range(1, min(lags, n - 1) + 1):
        long_run += 2 * (1 - lag / (lags + 1)) * (e[lag:] @ e[:-lag]) / n
    if long_run <= 0:
        return float("nan")
    return float(x.mean() / math.sqrt(long_run / n))


def load_calendar(config: dict) -> tuple[pd.DatetimeIndex, pd.Series]:
    index = pd.read_parquet(INDEX_DIR / "hs300.parquet").sort_values("date")
    index["date"] = pd.to_datetime(index["date"])
    index["bull"] = index["close"] > index["close"].rolling(60, min_periods=60).mean()
    calendar = pd.DatetimeIndex(index["date"])
    return calendar, index.set_index("date")["bull"]


def build_panel(config: dict) -> tuple[pd.DataFrame, dict]:
    research = config["research"]
    start, end = pd.Timestamp(research["start"]), pd.Timestamp(research["end"])
    horizons = list(research["horizons"])
    calendar, bull = load_calendar(config)
    window = calendar[(calendar >= start) & (calendar <= end)]
    next_trading_day = pd.Series(calendar[1:].append(pd.DatetimeIndex([pd.NaT])), index=calendar)
    membership = load_membership(window)
    member_dates = membership.groupby("code")["date"].apply(lambda s: set(s))

    event_cols = [f"ev_{event.name}" for event in EVENTS]
    frames = []
    skipped = 0
    for path in sorted(PRICE_DIR.glob("*.parquet")):
        code = path.stem
        dates_in_pool = member_dates.get(code)
        if not dates_in_pool:
            skipped += 1
            continue
        raw = pd.read_parquet(path)
        raw["date"] = pd.to_datetime(raw["date"])
        raw = raw[raw["date"] <= end]
        if len(raw) < 80:
            continue
        data = add_events(add_indicators(raw, code))
        next_open = data["open"].shift(-1)
        next_date = data["date"].shift(-1)
        expected = data["date"].map(next_trading_day)
        entry_ok = next_date.eq(expected)
        entry_gap = next_open / data["close"] - 1
        next_limit = data["limit"].shift(-1)
        blocked = (entry_gap >= next_limit - 0.002) | (entry_gap <= -(next_limit - 0.002))
        keep = pd.DataFrame({
            "date": data["date"],
            "code": code,
            "entry_ok": entry_ok,
            "tradable": entry_ok & ~blocked,
            "amount_ma20": data["amount_ma20"].astype("float32"),
        })
        for horizon in horizons:
            exit_open = data["open"].shift(-(1 + horizon))
            exit_date = data["date"].shift(-(1 + horizon))
            keep[f"fwd{horizon}"] = (exit_open / next_open - 1).astype("float32")
            cost = round_trip_cost(next_date, exit_date, config["costs"])
            keep[f"cost{horizon}"] = pd.Series(cost, index=data.index).astype("float32")
        for factor in FACTORS:
            keep[factor] = data[factor].astype("float32")
        for column in event_cols:
            keep[column] = data[column].to_numpy(dtype=bool)
        mask = (keep["date"] >= start) & keep["date"].isin(dates_in_pool)
        frames.append(keep[mask])
    panel = pd.concat(frames, ignore_index=True)
    panel["code"] = panel["code"].astype("category")
    panel["bull"] = panel["date"].map(bull).fillna(False).astype(bool)
    panel["liquid"] = panel["amount_ma20"] >= float(research["min_amount_20d"])
    for horizon in horizons:
        valid = panel["entry_ok"] & panel[f"fwd{horizon}"].notna()
        bench = panel[f"fwd{horizon}"].where(valid).groupby(panel["date"]).transform("mean")
        panel[f"exc{horizon}"] = (panel[f"fwd{horizon}"] - bench).astype("float32")
    meta = {
        "stocks": int(panel["code"].nunique()),
        "rows": int(len(panel)),
        "dates": int(panel["date"].nunique()),
        "first_date": str(panel["date"].min().date()),
        "last_date": str(panel["date"].max().date()),
        "skipped_not_in_pool": skipped,
    }
    return panel, meta


def lookahead_check(sample_codes: int = 12, dates_per_code: int = 4, seed: int = 7) -> dict:
    """freqtrade 式复核：把数据截断到某一天再算一遍，指标和信号必须与全量计算完全一致。"""
    rng = np.random.default_rng(seed)
    paths = sorted(PRICE_DIR.glob("*.parquet"))
    columns = list(FACTORS) + [f"ev_{event.name}" for event in EVENTS]
    checked = mismatched = 0
    details = []
    for path in rng.choice(paths, size=min(sample_codes, len(paths)), replace=False):
        raw = pd.read_parquet(path)
        raw["date"] = pd.to_datetime(raw["date"])
        if len(raw) < 400:
            continue
        full = add_events(add_indicators(raw, path.stem))
        for position in rng.integers(300, len(raw) - 30, size=dates_per_code):
            truncated = add_events(add_indicators(raw.iloc[: position + 1], path.stem))
            left = full.iloc[position][columns]
            right = truncated.iloc[-1][columns]
            for column in columns:
                a, b = left[column], right[column]
                same = (pd.isna(a) and pd.isna(b)) or (isinstance(a, (bool, np.bool_)) and a == b) or (
                    not isinstance(a, (bool, np.bool_)) and np.isclose(float(a), float(b), rtol=1e-6, atol=1e-9)
                )
                checked += 1
                if not same:
                    mismatched += 1
                    details.append(f"{path.stem} {raw['date'].iat[position].date()} {column}")
    return {"checked": checked, "mismatched": mismatched, "examples": details[:10]}


def event_stats(panel: pd.DataFrame, config: dict) -> pd.DataFrame:
    is_end = pd.Timestamp(config["research"]["in_sample_end"])
    rows = []
    base = panel["tradable"] & panel["liquid"]
    for event in EVENTS:
        flag = panel[f"ev_{event.name}"] & base
        for regime in REGIMES:
            selected = flag if regime == "any" else flag & (panel["bull"] if regime == "bull" else ~panel["bull"])
            for horizon in config["research"]["horizons"]:
                sub = panel.loc[selected & panel[f"exc{horizon}"].notna(), ["date", f"fwd{horizon}", f"cost{horizon}", f"exc{horizon}", "bull"]]
                if len(sub) < 30:
                    continue
                excess = sub[f"exc{horizon}"].astype(float)
                net = sub[f"fwd{horizon}"].astype(float) - sub[f"cost{horizon}"].astype(float)
                by_date = excess.groupby(sub["date"]).mean().sort_index()
                in_sample = by_date.index <= is_end
                yearly = by_date.groupby(by_date.index.year).agg(["mean", "count"])
                yearly = yearly[yearly["count"] >= 10]
                direction = np.sign(by_date.mean())
                rows.append({
                    "event": event.name,
                    "label": event.label,
                    "hint": event.hint,
                    "regime": regime,
                    "horizon": horizon,
                    "n_events": len(sub),
                    "n_dates": len(by_date),
                    "mean_fwd": sub[f"fwd{horizon}"].mean(),
                    "mean_cost": sub[f"cost{horizon}"].mean(),
                    "mean_net": net.mean(),
                    "win_net": (net > 0).mean(),
                    "mean_excess": excess.mean(),
                    "median_excess": excess.median(),
                    "win_excess": (excess > 0).mean(),
                    "net_excess": excess.mean() - sub[f"cost{horizon}"].mean(),
                    "t_full": nw_tstat(by_date.to_numpy(), horizon),
                    "is_mean": by_date[in_sample].mean(),
                    "t_is": nw_tstat(by_date[in_sample].to_numpy(), horizon),
                    "oos_mean": by_date[~in_sample].mean(),
                    "t_oos": nw_tstat(by_date[~in_sample].to_numpy(), horizon),
                    "n_oos_events": int((sub["date"] > is_end).sum()),
                    "year_consistency": float((np.sign(yearly["mean"]) == direction).mean()) if len(yearly) else float("nan"),
                    "n_years": len(yearly),
                })
    return pd.DataFrame(rows)


def event_series(panel: pd.DataFrame, events: list[str], horizon: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """重点信号的逐日平均超额（画累计曲线用）和逐年平均超额。"""
    base = panel["tradable"] & panel["liquid"] & panel[f"exc{horizon}"].notna()
    daily, yearly = [], []
    for name in events:
        sub = panel.loc[base & panel[f"ev_{name}"], ["date", f"exc{horizon}"]]
        by_date = sub.groupby("date")[f"exc{horizon}"].agg(["mean", "count"]).reset_index()
        by_date["event"] = name
        daily.append(by_date)
        by_year = sub.groupby(sub["date"].dt.year)[f"exc{horizon}"].agg(["mean", "count"]).reset_index()
        by_year = by_year.rename(columns={"date": "year"})
        by_year["event"] = name
        yearly.append(by_year)
    return pd.concat(daily, ignore_index=True), pd.concat(yearly, ignore_index=True)


def factor_ic(panel: pd.DataFrame, config: dict) -> pd.DataFrame:
    is_end = pd.Timestamp(config["research"]["in_sample_end"])
    rows = []
    monthly = []
    for horizon in (5, 20):
        target = f"exc{horizon}"
        for factor, label in FACTORS.items():
            sub = panel.loc[panel["liquid"] & panel["entry_ok"] & panel[factor].notna() & panel[target].notna(), ["date", factor, target]]
            grouped = sub.groupby("date")
            x = grouped[factor].rank(pct=True)
            y = grouped[target].rank(pct=True)
            frame = pd.DataFrame({"date": sub["date"], "x": x - x.groupby(sub["date"]).transform("mean"),
                                  "y": y - y.groupby(sub["date"]).transform("mean")})
            frame["xy"], frame["xx"], frame["yy"] = frame["x"] * frame["y"], frame["x"] ** 2, frame["y"] ** 2
            sums = frame.groupby("date")[["xy", "xx", "yy"]].sum()
            counts = frame.groupby("date").size()
            ic = (sums["xy"] / np.sqrt(sums["xx"] * sums["yy"]))[counts >= 50].dropna().sort_index()
            quintile = np.clip(np.ceil(x * 5) - 1, 0, 4)   # x 是当日截面百分位排名
            q_means = sub[target].groupby([sub["date"], quintile]).mean().unstack().mean()
            in_sample = ic.index <= is_end
            by_month = ic.groupby(ic.index.to_period("M")).mean()
            monthly.append(pd.DataFrame({"month": by_month.index.astype(str), "ic": by_month.to_numpy(),
                                         "factor": factor, "horizon": horizon}))
            rows.append({
                "factor": factor, "label": label, "horizon": horizon,
                "mean_ic": ic.mean(), "icir": ic.mean() / ic.std(), "t_nw": nw_tstat(ic.to_numpy(), horizon),
                "pct_positive": (ic > 0).mean(), "is_ic": ic[in_sample].mean(), "oos_ic": ic[~in_sample].mean(),
                **{f"q{int(q) + 1}": q_means.get(q, np.nan) for q in range(5)},
                "q5_minus_q1": q_means.get(4, np.nan) - q_means.get(0, np.nan),
                "n_dates": len(ic),
            })
    pd.concat(monthly, ignore_index=True).to_csv(RESULT_DIR / "factor_ic_monthly.csv", index=False, encoding="utf-8-sig")
    return pd.DataFrame(rows)


def select_strong(stats: pd.DataFrame, config: dict) -> dict:
    rule = config["selection"]
    candidates = stats[(stats["horizon"] == rule["horizon"]) & (stats["n_events"] >= rule["min_events"])
                       & (stats["n_dates"] >= rule["min_dates"])
                       & (stats["year_consistency"] >= rule["min_positive_year_ratio"])]
    buy = candidates[(candidates["t_full"] >= rule["min_t_full"]) & (candidates["t_oos"] >= rule["min_t_oos"])
                     & (candidates["is_mean"] > 0) & (candidates["oos_mean"] > 0)
                     & (candidates["net_excess"] >= rule["min_net_excess"])]
    sell = candidates[(candidates["t_full"] <= -rule["min_t_full"]) & (candidates["t_oos"] <= -rule["min_t_oos"])
                      & (candidates["is_mean"] < 0) & (candidates["oos_mean"] < 0)
                      & (candidates["mean_excess"] <= -rule["min_net_excess"])]

    def pack(frame: pd.DataFrame) -> list[dict]:
        items = []
        for _, row in frame.sort_values("t_full", key=abs, ascending=False).iterrows():
            items.append({
                "event": row["event"], "label": row["label"], "regime": row["regime"],
                "regime_label": REGIMES[row["regime"]], "horizon_days": int(row["horizon"]),
                "stats": {key: round(float(row[key]), 5) for key in (
                    "n_events", "mean_excess", "net_excess", "win_excess", "t_full", "t_oos", "oos_mean", "year_consistency")},
            })
        return items

    return {"buy": pack(buy), "sell": pack(sell)}


def fmt_pct(value: float, digits: int = 2) -> str:
    return "—" if pd.isna(value) else f"{value * 100:+.{digits}f}%"


def fmt_num(value: float, digits: int = 1) -> str:
    return "—" if pd.isna(value) else f"{value:.{digits}f}"


def write_report(meta: dict, look: dict, stats: pd.DataFrame, ic: pd.DataFrame, strong: dict, config: dict) -> None:
    horizon = config["selection"]["horizon"]
    main = stats[(stats["horizon"] == horizon) & (stats["regime"] == "any")].sort_values("t_full", ascending=False)
    lines = [
        "# A股技术指标信号研究报告",
        "",
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M}（数据截至 {meta['last_date']}）",
        "",
        "## 数据和口径",
        "",
        f"- 股票池：当时在沪深300/中证500 里的股票（时点一致），共 {meta['stocks']} 只，{meta['dates']} 个交易日，{meta['rows']:,} 个股票·日样本；"
        f"20 日平均成交额 ≥ {config['research']['min_amount_20d'] / 1e4:,.0f} 万。",
        f"- 期间：{meta['first_date']} 至 {meta['last_date']}；样本内截至 {config['research']['in_sample_end']}，之后为样本外。",
        "- 执行：信号日收盘出信号 → 次日开盘买入 → 持有 H 个交易日后开盘卖出；次日停牌或开盘即涨跌停的不计入。",
        f"- 成本：佣金 {config['costs']['commission_rate'] * 1e4:.1f}‱ ×2、滑点 {config['costs']['slippage_per_side'] * 1e4:.0f}bp ×2、"
        "印花税（2023-08-28 前 0.1%，之后 0.05%）、过户费（2022-04-29 前 0.002%，之后 0.001%）。",
        "- 超额收益 = 个股收益 − 同日股票池等权平均收益。t 值按入场日聚类 + Newey-West 调整。",
        f"- 未来函数复核：随机截断数据重算 {look['checked']} 个指标/信号值，不一致 {look['mismatched']} 个。",
        "",
        "## 结论",
        "",
    ]
    if strong["buy"]:
        lines.append(f"达到“强买入”门槛（持有 {horizon} 日）的信号：")
        for item in strong["buy"]:
            s = item["stats"]
            lines.append(f"- **{item['label']}**（{item['regime_label']}）：事件 {int(s['n_events'])} 次，"
                         f"平均超额 {fmt_pct(s['mean_excess'])}，扣成本后 {fmt_pct(s['net_excess'])}，"
                         f"超额胜率 {s['win_excess']:.0%}，t={s['t_full']:.1f}，样本外 t={s['t_oos']:.1f}")
    else:
        lines.append(f"**没有信号达到“强买入”门槛**（持有 {horizon} 日；门槛见 config.yaml 的 selection）。")
    lines.append("")
    if strong["sell"]:
        lines.append("达到“强卖出/回避”门槛的信号（持有的话应考虑卖出）：")
        for item in strong["sell"]:
            s = item["stats"]
            lines.append(f"- **{item['label']}**（{item['regime_label']}）：事件 {int(s['n_events'])} 次，"
                         f"平均超额 {fmt_pct(s['mean_excess'])}，t={s['t_full']:.1f}，样本外 t={s['t_oos']:.1f}")
    else:
        lines.append("**没有信号达到“强卖出/回避”门槛。**")
    lines += [
        "",
        f"## 全部信号（持有 {horizon} 日，不限市场环境，按 t 值排序）",
        "",
        "| 信号 | 传统解读 | 事件数 | 平均超额 | 买入扣费后超额 | 跑赢市场占比 | 买入扣费后赚钱占比 | t 值 | 样本内超额 | 样本外超额 | 样本外 t | 年份一致 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in main.iterrows():
        lines.append(
            f"| {row['label']} | {'买' if row['hint'] == 'buy' else '卖'} | {int(row['n_events']):,} | {fmt_pct(row['mean_excess'])} | "
            f"{fmt_pct(row['net_excess'])} | {row['win_excess']:.0%} | {row['win_net']:.0%} | {fmt_num(row['t_full'])} | "
            f"{fmt_pct(row['is_mean'])} | {fmt_pct(row['oos_mean'])} | {fmt_num(row['t_oos'])} | {fmt_num(row['year_consistency'] * 100, 0)}% |"
        )
    regime_view = stats[(stats["horizon"] == horizon)].pivot_table(index="event", columns="regime", values="mean_excess")
    lines += [
        "",
        f"## 市场环境的影响（持有 {horizon} 日平均超额）",
        "",
        "| 信号 | 不限 | 沪深300 在 60 日线上方 | 在 60 日线下方 |",
        "|---|---:|---:|---:|",
    ]
    for name in main["event"]:
        if name in regime_view.index:
            row = regime_view.loc[name]
            lines.append(f"| {EVENT_BY_NAME[name].label} | {fmt_pct(row.get('any'))} | {fmt_pct(row.get('bull'))} | {fmt_pct(row.get('bear'))} |")
    top = main.reindex(main["t_full"].abs().sort_values(ascending=False).index)["event"].head(8)
    by_horizon = stats[(stats["regime"] == "any") & stats["event"].isin(top)].pivot_table(index="event", columns="horizon", values="mean_excess")
    lines += [
        "",
        "## 不同持有期的平均超额（|t| 最大的 8 个信号）",
        "",
        "| 信号 | " + " | ".join(f"{h} 日" for h in by_horizon.columns) + " |",
        "|---|" + "---:|" * len(by_horizon.columns),
    ]
    for name in top:
        if name in by_horizon.index:
            lines.append(f"| {EVENT_BY_NAME[name].label} | " + " | ".join(fmt_pct(v) for v in by_horizon.loc[name]) + " |")
    lines += [
        "",
        "## 截面因子 IC（数值越大 → 未来超额越高为正）",
        "",
        "| 因子 | 持有期 | 平均 IC | ICIR | t 值 | IC>0 占比 | 样本内 IC | 样本外 IC | 最低 20% 组 | 最高 20% 组 | 高减低 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in ic.sort_values(["horizon", "t_nw"], key=lambda s: s.abs() if s.name == "t_nw" else s, ascending=[True, False]).iterrows():
        lines.append(
            f"| {row['label']} | {int(row['horizon'])} 日 | {row['mean_ic']:+.3f} | {row['icir']:+.2f} | {fmt_num(row['t_nw'])} | "
            f"{row['pct_positive']:.0%} | {row['is_ic']:+.3f} | {row['oos_ic']:+.3f} | {fmt_pct(row['q1'])} | {fmt_pct(row['q5'])} | {fmt_pct(row['q5_minus_q1'])} |"
        )
    lines += [
        "",
        "## 局限",
        "",
        "- 成分股快照每半年一次，期间被调出的股票最多滞后半年；已退市股票若新浪/腾讯都取不到数据则缺失，仍有少量幸存者偏差。",
        "- 未区分 ST 股的 5% 涨跌幅（成分股基本不含 ST）；涨跌停按后复权价格推算，有 0.2 个百分点容差。",
        "- 假设能按开盘价成交。集合竞价的实际成交价和你的挂单方式有关，滑点假设需要用真实成交校准。",
        "- 测试了几十个信号 × 3 种市场环境 × 5 个持有期，门槛定得高（t≥3、样本外也显著）就是为了降低“碰巧显著”的概率。",
    ]
    (RESULT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    config = load_config()
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    panel, meta = build_panel(config)
    print(f"面板：{meta}，用时 {time.time() - started:.0f}s", flush=True)
    look = lookahead_check()
    print(f"未来函数复核：{look}", flush=True)
    stats = event_stats(panel, config)
    stats.to_csv(RESULT_DIR / "event_stats.csv", index=False, encoding="utf-8-sig")
    print(f"事件统计 {len(stats)} 行，用时 {time.time() - started:.0f}s", flush=True)
    ic = factor_ic(panel, config)
    ic.to_csv(RESULT_DIR / "factor_ic.csv", index=False, encoding="utf-8-sig")
    strong = select_strong(stats, config)
    horizon = int(config["selection"]["horizon"])
    main_rows = stats[(stats["horizon"] == horizon) & (stats["regime"] == "any")]
    focus = main_rows.reindex(main_rows["t_full"].abs().sort_values(ascending=False).index)["event"].head(10).tolist()
    focus += [item["event"] for item in strong["buy"] + strong["sell"] if item["event"] not in focus]
    daily, yearly = event_series(panel, focus, horizon)
    daily.to_csv(RESULT_DIR / "event_daily.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(RESULT_DIR / "event_yearly.csv", index=False, encoding="utf-8-sig")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_end": meta["last_date"],
        "selection": config["selection"],
        **strong,
    }
    (RESULT_DIR / "strong_signals.yaml").write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (RESULT_DIR / "meta.yaml").write_text(yaml.safe_dump({"panel": meta, "lookahead": look}, allow_unicode=True), encoding="utf-8")
    write_report(meta, look, stats, ic, strong, config)
    print(f"强买入 {len(strong['buy'])} 个，强卖出 {len(strong['sell'])} 个；总用时 {time.time() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
