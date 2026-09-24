"""信号推送。默认只打印到屏幕并写文件；在 .env 里配置后可推送到 Telegram、企业微信、飞书或 PushPlus（微信）。

.env 放在项目根目录（D:\\yin ho\\股票量化分析\\.env），不要把密钥写进代码：
  SIGNAL_PUSH=telegram,wecom        # 逗号分隔：telegram / wecom / feishu / pushplus；留空 = 不推送
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
  FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/...
  PUSHPLUS_TOKEN=...

注意：各推送通道的代码还没用真实密钥测过，第一次启用时先发一条测试消息：
  ..\\..\\.venv\\Scripts\\python.exe notifier.py --test
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import requests

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
TELEGRAM_LIMIT = 3900  # Telegram 单条上限 4096 字符，留余量


def load_env() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _telegram(title: str, body: str) -> None:
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    text = f"{title}\n\n{body}"
    for start in range(0, len(text), TELEGRAM_LIMIT):
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[start:start + TELEGRAM_LIMIT]},
            timeout=15,
        )
        response.raise_for_status()


def _wecom(title: str, body: str) -> None:
    response = requests.post(
        os.environ["WECOM_WEBHOOK"],
        json={"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{body}"[:4000]}},
        timeout=15,
    )
    response.raise_for_status()


def _feishu(title: str, body: str) -> None:
    response = requests.post(
        os.environ["FEISHU_WEBHOOK"],
        json={"msg_type": "text", "content": {"text": f"{title}\n\n{body}"}},
        timeout=15,
    )
    response.raise_for_status()


def _pushplus(title: str, body: str) -> None:
    response = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": os.environ["PUSHPLUS_TOKEN"], "title": title, "content": body, "template": "markdown"},
        timeout=15,
    )
    response.raise_for_status()


CHANNELS = {"telegram": _telegram, "wecom": _wecom, "feishu": _feishu, "pushplus": _pushplus}


def notify(title: str, body: str) -> list[str]:
    """打印并按 SIGNAL_PUSH 推送；单个通道失败只记录，不影响其它通道和扫描结果。返回失败信息列表。"""
    load_env()
    print(f"\n===== {title} =====\n{body}\n", flush=True)
    failures = []
    for name in filter(None, (item.strip() for item in os.getenv("SIGNAL_PUSH", "").split(","))):
        sender = CHANNELS.get(name)
        if sender is None:
            failures.append(f"未知推送通道：{name}")
            continue
        try:
            sender(title, body)
        except Exception as exc:  # noqa: BLE001 - 推送失败不能让扫描失败
            failures.append(f"{name} 推送失败：{type(exc).__name__}: {exc}")
    for failure in failures:
        print(failure, flush=True)
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="发送一条测试消息，检查推送配置")
    parser.add_argument("--test", action="store_true")
    if parser.parse_args().test:
        notify("A股信号推送测试", "如果你在手机上看到这条消息，推送通道就配置好了。")
