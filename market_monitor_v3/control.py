"""本机控制通道：监控进程在 Unix socket 上听，网页后台通过它重载配置、查状态、发测试消息、转发 TradingView 告警。

只在本机文件系统上，不开网络端口。socket 文件权限 0660，属组是监控服务的组，
网页后台的系统用户加进这个组才能连。协议：一行 JSON 请求，一行 JSON 回应。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_LINE = 256 * 1024

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ControlServer:
    def __init__(self, path: str | Path, handler: Handler):
        self.path = Path(path)
        self.handler = handler
        self.server: asyncio.base_events.Server | None = None

    async def start(self) -> bool:
        if not self.path.parent.is_dir():
            log.warning("控制通道目录 %s 不存在，网页后台将无法连接监控进程", self.path.parent)
            return False
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        old = os.umask(0o117)
        try:
            self.server = await asyncio.start_unix_server(self._client, path=str(self.path), limit=MAX_LINE)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o660)
        log.info("控制通道已打开：%s", self.path)
        return True

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            try:
                req = json.loads(line or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("请求必须是 JSON 对象")
                resp = await self.handler(req)
            except Exception as e:  # noqa: BLE001
                log.exception("控制命令出错")
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            writer.write(json.dumps(resp, ensure_ascii=False, default=str).encode() + b"\n")
            await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


class ControlUnavailable(Exception):
    pass


async def control_call(path: str | Path, cmd: str, timeout: float = 15.0, **args: Any) -> dict[str, Any]:
    """网页后台用：发一条命令，等回应。监控没在运行时抛 ControlUnavailable。"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path), limit=MAX_LINE), 3)
    except (FileNotFoundError, ConnectionRefusedError, PermissionError, TimeoutError, OSError) as e:
        raise ControlUnavailable(f"连不上监控进程（{type(e).__name__}）") from e
    try:
        writer.write(json.dumps({"cmd": cmd, **args}, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (TimeoutError, ConnectionError) as e:
        raise ControlUnavailable(f"监控进程没有回应（{type(e).__name__}）") from e
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if not line:
        raise ControlUnavailable("监控进程断开了连接")
    return json.loads(line)
