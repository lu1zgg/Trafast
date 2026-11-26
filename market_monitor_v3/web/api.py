"""V4 P3 对外 API：/api/v1（REST）和 /api/v1/stream（WebSocket），给桌面版和其他客户端用。

- 鉴权：REST 带 `Authorization: Bearer tfk_…`；WebSocket 连上后第一条消息发 {"type": "auth", "key": "tfk_…"}，
  密钥不放在 URL 里，避免进访问日志。同一 IP 连续错 10 次封 15 分钟。
- 权限：read 只能看；write 还能改自定义提醒、类型开关、静音和反馈。
- 数据都经本机控制通道向监控进程要（网页后台进程不直接读数据库）。监控没在运行时返回 503。
- 跨域：桌面版的界面跑在系统 WebView 里，来源不是服务器域名，所以 /api/v1 允许任意来源（只认 Bearer 密钥，不用 Cookie）。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from .. import __version__
from ..apikeys import ApiKey, ApiKeyStore, FailGuard

API_VERSION = 1
PREFIX = "/api/v1"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Max-Age": "600",
}
POLL_S = 1.0
QUOTES_EVERY_S = 5.0
KEYCHECK_EVERY_S = 5.0
AUTH_TIMEOUT_S = 10.0

Monitor = Callable[..., Awaitable[dict[str, Any] | None]]


def mount_api(app: FastAPI, keys: ApiKeyStore, monitor: Monitor, guard: FailGuard | None = None, clock: Callable[[], float] = time.time) -> None:
    guard = guard or FailGuard()

    def ip_of(request: Request | WebSocket) -> str:
        return request.client.host if request.client else "?"

    def bearer(request: Request) -> str | None:
        h = request.headers.get("authorization") or ""
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return None

    def auth(request: Request, write: bool = False) -> ApiKey:
        ip = ip_of(request)
        now = clock()
        if guard.locked(ip, now):
            raise HTTPException(429, "错误次数太多，15 分钟后再试")
        key = keys.verify(bearer(request), now)
        if key is None:
            guard.fail(ip, now)
            raise HTTPException(401, "API 密钥无效或已吊销")
        guard.ok(ip)
        if write and key.scope != "write":
            raise HTTPException(403, "这个密钥只有只读权限")
        return key

    async def call(cmd: str, **args: Any) -> dict[str, Any]:
        res = await monitor(cmd, **args)
        if res is None:
            raise HTTPException(503, "监控进程没有运行")
        if not res.get("ok", False):
            raise HTTPException(400, str(res.get("error") or "请求失败"))
        res.pop("ok", None)
        return res

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(400, "请求体不是 JSON") from e
        if not isinstance(data, dict):
            raise HTTPException(400, "请求体要是 JSON 对象")
        return data

    @app.middleware("http")
    async def api_cors(request: Request, call_next: Any) -> Response:
        if not request.url.path.startswith(PREFIX):
            return await call_next(request)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=CORS)
        try:
            resp: Response = await call_next(request)
        except HTTPException as e:  # 一般由异常处理器转换；保险起见
            resp = JSONResponse({"error": str(e.detail)}, status_code=e.status_code)
        for k, v in CORS.items():
            resp.headers[k] = v
        return resp

    def err(e: HTTPException) -> JSONResponse:
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)

    def route(method: str, path: str, write: bool = False) -> Callable[[Callable[..., Awaitable[Any]]], Any]:
        """注册一个 /api/v1 接口：统一鉴权、把 HTTPException 转成 JSON 错误。"""

        def deco(fn: Callable[..., Awaitable[Any]]) -> Any:
            async def handler(request: Request) -> Response:
                try:
                    key = auth(request, write)
                    out = await fn(request, key)
                except HTTPException as e:
                    return err(e)
                return JSONResponse(out)

            handler.__name__ = "api_" + fn.__name__
            app.add_api_route(PREFIX + path, handler, methods=[method], include_in_schema=False)
            return fn

        return deco

    def q_int(request: Request, name: str, default: int, lo: int, hi: int) -> int:
        raw = request.query_params.get(name)
        if raw in (None, ""):
            return default
        try:
            return max(lo, min(hi, int(raw)))
        except ValueError as e:
            raise HTTPException(400, f"{name} 要是整数") from e

    # ---- 基本 ----
    @route("GET", "/ping")
    async def ping(request: Request, key: ApiKey) -> Any:
        res = await monitor("status")
        return {
            "ok": True,
            "api": API_VERSION,
            "version": __version__,
            "server_time": clock(),
            "key": {"id": key.id, "name": key.name, "scope": key.scope},
            "monitor": res is not None,
            "rule_version": (res or {}).get("rule_version"),
        }

    @route("GET", "/status")
    async def status(request: Request, key: ApiKey) -> Any:
        return await call("status")

    @route("GET", "/symbols")
    async def symbols(request: Request, key: ApiKey) -> Any:
        return await call("api_symbols")

    @route("GET", "/signals")
    async def signals(request: Request, key: ApiKey) -> Any:
        return await call(
            "api_signals",
            after=q_int(request, "after", 0, 0, 2**62),
            since=q_int(request, "since", 0, 0, 2**40),
            limit=q_int(request, "limit", 100, 1, 500),
            symbol=request.query_params.get("symbol") or None,
        )

    @route("GET", "/bars")
    async def bars(request: Request, key: ApiKey) -> Any:
        sym = request.query_params.get("symbol")
        if not sym:
            raise HTTPException(400, "缺少 symbol")
        return await call("api_bars", symbol=sym, tf=request.query_params.get("tf") or "1m", limit=q_int(request, "limit", 300, 1, 2000))

    @route("GET", "/levels")
    async def levels(request: Request, key: ApiKey) -> Any:
        sym = request.query_params.get("symbol")
        if not sym:
            raise HTTPException(400, "缺少 symbol")
        return await call("api_levels", symbol=sym)

    @route("GET", "/stats")
    async def stats(request: Request, key: ApiKey) -> Any:
        return await call("api_stats")

    @route("GET", "/trend")
    async def trend(request: Request, key: ApiKey) -> Any:
        return await call("trend")

    @route("GET", "/quotes")
    async def quotes(request: Request, key: ApiKey) -> Any:
        return await call("api_quotes")

    # ---- 自定义提醒、类型开关、静音、反馈 ----
    @route("GET", "/alerts")
    async def alerts(request: Request, key: ApiKey) -> Any:
        return await call("alerts")

    @route("POST", "/alerts", write=True)
    async def alert_add(request: Request, key: ApiKey) -> Any:
        data = await body(request)
        text = str(data.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "缺少 text，例如 NVDA > 140")
        return await call("alert_add", text=text, by=f"api:{key.name}")

    @route("DELETE", "/alerts/{rule_id}", write=True)
    async def alert_del(request: Request, key: ApiKey) -> Any:
        rid = request.path_params["rule_id"]
        if rid != "all" and not rid.isdigit():
            raise HTTPException(400, "id 要是数字或 all")
        return await call("alert_del", id=None if rid == "all" else int(rid))

    @route("GET", "/types")
    async def types(request: Request, key: ApiKey) -> Any:
        return await call("types")

    @route("POST", "/types", write=True)
    async def types_set(request: Request, key: ApiKey) -> Any:
        data = await body(request)
        return await call("types_set", target=str(data.get("target") or "*"), pattern=str(data.get("pattern") or ""), off=bool(data.get("off")))

    @route("POST", "/mute", write=True)
    async def mute(request: Request, key: ApiKey) -> Any:
        data = await body(request)
        try:
            seconds = int(data.get("seconds") or 3600)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "seconds 要是整数") from e
        return await call("mute", target=str(data.get("target") or ""), seconds=seconds)

    @route("DELETE", "/mute/{target}", write=True)
    async def unmute(request: Request, key: ApiKey) -> Any:
        return await call("unmute", target=request.path_params["target"])

    @route("POST", "/feedback", write=True)
    async def feedback(request: Request, key: ApiKey) -> Any:
        data = await body(request)
        value = data.get("value")
        if value not in (0, 1) or not data.get("episode_id"):
            raise HTTPException(400, "需要 episode_id 和 value（1 = 有用，0 = 没用）")
        return await call("api_feedback", episode_id=str(data["episode_id"]), value=int(value), user=f"api:{key.id}")

    # ---- WebSocket ----
    @app.websocket(PREFIX + "/stream")
    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        ip = ip_of(ws)
        if guard.locked(ip, clock()):
            await ws.close(code=4429, reason="too many failures")
            return
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S))
        except (TimeoutError, ValueError, WebSocketDisconnect):
            await _close(ws, 4401, "auth required")
            return
        key = keys.verify(first.get("key") if isinstance(first, dict) and first.get("type") == "auth" else None, clock())
        if key is None:
            guard.fail(ip, clock())
            await _close(ws, 4401, "invalid key")
            return
        guard.ok(ip)
        cursor = first.get("after")
        if not isinstance(cursor, int) or cursor < 0:
            latest = await monitor("api_signals", after=0, limit=1, latest=True)
            cursor = int((latest or {}).get("next") or 0)
        await ws.send_json({"type": "hello", "api": API_VERSION, "version": __version__, "scope": key.scope, "after": cursor})

        async def reader() -> None:
            nonlocal cursor
            while True:
                msg = json.loads(await ws.receive_text())
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "ping":
                    await ws.send_json({"type": "pong", "t": clock()})
                elif msg.get("type") == "resume" and isinstance(msg.get("after"), int):
                    cursor = max(0, msg["after"])

        rtask = asyncio.create_task(reader())
        last_quotes = last_check = 0.0
        try:
            while not rtask.done():
                now = clock()
                if now - last_check >= KEYCHECK_EVERY_S:
                    last_check = now
                    if not keys.is_active(key.id):
                        await _close(ws, 4403, "key revoked")
                        return
                res = await monitor("api_signals", after=cursor, limit=200)
                if res is None:
                    await ws.send_json({"type": "monitor", "running": False})
                else:
                    for item in res.get("items", []):
                        await ws.send_json({"type": "signal", "seq": item["seq"], "data": item})
                    cursor = int(res.get("next") or cursor)
                if now - last_quotes >= QUOTES_EVERY_S:
                    last_quotes = now
                    q = await monitor("api_quotes")
                    if q is not None:
                        await ws.send_json({"type": "quotes", "items": q.get("items", []), "t": now})
                await asyncio.wait({rtask}, timeout=POLL_S)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            rtask.cancel()


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except RuntimeError:
        pass
