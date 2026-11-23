"""TradingView 告警 webhook：解析、限流去重、按告警卡片的样式排版。

在 TradingView 里建告警时，“Webhook URL” 填后台给出的地址（https://你的域名/hooks/tradingview/密钥），
“消息”推荐写成 JSON（TradingView 会自动替换 {{...}} 占位符）：

    {"ticker": "{{ticker}}", "exchange": "{{exchange}}", "price": {{close}},
     "interval": "{{interval}}", "action": "buy", "message": "突破前高"}

action 写 buy / long / up 显示绿灯，sell / short / down 显示红灯，其他或不写显示 🔔。
不是 JSON 的消息按纯文本整段转发。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .render import DIVIDER, b, esc, usd

BJ = ZoneInfo("Asia/Shanghai")
MAX_BODY = 8 * 1024
UP_WORDS = {"buy", "long", "up", "bull", "bullish", "涨", "买", "多"}
DOWN_WORDS = {"sell", "short", "down", "bear", "bearish", "跌", "卖", "空"}


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def parse_alert(body: bytes) -> dict[str, Any]:
    """把 webhook 正文整理成统一字段。正文太长时截断。"""
    raw = body[:MAX_BODY].decode("utf-8", errors="replace").strip()
    data: dict[str, Any] = {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            data = obj
    except ValueError:
        pass
    if not data:
        return {"ticker": "", "exchange": "", "price": None, "interval": "", "action": "", "message": raw[:1500]}

    def pick(*names: str) -> Any:
        for n in names:
            if n in data and data[n] not in (None, ""):
                return data[n]
        return None

    return {
        "ticker": str(pick("ticker", "symbol") or "")[:40],
        "exchange": str(pick("exchange") or "")[:40],
        "price": _num(pick("price", "close")),
        "interval": str(pick("interval", "timeframe") or "")[:20],
        "action": str(pick("action", "side", "direction", "signal") or "")[:40],
        "message": str(pick("message", "text", "comment", "title") or "")[:1500],
    }


def direction_of(action: str) -> int:
    a = action.strip().lower()
    if a in UP_WORDS:
        return 1
    if a in DOWN_WORDS:
        return -1
    return 0


def _interval_text(v: str) -> str:
    if not v:
        return ""
    if v.isdigit():
        n = int(v)
        return f"{n // 60} 小时" if n >= 60 and n % 60 == 0 else f"{n} 分钟"
    return {"D": "日线", "1D": "日线", "W": "周线", "1W": "周线", "M": "月线", "1M": "月线"}.get(v.upper(), v)


def card_text(alert: dict[str, Any], now: float) -> tuple[str, int]:
    """返回 (Telegram HTML, 方向)。"""
    d = direction_of(alert.get("action") or "")
    icon = "🟢" if d > 0 else "🔴" if d < 0 else "🔔"
    name = alert.get("ticker") or "TradingView"
    head = f"{icon} {b(esc(name))}"
    if alert.get("exchange"):
        head += f" · {esc(alert['exchange'])}"
    head += "｜TradingView 告警"
    lines = [head]
    facts = []
    if alert.get("price") is not None:
        facts.append(f"价格 {b(usd(alert['price']))}")
    if alert.get("interval"):
        facts.append(f"周期 {esc(_interval_text(alert['interval']))}")
    if alert.get("action"):
        facts.append(f"信号 {b(esc(alert['action']))}")
    if facts:
        lines.append("｜".join(facts))
    if alert.get("message"):
        lines += [DIVIDER, esc(alert["message"])]
    bj = datetime.fromtimestamp(now, BJ).strftime("%m-%d %H:%M")
    utc = datetime.fromtimestamp(now, UTC).strftime("%H:%M")
    lines += [DIVIDER, f"北京 {bj}｜UTC {utc}"]
    return "\n".join(lines), d


class Throttle:
    """每分钟上限 + 相同内容去重。"""

    def __init__(self, max_per_min: int, dedup_s: int):
        self.max_per_min = max_per_min
        self.dedup_s = dedup_s
        self.sent: deque[float] = deque()
        self.recent: dict[str, float] = {}

    def allow(self, alert: dict[str, Any], now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now
        while self.sent and now - self.sent[0] >= 60:
            self.sent.popleft()
        self.recent = {k: t for k, t in self.recent.items() if now - t < self.dedup_s}
        digest = hashlib.sha256(json.dumps(alert, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        if digest in self.recent:
            return False, "重复"
        if len(self.sent) >= self.max_per_min:
            return False, "超过每分钟上限"
        self.sent.append(now)
        self.recent[digest] = now
        return True, digest
