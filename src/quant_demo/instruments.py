from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from nautilus_trader.model import AssetClass
from nautilus_trader.model import Commodity
from nautilus_trader.model import Currency
from nautilus_trader.model import CurrencyPair
from nautilus_trader.model import Equity
from nautilus_trader.model import FuturesContract
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import Symbol


def _ns(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)


def _price(cfg: dict[str, Any]) -> Price:
    return Price.from_str(str(cfg["price_increment"]))


def build_instrument(market_id: str, market_cfg: dict[str, Any], cfg: dict[str, Any]) -> Any:
    """Build a Nautilus instrument without connecting to a broker."""
    instrument_id = InstrumentId.from_str(cfg["instrument_id"])
    raw_symbol = Symbol(cfg["raw_symbol"])
    ts_event = 0
    ts_init = 0
    price_increment = _price(cfg)

    if cfg["kind"] == "equity":
        return Equity(
            instrument_id=instrument_id,
            raw_symbol=raw_symbol,
            currency=Currency.from_str(market_cfg["base_currency"]),
            price_precision=int(cfg["price_precision"]),
            price_increment=price_increment,
            lot_size=Quantity.from_int(int(cfg.get("lot_size", 100))),
            ts_event=ts_event,
            ts_init=ts_init,
            info={"market_id": market_id, "paper": True, "source": "AKShare"},
        )

    if cfg["kind"] == "futures":
        return FuturesContract(
            instrument_id=instrument_id,
            raw_symbol=raw_symbol,
            asset_class=AssetClass.COMMODITY,
            underlying=cfg.get("underlying", "RB"),
            activation_ns=_ns("2000-01-01T00:00:00"),
            # RB0 is a continuous historical series, so it has no true expiry.
            # This broad validity window is a backtest proxy, not a roll model.
            expiration_ns=_ns("2099-01-01T00:00:00"),
            currency=Currency.from_str(market_cfg["base_currency"]),
            price_precision=int(cfg["price_precision"]),
            price_increment=price_increment,
            multiplier=Quantity.from_int(int(cfg["multiplier"])),
            lot_size=Quantity.from_int(int(cfg.get("lot_size", 1))),
            margin_init=Decimal(str(cfg["margin_init"])),
            margin_maint=Decimal(str(cfg["margin_maint"])),
            exchange=cfg.get("exchange", "SHFE"),
            ts_event=ts_event,
            ts_init=ts_init,
            info={
                "market_id": market_id,
                "paper": True,
                "source": "AKShare futures_main_sina",
                "continuous_contract": True,
            },
        )

    if cfg["kind"] == "currency_pair":
        return CurrencyPair(
            instrument_id=instrument_id,
            raw_symbol=raw_symbol,
            base_currency=Currency.from_str(cfg["base_currency"]),
            quote_currency=Currency.from_str(cfg["quote_currency"]),
            price_precision=int(cfg["price_precision"]),
            size_precision=int(cfg.get("size_precision", 0)),
            price_increment=price_increment,
            size_increment=Quantity.from_int(int(cfg.get("size_increment", 1))),
            lot_size=Quantity.from_int(int(cfg.get("lot_size", 1))),
            margin_init=Decimal(str(cfg["margin_init"])),
            margin_maint=Decimal(str(cfg["margin_maint"])),
            ts_event=ts_event,
            ts_init=ts_init,
            info={"market_id": market_id, "paper": True, "source": "MT5"},
        )

    if cfg["kind"] == "commodity":
        return Commodity(
            instrument_id=instrument_id,
            raw_symbol=raw_symbol,
            asset_class=AssetClass.COMMODITY,
            quote_currency=Currency.from_str(cfg["quote_currency"]),
            price_precision=int(cfg["price_precision"]),
            size_precision=int(cfg.get("size_precision", 0)),
            price_increment=price_increment,
            size_increment=Quantity.from_int(int(cfg.get("size_increment", 1))),
            lot_size=Quantity.from_int(int(cfg.get("lot_size", 1))),
            margin_init=Decimal(str(cfg["margin_init"])),
            margin_maint=Decimal(str(cfg["margin_maint"])),
            ts_event=ts_event,
            ts_init=ts_init,
            info={"market_id": market_id, "paper": True, "source": "MT5", "spot_cfd": True},
        )

    raise ValueError(f"Unsupported instrument kind: {cfg['kind']}")
