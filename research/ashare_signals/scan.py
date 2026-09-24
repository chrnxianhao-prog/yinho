"""每日收盘后扫描：用研究筛出的强信号（results/strong_signals.yaml）检查当前沪深300/中证500 成分股，
命中的生成信号单，写入 signals/ 并推送。

每天的顺序：
  1. 更新行情：..\\..\\.venv\\Scripts\\python.exe fetch_prices.py --current-only --refresh --end 今天日期
  2. 扫描推送：..\\..\\.venv\\Scripts\\python.exe scan.py

安全规则：
  - 最新K线必须是最近一个已收盘的交易日；北京时间 15:05 前运行、或数据没更新到最新，直接拒绝出信号；
  - 信号里的价格是不复权的真实价格（单独拉取），止损和股数按 config.yaml 的 scan 段计算；
  - 买入信号只给建议，是否下单由你决定；卖出信号对照 holdings.yaml（可选）标出你持有的股票。
"""
from __future__ import annotations

import argparse
import json
import math
import os

os.environ.setdefault("TQDM_DISABLE", "1")

import akshare as ak
import pandas as pd
import yaml

from common import DATA_DIR, HERE, INDEX_DIR, PRICE_DIR, RESULT_DIR, SIGNAL_DIR, exchange_prefix, load_config, price_limit
from indicators import EVENT_BY_NAME, add_events, add_indicators
from notifier import notify

MARKET_CLOSE_BJ = (15, 5)


def latest_trading_day() -> tuple[pd.Timestamp, bool]:
    index = pd.read_parquet(INDEX_DIR / "hs300.parquet").sort_values("date")
    index["date"] = pd.to_datetime(index["date"])
    bull = index["close"].iat[-1] > index["close"].tail(60).mean()
    return index["date"].iat[-1], bool(bull)


def current_members() -> pd.DataFrame:
    """优先用中证指数公司的最新成分股；取不到时退回最近一次 baostock 快照。"""
    frames = []
    try:
        for symbol, index_name in (("000300", "hs300"), ("000905", "zz500")):
            frame = ak.index_stock_cons_csindex(symbol=symbol)
            frames.append(pd.DataFrame({"code": frame["成分券代码"].astype(str).str.zfill(6),
                                        "name": frame["成分券名称"], "index": index_name}))
        return pd.concat(frames).drop_duplicates("code")
    except Exception:  # noqa: BLE001 - 网络问题时用本地快照
        snapshots = pd.read_csv(DATA_DIR / "universe_snapshots.csv", dtype={"code": str})
        latest = snapshots[snapshots["query_date"] == snapshots["query_date"].max()]
        return latest.drop_duplicates("code")[["code", "name", "index"]]


def raw_close(code: str, day: pd.Timestamp) -> float | None:
    """拉取不复权收盘价（信号单里的真实价格）。取不到时返回 None，信号单改用百分比描述。"""
    try:
        frame = ak.stock_zh_a_daily(symbol=exchange_prefix(code) + code,
                                    start_date=(day - pd.Timedelta(days=10)).strftime("%Y%m%d"),
                                    end_date=day.strftime("%Y%m%d"), adjust="")
        frame["date"] = pd.to_datetime(frame["date"])
        row = frame[frame["date"] == day]
        return float(row["close"].iat[0]) if len(row) else None
    except Exception:  # noqa: BLE001 - 取不到就退回百分比描述
        return None


def load_holdings() -> set[str]:
    path = HERE / "holdings.yaml"
    if not path.is_file():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(code).zfill(6) for code in data.get("codes", [])}


def board_lot(code: str, max_shares: float) -> int:
    """按交易规则取整：科创板单笔至少 200 股、之后 1 股递增；其它板块 100 股整数倍。"""
    if code.startswith("688"):
        return int(math.floor(max_shares)) if max_shares >= 200 else 0
    return int(math.floor(max_shares / 100) * 100)


def build_ticket(hit: dict, scan_cfg: dict, day: pd.Timestamp) -> dict:
    code, last = hit["code"], hit["last"]
    close = raw_close(code, day)
    atr_pct = float(last["atr_pct"])
    limit = float(price_limit(code, pd.Series([day]))[0])
    ticket = {
        "signal_id": f"SIG-{day:%Y%m%d}-{code}-{'-'.join(sorted(hit['events']))}",
        "side": hit["side"],
        "date": f"{day:%Y-%m-%d}",
        "code": code,
        "name": hit["name"],
        "signals": [EVENT_BY_NAME[name].label for name in hit["events"]],
        "horizon_days": hit["horizon"],
        "research": hit["stats"],
        "atr_pct": round(atr_pct, 4),
        "reference_close": close,
    }
    if hit["side"] != "buy":
        ticket["plan"] = "若持有：次日集合竞价或开盘卖出；未持有：回避，不要买入"
        return ticket
    stop_pct = float(scan_cfg["stop_atr"]) * atr_pct
    chase = min(float(scan_cfg["max_chase"]), limit - 0.005)
    ticket["plan"] = (f"次日 9:15–9:25 集合竞价挂限价买入；开盘价高于参考价 {chase:.1%} 以上就放弃；"
                      f"持有 {hit['horizon']} 个交易日后开盘卖出；收盘跌破止损价则次日开盘卖出")
    if close:
        entry_limit = round(close * (1 + chase), 2)
        stop = round(close * (1 - stop_pct), 2)
        risk_per_share = entry_limit - stop
        equity = float(scan_cfg["equity"])
        by_risk = equity * float(scan_cfg["risk_per_trade"]) / risk_per_share if risk_per_share > 0 else 0
        by_cap = equity * float(scan_cfg["max_position_pct"]) / entry_limit
        shares = board_lot(code, min(by_risk, by_cap))
        ticket.update({
            "entry_limit": entry_limit,
            "stop": stop,
            "shares": shares,
            "max_loss": round(shares * risk_per_share, 0),
            "position_value": round(shares * entry_limit, 0),
        })
    else:
        ticket.update({"entry_limit_pct": round(chase, 4), "stop_pct": round(-stop_pct, 4)})
    return ticket


def render(tickets: list[dict], day: pd.Timestamp, bull: bool, stale: int, holdings: set[str]) -> str:
    lines = [f"数据日期 {day:%Y-%m-%d}；沪深300 {'在' if bull else '不在'} 60 日线上方" + (f"；{stale} 只股票数据未更新已跳过" if stale else "")]
    for side, title in (("buy", "【买入信号】"), ("sell", "【卖出/回避信号】")):
        group = [t for t in tickets if t["side"] == side]
        if not group:
            continue
        lines.append(f"\n{title}")
        if side == "sell":
            # 你持有的全部列出；没持有的只列前 10 只，避免刷屏（完整清单在 signals/ 的 json 里）
            held_first = sorted(group, key=lambda t: t["code"] not in holdings)
            shown = [t for t in held_first if t["code"] in holdings] + [t for t in held_first if t["code"] not in holdings][:10]
            if len(shown) < len(group):
                lines.append(f"（共 {len(group)} 只，以下列出你持有的和前 10 只，完整清单见 signals 目录）")
            group = shown
        for t in group:
            held = "（你持有）" if t["code"] in holdings else ""
            stats = t["research"]
            lines.append(f"{t['code']} {t['name']}{held}：{'、'.join(t['signals'])}")
            lines.append(f"  历史：{int(stats['n_events'])} 次，持有 {t['horizon_days']} 日平均超额 {stats['mean_excess']:+.2%}，"
                         f"跑赢市场占比 {stats['win_excess']:.0%}，t={stats['t_full']:.1f}")
            if side == "buy" and t.get("shares") == 0:
                lines.append(f"  参考价 {t['reference_close']:.2f}，止损 {t['stop']:.2f}：按单笔风险预算买不到 1 手，建议放弃"
                             "（或在 config.yaml 调高 equity / risk_per_trade）")
            elif side == "buy" and t.get("shares") is not None:
                lines.append(f"  参考价 {t['reference_close']:.2f}，限价 ≤ {t['entry_limit']:.2f}，止损 {t['stop']:.2f}，"
                             f"建议 {t['shares']} 股（约 {t['position_value']:,.0f} 元，最多亏 {t['max_loss']:,.0f} 元）")
            elif side == "buy":
                lines.append(f"  开盘价高于昨收 {t['entry_limit_pct']:.1%} 以上不追；止损 {t['stop_pct']:.1%}")
            if not (side == "buy" and t.get("shares") == 0):
                lines.append(f"  {t['plan']}")
    if len(lines) == 1:
        lines.append("今天没有股票触发强信号。")
    lines.append("\n以上为研究信号，不构成投资建议；是否下单由你决定。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="跳过收盘时间检查（只用于测试历史数据）")
    parser.add_argument("--quiet-empty", action="store_true", help="没有信号时不推送")
    args = parser.parse_args()
    config = load_config()
    scan_cfg = config["scan"]
    strong = yaml.safe_load((RESULT_DIR / "strong_signals.yaml").read_text(encoding="utf-8"))
    rules = [(item, "buy") for item in strong.get("buy", [])] + [(item, "sell") for item in strong.get("sell", [])]

    day, bull = latest_trading_day()
    now_bj = pd.Timestamp.now(tz="Asia/Shanghai")
    if not args.force and day.date() == now_bj.date() and (now_bj.hour, now_bj.minute) < MARKET_CLOSE_BJ:
        raise SystemExit(f"北京时间 {now_bj:%H:%M} 还没收盘，拒绝使用未完成的K线出信号")

    members = current_members()
    needed = sorted({item["event"] for item, _ in rules})
    hits: dict[tuple[str, str], dict] = {}
    stale = 0
    for code, name in zip(members["code"], members["name"]):
        path = PRICE_DIR / f"{code}.parquet"
        if not path.is_file():
            stale += 1
            continue
        raw = pd.read_parquet(path)
        raw["date"] = pd.to_datetime(raw["date"])
        if raw["date"].max() != day:
            stale += 1
            continue
        data = add_indicators(raw.tail(400), code)
        if needed:
            data = add_events(data, needed)
        last = data.iloc[-1]
        if not last["amount_ma20"] >= float(scan_cfg["min_amount_20d"]):
            continue
        for item, side in rules:
            regime_ok = item["regime"] == "any" or (item["regime"] == "bull") == bull
            if regime_ok and bool(last[f"ev_{item['event']}"]):
                hit = hits.setdefault((code, side), {"code": code, "name": name, "side": side, "events": [],
                                                     "horizon": item["horizon_days"], "stats": item["stats"], "last": last})
                hit["events"].append(item["event"])
                if abs(item["stats"]["t_full"]) > abs(hit["stats"]["t_full"]):
                    hit["stats"] = item["stats"]
    if stale > len(members) * 0.2:
        raise SystemExit(f"{stale}/{len(members)} 只股票的数据没有更新到 {day:%Y-%m-%d}，先运行 fetch_prices.py 更新")

    tickets = [build_ticket(hit, scan_cfg, day) for hit in hits.values()]
    tickets.sort(key=lambda t: (t["side"] != "buy", -abs(t["research"]["t_full"])))
    body = render(tickets, day, bull, stale, load_holdings())
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    (SIGNAL_DIR / f"{day:%Y-%m-%d}.json").write_text(json.dumps(tickets, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (SIGNAL_DIR / f"{day:%Y-%m-%d}.md").write_text(body, encoding="utf-8")
    if tickets or not args.quiet_empty:
        notify(f"A股强信号 {day:%m-%d}（{sum(t['side'] == 'buy' for t in tickets)} 买 / {sum(t['side'] == 'sell' for t in tickets)} 卖）", body)


if __name__ == "__main__":
    main()
