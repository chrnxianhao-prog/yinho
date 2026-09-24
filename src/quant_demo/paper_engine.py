from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from quant_demo.audit_store import AuditStore, utc_now


class RiskRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class InstrumentSpec:
    market_id: str
    account_id: str
    instrument_id: str
    raw_symbol: str
    kind: str
    lot_size: float
    multiplier: float
    margin_rate: float
    allow_short: bool
    trade_size: float


class PaperBroker:
    """Persistent local paper broker with independent market accounts."""

    def __init__(self, store: AuditStore, risk_config: dict[str, Any]) -> None:
        self.store = store
        self.risk_config = risk_config

    def _reject(self, spec: InstrumentSpec, request: dict[str, Any], reason: str) -> None:
        self.store.execute(
            "INSERT INTO risk_decisions VALUES (?, ?, ?, 0, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                spec.market_id,
                spec.instrument_id,
                reason,
                json.dumps(request, ensure_ascii=False),
                utc_now(),
            ),
        )
        self.store.audit("RISK", "ORDER_REJECTED", {"reason": reason, **request}, spec.market_id)
        raise RiskRejected(reason)

    def submit_market_order(
        self,
        *,
        spec: InstrumentSpec,
        side: str,
        quantity: float,
        price: float,
        bar_timestamp: str,
        source: str,
        idempotency_key: str,
        signal_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        side = side.upper()
        request = {
            "side": side,
            "quantity": quantity,
            "price": price,
            "source": source,
            "bar_timestamp": bar_timestamp,
        }
        if side not in {"BUY", "SELL"}:
            self._reject(spec, request, "方向必须是 BUY 或 SELL")
        if not math.isfinite(quantity) or quantity <= 0:
            self._reject(spec, request, "数量必须大于零")
        if not math.isfinite(price) or price <= 0:
            self._reject(spec, request, "成交价格无效")

        risk = self.risk_config[spec.market_id]
        if quantity > float(risk["max_order_quantity"]):
            self._reject(spec, request, "超过单笔最大数量")
        notional = quantity * price * spec.multiplier
        if notional > float(risk["max_order_notional"]):
            self._reject(spec, request, "超过单笔最大名义金额")
        if spec.kind == "equity" and quantity % spec.lot_size != 0:
            self._reject(spec, request, f"A股买卖数量必须是 {spec.lot_size:g} 股整手")

        with self.store._lock, self.store.connection() as connection:  # noqa: SLF001 - one local unit of work
            existing = connection.execute(
                "SELECT * FROM orders WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return dict(existing)

            flags = connection.execute(
                "SELECT * FROM risk_flags WHERE market_id=?", (spec.market_id,),
            ).fetchone()
            if flags is not None and bool(flags["kill_switch"]):
                self._reject(spec, request, f"市场 Kill Switch 已开启：{flags['reason']}")

            account = connection.execute(
                "SELECT * FROM accounts WHERE market_id=?", (spec.market_id,),
            ).fetchone()
            if account is None:
                self._reject(spec, request, "纸面账户不存在")

            position = connection.execute(
                "SELECT * FROM positions WHERE market_id=? AND instrument_id=?",
                (spec.market_id, spec.instrument_id),
            ).fetchone()
            old_qty = float(position["quantity"]) if position else 0.0
            old_avg = float(position["average_price"]) if position else 0.0
            old_realized = float(position["realized_pnl"]) if position else 0.0
            sellable = float(position["sellable_quantity"]) if position else 0.0
            last_buy_date = position["last_buy_date"] if position else None
            trade_date = bar_timestamp[:10]
            if spec.kind == "equity" and last_buy_date and trade_date > last_buy_date:
                sellable = max(old_qty, 0.0)

            signed_delta = quantity if side == "BUY" else -quantity
            new_qty = old_qty + signed_delta
            if not spec.allow_short and new_qty < -1e-9:
                self._reject(spec, request, "该市场禁止裸卖或持有空头")
            if abs(new_qty) > float(risk["max_position_quantity"]):
                self._reject(spec, request, "超过最大持仓数量")
            if flags is not None and bool(flags["close_only"]) and abs(new_qty) >= abs(old_qty):
                self._reject(spec, request, f"市场处于只允许平仓模式：{flags['reason']}")
            if spec.kind == "equity" and side == "SELL" and quantity > sellable + 1e-9:
                self._reject(spec, request, "A股可卖数量不足（纸面账户执行 T+1）")

            realized_delta = 0.0
            if old_qty == 0 or old_qty * signed_delta > 0:
                total_abs = abs(old_qty) + quantity
                new_avg = (abs(old_qty) * old_avg + quantity * price) / total_abs
            else:
                closed = min(abs(old_qty), quantity)
                realized_delta = (price - old_avg) * closed * spec.multiplier * (1 if old_qty > 0 else -1)
                if abs(new_qty) < 1e-9:
                    new_avg = 0.0
                    new_qty = 0.0
                elif old_qty * new_qty > 0:
                    new_avg = old_avg
                else:
                    new_avg = price

            fee = self._fee(spec, side, quantity, price, risk)
            cash = float(account["cash"])
            if spec.kind == "equity":
                cash -= signed_delta * price * spec.multiplier
                cash -= fee
                if cash < -1e-6:
                    self._reject(spec, request, "可用资金不足")
                if side == "SELL":
                    sellable -= quantity
                new_last_buy_date = trade_date if side == "BUY" else last_buy_date
            else:
                cash += realized_delta - fee
                new_last_buy_date = None

            unrealized = (price - new_avg) * new_qty * spec.multiplier if new_qty else 0.0
            now = utc_now()
            order_id = str(uuid.uuid4())
            fill_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO risk_decisions VALUES (?, ?, ?, 1, ?, ?, ?)",
                (
                    str(uuid.uuid4()), spec.market_id, spec.instrument_id, "通过",
                    json.dumps(request, ensure_ascii=False), now,
                ),
            )
            connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?, ?, ?, ?, ?)",
                (
                    order_id, spec.market_id, spec.account_id, spec.instrument_id, spec.raw_symbol,
                    side, quantity, price, fee, source, idempotency_key, signal_id, note, now,
                ),
            )
            connection.execute(
                "INSERT INTO order_events VALUES (?, ?, 'FILLED', ?, ?)",
                (str(uuid.uuid4()), order_id, json.dumps(request, ensure_ascii=False), now),
            )
            connection.execute(
                "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fill_id, order_id, spec.market_id, spec.instrument_id, side, quantity, price, fee, now),
            )
            connection.execute(
                """
                INSERT INTO positions
                (market_id, instrument_id, raw_symbol, quantity, sellable_quantity, average_price,
                 last_price, realized_pnl, unrealized_pnl, last_buy_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id, instrument_id) DO UPDATE SET
                  quantity=excluded.quantity,
                  sellable_quantity=excluded.sellable_quantity,
                  average_price=excluded.average_price,
                  last_price=excluded.last_price,
                  realized_pnl=excluded.realized_pnl,
                  unrealized_pnl=excluded.unrealized_pnl,
                  last_buy_date=excluded.last_buy_date,
                  updated_at=excluded.updated_at
                """,
                (
                    spec.market_id, spec.instrument_id, spec.raw_symbol, new_qty,
                    max(sellable, 0.0), new_avg, price, old_realized + realized_delta,
                    unrealized, new_last_buy_date, now,
                ),
            )

            positions = connection.execute(
                "SELECT * FROM positions WHERE market_id=?", (spec.market_id,),
            ).fetchall()
            if spec.kind == "equity":
                market_value = sum(float(row["quantity"]) * float(row["last_price"]) for row in positions)
                equity = cash + market_value
                available = cash
            else:
                total_unrealized = sum(float(row["unrealized_pnl"]) for row in positions)
                used_margin = sum(
                    abs(float(row["quantity"])) * float(row["last_price"]) * spec.multiplier * spec.margin_rate
                    for row in positions
                )
                equity = cash + total_unrealized
                available = equity - used_margin
                if available < -1e-6:
                    raise RiskRejected("保证金不足")
            connection.execute(
                "UPDATE accounts SET cash=?, equity=?, available=?, updated_at=? WHERE market_id=?",
                (cash, equity, available, now, spec.market_id),
            )

        result = self.store.query("SELECT * FROM orders WHERE order_id=?", (order_id,))[0]
        self.store.audit("ORDER", "PAPER_FILLED", result, spec.market_id)
        return result

    @staticmethod
    def _fee(
        spec: InstrumentSpec,
        side: str,
        quantity: float,
        price: float,
        risk: dict[str, Any],
    ) -> float:
        if spec.kind == "futures":
            return quantity * float(risk.get("fee_per_contract", 0.0))
        notional = quantity * price * spec.multiplier
        commission = notional * float(risk.get("commission_rate", 0.0))
        if spec.kind == "equity":
            commission = max(commission, float(risk.get("minimum_commission", 0.0)))
            if side == "SELL":
                commission += notional * float(risk.get("sell_tax_rate", 0.0))
        return round(commission, 8)

    def close_position(
        self,
        *,
        spec: InstrumentSpec,
        price: float,
        bar_timestamp: str,
        source: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        rows = self.store.query(
            "SELECT * FROM positions WHERE market_id=? AND instrument_id=?",
            (spec.market_id, spec.instrument_id),
        )
        if not rows or abs(float(rows[0]["quantity"])) < 1e-9:
            raise RiskRejected("没有可平持仓")
        quantity = abs(float(rows[0]["quantity"]))
        side = "SELL" if float(rows[0]["quantity"]) > 0 else "BUY"
        return self.submit_market_order(
            spec=spec,
            side=side,
            quantity=quantity,
            price=price,
            bar_timestamp=bar_timestamp,
            source=source,
            idempotency_key=idempotency_key,
            note="平仓",
        )
