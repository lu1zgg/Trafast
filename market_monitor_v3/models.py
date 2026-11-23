"""各模块共用的数据结构。时间一律用 epoch 秒；K 线的 ts 是这一分钟的开始时间。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
LEVEL_NAMES = {"L0": "记录", "L1": "异动", "L2": "显著", "L3": "剧烈"}


def market_of(symbol: str) -> str:
    """美股代码形如 NVDA.US，其余视为加密货币现货（BTCUSDT）。"""
    return "US" if symbol.upper().endswith(".US") else "CRYPTO"


def display_name(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith(".US"):
        return s[:-3]
    for quote in ("USDT", "USDC", "FDUSD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


@dataclass(slots=True)
class Bar:
    symbol: str
    ts: int  # 分钟开始时间（epoch 秒，整分钟）
    o: float
    h: float
    l: float
    c: float
    volume: float = 0.0
    turnover: float = 0.0  # 成交额：美股为美元，加密为 USDT
    session: str = ""  # PRE / REG / POST / ON / CWD / CWE / CLOSED
    source: str = ""
    synthetic: bool = False  # 无成交时补的平 K 线

    @property
    def close_ts(self) -> int:
        return self.ts + 60

    def same_values(self, other: "Bar") -> bool:
        return (
            self.c == other.c
            and self.o == other.o
            and self.h == other.h
            and self.l == other.l
            and self.turnover == other.turnover
            and self.synthetic == other.synthetic
        )


@dataclass(slots=True)
class WindowHit:
    k: int  # 窗口分钟数 1 / 5 / 15
    d: float  # 对数位移 ln(P_t / P_{t-k})
    p0: float  # 起点价 P_{t-k}
    anchor_close_ts: int  # 起点 K 线的收盘时间
    threshold: float | None  # max(地板, Z·σ)；None 表示基线未就绪
    floor: float
    sigma: float | None
    bypass: float
    by_z: bool
    by_bypass: bool
    score: float
    turnover: float
    turnover_ratio: float | None


@dataclass(slots=True)
class Candidate:
    symbol: str
    market: str
    bar_ts: int
    close_ts: int
    session: str
    price: float
    direction: int  # +1 上涨，-1 下跌
    hits: list[WindowHit]
    primary: WindowHit
    score: float
    fast: bool
    level: str
    warmup: bool = False


@dataclass(slots=True)
class Evaluation:
    candidate: Candidate | None = None
    thin_blocked: list[WindowHit] = field(default_factory=list)


@dataclass
class Episode:
    id: str
    symbol: str
    market: str
    direction: int
    session: str
    k: int
    anchor: float  # 起点价
    anchor_close_ts: int
    open_close_ts: int  # 首条消息对应 K 线的收盘时间
    first_d: float  # 触发时的对数位移
    level: str
    score: float
    retained: float | None
    extreme: float  # 相对起点的最大同向位移（对数，≥0）
    last_report: float  # 上一次推送时的位移
    last_msg_at: float
    last_extreme_at: int
    last_price: float
    replies: int = 0
    root_key: str | None = None
    state: str = "active"  # active / ended
    end_reason: str | None = None
    ended_at: int | None = None
    end_move: float | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Episode":
        return cls(**d)


@dataclass
class Decision:
    """每一次判定都落库（包括没推送的 L0），用于校准和日报。"""

    ts: int  # K 线收盘时间
    symbol: str
    session: str
    level: str
    outcome: str  # pushed / rejected / absorbed / muted / cooldown / thin / expired_pending / warmup
    k: int | None = None
    d: float | None = None
    threshold: float | None = None
    sigma: float | None = None
    floor: float | None = None
    bypass: float | None = None
    score: float | None = None
    retained: float | None = None
    turnover_ratio: float | None = None
    episode_id: str | None = None
    rule_version: str = ""


@dataclass
class OutMsg:
    key: str  # 去重键；同一个键只会发一次
    kind: str  # send / reply / edit / ops / cmd
    text: str
    created_at: float
    expires_at: float
    parent_key: str | None = None  # reply：回复哪条；edit：编辑哪条
    reply_to_tg: int | None = None  # 直接回复某条 Telegram 消息（命令回复用）
    level: str | None = None
    silent: bool = False
    buttons: list[list[dict[str, str]]] | None = None


@dataclass
class RootMessage:
    """一条首发消息（单标的或联动）的结构化内容，编辑时整条重新渲染。"""

    key: str
    market: str
    direction: int
    lead_html: str
    symbols: list[str]
    members: list[str] = field(default_factory=list)
    end_lines: list[str] = field(default_factory=list)
    buttons: list[list[dict[str, str]]] | None = None
    rev: int = 0

    def text(self) -> str:
        parts = [self.lead_html]
        if self.members:
            parts.append("同时：" + "，".join(self.members))
        parts.extend(self.end_lines)
        return "\n".join(parts)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "RootMessage":
        return cls(**d)
