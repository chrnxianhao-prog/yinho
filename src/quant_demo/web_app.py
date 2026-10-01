from __future__ import annotations

import os
import json
import importlib.util
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from quant_demo.adapters.mt5_demo_adapter import Mt5DemoAdapter, load_dotenv
from quant_demo.paper_engine import RiskRejected
from quant_demo.trading_service import TradingService


ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = Path(__file__).resolve().parent / "web"
MT5_ENV_PATH = Path(os.getenv("MT5_ENV_PATH", str(ROOT / ".env")))
load_dotenv(MT5_ENV_PATH)

service = TradingService(ROOT)
app = FastAPI(title="银禾多市场持仓与交割终端", version="1.1.0")


class PaperOrderRequest(BaseModel):
    market_id: str
    instrument_id: str
    side: str = Field(pattern="^(BUY|SELL)$")
    quantity: float = Field(gt=0)
    note: str = Field(default="", max_length=1000)
    confirm: bool = False
    idempotency_key: str | None = Field(default=None, max_length=128)


class ClosePositionRequest(BaseModel):
    market_id: str
    instrument_id: str
    confirm: bool = False


class ReplayRequest(BaseModel):
    market_id: str
    instrument_id: str
    auto_trade: bool = False
    reset_account: bool = False
    confirm: bool = False


class AnnotationRequest(BaseModel):
    market_id: str
    instrument_id: str
    bar_timestamp: str
    price: float = Field(gt=0)
    annotation_type: str = Field(
        pattern="^(WATCH|ENTRY|STRONG_ENTRY|EXIT|STRONG_EXIT|BAD_SIGNAL|REVIEW)$",
    )
    note: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    signal_id: str | None = None
    indicator_snapshot: dict[str, Any] = Field(default_factory=dict)


class AnnotationUpdateRequest(BaseModel):
    annotation_type: str | None = Field(
        default=None,
        pattern="^(WATCH|ENTRY|STRONG_ENTRY|EXIT|STRONG_EXIT|BAD_SIGNAL|REVIEW)$",
    )
    note: str | None = Field(default=None, max_length=4000)
    tags: list[str] | None = None
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    deleted: bool | None = None
    confirm: bool = False


class RuleRequest(BaseModel):
    config: dict[str, Any]
    confirm: bool = False


class StrategyPlanRequest(BaseModel):
    market_id: str
    instrument_id: str
    title: str = Field(min_length=1, max_length=160)
    direction: str = Field(pattern="^(LONG|SHORT|FLAT)$")
    timeframe: str = Field(default="1D", min_length=1, max_length=32)
    entry_condition: str = Field(default="", max_length=4000)
    exit_condition: str = Field(default="", max_length=4000)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    risk_percent: float | None = Field(default=None, ge=0, le=5)
    execution_mode: str = Field(pattern="^(OBSERVE_ONLY|AI_SUGGEST|PAPER_CONFIRM|MT5_DEMO_CONFIRM)$")
    status: str = Field(default="DRAFT", pattern="^(DRAFT|ACTIVE|PAUSED|COMPLETED)$")
    notes: str = Field(default="", max_length=4000)


class StrategyPlanUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    direction: str | None = Field(default=None, pattern="^(LONG|SHORT|FLAT)$")
    timeframe: str | None = Field(default=None, min_length=1, max_length=32)
    entry_condition: str | None = Field(default=None, max_length=4000)
    exit_condition: str | None = Field(default=None, max_length=4000)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    risk_percent: float | None = Field(default=None, ge=0, le=5)
    execution_mode: str | None = Field(default=None, pattern="^(OBSERVE_ONLY|AI_SUGGEST|PAPER_CONFIRM|MT5_DEMO_CONFIRM)$")
    status: str | None = Field(default=None, pattern="^(DRAFT|ACTIVE|PAUSED|COMPLETED)$")
    notes: str | None = Field(default=None, max_length=4000)


class RiskRequest(BaseModel):
    market_id: str
    kill_switch: bool = False
    close_only: bool = False
    reason: str = Field(default="", max_length=500)
    confirm: bool = False


class Mt5OrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    side: str = Field(pattern="^(BUY|SELL)$")
    volume: float = Field(gt=0)
    confirm: bool = False


class Mt5CloseRequest(BaseModel):
    ticket: int = Field(gt=0)
    confirm: bool = False


def _check_token(authorization: str | None) -> None:
    expected = os.getenv("WEB_API_TOKEN", "")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="WEB_API_TOKEN 缺失或不正确")


def _require_confirmation(confirmed: bool, action: str) -> None:
    if not confirmed:
        raise HTTPException(status_code=400, detail=f"{action}需要二次确认")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ValueError, RiskRejected)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@contextmanager
def _mt5_session(*, require_trading: bool = False) -> Iterator[tuple[Mt5DemoAdapter, Any]]:
    adapter = Mt5DemoAdapter(MT5_ENV_PATH)
    try:
        account = adapter.connect_demo(require_trading=require_trading)
        yield adapter, account
    finally:
        adapter.close()


def _mt5_account(account: Any) -> dict[str, Any]:
    balance = float(account.balance)
    equity = float(account.equity)
    margin = float(account.margin)
    margin_usage = margin / equity * 100.0 if equity > 0 else 100.0
    drawdown = max(0.0, (balance - equity) / balance * 100.0) if balance > 0 else 100.0

    # Product warning bands; broker stop-out rules remain authoritative.
    usage_band = (
        3 if margin_usage >= 80 else 2 if margin_usage >= 50
        else 1 if margin_usage >= 25 else 0
    )
    drawdown_band = (
        3 if drawdown >= 20 else 2 if drawdown >= 10
        else 1 if drawdown >= 5 else 0
    )
    risk_level = ("低", "中", "高", "极高")[max(usage_band, drawdown_band)]
    risk_message = {
        "低": "保证金占用与余额-净值差处于终端预警低档",
        "中": "注意控制仓位，保证金占用或浮动亏损达到中档提醒",
        "高": "风险偏高，建议减仓；请同时核对券商强平线",
        "极高": "极高风险提醒；终端不保证阻止券商强平",
    }[risk_level]
    return {
        "login": int(account.login),
        "server": str(account.server),
        "currency": str(account.currency),
        "balance": balance,
        "equity": equity,
        "margin": margin,
        "margin_free": float(account.margin_free),
        "profit": float(account.profit),
        "margin_level_pct": equity / margin * 100.0 if margin > 0 else None,
        "margin_usage_pct": margin_usage,
        "equity_drawdown_pct": drawdown,
        "risk_level": risk_level,
        "risk_message": risk_message,
        "trade_allowed": bool(account.trade_allowed),
    }


def _mt5_readiness() -> dict[str, Any]:
    terminal_path = os.getenv("MT5_PATH") or os.getenv("MT5_TERMINAL_PATH") or ""
    required = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
    return {
        "env_path": str(MT5_ENV_PATH),
        "env_file_exists": MT5_ENV_PATH.is_file(),
        "demo_enabled": os.getenv("MT5_DEMO_ENABLED", "false").lower() == "true",
        "missing_required": [key for key in required if not os.getenv(key)],
        "terminal_path_configured": bool(terminal_path),
        "terminal_path_exists": bool(terminal_path and Path(terminal_path).is_file()),
        "python_package_available": importlib.util.find_spec("MetaTrader5") is not None,
        "symbols": [item.strip().upper() for item in os.getenv("WEB_SYMBOLS", "EURUSD,XAUUSD").split(",") if item.strip()],
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "mode": "multi-market-paper", "live_trading": "disabled"}


@app.get("/api/config")
def config(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(authorization)
    return {
        "mode": "PAPER ONLY",
        "live_trading_enabled": False,
        "markets": service.market_catalog(),
        "plugins": service.registry.metadata(),
        "loaded_plugin_modules": service.loaded_plugin_modules,
        "broker_readiness": service.broker_readiness(),
        "mt5_readiness": _mt5_readiness(),
    }


@app.get("/api/status")
def status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(authorization)
    return service.dashboard()


@app.get("/api/analysis")
def analysis(
    market_id: str,
    instrument_id: str,
    limit: int = Query(default=300, ge=10, le=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    try:
        return service.analysis(market_id, instrument_id, limit)
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/rules/{market_id}/{instrument_id:path}")
def get_rule(
    market_id: str,
    instrument_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    try:
        return service.get_rule(market_id, instrument_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@app.put("/api/rules/{market_id}/{instrument_id:path}")
def update_rule(
    market_id: str,
    instrument_id: str,
    payload: RuleRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    _require_confirmation(payload.confirm, "更新指标规则")
    try:
        return service.update_rule(market_id, instrument_id, payload.config)
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/strategy-plans")
def list_strategy_plans(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(authorization)
    return {"items": service.list_strategy_plans()}


@app.post("/api/strategy-plans")
def create_strategy_plan(
    payload: StrategyPlanRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    try:
        return {"plan": service.create_strategy_plan(payload.model_dump())}
    except Exception as exc:
        raise _http_error(exc) from exc


@app.patch("/api/strategy-plans/{plan_id}")
def update_strategy_plan(
    plan_id: str,
    payload: StrategyPlanUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    try:
        return {"plan": service.update_strategy_plan(plan_id, payload.model_dump(exclude_none=True))}
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/annotations")
def create_annotation(
    payload: AnnotationRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    try:
        return service.create_annotation(payload.model_dump())
    except Exception as exc:
        raise _http_error(exc) from exc


@app.patch("/api/annotations/{annotation_id}")
def update_annotation(
    annotation_id: str,
    payload: AnnotationUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    _require_confirmation(payload.confirm, "修改或删除备注")
    try:
        changes = payload.model_dump(exclude_none=True)
        changes.pop("confirm", None)
        return service.update_annotation(annotation_id, changes)
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/paper/orders")
def submit_paper_order(
    payload: PaperOrderRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    _require_confirmation(payload.confirm, "纸面下单")
    try:
        order = service.submit_manual_order(
            market_id=payload.market_id,
            instrument_id=payload.instrument_id,
            side=payload.side,
            quantity=payload.quantity,
            note=payload.note,
            idempotency_key=payload.idempotency_key,
        )
        return {"order": order, "dashboard": service.dashboard()}
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/paper/positions/close")
def close_paper_position(
    payload: ClosePositionRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    _require_confirmation(payload.confirm, "纸面平仓")
    try:
        order = service.close_position(payload.market_id, payload.instrument_id)
        return {"order": order, "dashboard": service.dashboard()}
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/replay")
def run_replay(
    payload: ReplayRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    if payload.auto_trade or payload.reset_account:
        _require_confirmation(payload.confirm, "自动纸面回放")
    try:
        return service.run_replay(
            market_id=payload.market_id,
            instrument_id=payload.instrument_id,
            auto_trade=payload.auto_trade,
            reset_account=payload.reset_account,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/risk")
def update_risk(
    payload: RiskRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    _require_confirmation(payload.confirm, "更改风控开关")
    try:
        return service.set_risk(payload.market_id, payload.kill_switch, payload.close_only, payload.reason)
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/mt5/status")
def mt5_status(
    history_days: int = Query(default=3650, ge=1, le=3650),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    try:
        symbols = [item.strip().upper() for item in os.getenv("WEB_SYMBOLS", "EURUSD,XAUUSD").split(",")]
        with _mt5_session() as (adapter, account):
            trades = adapter.get_trade_summaries(history_days)
            for trade in trades:
                trade["currency"] = str(account.currency)
            service.store.upsert_mt5_trade_summaries(int(account.login), trades)
            return {
                "account": _mt5_account(account),
                "quotes": {symbol: adapter.get_quote(symbol) for symbol in symbols if symbol},
                "positions": adapter.get_positions(),
                "trades": trades[:500],
            }
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/mt5/readiness")
def mt5_readiness(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(authorization)
    return _mt5_readiness()


@app.post("/api/mt5/orders")
def mt5_order(
    payload: Mt5OrderRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    _require_confirmation(payload.confirm, "MT5 Demo 下单")
    max_volume = float(os.getenv("WEB_MAX_VOLUME", "0.01"))
    if payload.volume > max_volume:
        raise HTTPException(status_code=400, detail=f"手数超过 WEB_MAX_VOLUME={max_volume}")
    try:
        allowed_symbols = {
            item.strip().upper()
            for item in os.getenv("WEB_SYMBOLS", "EURUSD,XAUUSD").split(",")
            if item.strip()
        }
        if payload.symbol.upper() not in allowed_symbols:
            raise HTTPException(status_code=400, detail="品种不在 WEB_SYMBOLS Demo 白名单中")
        with _mt5_session(require_trading=True) as (adapter, account):
            result = adapter.open_market(
                payload.symbol.upper(), payload.side, payload.volume, origin="MANUAL",
            )
            service.store.audit(
                "MT5_DEMO_ORDER", "OPEN_ACCEPTED",
                {
                    "symbol": payload.symbol.upper(), "side": payload.side,
                    "requested_volume": payload.volume, "order_ticket": result.order_ticket,
                    "deal_ticket": result.deal_ticket, "filled_volume": result.volume,
                    "price": result.price, "retcode": result.retcode, "source": "人工下单",
                },
                "fx_gold",
            )
            return {"account": _mt5_account(account), "order": result.__dict__, "positions": adapter.get_positions()}
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise _http_error(exc) from exc


@app.post("/api/mt5/positions/close")
def mt5_close_position(
    payload: Mt5CloseRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    _require_confirmation(payload.confirm, "MT5 Demo 平仓")
    try:
        with _mt5_session(require_trading=True) as (adapter, account):
            result = adapter.close_position_ticket(payload.ticket, allow_external=True)
            service.store.audit(
                "MT5_DEMO_ORDER", "CLOSE_ACCEPTED",
                {
                    "position_ticket": payload.ticket, "order_ticket": result.order_ticket,
                    "deal_ticket": result.deal_ticket, "filled_volume": result.volume,
                    "price": result.price, "retcode": result.retcode, "source": "本人手动平仓",
                },
                "fx_gold",
            )
            return {
                "account": _mt5_account(account), "order": result.__dict__,
                "positions": adapter.get_positions(),
            }
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/activity")
def activity(
    limit: int = Query(default=500, ge=1, le=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(authorization)
    paper_rows = service.store.query(
        """
        SELECT o.order_id AS id, o.created_at AS time, o.market_id, o.raw_symbol AS symbol,
               o.side, o.quantity, o.price, o.fee, o.realized_pnl AS gross_pnl, o.net_pnl,
               o.source, o.note, o.status, 'PAPER' AS account_type,
               COALESCE(a.currency, '') AS currency
        FROM orders o LEFT JOIN accounts a ON a.market_id=o.market_id
        ORDER BY o.created_at DESC LIMIT ?
        """,
        (limit,),
    )
    for row in paper_rows:
        row["category"] = (
            "本人手动" if str(row["source"]).startswith("MANUAL")
            else "AI/策略" if str(row["source"]).startswith(("AUTO", "AI"))
            else "回测/其他"
        )
        row["commission"] = float(row.pop("fee") or 0.0)
        row["swap"] = 0.0
        row["other_fee"] = 0.0
    mt5_rows = []
    for stored in service.store.query(
        "SELECT payload_json FROM mt5_trade_summaries ORDER BY updated_at DESC",
    ):
        trade = json.loads(stored["payload_json"])
        mt5_rows.append({
            "id": f"MT5:{trade['trade_key']}",
            "time": trade["last_time"],
            "market_id": "fx_gold",
            "symbol": trade["symbol"],
            "side": trade.get("side", "—"),
            "quantity": trade["volume"],
            "price": None,
            "gross_pnl": trade["profit"],
            "commission": trade["commission"],
            "swap": trade["swap"],
            "other_fee": trade["fee"],
            "net_pnl": trade["net_pnl"],
            "source": trade["source"],
            "category": trade["source"],
            "note": f"{trade['deal_count']} 笔成交 · position {trade['position_id']}",
            "status": "已归档",
            "account_type": "MT5 Demo",
            "currency": trade.get("currency", ""),
        })
    def activity_timestamp(row: dict[str, Any]) -> float:
        value = row["time"]
        if isinstance(value, (int, float)):
            return float(value)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0.0

    items = sorted([*paper_rows, *mt5_rows], key=activity_timestamp, reverse=True)[:limit]
    return {"items": items, "count": len(items)}
