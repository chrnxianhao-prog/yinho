"""拉取沪深300、中证500 的历史成分股快照，构建时点一致的股票池（减少幸存者偏差）。

指数每年 6 月、12 月定期调整，所以每年 1 月、7 月各取一次快照，外加最新一次。
成分股在两次快照之间被调出（如退市）的情况，最多有半年滞后。
baostock 单次查询 15–30 秒，全部跑完约 20 分钟；已拉过的快照会跳过，可以断点续跑。

用法：
  ..\\..\\.venv\\Scripts\\python.exe fetch_universe.py
"""
from __future__ import annotations

import csv
import threading
import time

import baostock as bs

from common import DATA_DIR, load_config, plain_code

QUERIES = {"hs300": "query_hs300_stocks", "zz500": "query_zz500_stocks"}
OUTPUT = DATA_DIR / "universe_snapshots.csv"
FIELDS = ["query_date", "index", "update_date", "code", "name"]


def snapshot_dates(start: str, end: str) -> list[str]:
    dates = [
        f"{year}-{month:02d}-10"
        for year in range(int(start[:4]), int(end[:4]) + 1)
        for month in (1, 7)
    ]
    return sorted({day for day in dates if start <= day <= end} | {end})


def query_with_timeout(index: str, day: str, timeout: float = 120.0) -> list[list[str]]:
    """baostock 偶尔会卡住不返回，所以放在线程里执行并设超时。"""
    result: dict[str, object] = {}

    def worker() -> None:
        try:
            rs = getattr(bs, QUERIES[index])(date=day)
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            result["rows"] = rows
        except Exception as exc:  # noqa: BLE001 - 把任何失败都交给重试逻辑
            result["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"{index} {day} 查询超时")
    if "error" in result:
        raise RuntimeError(f"{index} {day}: {result['error']}")
    return result["rows"]  # type: ignore[return-value]


def main() -> None:
    config = load_config()
    end = config["research"]["end"]
    dates = snapshot_dates(config["universe"]["snapshot_start"], end)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    done: set[tuple[str, str]] = set()
    if OUTPUT.is_file():
        with OUTPUT.open(encoding="utf-8") as handle:
            done = {(row["index"], row["query_date"]) for row in csv.DictReader(handle)}
    else:
        with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(FIELDS)

    login = bs.login()
    print(f"baostock login: {login.error_msg}", flush=True)
    for day in dates:
        for index in config["universe"]["indices"]:
            if (index, day) in done:
                continue
            for attempt in range(1, 4):
                started = time.time()
                try:
                    rows = query_with_timeout(index, day)
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"  {index} {day} 第{attempt}次失败：{exc}", flush=True)
                    time.sleep(5 * attempt)
                    bs.login()
            else:
                print(f"  放弃 {index} {day}", flush=True)
                continue
            with OUTPUT.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                for update_date, bs_code, name in rows:
                    writer.writerow([day, index, update_date, plain_code(bs_code), name])
            print(f"{index} {day}: {len(rows)} 只（更新日 {rows[0][0] if rows else '-'}，{time.time() - started:.0f}s）", flush=True)
    print(f"完成：{OUTPUT}", flush=True)


if __name__ == "__main__":
    main()
