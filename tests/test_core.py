from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_demo.audit_store import AuditStore
from quant_demo.indicator_engine import SignalLevel, analyze_frame, merge_rule
from quant_demo.paper_engine import InstrumentSpec, PaperBroker, RiskRejected


class IndicatorEngineTests(unittest.TestCase):
    def test_strong_signal_is_explainable(self) -> None:
        rows = []
        for index in range(100):
            close = 10 + index * 0.1
            rows.append(
                {
                    "timestamp": f"2026-01-{1 + index // 24:02d}T{index % 24:02d}:00:00Z",
                    "open": close - 0.05,
                    "high": close + 0.1,
                    "low": close - 0.1,
                    "close": close,
                    "volume": 1000 if index < 99 else 5000,
                },
            )
        frame, points = analyze_frame(pd.DataFrame(rows), merge_rule({}))
        self.assertEqual(len(frame), 100)
        self.assertIn(points[-1].level, {SignalLevel.ENTRY, SignalLevel.STRONG_ENTRY})
        self.assertGreater(points[-1].entry_score, points[-1].exit_score)
        self.assertTrue(points[-1].reasons)


class PaperBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.temp.name) / "test.db")
        self.store.ensure_account("stock_cn", "PAPER", "CNY", 10000)
        self.broker = PaperBroker(
            self.store,
            {
                "stock_cn": {
                    "max_order_quantity": 1000,
                    "max_position_quantity": 2000,
                    "max_order_notional": 100000,
                    "commission_rate": 0.0003,
                    "minimum_commission": 5,
                    "sell_tax_rate": 0.0005,
                },
            },
        )
        self.spec = InstrumentSpec(
            market_id="stock_cn",
            account_id="PAPER",
            instrument_id="600000.SH.SIM",
            raw_symbol="600000.SH",
            kind="equity",
            lot_size=100,
            multiplier=1,
            margin_rate=0,
            allow_short=False,
            trade_size=100,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_idempotency_and_stock_t_plus_one(self) -> None:
        order = self.broker.submit_market_order(
            spec=self.spec,
            side="BUY",
            quantity=100,
            price=10,
            bar_timestamp="2026-01-05T07:00:00Z",
            source="TEST",
            idempotency_key="same-key",
        )
        self.assertEqual(order["realized_pnl"], 0)
        self.assertAlmostEqual(order["net_pnl"], -order["fee"])
        duplicate = self.broker.submit_market_order(
            spec=self.spec,
            side="BUY",
            quantity=100,
            price=10,
            bar_timestamp="2026-01-05T07:00:00Z",
            source="TEST",
            idempotency_key="same-key",
        )
        self.assertEqual(order["order_id"], duplicate["order_id"])
        with self.assertRaises(RiskRejected):
            self.broker.close_position(
                spec=self.spec,
                price=10.1,
                bar_timestamp="2026-01-05T08:00:00Z",
                source="TEST",
                idempotency_key="close-too-soon",
            )
        closed = self.broker.close_position(
            spec=self.spec,
            price=10.1,
            bar_timestamp="2026-01-06T07:00:00Z",
            source="TEST",
            idempotency_key="close-next-day",
        )
        self.assertEqual(closed["status"], "FILLED")
        self.assertAlmostEqual(
            closed["net_pnl"],
            closed["realized_pnl"] - closed["fee"],
        )

    def test_kill_switch_rejects_order(self) -> None:
        self.store.set_risk_flags("stock_cn", True, False, "test")
        with self.assertRaises(RiskRejected):
            self.broker.submit_market_order(
                spec=self.spec,
                side="BUY",
                quantity=100,
                price=10,
                bar_timestamp="2026-01-05T07:00:00Z",
                source="TEST",
                idempotency_key="kill-switch",
            )


if __name__ == "__main__":
    unittest.main()
