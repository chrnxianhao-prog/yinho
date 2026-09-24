"""下载股票池内所有股票的后复权日线（新浪为主、腾讯备用）和沪深300/中证500 指数日线。

- 股票列表 = universe_snapshots.csv 里出现过的全部代码 + 当前沪深300/中证500 成分股。
- 每只股票一个 parquet 文件，已下载的跳过，可断点续跑；--refresh 强制全部重下（每日更新时用）。
- 新浪频繁请求会封 IP，所以单线程、每次间隔 1–1.5 秒，1300 只约 1 小时。

用法：
  ..\\..\\.venv\\Scripts\\python.exe fetch_prices.py [--refresh] [--end 20260918]
  每日收盘后更新（只刷新当前成分股，约 40 分钟）：
  ..\\..\\.venv\\Scripts\\python.exe fetch_prices.py --current-only --refresh --end 今天日期
"""
from __future__ import annotations

import os

os.environ.setdefault("TQDM_DISABLE", "1")  # 腾讯接口按年分段下载，会刷进度条

import argparse
import random
import time

import akshare as ak
import pandas as pd

from common import DATA_DIR, INDEX_DIR, PRICE_DIR, exchange_prefix, load_config

COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
INDEXES = {"hs300": "sh000300", "zz500": "sh000905"}


def universe_codes(current_only: bool = False) -> list[str]:
    codes: set[str] = set()
    snapshot_file = DATA_DIR / "universe_snapshots.csv"
    if snapshot_file.is_file() and not current_only:
        codes |= set(pd.read_csv(snapshot_file, dtype={"code": str})["code"])
    for symbol in ("000300", "000905"):
        try:
            frame = ak.index_stock_cons_csindex(symbol=symbol)
            codes |= set(frame["成分券代码"].astype(str).str.zfill(6))
        except Exception as exc:  # noqa: BLE001 - 取不到当前成分股时只用快照
            print(f"当前成分股 {symbol} 获取失败：{exc}", flush=True)
    return sorted(code for code in codes if code[:1] in "036")


def normalize(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    output = frame.rename(columns={"turnover": "turnover_rate"}).copy()
    output["date"] = pd.to_datetime(output["date"])
    for column in COLUMNS[1:]:
        output[column] = pd.to_numeric(output.get(column), errors="coerce")
    output = output[COLUMNS].dropna(subset=["open", "high", "low", "close"])
    output = output[(output["high"] >= output[["open", "close"]].max(axis=1)) & (output["volume"] >= 0)]
    output = output.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    output["source"] = source
    return output


def fetch_stock(code: str, start: str, end: str) -> pd.DataFrame:
    symbol = exchange_prefix(code) + code
    errors = []
    try:
        frame = ak.stock_zh_a_daily(symbol=symbol, start_date=start, end_date=end, adjust="hfq")
        if frame is not None and len(frame):
            return normalize(frame, "sina")
        errors.append("sina: 空数据")
    except Exception as exc:  # noqa: BLE001 - 退市股新浪会报 JSONDecodeError，换腾讯
        errors.append(f"sina: {type(exc).__name__}")
    try:
        frame = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end, adjust="hfq")
        if frame is not None and len(frame):
            return normalize(frame, "tencent")
        errors.append("tencent: 空数据")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"tencent: {type(exc).__name__}")
    raise RuntimeError("; ".join(errors))


def fetch_indexes(end: str) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    for name, symbol in INDEXES.items():
        frame = ak.stock_zh_index_daily(symbol=symbol)
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[frame["date"] <= pd.Timestamp(end)]
        frame.to_parquet(INDEX_DIR / f"{name}.parquet", index=False)
        print(f"指数 {name}: {len(frame)} 行，至 {frame['date'].max().date()}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="忽略已下载文件，全部重新下载")
    parser.add_argument("--end", default=None, help="截止日期 YYYYMMDD，默认取 config 里的 research.end")
    parser.add_argument("--start", default="20140101", help="起始日期，多留一年给指标预热")
    parser.add_argument("--current-only", action="store_true", help="只处理当前沪深300/中证500 成分股（每日扫描用）")
    parser.add_argument("--reverse", action="store_true", help="倒序下载；可以再开一个进程从名单另一头下载，加快首次建库")
    parser.add_argument("--skip-indexes", action="store_true", help="不重新下载指数（第二个下载进程用）")
    args = parser.parse_args()
    config = load_config()
    end = args.end or config["research"]["end"].replace("-", "")

    if not args.skip_indexes:
        fetch_indexes(end)
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    codes = universe_codes(current_only=args.current_only)
    todo = [code for code in codes if args.refresh or not (PRICE_DIR / f"{code}.parquet").is_file()]
    if args.reverse:
        todo.reverse()
    print(f"股票池 {len(codes)} 只，本次需要下载 {len(todo)} 只", flush=True)
    failed = []
    started = time.time()
    for number, code in enumerate(todo, 1):
        if not args.refresh and (PRICE_DIR / f"{code}.parquet").is_file():
            continue  # 另一个下载进程已经下好了
        try:
            frame = fetch_stock(code, args.start, end)
            frame.to_parquet(PRICE_DIR / f"{code}.parquet", index=False)
            status = f"{len(frame)} 行 {frame['source'].iat[0]}"
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响其它
            failed.append(code)
            status = f"失败 {exc}"
        if number % 25 == 0 or status.startswith("失败"):
            elapsed = time.time() - started
            print(f"[{number}/{len(todo)}] {code} {status}  已用 {elapsed / 60:.1f} 分钟", flush=True)
        time.sleep(1.0 + random.random() * 0.5)
    (DATA_DIR / "failed_codes.txt").write_text("\n".join(failed), encoding="utf-8")
    print(f"完成，失败 {len(failed)} 只：{failed[:20]}", flush=True)


if __name__ == "__main__":
    main()
