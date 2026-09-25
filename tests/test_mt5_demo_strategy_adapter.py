from __future__ import annotations

import unittest
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_demo.adapters.mt5_demo_adapter import DemoOrderResult, Mt5DemoAdapter


class FakeMt5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    POSITION_TYPE_BUY = 0

    def __init__(self, *, demo: bool = True, hedge: bool = True) -> None:
        self.account = SimpleNamespace(
            trade_mode=0 if demo else 1, trade_allowed=True, margin_mode=2 if hedge else 0,
        )
        self.info = SimpleNamespace(
            volume_min=0.01, volume_max=100.0, volume_step=0.01,
            filling_mode=2, digits=3, point=0.001, trade_tick_size=0.001,
            trade_stops_level=0, trade_freeze_level=0,
        )
        self.tick = SimpleNamespace(bid=2000.0, ask=2000.2)

    def account_info(self):
        return self.account

    def symbol_select(self, symbol, selected):
        return True

    def symbol_info(self, symbol):
        return self.info

    def symbol_info_tick(self, symbol):
        return self.tick

    def last_error(self):
        return (0, "ok")


def fake_adapter(*, demo: bool = True, hedge: bool = True) -> Mt5DemoAdapter:
    adapter = object.__new__(Mt5DemoAdapter)
    adapter.mt5 = FakeMt5(demo=demo, hedge=hedge)
    adapter.ai_magic = 26091703
    adapter.manual_magic = 26091702
    adapter.magic = 26091701
    adapter.deviation = 20
    adapter._connected = True
    return adapter


class DemoStrategyAdapterTests(unittest.TestCase):
    def test_strategy_entry_attaches_server_side_stop_and_strategy_magic(self) -> None:
        adapter = fake_adapter()
        result = DemoOrderResult(1, 2, 0.05, 2000.2, 10009, "done")
        with patch.object(adapter, "_send", return_value=result) as send:
            actual = adapter.open_strategy_market("XAUUSD", "BUY", 0.05, 1999.1234)
        self.assertEqual(actual, result)
        request = send.call_args.args[0]
        self.assertEqual(request["magic"], adapter.ai_magic)
        self.assertEqual(request["sl"], 1999.123)
        self.assertEqual(request["tp"], 0.0)
        self.assertIn("quant_demo_ai_", request["comment"])

    def test_strategy_entry_refuses_non_demo_or_netting_account(self) -> None:
        for adapter in (fake_adapter(demo=False), fake_adapter(hedge=False)):
            with patch.object(adapter, "_send") as send:
                with self.assertRaises(RuntimeError):
                    adapter.open_strategy_market("XAUUSD", "BUY", 0.05, 1999.0)
            send.assert_not_called()

    def test_stop_update_only_allows_favorable_direction(self) -> None:
        adapter = fake_adapter()
        position = SimpleNamespace(
            magic=adapter.ai_magic, symbol="XAUUSD", volume=0.05,
            type=adapter.mt5.POSITION_TYPE_BUY, ticket=42, sl=1990.0, tp=0.0,
        )
        result = DemoOrderResult(1, 2, 0.05, 0.0, 10009, "done")
        with patch.object(adapter, "_send", return_value=result) as send:
            adapter.update_strategy_stop(position, 1991.0)
            self.assertEqual(send.call_args.args[0]["sl"], 1991.0)
            with self.assertRaises(ValueError):
                adapter.update_strategy_stop(position, 1989.0)
            self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
