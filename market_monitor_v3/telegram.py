"""Telegram Bot API 客户端与错误分类。

token 在 URL 里，所以：httpx 日志调到 WARNING（见 logsetup），异常信息一律经过 redact() 再记录。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class TgResult:
    kind: str  # ok / not_modified / parse / parent_missing / edit_missing / bad_request / migrated / auth / rate / server / network / timeout
    result: Any = None
    code: int | None = None
    description: str = ""
    retry_after: float | None = None
    migrate_to: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind in ("ok", "not_modified")

    @property
    def message_id(self) -> int | None:
        if isinstance(self.result, dict):
            return self.result.get("message_id")
        return None


def classify(status: int, data: dict[str, Any] | None) -> TgResult:
    if data is None:
        if status >= 500:
            return TgResult("server", code=status, description=f"HTTP {status}")
        return TgResult("bad_request", code=status, description=f"HTTP {status}，返回不是 JSON")
    if data.get("ok"):
        return TgResult("ok", result=data.get("result"), code=status)
    code = int(data.get("error_code") or status)
    desc = str(data.get("description") or "")
    params = data.get("parameters") or {}
    d = desc.lower()
    if "migrate_to_chat_id" in params:
        return TgResult("migrated", code=code, description=desc, migrate_to=int(params["migrate_to_chat_id"]))
    if code == 429:
        return TgResult("rate", code=code, description=desc, retry_after=float(params.get("retry_after", 5)))
    if code in (401, 403) or "chat not found" in d or "bot was kicked" in d:
        return TgResult("auth", code=code, description=desc)
    if code >= 500:
        return TgResult("server", code=code, description=desc)
    if "message is not modified" in d:
        return TgResult("not_modified", code=code, description=desc)
    if "message to edit not found" in d or "message can't be edited" in d:
        return TgResult("edit_missing", code=code, description=desc)
    if "replied message not found" in d or "message to be replied not found" in d:
        return TgResult("parent_missing", code=code, description=desc)
    if "can't parse entities" in d or "can't find end of the entity" in d or "unsupported start tag" in d:
        return TgResult("parse", code=code, description=desc)
    return TgResult("bad_request", code=code, description=desc)


class TelegramClient:
    def __init__(
        self,
        token: str,
        api_base: str = "https://api.telegram.org",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._token = token
        self._base = f"{api_base.rstrip('/')}/bot{token}/"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=5.0),
            transport=transport,
        )

    def redact(self, s: str) -> str:
        return s.replace(self._token, "***") if self._token else s

    async def call(self, method: str, payload: dict[str, Any] | None = None, read_timeout: float | None = None) -> TgResult:
        kw: dict[str, Any] = {"json": payload or {}}
        if read_timeout is not None:
            kw["timeout"] = httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0)
        try:
            r = await self._client.post(self._base + method, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            # 请求没送到，可以放心重发
            return TgResult("network", description=f"{type(e).__name__}: {self.redact(str(e))}")
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError) as e:
            # 请求可能已经送到
            return TgResult("timeout", description=f"{type(e).__name__}: {self.redact(str(e))}")
        except httpx.HTTPError as e:
            return TgResult("network", description=f"{type(e).__name__}: {self.redact(str(e))}")
        try:
            data = r.json()
        except ValueError:
            data = None
        return classify(r.status_code, data)

    async def aclose(self) -> None:
        await self._client.aclose()
