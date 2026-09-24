from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE lines without printing credentials."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class DemoOrderResult:
    order_ticket: int
    deal_ticket: int
    volume: float
    price: float
    retcode: int
    comment: str


class Mt5DemoAdapter:
    """Fail-closed adapter for explicitly authorised MT5 Demo paper orders."""

    def __init__(self, env_path: str | Path = ".env") -> None:
        load_dotenv(env_path)
        if os.getenv("MT5_DEMO_ENABLED", "false").lower() != "true":
            raise RuntimeError(
                "MT5 demo orders are disabled. Set MT5_DEMO_ENABLED=true in a local .env "
                "and pass --confirm-demo-order explicitly.",
            )
        missing = [key for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER") if not os.getenv(key)]
        if missing:
            raise RuntimeError(f"Missing MT5 Demo environment variables: {', '.join(missing)}")
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError("Install MetaTrader5 with: pip install MetaTrader5") from exc
        self.mt5 = mt5
        self._connected = False
        self.magic = int(os.getenv("MT5_MAGIC", "26091701"))
        self.manual_magic = int(os.getenv("MT5_MANUAL_MAGIC", "26091702"))
        self.ai_magic = int(os.getenv("MT5_AI_MAGIC", "26091703"))
        self.deviation = int(os.getenv("MT5_DEVIATION", "20"))

    def connect_demo(self, *, require_trading: bool = False) -> Any:
        kwargs: dict[str, object] = {
            "login": int(os.environ["MT5_LOGIN"]),
            "password": os.environ["MT5_PASSWORD"],
            "server": os.environ["MT5_SERVER"],
        }
        terminal_path = os.getenv("MT5_PATH") or os.getenv("MT5_TERMINAL_PATH")
        if terminal_path:
            kwargs["path"] = terminal_path
        if not self.mt5.initialize(**kwargs):
            raise RuntimeError(f"mt5.initialize failed: {self.mt5.last_error()}")
        self._connected = True
        account = self.mt5.account_info()
        if account is None:
            self.close()
            raise RuntimeError(f"mt5.account_info failed: {self.mt5.last_error()}")
        demo_mode = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        if int(account.trade_mode) != int(demo_mode):
            self.close()
            raise RuntimeError(
                f"Refusing to trade: account {account.login} is not Demo "
                f"(trade_mode={account.trade_mode}).",
            )
        if require_trading and not bool(account.trade_allowed):
            self.close()
            raise RuntimeError("MT5 account does not allow trading.")
        return account

    def close(self) -> None:
        if self._connected:
            self.mt5.shutdown()
            self._connected = False

    def _filling_mode(self, symbol_info: Any) -> int:
        mode = int(getattr(symbol_info, "filling_mode", 0))
        if mode & int(getattr(self.mt5, "SYMBOL_FILLING_IOC", 2)):
            return int(self.mt5.ORDER_FILLING_IOC)
        if mode & int(getattr(self.mt5, "SYMBOL_FILLING_FOK", 1)):
            return int(self.mt5.ORDER_FILLING_FOK)
        return int(self.mt5.ORDER_FILLING_RETURN)

    def _check_symbol(self, symbol: str, volume: float) -> tuple[Any, Any, float]:
        if not self.mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select failed for {symbol}: {self.mt5.last_error()}")
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise RuntimeError(f"No symbol/tick information for {symbol}: {self.mt5.last_error()}")
        minimum = float(info.volume_min)
        maximum = float(info.volume_max)
        step = float(info.volume_step)
        if volume < minimum or volume > maximum:
            raise ValueError(f"volume={volume} outside MT5 range [{minimum}, {maximum}]")
        normalized = round(round(volume / step) * step, 8)
        if normalized <= 0:
            raise ValueError("normalized volume is zero")
        return info, tick, normalized

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Return a current quote without placing an order."""
        if not self.mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select failed for {symbol}: {self.mt5.last_error()}")
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise RuntimeError(f"No quote for {symbol}: {self.mt5.last_error()}")
        return {
            "symbol": symbol,
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "spread_points": float((tick.ask - tick.bid) / info.point) if info.point else None,
            "digits": int(info.digits),
            "volume_min": float(info.volume_min),
            "volume_step": float(info.volume_step),
            "time": int(tick.time),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        positions = self.mt5.positions_get() or ()
        return [
            {
                "ticket": int(position.ticket),
                "symbol": str(position.symbol),
                "type": "BUY" if int(position.type) == int(self.mt5.POSITION_TYPE_BUY) else "SELL",
                "volume": float(position.volume),
                "price_open": float(position.price_open),
                "price_current": float(position.price_current),
                "profit": float(position.profit),
                "swap": float(getattr(position, "swap", 0.0)),
                "net_floating": float(position.profit) + float(getattr(position, "swap", 0.0)),
                "magic": int(position.magic),
                "time": int(position.time),
            }
            for position in positions
        ]

    def get_deals(self, days: int = 90) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, min(days, 3650)))
        deals = self.mt5.history_deals_get(start, end)
        if deals is None:
            raise RuntimeError(f"history_deals_get failed: {self.mt5.last_error()}")
        rows: list[dict[str, Any]] = []
        for deal in deals:
            profit = float(getattr(deal, "profit", 0.0))
            commission = float(getattr(deal, "commission", 0.0))
            swap = float(getattr(deal, "swap", 0.0))
            fee = float(getattr(deal, "fee", 0.0))
            magic = int(getattr(deal, "magic", 0))
            comment = str(getattr(deal, "comment", ""))
            if magic == self.manual_magic or "MANUAL" in comment.upper():
                source = "人工下单"
            elif magic == self.ai_magic or any(tag in comment.upper() for tag in ("AI", "STRATEGY")):
                source = "AI/策略"
            elif magic == 0:
                source = "MT5手动/外部"
            elif magic == self.magic:
                source = "程序测试"
            else:
                source = "其他EA/外部"
            rows.append({
                "ticket": int(deal.ticket), "order": int(getattr(deal, "order", 0)),
                "position_id": int(getattr(deal, "position_id", 0)),
                "time": int(deal.time), "symbol": str(getattr(deal, "symbol", "")),
                "type": int(getattr(deal, "type", -1)), "entry": int(getattr(deal, "entry", -1)),
                "side": (
                    "BUY" if int(getattr(deal, "type", -1)) == int(self.mt5.DEAL_TYPE_BUY)
                    else "SELL" if int(getattr(deal, "type", -1)) == int(self.mt5.DEAL_TYPE_SELL)
                    else "—"
                ),
                "volume": float(getattr(deal, "volume", 0.0)),
                "price": float(getattr(deal, "price", 0.0)), "profit": profit,
                "commission": commission, "swap": swap, "fee": fee,
                "net_pnl": profit + commission + swap + fee,
                "magic": magic, "source": source, "comment": comment,
            })
        return sorted(rows, key=lambda row: row["time"], reverse=True)

    def get_trade_summaries(self, days: int = 3650) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        # Deals are stored newest-first for the UI; walk chronologically so
        # the summary direction/source comes from the opening fill.
        for deal in reversed(self.get_deals(days)):
            if not deal["symbol"] and not deal["position_id"]:
                continue
            key = str(deal["position_id"] or deal["order"])
            row = grouped.setdefault(key, {
                "trade_key": key, "position_id": deal["position_id"],
                "symbol": deal["symbol"], "side": deal.get("side", "—"),
                "source": deal["source"], "first_time": deal["time"],
                "last_time": deal["time"], "volume": 0.0,
                "buy_volume": 0.0, "sell_volume": 0.0, "profit": 0.0,
                "commission": 0.0, "swap": 0.0, "fee": 0.0, "net_pnl": 0.0,
                "deal_count": 0, "tickets": [],
            })
            row["first_time"] = min(row["first_time"], deal["time"])
            row["last_time"] = max(row["last_time"], deal["time"])
            if deal["side"] == "BUY":
                row["buy_volume"] += deal["volume"]
            elif deal["side"] == "SELL":
                row["sell_volume"] += deal["volume"]
            row["volume"] = max(row["buy_volume"], row["sell_volume"])
            for field in ("profit", "commission", "swap", "fee", "net_pnl"):
                row[field] += deal[field]
            row["deal_count"] += 1
            row["tickets"].append(deal["ticket"])
        return sorted(grouped.values(), key=lambda row: row["last_time"], reverse=True)

    def _send(self, request: dict[str, Any]) -> DemoOrderResult:
        check = self.mt5.order_check(request)
        if check is None:
            raise RuntimeError(f"order_check failed: {self.mt5.last_error()}")
        result = self.mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"order_send failed: {self.mt5.last_error()}")
        ok_codes = {
            int(self.mt5.TRADE_RETCODE_DONE),
            int(self.mt5.TRADE_RETCODE_PLACED),
            int(self.mt5.TRADE_RETCODE_DONE_PARTIAL),
        }
        if int(result.retcode) not in ok_codes:
            raise RuntimeError(
                f"MT5 order rejected: retcode={result.retcode} comment={result.comment} result={result}",
            )
        return DemoOrderResult(
            order_ticket=int(result.order),
            deal_ticket=int(result.deal),
            volume=float(result.volume),
            price=float(result.price),
            retcode=int(result.retcode),
            comment=str(result.comment),
        )

    def open_market(
        self, symbol: str, side: str, volume: float, *,
        origin: str = "MANUAL", strategy_id: str | None = None,
    ) -> DemoOrderResult:
        info, tick, normalized = self._check_symbol(symbol, volume)
        existing = self.mt5.positions_get(symbol=symbol) or ()
        if existing:
            raise RuntimeError(f"Refusing to trade {symbol}: existing positions found.")
        side_upper = side.upper()
        if side_upper not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        order_type = self.mt5.ORDER_TYPE_BUY if side_upper == "BUY" else self.mt5.ORDER_TYPE_SELL
        price = float(tick.ask if side_upper == "BUY" else tick.bid)
        origin_upper = origin.upper()
        magic_by_origin = {"TEST": self.magic, "MANUAL": self.manual_magic, "AI": self.ai_magic}
        if origin_upper not in magic_by_origin:
            raise ValueError("origin must be TEST, MANUAL, or AI")
        comment = {
            "TEST": "quant_demo_smoke_test",
            "MANUAL": "quant_demo_manual_ui",
            "AI": f"quant_demo_ai_{strategy_id or 'strategy'}"[:31],
        }[origin_upper]
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": normalized,
            "type": order_type,
            "price": price,
            "deviation": self.deviation,
            "magic": magic_by_origin[origin_upper],
            "comment": comment,
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(info),
        }
        return self._send(request)

    def wait_for_new_position(self, symbol: str, timeout_seconds: int = 10) -> Any:
        deadline = datetime.now().timestamp() + timeout_seconds
        while datetime.now().timestamp() < deadline:
            positions = self.mt5.positions_get(symbol=symbol) or ()
            matching = [position for position in positions if int(position.magic) == self.magic]
            if matching:
                return matching[-1]
            sleep(0.25)
        raise RuntimeError(f"No new MT5 position appeared for {symbol} after the order.")

    def close_position(self, position: Any, *, allow_external: bool = False) -> DemoOrderResult:
        owned_magics = {self.magic, self.manual_magic, self.ai_magic}
        if int(position.magic) not in owned_magics and not allow_external:
            raise RuntimeError("Refusing to close a position not created by this paper adapter.")
        symbol = str(position.symbol)
        info, tick, normalized = self._check_symbol(symbol, float(position.volume))
        is_buy = int(position.type) == int(self.mt5.POSITION_TYPE_BUY)
        order_type = self.mt5.ORDER_TYPE_SELL if is_buy else self.mt5.ORDER_TYPE_BUY
        price = float(tick.bid if is_buy else tick.ask)
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": normalized,
            "type": order_type,
            "position": int(position.ticket),
            "price": price,
            "deviation": self.deviation,
            "magic": int(position.magic),
            "comment": (
                "quant_demo_manual_close" if int(position.magic) == self.manual_magic
                else "quant_demo_ai_close" if int(position.magic) == self.ai_magic
                else "quant_demo_smoke_close" if int(position.magic) == self.magic
                else "quant_demo_external_close"
            ),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(info),
        }
        return self._send(request)

    def close_position_ticket(self, ticket: int, *, allow_external: bool = False) -> DemoOrderResult:
        positions = self.mt5.positions_get(ticket=int(ticket)) or ()
        if not positions:
            raise RuntimeError(f"Position ticket {ticket} was not found")
        return self.close_position(positions[0], allow_external=allow_external)
