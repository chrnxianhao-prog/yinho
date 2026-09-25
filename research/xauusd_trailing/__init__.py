"""Auditable, backtest-only XAUUSD trailing-stop research package."""

from .engine import BacktestResult, run_backtest
from .models import BacktestConfig

__all__ = ["BacktestConfig", "BacktestResult", "run_backtest"]
