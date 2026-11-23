"""Telegram 渠道：sendMessage / editMessageText，HTML 排版，告警下面带 👍/👎 按钮。"""
from __future__ import annotations

import json
from typing import Any

from ..telegram import TelegramClient, TgResult
from . import DeliveryResult


class ChatRef:
    """群组 ID 的可变引用：群升级为超级群或在后台改群时，发件和命令轮询一起切换。"""

    def __init__(self, chat_id: int | str):
        self.chat_id = chat_id


def to_delivery(res: TgResult) -> DeliveryResult:
    mid = res.message_id
    return DeliveryResult(
        kind=res.kind,
        ext_id=str(mid) if mid is not None else None,
        code=res.code,
        description=res.description,
        retry_after=res.retry_after,
        migrate_to=res.migrate_to,
    )


class TelegramNotifier:
    name = "telegram"
    label = "Telegram"

    def __init__(self, tg: TelegramClient, chat: ChatRef):
        self.tg = tg
        self.chat = chat

    def payload(self, row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
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

    async def deliver(self, row: dict[str, Any]) -> DeliveryResult:
        method, payload = self.payload(row)
        return to_delivery(await self.tg.call(method, payload))

    async def probe(self) -> bool:
        return (await self.tg.call("getChat", {"chat_id": self.chat.chat_id})).ok

    def migrate(self, new_chat_id: int) -> str:
        old = self.chat.chat_id
        self.chat.chat_id = new_chat_id
        return f"群组已升级为超级群，已自动从 {old} 切换到 {new_chat_id}。请在后台或 config.toml 里把 chat_id 改成新值。"

    def redact(self, s: str) -> str:
        return self.tg.redact(s)

    async def aclose(self) -> None:
        await self.tg.aclose()
