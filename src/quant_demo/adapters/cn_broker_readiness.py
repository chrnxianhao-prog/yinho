from __future__ import annotations

import importlib.util
import os
from typing import Any


def qmt_readiness() -> dict[str, Any]:
    required = ["QMT_ACCOUNT_ID", "QMT_PATH"]
    missing = [name for name in required if not os.getenv(name)]
    return {
        "adapter": "QMT/MiniQMT",
        "market": "stock_cn",
        "sdk_installed": importlib.util.find_spec("xtquant") is not None,
        "missing_environment": missing,
        "execution_enabled": False,
        "status": "RESERVED_ONLY",
        "message": "等待券商提供 MiniQMT 客户端、账号和程序化交易权限。",
    }


def ctp_readiness() -> dict[str, Any]:
    required = ["CTP_BROKER_ID", "CTP_USER_ID", "CTP_APP_ID", "CTP_AUTH_CODE"]
    missing = [name for name in required if not os.getenv(name)]
    sdk_installed = any(
        importlib.util.find_spec(module) is not None
        for module in ("vnpy_ctp", "openctp_ctp", "thostmduserapi")
    )
    return {
        "adapter": "CTP",
        "market": "futures_cn",
        "sdk_installed": sdk_installed,
        "missing_environment": missing,
        "execution_enabled": False,
        "status": "RESERVED_ONLY",
        "message": "等待期货公司提供 BrokerID、前置地址、AppID、AuthCode 和交易账号。",
    }

