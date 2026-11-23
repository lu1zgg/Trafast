"""发件箱发送器：先写库再发送，按 Telegram 错误分别处理。

原则：每条告警要么发出去，要么让 louis 知道它为什么没发出去（进日报和运维消息）。
- 400 只影响这一条：格式错误改纯文本重发一次，仍失败就丢弃；其他消息照常。
- 回复的原消息不存在：改成独立消息（另外请求里已带 allow_sending_without_reply）。
- 群升级为超级群：按 migrate_to_chat_id 自动切换。
- 401/403/chat not found：全局暂停，每 5 分钟用 getChat 探测，恢复后自动继续。
- 429：按 retry_after 等待。
- 5xx / 网络错误（请求没送到）：1、2、4、8… 秒退避重试，直到过期。
- 超时（请求可能送到了）：L2、L3 重发一次并标注“可能重复”；L1 不重发。
- 同一个群同一时间只发一个请求，回复和编辑一定在原消息之后。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from .models import OutMsg
from .render import to_plain
from .store import Store
from .telegram import TelegramClient

log = logging.getLogger(__name__)

MAYBE_DUP_NOTE = "（网络超时后重发，可能重复）"


class ChatRef:
    def __init__(self, chat_id: int | str):
        self.chat_id = chat_id


class OutboxSender:
    def __init__(
        self,
        store: Store,
        tg: TelegramClient,
        chat: ChatRef,
        clock: Callable[[], float] = time.time,
        on_auth_change: Callable[[bool], Awaitable[None] | None] | None = None,
        probe_interval_s: float = 300.0,
    ):
        self.store = store
        self.tg = tg
        self.chat = chat
        self.clock = clock
        self.on_auth_change = on_auth_change
        self.probe_interval_s = probe_interval_s
        self.wake = asyncio.Event()
        self.paused = False
        self.paused_reason = ""
        self.stats: Counter[str] = Counter()
        self.last_ok_at: float | None = None

    def notify(self) -> None:
        self.wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if self.paused:
                    await self._probe(stop)
                    continue
                row = await self.store.call(Store.outbox_next, self.clock())
                if row is None:
                    self.wake.clear()
                    try:
                        await asyncio.wait_for(self.wake.wait(), 1.0)
                    except TimeoutError:
                        pass
                    continue
                await self.handle(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("发件箱循环异常，1 秒后继续")
                await asyncio.sleep(1)

    def _payload(self, row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if row["kind"] == "edit":
            method = "editMessageText"
            payload: dict[str, Any] = {
                "chat_id": self.chat.chat_id,
                "message_id": row["target_tg"],
                "text": row["text"],
                "link_preview_options": {"is_disabled": True},
            }
        else:
            method = "sendMessage"
            payload = {
                "chat_id": self.chat.chat_id,
                "text": row["text"],
                "disable_notification": bool(row["silent"]),
                "link_preview_options": {"is_disabled": True},
            }
            reply_to = row.get("target_tg") or row.get("reply_to_tg")
            if reply_to:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if not row["plain"]:
            payload["parse_mode"] = "HTML"
        if row["buttons"]:
            payload["reply_markup"] = {"inline_keyboard": json.loads(row["buttons"])}
        return method, payload

    async def handle(self, row: dict[str, Any]) -> None:
        method, payload = self._payload(row)
        res = await self.tg.call(method, payload)
        now = self.clock()
        rid = row["id"]
        k = res.kind
        self.stats[k] += 1
        if res.ok:
            self.last_ok_at = now
            await self.store.call(Store.outbox_mark, rid, "sent", tg_message_id=res.message_id, attempts_inc=1)
            return
        log.warning("Telegram %s 失败：%s %s（消息 %s）", method, k, res.description, row["key"])
        if k == "parse" and not row["plain"]:
            await self.store.call(
                Store.outbox_mark, rid, "pending", text=to_plain(row["text"]), plain=True, next_at=now, attempts_inc=1
            )
        elif k == "parent_missing":
            await self.store.call(_detach, rid)
        elif k == "edit_missing":
            await self.store.call(Store.outbox_mark, rid, "dropped", error=res.description, attempts_inc=1)
        elif k == "migrated" and res.migrate_to:
            old = self.chat.chat_id
            self.chat.chat_id = res.migrate_to
            await self.store.call(Store.kv_set, "chat_id_override", str(res.migrate_to))
            await self.store.call(Store.outbox_mark, rid, "pending", next_at=now)
            await self.store.call(
                Store.enqueue,
                OutMsg(
                    key=f"ops:migrate:{res.migrate_to}",
                    kind="ops",
                    text=f"⚙️ 运维：群组已升级为超级群，已自动从 {old} 切换到 {res.migrate_to}。请把 config.toml 里的 chat_id 改成新值。",
                    created_at=now,
                    expires_at=now + 3600,
                ),
            )
        elif k == "auth":
            self.paused = True
            self.paused_reason = f"{res.code} {res.description}"
            await self.store.call(Store.outbox_mark, rid, "pending", error=res.description, next_at=now, attempts_inc=1)
            await self._auth_changed(False)
        elif k == "rate":
            wait = max(1.0, res.retry_after or 5.0)
            await self.store.call(Store.outbox_mark, rid, "pending", next_at=now + wait, attempts_inc=1)
            await asyncio.sleep(wait)
        elif k in ("server", "network"):
            backoff = min(60.0, 2.0 ** row["attempts"])
            await self.store.call(
                Store.outbox_mark, rid, "pending", error=res.description, next_at=now + backoff, attempts_inc=1
            )
        elif k == "timeout":
            if row["kind"] == "edit":
                backoff = min(60.0, 2.0 ** row["attempts"])
                await self.store.call(Store.outbox_mark, rid, "pending", next_at=now + backoff, attempts_inc=1)
            elif row["level"] in ("L2", "L3") and not row["maybe_dup"]:
                note = MAYBE_DUP_NOTE if row["plain"] else f"<i>{MAYBE_DUP_NOTE}</i>"
                await self.store.call(
                    Store.outbox_mark,
                    rid,
                    "pending",
                    text=row["text"] + "\n" + note,
                    maybe_dup=True,
                    next_at=now,
                    attempts_inc=1,
                )
            else:
                await self.store.call(Store.outbox_mark, rid, "unknown", error="超时，可能已送达，不重发", attempts_inc=1)
        else:
            await self.store.call(Store.outbox_mark, rid, "failed", error=res.description, attempts_inc=1)

    async def _probe(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), self.probe_interval_s)
            return
        except TimeoutError:
            pass
        res = await self.tg.call("getChat", {"chat_id": self.chat.chat_id})
        if res.ok:
            self.paused = False
            self.paused_reason = ""
            log.warning("Telegram 投递恢复")
            await self._auth_changed(True)
            now = self.clock()
            await self.store.call(
                Store.enqueue,
                OutMsg(
                    key=f"ops:tg-recovered:{int(now)}",
                    kind="ops",
                    text="⚙️ 运维：Telegram 投递已恢复。暂停期间过期的告警会记入日报。",
                    created_at=now,
                    expires_at=now + 3600,
                ),
            )

    async def _auth_changed(self, ok: bool) -> None:
        if self.on_auth_change is None:
            return
        r = self.on_auth_change(ok)
        if asyncio.iscoroutine(r):
            await r


def _detach(store: Store, row_id: int) -> None:
    with store.c:
        store.c.execute(
            "UPDATE outbox SET parent_key=NULL, reply_to_tg=NULL, kind=CASE kind WHEN 'reply' THEN 'send' ELSE kind END, "
            "attempts=attempts+1, next_at=? WHERE id=?",
            (time.time(), row_id),
        )
