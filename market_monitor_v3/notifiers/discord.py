"""Discord 机器人渠道。

发送：REST API（/channels/{id}/messages），每条告警是一张彩色卡片（embed），绿色上涨、红色下跌；
延伸和联动是对原消息的回复，事件结束时编辑原消息；👍/👎 是消息下面的按钮。
接收：连 Discord Gateway（出站 WebSocket，不开端口），只处理按钮点击和斜杠命令
（/status /params /mutes /report /mute /unmute /help）。

需要的权限：在频道里 View Channel、Send Messages、Embed Links、Read Message History。
Gateway 只用 GUILDS 意图，不需要任何特权意图（不读消息内容）。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..render import to_plain
from . import DeliveryResult

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
USER_AGENT = "DiscordBot (https://github.com/lu1zgg/Trafast, 3.0)"

COLOR_UP = 0x2ECC71
COLOR_DOWN = 0xE74C3C
COLOR_NEUTRAL = 0x5865F2
EMBED_MAX = 4096
CONTENT_MAX = 2000
FLAG_SUPPRESS_NOTIFICATIONS = 1 << 12
FLAG_EPHEMERAL = 1 << 6
INTENT_GUILDS = 1

# 错误码：https://discord.com/developers/docs/topics/opcodes-and-status-codes#json
ERR_UNKNOWN_CHANNEL = 10003
ERR_UNKNOWN_MESSAGE = 10008
ERR_MISSING_ACCESS = 50001
ERR_MISSING_PERMISSIONS = 50013

# ---- HTML（Telegram 子集）→ Discord Markdown ----
_TAG_RE = re.compile(r"<(/?)(b|strong|i|em|u|s|code|pre|a)(\s+href=\"([^\"]*)\")?\s*>", re.I)
_MD_SPECIAL = re.compile(r"([\\*_~`|])")
_LINE_START = re.compile(r"^([#>\-+]|\d+\.)", re.M)
_MARKS = {"b": "**", "strong": "**", "i": "*", "em": "*", "u": "__", "s": "~~", "code": "`", "pre": "```"}


def _escape_md(s: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", s)


def html_to_markdown(text: str) -> str:
    """把告警用的 HTML 子集转成 Discord Markdown，正文里的 Markdown 特殊字符先转义。"""
    out: list[str] = []
    pos = 0
    link_stack: list[str] = []
    for m in _TAG_RE.finditer(text):
        out.append(_escape_md(html.unescape(_strip_other_tags(text[pos : m.start()]))))
        closing, tag, href = m.group(1) == "/", m.group(2).lower(), m.group(4)
        if tag == "a":
            if closing:
                out.append(f"]({link_stack.pop() if link_stack else ''})")
            else:
                link_stack.append(html.unescape(href or ""))
                out.append("[")
        else:
            out.append(_MARKS[tag])
        pos = m.end()
    out.append(_escape_md(html.unescape(_strip_other_tags(text[pos:]))))
    md = "".join(out)
    # 行首的 # > - 1. 在 Discord 里会变成标题、引用、列表，转义掉
    return _LINE_START.sub(r"\\\1", md)


_OTHER_TAG = re.compile(r"<[^>]+>")


def _strip_other_tags(s: str) -> str:
    return _OTHER_TAG.sub("", s)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ---- REST 客户端 ----
def classify(status: int, data: Any, kind: str = "send", retry_after_header: str | None = None) -> DeliveryResult:
    if 200 <= status < 300:
        mid = data.get("id") if isinstance(data, dict) else None
        return DeliveryResult("ok", ext_id=str(mid) if mid is not None else None, code=status)
    d = data if isinstance(data, dict) else {}
    code = int(d.get("code") or 0)
    desc = str(d.get("message") or f"HTTP {status}")
    if status == 429:
        retry = d.get("retry_after") or retry_after_header or 5
        try:
            wait = float(retry)
        except (TypeError, ValueError):
            wait = 5.0
        return DeliveryResult("rate", code=status, description=desc, retry_after=wait)
    if status >= 500:
        return DeliveryResult("server", code=status, description=desc)
    if code == ERR_UNKNOWN_MESSAGE:
        return DeliveryResult("edit_missing" if kind == "edit" else "parent_missing", code=status, description=desc)
    if status in (401, 403) or code in (ERR_UNKNOWN_CHANNEL, ERR_MISSING_ACCESS, ERR_MISSING_PERMISSIONS):
        return DeliveryResult("auth", code=status, description=desc, extra={"discord_code": code})
    return DeliveryResult("bad_request", code=status, description=f"{desc}（{code}）" if code else desc)


class DiscordClient:
    def __init__(
        self,
        token: str,
        api_base: str = API_BASE,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._token = token
        self.base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=5.0),
            transport=transport,
            headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
        )

    def redact(self, s: str) -> str:
        return s.replace(self._token, "***") if self._token else s

    async def request(self, method: str, path: str, payload: dict[str, Any] | list[Any] | None = None, kind: str = "send") -> tuple[DeliveryResult, Any]:
        try:
            r = await self._client.request(method, self.base + path, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            return DeliveryResult("network", description=f"{type(e).__name__}: {self.redact(str(e))}"), None
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError) as e:
            return DeliveryResult("timeout", description=f"{type(e).__name__}: {self.redact(str(e))}"), None
        except httpx.HTTPError as e:
            return DeliveryResult("network", description=f"{type(e).__name__}: {self.redact(str(e))}"), None
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = None
        return classify(r.status_code, data, kind, r.headers.get("Retry-After")), data

    async def aclose(self) -> None:
        await self._client.aclose()


def buttons_to_components(buttons: list[list[dict[str, str]]] | None) -> list[dict[str, Any]]:
    """Telegram 的 inline_keyboard 转成 Discord 的按钮行。callback_data 原样当 custom_id。"""
    rows = []
    for row in buttons or []:
        comps = [
            {"type": 2, "style": 2, "label": btn.get("text", "")[:80], "custom_id": btn["callback_data"][:100]}
            for btn in row[:5]
            if btn.get("callback_data")
        ]
        if comps:
            rows.append({"type": 1, "components": comps})
    return rows[:5]


class DiscordNotifier:
    name = "discord"
    label = "Discord"

    def __init__(self, client: DiscordClient, channel_id: str | int):
        self.client = client
        self.channel_id = str(channel_id)

    def body(self, row: dict[str, Any]) -> dict[str, Any]:
        text = row["text"]
        md = _escape_md(to_plain(text)) if row["plain"] else html_to_markdown(text)
        direction = row.get("direction") or 0
        color = COLOR_UP if direction > 0 else COLOR_DOWN if direction < 0 else COLOR_NEUTRAL
        body: dict[str, Any] = {
            "embeds": [{"description": _truncate(md, EMBED_MAX), "color": color}],
            "allowed_mentions": {"parse": []},
        }
        buttons = json.loads(row["buttons"]) if row.get("buttons") else None
        body["components"] = buttons_to_components(buttons)
        return body

    async def deliver(self, row: dict[str, Any]) -> DeliveryResult:
        body = self.body(row)
        if row["kind"] == "edit":
            target = row.get("target_ext")
            if not target:
                return DeliveryResult("edit_missing", description="编辑缺少目标")
            res, _ = await self.client.request("PATCH", f"/channels/{self.channel_id}/messages/{target}", body, kind="edit")
            return res
        if row.get("silent"):
            body["flags"] = FLAG_SUPPRESS_NOTIFICATIONS
        target = row.get("target_ext")
        if target:
            body["message_reference"] = {"message_id": str(target), "fail_if_not_exists": False}
        res, _ = await self.client.request("POST", f"/channels/{self.channel_id}/messages", body)
        return res

    async def probe(self) -> bool:
        res, _ = await self.client.request("GET", f"/channels/{self.channel_id}", kind="probe")
        return res.ok

    async def check(self) -> tuple[bool, str]:
        """连接自检：机器人身份 + 频道可见。返回 (是否正常, 说明)。"""
        me, data = await self.client.request("GET", "/users/@me", kind="probe")
        if not me.ok:
            return False, f"token 无效或网络不通：{me.kind} {me.description}"
        name = (data or {}).get("username", "?")
        ch, cdata = await self.client.request("GET", f"/channels/{self.channel_id}", kind="probe")
        if not ch.ok:
            return False, f"机器人 {name} 看不到频道 {self.channel_id}：{ch.description}。确认已邀请进服务器并有频道权限。"
        return True, f"机器人 {name}，频道 #{(cdata or {}).get('name', self.channel_id)}"

    def redact(self, s: str) -> str:
        return self.client.redact(s)

    async def aclose(self) -> None:
        await self.client.aclose()


# ---- Gateway：按钮和斜杠命令 ----
SLASH_COMMANDS: list[dict[str, Any]] = [
    {"name": "status", "description": "运行状态、数据源、进行中的异动"},
    {"name": "params", "description": "当前触发门槛"},
    {"name": "mutes", "description": "当前静音列表"},
    {"name": "report", "description": "立即生成最近 24 小时日报"},
    {"name": "stats", "description": "异动统计：最近 30 分钟和今天的涨跌异动数和多空比"},
    {"name": "help", "description": "命令说明"},
    {
        "name": "trend",
        "description": "趋势状态；带代码看详情和支撑压力位",
        "options": [{"type": 3, "name": "symbol", "description": "代码，例如 NVDA、BTC（不填看全部）", "required": False}],
    },
    {
        "name": "mute",
        "description": "静音一个标的",
        "options": [
            {"type": 3, "name": "symbol", "description": "代码，例如 MU、BTC、XAU；all 表示全部；美股 / 加密 / 大宗商品 表示整个市场", "required": True},
            {"type": 3, "name": "duration", "description": "时长，例如 30m、2h、1d（默认 1h，最长 7d）", "required": False},
        ],
    },
    {
        "name": "unmute",
        "description": "取消静音",
        "options": [{"type": 3, "name": "symbol", "description": "代码，或 all", "required": True}],
    },
    {
        "name": "types",
        "description": "异动类型开关",
        "options": [{"type": 3, "name": "args", "description": "例如 off 急拉急跌 NVDA；不填看当前状态", "required": False}],
    },
    {
        "name": "alert",
        "description": "添加自定义提醒",
        "options": [{"type": 3, "name": "rule", "description": "例如 NVDA > 140、BTC 5m -2%、NVDA 当日 5%", "required": True}],
    },
    {"name": "alerts", "description": "自定义提醒列表"},
    {
        "name": "unalert",
        "description": "删除自定义提醒",
        "options": [{"type": 3, "name": "id", "description": "编号，或 all", "required": True}],
    },
]

CommandFn = Callable[[str, list[str], int | None, str], Awaitable[str | None]]
FeedbackFn = Callable[[str, int, int], Awaitable[None]]
OpsFn = Callable[[str, str], Awaitable[None]]


class GatewayFatal(Exception):
    pass


# 这些关闭码说明配置有问题，重连也没用
FATAL_CLOSE = {4004: "token 无效", 4010: "分片配置无效", 4011: "需要分片", 4012: "API 版本无效", 4013: "意图无效", 4014: "意图未开通"}


class DiscordGateway:
    def __init__(
        self,
        token: str,
        client: DiscordClient,
        channel_id: str | int,
        on_command: CommandFn,
        on_feedback: FeedbackFn,
        ops: OpsFn,
        commands_enabled: bool = True,
        feedback_enabled: bool = True,
        ws_connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._token = token
        self.client = client
        self.channel_id = str(channel_id)
        self.on_command = on_command
        self.on_feedback = on_feedback
        self.ops = ops
        self.commands_enabled = commands_enabled
        self.feedback_enabled = feedback_enabled
        if ws_connect is None:
            from websockets.asyncio.client import connect as ws_connect  # noqa: N813
        self.ws_connect = ws_connect
        self.clock = clock
        self.seq: int | None = None
        self.application_id: str | None = None
        self.connected = False
        self._commands_registered = False
        self._acked = True
        self._register_task: asyncio.Task[None] | None = None

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 5.0
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = 5.0
            except GatewayFatal as e:
                log.error("Discord Gateway 停止：%s", e)
                await self.ops("discord-gateway-fatal", f"Discord 机器人收不到按钮和命令：{e}。推送不受影响。")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Discord Gateway 断开：%s", self.client.redact(f"{type(e).__name__}: {e}"))
            self.connected = False
            try:
                await asyncio.wait_for(stop.wait(), backoff + random.uniform(0, 2))
            except TimeoutError:
                pass
            backoff = min(300.0, backoff * 2)

    async def _session(self, stop: asyncio.Event) -> None:
        async with self.ws_connect(GATEWAY_URL, open_timeout=20, max_size=2**22, close_timeout=3) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if hello.get("op") != 10:
                raise RuntimeError(f"预期 Hello，收到 op={hello.get('op')}")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            await ws.send(json.dumps(self.identify_payload()))
            hb = asyncio.create_task(self._heartbeat(ws, interval))
            stop_wait = asyncio.create_task(stop.wait())
            try:
                while not stop.is_set():
                    recv = asyncio.create_task(ws.recv())
                    done, _ = await asyncio.wait({recv, stop_wait, hb}, return_when=asyncio.FIRST_COMPLETED)
                    if hb in done:
                        recv.cancel()
                        raise RuntimeError("心跳没有回应，重连")
                    if recv not in done:
                        recv.cancel()
                        return
                    try:
                        raw = recv.result()
                    except Exception as e:  # noqa: BLE001
                        code = getattr(getattr(e, "rcvd", None), "code", None)
                        if code in FATAL_CLOSE:
                            raise GatewayFatal(f"{FATAL_CLOSE[code]}（关闭码 {code}）") from e
                        raise
                    if await self.handle_frame(ws, json.loads(raw)) == "reconnect":
                        return
            finally:
                hb.cancel()
                stop_wait.cancel()

    def identify_payload(self) -> dict[str, Any]:
        return {
            "op": 2,
            "d": {
                "token": self._token,
                "intents": INTENT_GUILDS,
                "properties": {"os": "linux", "browser": "trafast", "device": "trafast"},
            },
        }

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        await asyncio.sleep(interval * random.random())
        self._acked = True
        while True:
            if not self._acked:
                return  # 上一个心跳没收到确认：连接已死
            self._acked = False
            await ws.send(json.dumps({"op": 1, "d": self.seq}))
            await asyncio.sleep(interval)

    async def handle_frame(self, ws: Any, msg: dict[str, Any]) -> str | None:
        op = msg.get("op")
        if msg.get("s") is not None:
            self.seq = msg["s"]
        if op == 11:
            self._acked = True
        elif op == 1:
            await ws.send(json.dumps({"op": 1, "d": self.seq}))
        elif op == 7:
            return "reconnect"
        elif op == 9:
            await asyncio.sleep(random.uniform(1, 5))
            return "reconnect"
        elif op == 0:
            t, d = msg.get("t"), msg.get("d") or {}
            if t == "READY":
                self.connected = True
                self.application_id = str((d.get("application") or {}).get("id") or "") or None
                log.info("Discord 机器人已连接：%s", (d.get("user") or {}).get("username"))
                if self.commands_enabled and not self._commands_registered:
                    self._register_task = asyncio.create_task(self.register_commands())
            elif t == "INTERACTION_CREATE":
                try:
                    await self.on_interaction(d)
                except Exception:
                    log.exception("处理 Discord 交互出错")
        return None

    async def register_commands(self) -> None:
        """把斜杠命令注册到频道所在的服务器（服务器级命令立即生效）。"""
        if not self.application_id:
            return
        res, data = await self.client.request("GET", f"/channels/{self.channel_id}", kind="probe")
        guild_id = (data or {}).get("guild_id") if res.ok else None
        if not guild_id:
            log.warning("Discord：取不到频道所在服务器，斜杠命令未注册（%s）", res.description)
            return
        res, _ = await self.client.request(
            "PUT", f"/applications/{self.application_id}/guilds/{guild_id}/commands", SLASH_COMMANDS, kind="probe"
        )
        if res.ok:
            self._commands_registered = True
            log.info("Discord 斜杠命令已注册（%d 个）", len(SLASH_COMMANDS))
        else:
            log.warning("Discord 斜杠命令注册失败：%s %s", res.kind, res.description)

    async def on_interaction(self, d: dict[str, Any]) -> None:
        itype = d.get("type")
        iid, itoken = d.get("id"), d.get("token")
        user = (d.get("member") or {}).get("user") or d.get("user") or {}
        user_id = int(user.get("id") or 0) or None
        who = user.get("username") or str(user_id)
        same_channel = str(d.get("channel_id") or "") == self.channel_id
        if itype == 3:  # 按钮
            data = (d.get("data") or {}).get("custom_id") or ""
            text = "无效"
            if same_channel and self.feedback_enabled and data.startswith("fb|"):
                try:
                    _, root_key, val = data.split("|", 2)
                    value = 1 if val == "1" else 0
                    await self.on_feedback(root_key, user_id or 0, value)
                    text = "已记录：有用 👍" if value else "已记录：没用 👎"
                except ValueError:
                    pass
            await self._respond(iid, itoken, text)
        elif itype == 2:  # 斜杠命令
            data = d.get("data") or {}
            name = str(data.get("name") or "")
            opts = {o.get("name"): str(o.get("value")) for o in data.get("options") or []}
            if not same_channel or not self.commands_enabled:
                await self._respond(iid, itoken, "这个频道不接收命令。")
                return
            args = [v for k in ("symbol", "duration") if (v := opts.get(k))]
            for k in ("args", "rule", "id"):
                if opts.get(k):
                    args += opts[k].split()
            # 先回一个“正在处理”，3 秒内必须响应；结果再编辑进去
            await self.client.request(
                "POST", f"/interactions/{iid}/{itoken}/callback", {"type": 5}, kind="probe"
            )
            reply = await self.on_command(name, args, user_id, who)
            content = _truncate(html_to_markdown(reply or "未知命令"), CONTENT_MAX)
            if self.application_id:
                await self.client.request(
                    "PATCH",
                    f"/webhooks/{self.application_id}/{itoken}/messages/@original",
                    {"content": content, "allowed_mentions": {"parse": []}},
                    kind="probe",
                )

    async def _respond(self, iid: Any, itoken: Any, text: str) -> None:
        await self.client.request(
            "POST",
            f"/interactions/{iid}/{itoken}/callback",
            {"type": 4, "data": {"content": text, "flags": FLAG_EPHEMERAL}},
            kind="probe",
        )
