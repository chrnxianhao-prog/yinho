from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_settings(path: str | Path) -> dict[str, Any]:
    """Load and validate the paper-only, catalog-driven configuration."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        settings = yaml.safe_load(handle) or {}

    app = settings.get("app", {})
    if app.get("mode") != "paper":
        raise ValueError("This project is paper/backtest only: app.mode must be 'paper'.")
    if app.get("live_trading_enabled", False):
        raise ValueError("This project never enables live trading.")

    data = settings.get("data", {})
    for key in ("catalog_path", "mt5_data_path", "ashare_data_path", "futures_data_path"):
        if not data.get(key):
            raise ValueError(f"data.{key} is required")
    if data.get("source") != "parquet_catalog":
        raise ValueError("data.source must be 'parquet_catalog'; synthetic data is disabled.")

    markets = settings.get("markets", {})
    if not markets:
        raise ValueError("At least one market must be configured.")

    for market_id, market in markets.items():
        if not market.get("enabled", False):
            continue
        required = ("venue", "account_id", "account_type", "base_currency", "instruments")
        missing = [key for key in required if key not in market]
        if missing:
            raise ValueError(f"{market_id} is missing configuration: {', '.join(missing)}")
        if market.get("account_mode", "paper") != "paper":
            raise ValueError(f"{market_id} must use a paper account.")
        if not market["instruments"]:
            raise ValueError(f"{market_id} must configure at least one instrument.")
        for instrument in market["instruments"]:
            for key in ("instrument_id", "raw_symbol", "kind", "data_file"):
                if not instrument.get(key):
                    raise ValueError(f"{market_id} instrument missing {key}")

    return settings
