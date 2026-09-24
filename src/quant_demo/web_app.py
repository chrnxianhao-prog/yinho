from __future__ import annotations

import os
from contextlib import contextmanager
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
load_dotenv(ROOT / ".env")

service = TradingService(ROOT)
app = FastAPI(title="多市场量化分析与纸面交易终端", version="1.0.0")


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
def _mt5_session() -> Iterator[tuple[Mt5DemoAdapter, Any]]:
    adapter = Mt5DemoAdapter(ROOT / ".env")
    try:
        account = adapter.connect_demo()
        yield adapter, account
    finally:
        adapter.close()


def _mt5_account(account: Any) -> dict[str, Any]:
    return {
        "login": int(account.login),
        "server": str(account.server),
        "currency": str(account.currency),
        "balance": float(account.balance),
        "equity": float(account.equity),
        "margin": float(account.margin),
        "margin_free": float(account.margin_free),
        "profit": float(account.profit),
        "trade_allowed": bool(account.trade_allowed),
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
def mt5_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(authorization)
    try:
        symbols = [item.strip().upper() for item in os.getenv("WEB_SYMBOLS", "EURUSD,XAUUSD").split(",")]
        with _mt5_session() as (adapter, account):
            return {
                "account": _mt5_account(account),
                "quotes": {symbol: adapter.get_quote(symbol) for symbol in symbols if symbol},
                "positions": adapter.get_positions(),
            }
    except Exception as exc:
        raise _http_error(exc) from exc


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
        with _mt5_session() as (adapter, account):
            result = adapter.open_market(payload.symbol.upper(), payload.side, payload.volume)
            return {"account": _mt5_account(account), "order": result.__dict__, "positions": adapter.get_positions()}
    except Exception as exc:
        raise _http_error(exc) from exc
