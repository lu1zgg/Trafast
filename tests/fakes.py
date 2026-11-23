"""假的 Telegram：httpx.MockTransport，按脚本返回。"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from market_monitor_v3.telegram import TelegramClient

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijk"


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def err(code: int, desc: str, **params: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": desc}
    if params:
        body["parameters"] = params
    return httpx.Response(code if code != 400 or not params.get("http") else 400, json=body)


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.script: list[Any] = []
        self.next_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if self.script:
            r = self.script.pop(0)
            if isinstance(r, Exception):
                raise r
            if callable(r):
                return r(method, body)
            return r
        if method in ("sendMessage", "editMessageText"):
            self.next_id += 1
            return ok({"message_id": self.next_id if method == "sendMessage" else body.get("message_id")})
        if method == "getUpdates":
            return ok([])
        return ok({"id": body.get("chat_id", 1)})

    def client(self) -> TelegramClient:
        return TelegramClient(TOKEN, "https://api.telegram.test", transport=httpx.MockTransport(self.handler))

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == method]


Responder = Callable[[str, dict[str, Any]], httpx.Response]
