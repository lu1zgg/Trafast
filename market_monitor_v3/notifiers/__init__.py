"""推送渠道插件。

每个渠道实现 Notifier：把发件箱里的一行投递出去，并把各家的报错归成同一套类别，
重试、暂停、过期这些策略由 outbox.OutboxSender 统一处理。

发件箱按渠道分行：一条逻辑消息给每个启用的渠道各写一行。Telegram 的行沿用原来的键，
其他渠道的键加后缀 “@渠道名”，回复和编辑只在原消息也进了该渠道时才写入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

PRIMARY = "telegram"
LEVELS = ("L1", "L2", "L3")


def chan_key(key: str, channel: str) -> str:
    return key if channel == PRIMARY else f"{key}@{channel}"


def base_key(key: str) -> str:
    return key.split("@", 1)[0]


@dataclass
class DeliveryResult:
    """投递结果，各渠道统一的类别：

    ok / not_modified：成功（ext_id 是该渠道的消息编号）
    parse：格式错误，改纯文本重发一次
    parent_missing：要回复的原消息不在了，改成独立消息
    edit_missing：要编辑的消息不在了，放弃这次编辑
    migrated：群组迁移（Telegram 普通群升级超级群）
    auth：token 失效、没有权限、频道不存在，整个渠道暂停并定时探测
    rate：限流，按 retry_after 等待
    server / network：请求没送到或对方出错，退避重试
    timeout：请求可能已经送到
    bad_request：其他 4xx，这一条放弃
    """

    kind: str
    ext_id: str | None = None
    code: int | None = None
    description: str = ""
    retry_after: float | None = None
    migrate_to: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind in ("ok", "not_modified")


@dataclass
class Route:
    """一个渠道收哪些消息。"""

    alerts: bool = True
    min_level: str = "L1"
    markets: tuple[str, ...] = ("US", "CRYPTO")
    tradingview: bool = True
    ops: bool = True
    reports: bool = True

    def accepts(self, kind: str, key: str, level: str | None, market: str | None) -> bool:
        if kind == "ops":
            return self.reports if key.startswith("report:") else self.ops
        if kind != "send":
            return True  # 回复、编辑跟随原消息，由发件箱判断
        if market == "TV":
            return self.tradingview
        if not self.alerts:
            return False
        if market and market not in self.markets:
            return False
        if level in LEVELS and LEVELS.index(level) < LEVELS.index(self.min_level):
            return False
        return True


class Notifier(Protocol):
    name: str  # 渠道名，也是发件箱 channel 列的值
    label: str  # 显示名，例如 Telegram、Discord

    async def deliver(self, row: dict[str, Any]) -> DeliveryResult: ...

    async def probe(self) -> bool: ...

    def redact(self, s: str) -> str: ...

    async def aclose(self) -> None: ...
