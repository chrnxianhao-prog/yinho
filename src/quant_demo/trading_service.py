from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from quant_demo.adapters.cn_broker_readiness import ctp_readiness, qmt_readiness
from quant_demo.audit_store import AuditStore
from quant_demo.config import load_settings
from quant_demo.indicator_engine import SignalLevel, analyze_frame, merge_rule, signal_transitions
from quant_demo.indicator_plugins import IndicatorRegistry, load_external_plugins
from quant_demo.paper_engine import InstrumentSpec, PaperBroker, RiskRejected


class TradingService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.settings = load_settings(root / "configs" / "settings.yaml")
        self.signal_config = self._yaml(root / "configs" / "signal_rules.yaml")
        self.execution_config = self._yaml(root / "configs" / "paper_execution.yaml")
        database_path = self._path(self.execution_config["database_path"])
        self.store = AuditStore(database_path)
        self.registry = IndicatorRegistry()
        self.loaded_plugin_modules = load_external_plugins(self.registry)
        self.paper = PaperBroker(self.store, self.execution_config["risk"])
        self.specs: dict[tuple[str, str], InstrumentSpec] = {}
        self._initialize_markets()

    @staticmethod
    def _yaml(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def _path(self, value: str | Path) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else self.root / candidate

    def _initialize_markets(self) -> None:
        defaults = dict(self.signal_config.get("defaults", {}))
        overrides = self.signal_config.get("rules", {})
        for market_id, market in self.settings["markets"].items():
            if not market.get("enabled", False):
                continue
            self.store.ensure_account(
                market_id,
                str(market["account_id"]),
                str(market["base_currency"]),
                float(market["starting_balance"]),
            )
            for instrument in market["instruments"]:
                raw_symbol = str(instrument["raw_symbol"])
                rule = merge_rule(defaults, overrides.get(market_id, {}).get(raw_symbol, {}))
                if self.store.get_rule(market_id, raw_symbol) is None:
                    self.store.set_rule(market_id, raw_symbol, rule)
                spec = InstrumentSpec(
                    market_id=market_id,
                    account_id=str(market["account_id"]),
                    instrument_id=str(instrument["instrument_id"]),
                    raw_symbol=raw_symbol,
                    kind=str(instrument["kind"]),
                    lot_size=float(instrument.get("lot_size", 1)),
                    multiplier=float(instrument.get("multiplier", 1)),
                    margin_rate=float(instrument.get("margin_init", 0)),
                    allow_short=bool(instrument.get("allow_short", False)),
                    trade_size=float(instrument.get("trade_size", 1)),
                )
                self.specs[(market_id, str(instrument["instrument_id"]))] = spec

    def market_catalog(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for market_id, market in self.settings["markets"].items():
            if not market.get("enabled", False):
                continue
            instruments = []
            for instrument in market["instruments"]:
                path = self._data_path(market_id, instrument)
                instruments.append(
                    {
                        "instrument_id": instrument["instrument_id"],
                        "raw_symbol": instrument["raw_symbol"],
                        "kind": instrument["kind"],
                        "price_precision": instrument.get("price_precision", 2),
                        "trade_size": instrument.get("trade_size", 1),
                        "allow_short": bool(instrument.get("allow_short", False)),
                        "data_file": str(path),
                        "data_available": path.is_file() and path.stat().st_size > 10,
                    },
                )
            result.append(
                {
                    "market_id": market_id,
                    "label": market.get("label", market_id),
                    "account_id": market["account_id"],
                    "currency": market["base_currency"],
                    "timeframe": market["timeframe"],
                    "instruments": instruments,
                },
            )
        return result

    def _instrument_config(self, market_id: str, instrument_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        market = self.settings["markets"].get(market_id)
        if market is None or not market.get("enabled", False):
            raise KeyError(f"Unknown market: {market_id}")
        for instrument in market["instruments"]:
            if str(instrument["instrument_id"]) == instrument_id:
                return market, instrument
        raise KeyError(f"Unknown instrument: {instrument_id}")

    def _data_path(self, market_id: str, instrument: dict[str, Any]) -> Path:
        key = {
            "stock_cn": "ashare_data_path",
            "futures_cn": "futures_data_path",
            "fx_gold": "mt5_data_path",
        }[market_id]
        return self._path(self.settings["data"][key]) / str(instrument["data_file"])

    def _load_frame(self, market_id: str, instrument_id: str) -> pd.DataFrame:
        _market, instrument = self._instrument_config(market_id, instrument_id)
        path = self._data_path(market_id, instrument)
        if not path.is_file():
            raise FileNotFoundError(f"历史数据不存在：{path}")
        frame = pd.read_csv(path)
        if frame.empty:
            raise ValueError(f"历史数据为空：{path}")
        return frame

    def get_rule(self, market_id: str, instrument_id: str) -> dict[str, Any]:
        spec = self.specs[(market_id, instrument_id)]
        stored = self.store.get_rule(market_id, spec.raw_symbol)
        if stored is None:
            raise KeyError(f"No rule for {market_id}/{spec.raw_symbol}")
        version, config = stored
        return {"version": version, "config": config}

    def update_rule(self, market_id: str, instrument_id: str, config: dict[str, Any]) -> dict[str, Any]:
        spec = self.specs[(market_id, instrument_id)]
        current = self.get_rule(market_id, instrument_id)["config"]
        merged = merge_rule(current, config)
        if int(merged["fast_ema"]) >= int(merged["slow_ema"]):
            raise ValueError("fast_ema 必须小于 slow_ema")
        for key in ("entry_threshold", "strong_entry_threshold", "exit_threshold", "strong_exit_threshold"):
            if not 0 <= float(merged[key]) <= 100:
                raise ValueError(f"{key} 必须在 0 到 100 之间")
        if float(merged["strong_entry_threshold"]) < float(merged["entry_threshold"]):
            raise ValueError("强入场阈值不能低于普通入场阈值")
        if float(merged["strong_exit_threshold"]) < float(merged["exit_threshold"]):
            raise ValueError("强退出阈值不能低于普通退出阈值")
        version = self.store.set_rule(market_id, spec.raw_symbol, merged)
        return {"version": version, "config": merged}

    def list_strategy_plans(self) -> list[dict[str, Any]]:
        return self.store.list_strategy_plans()

    def create_strategy_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = self.specs.get((payload["market_id"], payload["instrument_id"]))
        if spec is None:
            raise KeyError(f"Unknown instrument: {payload['market_id']}/{payload['instrument_id']}")
        self._validate_strategy_plan(payload)
        return self.store.create_strategy_plan({**payload, "raw_symbol": spec.raw_symbol})

    def update_strategy_plan(self, plan_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.store.get_strategy_plan(plan_id)
        merged = {**current, **changes}
        if (merged["market_id"], merged["instrument_id"]) not in self.specs:
            raise KeyError(f"Unknown instrument: {merged['market_id']}/{merged['instrument_id']}")
        self._validate_strategy_plan(merged)
        return self.store.update_strategy_plan(plan_id, changes)

    @staticmethod
    def _validate_strategy_plan(payload: dict[str, Any]) -> None:
        if payload["direction"] not in {"LONG", "SHORT", "FLAT"}:
            raise ValueError("direction must be LONG, SHORT, or FLAT")
        if payload["execution_mode"] not in {"OBSERVE_ONLY", "AI_SUGGEST", "PAPER_CONFIRM", "MT5_DEMO_CONFIRM"}:
            raise ValueError("unsupported strategy execution mode")
        if payload["status"] not in {"DRAFT", "ACTIVE", "PAUSED", "COMPLETED"}:
            raise ValueError("unsupported strategy plan status")
        if payload.get("risk_percent") is not None and not 0 <= float(payload["risk_percent"]) <= 5:
            raise ValueError("risk_percent must be between 0 and 5")
        for field in ("stop_loss", "take_profit"):
            if payload.get(field) is not None and float(payload[field]) <= 0:
                raise ValueError(f"{field} must be greater than zero")
        if not str(payload.get("title", "")).strip():
            raise ValueError("strategy plan title is required")

    def analysis(self, market_id: str, instrument_id: str, limit: int = 300) -> dict[str, Any]:
        spec = self.specs[(market_id, instrument_id)]
        frame = self._load_frame(market_id, instrument_id)
        total_bars = len(frame)
        rule_data = self.get_rule(market_id, instrument_id)
        display_count = max(10, min(limit, 2000))
        periods = [
            int(rule_data["config"].get(key, 1))
            for key in ("slow_ema", "macd_slow", "rsi_period", "atr_period", "volume_period")
        ]
        # A chart request only returns the recent visible window. Keep enough
        # warm-up bars for EMA/RSI/ATR to converge instead of iterating over
        # every minute in multi-year FX files on every symbol change.
        analysis_window = max(5000, display_count + max(periods, default=1) * 10)
        frame = frame.tail(analysis_window).reset_index(drop=True)
        enriched, points = analyze_frame(frame, rule_data["config"], self.registry)
        transitions = signal_transitions(points)
        self.store.record_signals(
            [
                {
                    "market_id": market_id,
                    "instrument_id": instrument_id,
                    "raw_symbol": spec.raw_symbol,
                    "point": point.to_dict(),
                    "rule_version": int(rule_data["version"]),
                }
                for point in transitions
            ],
        )

        subset = enriched.tail(display_count)
        bars: list[dict[str, Any]] = []
        for _, row in subset.iterrows():
            timestamp = pd.Timestamp(row["timestamp"]).isoformat().replace("+00:00", "Z")
            bars.append(
                {
                    "timestamp": timestamp,
                    "open": self._number(row.get("open")),
                    "high": self._number(row.get("high")),
                    "low": self._number(row.get("low")),
                    "close": self._number(row.get("close")),
                    "volume": self._number(row.get("volume")),
                    "ema_fast": self._number(row.get("ema_fast")),
                    "ema_slow": self._number(row.get("ema_slow")),
                    "macd_hist": self._number(row.get("macd_hist")),
                    "rsi": self._number(row.get("rsi")),
                    "atr": self._number(row.get("atr")),
                    "volume_ratio": self._number(row.get("volume_ratio")),
                    "signal_level": str(row.get("signal_level", "WATCH")),
                    "entry_score": self._number(row.get("entry_score")),
                    "exit_score": self._number(row.get("exit_score")),
                },
            )
        point_dicts = [point.to_dict() for point in transitions if point.timestamp in {bar["timestamp"] for bar in bars}]
        return {
            "market_id": market_id,
            "instrument_id": instrument_id,
            "raw_symbol": spec.raw_symbol,
            "rule": rule_data,
            "bars": bars,
            "signals": point_dicts,
            "annotations": self.store.list_annotations(market_id, instrument_id),
            "latest": points[-1].to_dict(),
            "total_bars": total_bars,
        }

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            converted = float(value)
        except (TypeError, ValueError):
            return None
        return converted if math.isfinite(converted) else None

    def create_annotation(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = self.specs[(payload["market_id"], payload["instrument_id"])]
        payload = {**payload, "raw_symbol": spec.raw_symbol}
        return self.store.create_annotation(payload)

    def update_annotation(self, annotation_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self.store.update_annotation(annotation_id, changes)

    def dashboard(self) -> dict[str, Any]:
        accounts = self.store.query("SELECT * FROM accounts ORDER BY market_id")
        positions = self.store.query(
            "SELECT * FROM positions WHERE ABS(quantity) > 0.0000001 ORDER BY updated_at DESC",
        )
        orders = self.store.query("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100")
        signals = self.store.query("SELECT * FROM signal_events ORDER BY bar_timestamp DESC LIMIT 100")
        flags = self.store.query("SELECT * FROM risk_flags ORDER BY market_id")
        for signal in signals:
            signal["reasons"] = json.loads(signal.pop("reasons_json"))
            signal["indicators"] = json.loads(signal.pop("indicators_json"))
        for flag in flags:
            flag["kill_switch"] = bool(flag["kill_switch"])
            flag["close_only"] = bool(flag["close_only"])
        return {"accounts": accounts, "positions": positions, "orders": orders, "signals": signals, "risk": flags}

    def submit_manual_order(
        self,
        *,
        market_id: str,
        instrument_id: str,
        side: str,
        quantity: float,
        note: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        analysis = self.analysis(market_id, instrument_id, limit=10)
        latest = analysis["bars"][-1]
        spec = self.specs[(market_id, instrument_id)]
        return self.paper.submit_market_order(
            spec=spec,
            side=side,
            quantity=quantity,
            price=float(latest["close"]),
            bar_timestamp=str(latest["timestamp"]),
            source="MANUAL_PAPER",
            idempotency_key=idempotency_key or str(uuid.uuid4()),
            note=note,
        )

    def close_position(self, market_id: str, instrument_id: str) -> dict[str, Any]:
        analysis = self.analysis(market_id, instrument_id, limit=10)
        latest = analysis["bars"][-1]
        spec = self.specs[(market_id, instrument_id)]
        return self.paper.close_position(
            spec=spec,
            price=float(latest["close"]),
            bar_timestamp=str(latest["timestamp"]),
            source="MANUAL_PAPER_CLOSE",
            idempotency_key=str(uuid.uuid4()),
        )

    def run_replay(
        self,
        *,
        market_id: str,
        instrument_id: str,
        auto_trade: bool,
        reset_account: bool,
    ) -> dict[str, Any]:
        spec = self.specs[(market_id, instrument_id)]
        frame = self._load_frame(market_id, instrument_id)
        rule_data = self.get_rule(market_id, instrument_id)
        _enriched, points = analyze_frame(frame, rule_data["config"], self.registry)
        transitions = signal_transitions(points)
        recorded = 0
        orders = 0
        rejected: list[str] = []
        if reset_account:
            self.store.reset_paper_market(market_id)
        for point in transitions:
            signal_id = self.store.record_signal(
                market_id=market_id,
                instrument_id=instrument_id,
                raw_symbol=spec.raw_symbol,
                point=point.to_dict(),
                rule_version=int(rule_data["version"]),
            )
            recorded += int(signal_id is not None)
            if not auto_trade or point.level not in {SignalLevel.STRONG_ENTRY, SignalLevel.STRONG_EXIT}:
                continue
            positions = self.store.query(
                "SELECT quantity FROM positions WHERE market_id=? AND instrument_id=?",
                (market_id, instrument_id),
            )
            current = float(positions[0]["quantity"]) if positions else 0.0
            target = spec.trade_size if point.level == SignalLevel.STRONG_ENTRY else (-spec.trade_size if spec.allow_short else 0.0)
            delta = target - current
            if abs(delta) < 1e-9:
                continue
            side = "BUY" if delta > 0 else "SELL"
            try:
                self.paper.submit_market_order(
                    spec=spec,
                    side=side,
                    quantity=abs(delta),
                    price=point.price,
                    bar_timestamp=point.timestamp,
                    source="AUTO_REPLAY",
                    idempotency_key=f"replay:{market_id}:{instrument_id}:{rule_data['version']}:{point.timestamp}:{target}",
                    signal_id=signal_id,
                    note=f"{point.level.value} 自动纸面交易",
                )
                orders += 1
            except RiskRejected as exc:
                rejected.append(f"{point.timestamp}: {exc}")
        self.store.audit(
            "REPLAY", "COMPLETED",
            {"instrument_id": instrument_id, "auto_trade": auto_trade, "signals": len(transitions), "orders": orders},
            market_id,
        )
        return {
            "market_id": market_id,
            "instrument_id": instrument_id,
            "bars": len(points),
            "signal_transitions": len(transitions),
            "new_signal_records": recorded,
            "paper_orders": orders,
            "rejections": rejected[-20:],
            "dashboard": self.dashboard(),
        }

    def set_risk(self, market_id: str, kill_switch: bool, close_only: bool, reason: str) -> dict[str, Any]:
        if market_id not in self.settings["markets"]:
            raise KeyError(market_id)
        self.store.set_risk_flags(market_id, kill_switch, close_only, reason)
        return self.store.query("SELECT * FROM risk_flags WHERE market_id=?", (market_id,))[0]

    def broker_readiness(self) -> list[dict[str, Any]]:
        return [qmt_readiness(), ctp_readiness()]
