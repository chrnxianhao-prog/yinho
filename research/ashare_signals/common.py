"""共享的路径、配置、交易成本和涨跌停规则。研究和每日扫描共用这一份，保证口径一致。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
import yaml

HERE = Path(__file__).resolve().parent

# AKShare 的新浪/腾讯接口调用 requests 时不带超时，服务器不响应就会永远卡住。统一补一个默认超时。
_original_request = requests.Session.request


def _request_with_timeout(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = 30
    return _original_request(self, method, url, **kwargs)


requests.Session.request = _request_with_timeout
DATA_DIR = HERE / "data"
PRICE_DIR = DATA_DIR / "prices"
INDEX_DIR = DATA_DIR / "index"
RESULT_DIR = HERE / "results"
SIGNAL_DIR = HERE / "signals"

STAMP_DUTY_HALVED = pd.Timestamp("2023-08-28")   # 印花税 0.1% -> 0.05%（卖出单边）
TRANSFER_FEE_CUT = pd.Timestamp("2022-04-29")    # 过户费 0.002% -> 0.001%（双边）
CHINEXT_20PCT = pd.Timestamp("2020-08-24")       # 创业板涨跌幅 10% -> 20%


def load_config() -> dict[str, Any]:
    with (HERE / "config.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def plain_code(code: str) -> str:
    """'sh.600000' / 'sh600000' / '600000' -> '600000'。"""
    return str(code).replace(".", "")[-6:]


def exchange_prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("4", "8")):
        return "bj"
    return "sz"


def price_limit(code: str, dates: pd.Series) -> np.ndarray:
    """每行对应的涨跌停幅度。未处理 ST 的 5%：沪深300/中证500 成分股基本不含 ST。"""
    if code.startswith("688"):
        return np.full(len(dates), 0.20)
    if code.startswith(("300", "301")):
        return np.where(dates >= CHINEXT_20PCT, 0.20, 0.10)
    if code.startswith(("4", "8")):
        return np.full(len(dates), 0.30)
    return np.full(len(dates), 0.10)


def round_trip_cost(entry_dates: pd.Series, exit_dates: pd.Series, costs: dict[str, Any]) -> np.ndarray:
    """买入 + 卖出一个来回的成本比例。印花税按卖出日期、过户费按各自成交日期取当时费率。"""
    stamp = np.where(exit_dates >= STAMP_DUTY_HALVED, 0.0005, 0.001)
    transfer_in = np.where(entry_dates >= TRANSFER_FEE_CUT, 0.00001, 0.00002)
    transfer_out = np.where(exit_dates >= TRANSFER_FEE_CUT, 0.00001, 0.00002)
    per_side = float(costs["commission_rate"]) + float(costs["slippage_per_side"])
    return 2 * per_side + stamp + transfer_in + transfer_out


def load_prices(codes: list[str] | None = None) -> pd.DataFrame:
    """读取已下载的后复权日线，合并成长表（code, date, open, high, low, close, volume, amount, turnover_rate）。"""
    frames = []
    paths = sorted(PRICE_DIR.glob("*.parquet"))
    wanted = set(codes) if codes else None
    for path in paths:
        code = path.stem
        if wanted is not None and code not in wanted:
            continue
        frame = pd.read_parquet(path)
        frame["code"] = code
        frames.append(frame)
    if not frames:
        raise FileNotFoundError(f"没有行情数据，请先运行 fetch_prices.py：{PRICE_DIR}")
    return pd.concat(frames, ignore_index=True)


def load_membership(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """按快照把成分股映射到每个交易日：返回 (date, code) 的在池标记。快照生效日 = 快照里的 update_date。"""
    snapshots = pd.read_csv(DATA_DIR / "universe_snapshots.csv", dtype={"code": str})
    snapshots["update_date"] = pd.to_datetime(snapshots["update_date"])
    rows = []
    for _index_name, group in snapshots.groupby("index"):
        effective = sorted(group["update_date"].unique())
        for position, start in enumerate(effective):
            end = effective[position + 1] if position + 1 < len(effective) else pd.Timestamp.max
            members = group.loc[group["update_date"] == start, "code"].unique()
            span = dates[(dates >= start) & (dates < end)]
            if len(span) and len(members):
                rows.append(pd.MultiIndex.from_product([span, members], names=["date", "code"]).to_frame(index=False))
    membership = pd.concat(rows, ignore_index=True).drop_duplicates()
    membership["in_universe"] = True
    return membership
