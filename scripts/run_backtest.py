from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nautilus_trader.backtest import BacktestDataConfig
from nautilus_trader.backtest import BacktestNode
from nautilus_trader.backtest import BacktestRunConfig
from nautilus_trader.backtest import BacktestVenueConfig
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggerConfig
from nautilus_trader.model import AccountType
from nautilus_trader.model import BookType
from nautilus_trader.model import Currency
from nautilus_trader.model import OmsType
from nautilus_trader.model import Venue
from nautilus_trader.persistence import ParquetDataCatalog

from quant_demo.config import load_settings
from quant_demo.data import make_bar_type
from quant_demo.instruments import build_instrument
from quant_demo.indicator_engine import merge_rule
from quant_demo.strategy import IndicatorSignalConfig, IndicatorSignalStrategy


CONFIG_PATH = ROOT / "configs" / "settings.yaml"


def _account_type(value: str) -> AccountType:
    return AccountType.CASH if value.upper() == "CASH" else AccountType.MARGIN


def _path(value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else ROOT / candidate


def _filter_instrument_report(report, instrument):
    if report.empty or "instrument_id" not in report.columns:
        return report
    return report[report["instrument_id"].astype(str) == str(instrument.id)].copy()


def _compact_report(report, name: str):
    columns = {
        "account": ["account_id", "account_type", "total", "free", "locked", "currency"],
        "orders": ["instrument_id", "side", "type", "quantity", "status", "filled_qty", "avg_px"],
        "fills": ["instrument_id", "order_side", "last_qty", "last_px", "currency", "account_id"],
        "positions": ["instrument_id", "side", "quantity", "avg_px_open", "avg_px_close", "realized_pnl"],
    }.get(name, [])
    selected = [column for column in columns if column in report.columns]
    return report[selected] if selected else report


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_signal_rule(
    *,
    market_id: str,
    raw_symbol: str,
    signal_config: dict[str, Any],
    database_path: Path,
) -> tuple[dict[str, Any], int | None]:
    defaults = signal_config.get("defaults", {})
    override = signal_config.get("rules", {}).get(market_id, {}).get(raw_symbol, {})
    rule = merge_rule(defaults, override)
    version: int | None = None

    # Web-edited, versioned rules are the active paper configuration. Read them
    # from the paper ledger without creating or changing that ledger.
    if database_path.is_file():
        uri = f"{database_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_rules'",
            ).fetchone()
            if table:
                row = connection.execute(
                    """
                    SELECT version, config_json FROM signal_rules
                    WHERE market_id=? AND raw_symbol=? AND active=1
                    ORDER BY version DESC LIMIT 1
                    """,
                    (market_id, raw_symbol),
                ).fetchone()
                if row:
                    version = int(row[0])
                    rule = json.loads(row[1])
    if int(rule["fast_ema"]) >= int(rule["slow_ema"]):
        raise ValueError(f"Invalid EMA periods for {market_id}/{raw_symbol}: fast must be less than slow")
    return rule, version


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real-data NautilusTrader paper backtests.")
    parser.add_argument(
        "--markets",
        nargs="+",
        choices=("stock_cn", "futures_cn", "fx_gold"),
        help="Only run selected markets; default runs every enabled market",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Report directory (relative to the project root unless absolute); defaults to app.output_dir",
    )
    args = parser.parse_args()
    settings = load_settings(CONFIG_PATH)
    app = settings["app"]
    data_cfg = settings["data"]
    signal_config = _read_yaml(ROOT / "configs" / "signal_rules.yaml")
    execution_config = _read_yaml(ROOT / "configs" / "paper_execution.yaml")
    database_path = _path(execution_config["database_path"])
    catalog_path = _path(data_cfg["catalog_path"])
    if not catalog_path.is_dir():
        raise FileNotFoundError(
            f"Parquet catalog not found: {catalog_path}. "
            "Run the data fetchers and then data_ingestion/build_catalog.py first.",
        )

    catalog = ParquetDataCatalog(str(catalog_path))
    venue_configs: list[BacktestVenueConfig] = []
    data_configs: list[BacktestDataConfig] = []
    strategies: list[tuple[str, object, object, dict, dict, dict[str, Any], int | None]] = []
    instrument_count = 0

    for market_id, market_cfg in settings["markets"].items():
        if not market_cfg.get("enabled", False):
            continue
        if args.markets and market_id not in args.markets:
            continue
        currency = Currency.from_str(market_cfg["base_currency"])
        venue_configs.append(
            BacktestVenueConfig(
                name=market_cfg["venue"],
                oms_type=OmsType.NETTING,
                account_type=_account_type(market_cfg["account_type"]),
                book_type=BookType.L1_MBP,
                base_currency=currency,
                starting_balances=[f"{market_cfg['starting_balance']} {currency.code}"],
                bar_execution=True,
            ),
        )

        for instrument_cfg in market_cfg["instruments"]:
            instrument = build_instrument(market_id, market_cfg, instrument_cfg)
            rule, rule_version = _load_signal_rule(
                market_id=market_id,
                raw_symbol=str(instrument_cfg["raw_symbol"]),
                signal_config=signal_config,
                database_path=database_path,
            )
            bar_type = make_bar_type(instrument, market_cfg["timeframe"])
            bars = catalog.query_bars(identifiers=[instrument.id.value])
            if not bars:
                raise RuntimeError(
                    f"No bars for {instrument.id} in {catalog_path}. "
                    "Rebuild the catalog from real CSV data.",
                )
            bars.sort(key=lambda bar: bar.ts_init)
            data_configs.append(
                BacktestDataConfig(
                    data_type="Bar",
                    catalog_path=str(catalog_path),
                    instrument_id=instrument.id,
                    start_time=bars[0].ts_init,
                    end_time=bars[-1].ts_init,
                    bar_types=[str(bar_type)],
                ),
            )
            strategies.append((market_id, instrument, bar_type, market_cfg, instrument_cfg, rule, rule_version))
            instrument_count += 1

    if not venue_configs or not data_configs:
        raise RuntimeError("No enabled market/instrument has been configured")

    run_config = BacktestRunConfig(
        id="REAL_DATA_PAPER",
        venues=venue_configs,
        data=data_configs,
        engine=BacktestEngineConfig(
            logging=LoggerConfig(stdout_level=LogLevel.ERROR),
        ),
        dispose_on_completion=False,
    )
    node = BacktestNode(configs=[run_config])
    run_id = run_config.id
    node.build()

    for market_id, instrument, bar_type, market_cfg, instrument_cfg, rule, _rule_version in strategies:
        node.add_strategy(
            run_id,
            IndicatorSignalStrategy(
                IndicatorSignalConfig(
                    market_id=market_id,
                    instrument_id=instrument.id,
                    bar_type=bar_type,
                    trade_size=Decimal(str(instrument_cfg["trade_size"])),
                    rule=rule,
                    allow_short=bool(instrument_cfg.get("allow_short", False)),
                ),
            ),
        )

    print("=== NautilusTrader real-data paper backtest started ===")
    print(f"mode={app['mode']} data_source={data_cfg['source']} catalog={catalog_path.resolve()}")
    print("live trading is disabled by design")
    print(f"configured instruments={instrument_count}")
    for market_id, instrument, _bar_type, market_cfg, _instrument_cfg, rule, rule_version in strategies:
        print(
            f"[{market_id}] venue={market_cfg['venue']} instrument={instrument.id} "
            f"account={market_cfg['account_id']} timeframe={market_cfg['timeframe']} "
            f"signal_rule={'db:v' + str(rule_version) if rule_version is not None else 'yaml'} "
            f"ema={rule['fast_ema']}/{rule['slow_ema']}",
        )

    output_dir = _path(args.output_dir or app.get("output_dir", "artifacts"))
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        node.run()
        for market_id, instrument, _bar_type, market_cfg, _instrument_cfg, _rule, _rule_version in strategies:
            venue = Venue(market_cfg["venue"])
            reports = {
                "account": node.generate_account_report(run_id, venue=venue),
                "orders": _filter_instrument_report(node.generate_orders_report(run_id), instrument),
                "fills": _filter_instrument_report(node.generate_fills_report(run_id), instrument),
                "positions": _filter_instrument_report(node.generate_positions_report(run_id), instrument),
            }
            print(f"\n=== {market_id} / {instrument.id} reports ===")
            for name, report in reports.items():
                print(f"[{name}] rows={len(report)}")
                if not report.empty:
                    print(_compact_report(report.tail(5), name).to_string(index=False))
                report.to_csv(output_dir / f"{market_id}_{instrument.id.value.replace('/', '_')}_{name}.csv", index=False)
        print(f"\nReports written to: {output_dir.resolve()}")
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
