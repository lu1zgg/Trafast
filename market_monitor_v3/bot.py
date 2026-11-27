"""Telegram Bot 接收：命令和反馈按钮（getUpdates 长轮询，不开任何入站端口）。

只处理配置里那个群的消息；其他聊天一律忽略。
修改类命令（/mute、/unmute、/types on|off、/alert、/unalert）在配置了 admin_user_ids 时只接受这些用户。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

from .models import MARKET_ALIASES, OutMsg, target_label
from .notifiers.telegram import ChatRef
from .store import Store
from .telegram import TelegramClient

log = logging.getLogger(__name__)

HELP = """<b>可用命令</b>
/status 运行状态、数据源、进行中的异动
/mute 代码 [时长]  静音一个标的，例如 /mute MU 2h、/mute XAU 30m（默认 1h，最长 7d；代码写 all 表示全部，写 美股 / 加密 / 大宗商品 表示整个市场）
/unmute 代码|all  取消静音
/mutes 当前静音列表
/params 当前触发门槛
/types 异动类型开关；/types off 急拉急跌 [代码|分组]，/types on 急拉急跌 [代码|分组]
/alert 自定义提醒，例如 /alert NVDA &gt; 140、/alert BTC 5m -2%、/alert NVDA 当日 5%（/alert 看完整写法）
/alerts 自定义提醒列表；/unalert 编号|all 删除
/report 立即生成最近 24 小时日报
/trend [代码] 趋势状态；带代码看详情和支撑压力位，例如 /trend NVDA
/stats 异动统计：最近 30 分钟和今天各市场、各分组的涨跌异动数和多空比
/help 本说明
告警和趋势消息下面的 👍/👎 用来标记这条有没有用，会进日报，帮助校准。"""

_DUR = re.compile(r"^(\d+)\s*(m|min|h|d)?$", re.I)


def parse_duration(s: str | None, default_s: int = 3600, max_s: int = 7 * 86400) -> int | None:
    if not s:
        return default_s
    m = _DUR.match(s.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "m").lower()
    mult = {"m": 60, "min": 60, "h": 3600, "d": 86400}[unit]
    return max(60, min(max_s, n * mult))


class BotHooks(Protocol):
    async def status_text(self) -> str: ...
    def params_text(self) -> str: ...
    def mutes_text(self) -> str: ...
    def resolve_symbol(self, s: str) -> str | None: ...
    async def mute(self, target: str, until: float, by: str) -> None: ...
    async def unmute(self, target: str | None) -> int: ...
    async def report_text(self) -> str: ...
    async def trend_text(self, args: list[str]) -> str: ...
    async def stats_text(self) -> str: ...
    def types_text(self) -> str: ...
    def resolve_target(self, s: str) -> str | None: ...
    async def set_type(self, target: str, pattern: str, off: bool) -> None: ...
    def alerts_text(self) -> str: ...
    async def add_alert(self, args: list[str], by: str) -> str: ...
    async def remove_alert(self, rule_id: int | None) -> int: ...


async def run_command(
    hooks: BotHooks,
    cmd: str,
    args: list[str],
    user_id: int | None,
    who: str,
    admins: set[int],
    clock: Callable[[], float] = time.time,
) -> str | None:
    """Telegram 和 Discord 共用的命令处理。返回 HTML 文本；未知命令返回 None。"""
    if cmd in ("help", "start"):
        return HELP
    if cmd == "status":
        return await hooks.status_text()
    if cmd == "params":
        return hooks.params_text()
    if cmd == "mutes":
        return hooks.mutes_text()
    if cmd == "report":
        return await hooks.report_text()
    if cmd == "trend":
        return await hooks.trend_text(args)
    if cmd == "stats":
        return await hooks.stats_text()
    if cmd == "types":
        if not args:
            return hooks.types_text()
        if admins and user_id not in admins:
            return "没有权限：只有配置里的管理员可以开关类型。"
        from .patterns import PATTERN_LABELS, parse_pattern

        action = args[0].lower()
        if action not in ("on", "off", "开", "关") or len(args) < 2:
            return "用法：/types off 急拉急跌 [代码|分组]、/types on 急拉急跌 [代码|分组]；不带参数看当前状态。"
        pattern = parse_pattern(args[1])
        if pattern is None:
            return f"没有这个类型：{args[1]}。可用：{'、'.join(PATTERN_LABELS.values())}"
        target = hooks.resolve_target(args[2]) if len(args) > 2 else "*"
        if target is None:
            return f"没找到标的或分组 {args[2]}。"
        off = action in ("off", "关")
        await hooks.set_type(target, pattern, off)
        where = "全部标的" if target == "*" else target.replace("group:", "分组 ")
        return f"已{'关闭' if off else '打开'}{where}的「{PATTERN_LABELS[pattern]}」。\n" + hooks.types_text()
    if cmd == "alerts":
        return hooks.alerts_text()
    if cmd in ("alert", "unalert"):
        if admins and user_id not in admins:
            return "没有权限：只有配置里的管理员可以设置自定义提醒。"
        if cmd == "alert":
            return await hooks.add_alert(args, who)
        if not args:
            return "用法：/unalert 编号，或 /unalert all 删除全部。"
        if args[0].lower() in ("all", "全部"):
            n = await hooks.remove_alert(None)
            return f"已删除全部 {n} 条自定义提醒。"
        try:
            rid = int(args[0].lstrip("#"))
        except ValueError:
            return "编号写得不对，/alerts 可以看编号。"
        return "已删除。" if await hooks.remove_alert(rid) else f"没有编号 {rid} 的提醒。"
    if cmd in ("mute", "unmute"):
        if admins and user_id not in admins:
            return "没有权限：只有配置里的管理员可以静音或取消静音。"
        if not args:
            return f"用法：/{cmd} 代码" + (" [时长]，例如 /mute MU 2h" if cmd == "mute" else "|all")
        target_raw = args[0]
        if target_raw.lower() in ("all", "全部", "*"):
            target: str | None = "*"
        elif target_raw.upper() in MARKET_ALIASES:
            target = "market:" + MARKET_ALIASES[target_raw.upper()]
        else:
            target = hooks.resolve_symbol(target_raw)
            if target is None:
                return f"没找到标的 {target_raw}。"
        if cmd == "mute":
            dur = parse_duration(args[1] if len(args) > 1 else None)
            if dur is None:
                return "时长格式不对，例如 30m、2h、1d。"
            until = clock() + dur
            await hooks.mute(target, until, who)
            name = "全部标的" if target == "*" else target_label(target)
            return f"已静音 {name}，{_fmt_dur(dur)}。静音期间的异动只记录不推送，运维消息照常。"
        n = await hooks.unmute(None if target == "*" else target)
        if target == "*":
            return "已取消全部静音。" if n else "当前没有静音。"
        return "已取消静音。" if n else f"{target_label(target)} 没有在静音。"
    return None  # 未知命令不回应，避免和群里其他 bot 冲突


class BotPoller:
    def __init__(
        self,
        store: Store,
        tg: TelegramClient,
        chat: ChatRef,
        hooks: BotHooks,
        admin_user_ids: list[int],
        notify_sender: Callable[[], None],
        clock: Callable[[], float] = time.time,
        feedback_enabled: bool = True,
    ):
        self.store = store
        self.tg = tg
        self.chat = chat
        self.hooks = hooks
        self.admins = set(admin_user_ids)
        self.notify_sender = notify_sender
        self.clock = clock
        self.feedback_enabled = feedback_enabled
        self.offset: int | None = None
        self._conflict_reported = False

    async def run(self, stop: asyncio.Event) -> None:
        raw = await self.store.call(Store.kv_get, "tg_update_offset")
        self.offset = int(raw) if raw else None
        backoff = 1.0
        while not stop.is_set():
            payload: dict[str, Any] = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if self.offset is not None:
                payload["offset"] = self.offset
            res = await self.tg.call("getUpdates", payload, read_timeout=40)
            if not res.ok:
                if res.code == 409 and not self._conflict_reported:
                    self._conflict_reported = True
                    log.warning("getUpdates 冲突（409）：该 bot 设置了 webhook 或有另一个程序在轮询")
                    await self._ops(
                        "bot-conflict",
                        "⚙️ 运维：收不到命令。这个 bot 设置了 webhook，或者另一个程序（例如旧版 V2）正在用同一个 bot 轮询。"
                        "停掉另一个程序，或执行 deleteWebhook 后会自动恢复。",
                    )
                wait = 60.0 if res.code == 409 else backoff
                backoff = min(60.0, backoff * 2)
                try:
                    await asyncio.wait_for(stop.wait(), wait)
                except TimeoutError:
                    pass
                continue
            backoff = 1.0
            self._conflict_reported = False
            for u in res.result or []:
                uid = u.get("update_id")
                if uid is None:
                    continue
                self.offset = int(uid) + 1
                try:
                    await self.handle_update(u)
                except Exception:
                    log.exception("处理 Telegram 更新出错")
            if res.result:
                await self.store.call(Store.kv_set, "tg_update_offset", str(self.offset))

    def _same_chat(self, chat_id: Any) -> bool:
        return str(chat_id) == str(self.chat.chat_id)

    async def handle_update(self, u: dict[str, Any]) -> None:
        if "callback_query" in u:
            await self._on_callback(u["callback_query"])
            return
        msg = u.get("message")
        if not msg or not self._same_chat((msg.get("chat") or {}).get("id")):
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0][1:].split("@", 1)[0].lower()
        args = parts[1:]
        user = msg.get("from") or {}
        user_id = user.get("id")
        who = user.get("username") or str(user_id)
        reply = await self.dispatch(cmd, args, user_id, who)
        if reply is None:
            return
        now = self.clock()
        await self.store.call(
            Store.enqueue,
            OutMsg(
                key=f"cmd:{u.get('update_id')}",
                kind="cmd",
                text=reply,
                created_at=now,
                expires_at=now + 120,
                reply_to_tg=msg.get("message_id"),
                silent=True,
                channel="telegram",
            ),
        )
        await self.store.call(_log_control, now, who, text, reply[:200])
        self.notify_sender()

    async def dispatch(self, cmd: str, args: list[str], user_id: int | None, who: str) -> str | None:
        return await run_command(self.hooks, cmd, args, user_id, who, self.admins, self.clock)

    async def _on_callback(self, cq: dict[str, Any]) -> None:
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = "已记录"
        if not self._same_chat(chat_id) or not self.feedback_enabled:
            text = "无效"
        elif data.startswith("fb|"):
            try:
                _, root_key, val = data.split("|", 2)
                value = 1 if val == "1" else 0
                user_id = int((cq.get("from") or {}).get("id") or 0)
                await self.store.call(Store.feedback_set, root_key, user_id, value)
                text = "已记录：有用 👍" if value else "已记录：没用 👎"
            except ValueError:
                text = "无效"
        await self.tg.call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": text})

    async def _ops(self, key: str, text: str) -> None:
        now = self.clock()
        await self.store.call(
            Store.enqueue,
            OutMsg(key=f"ops:{key}:{int(now) // 3600}", kind="ops", text=text, created_at=now, expires_at=now + 3600),
        )
        self.notify_sender()


def _fmt_dur(s: int) -> str:
    if s % 86400 == 0:
        return f"{s // 86400} 天"
    if s % 3600 == 0:
        return f"{s // 3600} 小时"
    return f"{s // 60} 分钟"


def _log_control(store: Store, ts: float, source: str, command: str, result: str) -> None:
    with store.c:
        store.c.execute(
            "INSERT INTO control (ts, source, command, result) VALUES (?,?,?,?)", (ts, source, command, result)
        )
