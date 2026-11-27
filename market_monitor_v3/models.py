"""各模块共用的数据结构。时间一律用 epoch 秒；K 线的 ts 是这一分钟的开始时间。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
LEVEL_NAMES = {"L0": "记录", "L1": "异动", "L2": "显著", "L3": "剧烈"}


# 大宗商品：Binance USDT-M 的 TradFi 永续合约（黄金、白银、原油等）。代码和加密现货同一个格式（XAUUSDT），
# 只能靠名单区分：内置常见品种，配置里 [commodities] symbols 加载时会再登记进来。
COMMODITY_SYMBOLS: set[str] = {
    "XAUUSDT",  # 黄金
    "XAGUSDT",  # 白银
    "XPTUSDT",  # 铂金
    "XPDUSDT",  # 钯金
    "COPPERUSDT",  # 铜
    "CLUSDT",  # WTI 原油
    "BZUSDT",  # 布伦特原油
}
COMMODITY_NAMES = {
    "XAU": "黄金",
    "XAG": "白银",
    "XPT": "铂金",
    "XPD": "钯金",
    "COPPER": "铜",
    "CL": "WTI原油",
    "BZ": "布伦特原油",
}
MARKET_LABELS = {"US": "美股", "CRYPTO": "加密", "CMDTY": "大宗商品"}
MARKETS = ("US", "CRYPTO", "CMDTY")


# /mute 大宗商品 之类：整个市场静音的写法
MARKET_ALIASES = {
    "美股": "US",
    "US": "US",
    "STOCKS": "US",
    "加密": "CRYPTO",
    "加密货币": "CRYPTO",
    "CRYPTO": "CRYPTO",
    "大宗": "CMDTY",
    "大宗商品": "CMDTY",
    "商品": "CMDTY",
    "CMDTY": "CMDTY",
    "COMMODITIES": "CMDTY",
}


def target_label(target: str) -> str:
    """静音 / 类型开关目标的可读名字。"""
    if target == "*":
        return "全部"
    if target.startswith("market:"):
        return MARKET_LABELS.get(target[7:], target[7:])
    if target.startswith("group:"):
        return "分组 " + target[6:]
    return display_name(target)


def register_commodities(symbols: list[str]) -> None:
    COMMODITY_SYMBOLS.update(s.upper() for s in symbols)


def market_of(symbol: str) -> str:
    """美股代码形如 NVDA.US；大宗商品按名单（XAUUSDT 等 Binance 永续）；其余视为加密货币现货（BTCUSDT）。"""
    s = symbol.upper()
    if s.endswith(".US"):
        return "US"
    return "CMDTY" if s in COMMODITY_SYMBOLS else "CRYPTO"


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
    # V4：形态分类（patterns.classify），只用于命名和统计，不影响是否触发
    pattern: str = ""
    tags: list[str] = field(default_factory=list)
    reason: dict[str, float | None] = field(default_factory=dict)


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
    pattern: str = ""
    tags: list[str] = field(default_factory=list)

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
    pattern: str | None = None


@dataclass
class SignalEvent:
    """V4 结构化信号：引擎每次推送（首发、延伸、升级）和事件结束都产出一条。

    推送文本仍由 render 生成；这条记录给回放统计、以后的 /api/v1 和桌面版用，字段只增不改。
    """

    id: str  # {episode_id}:{seq}
    ts: int  # 对应 K 线的收盘时间
    kind: str  # open / extend / upgrade / end
    symbol: str
    market: str
    session: str
    direction: int
    level: str
    pattern: str
    tags: list[str]
    price: float
    anchor: float  # 起点价
    move: float  # 相对起点的对数位移（与方向同号为正）
    k: int
    score: float | None
    episode_id: str
    rule_version: str
    retained: float | None = None
    end_reason: str | None = None
    reason: dict[str, float | None] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


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
    market: str | None = None  # US / CRYPTO / TV（TradingView 转发），用于按渠道筛选
    direction: int = 0  # 涨跌方向，Discord 用来给卡片上色
    channel: str | None = None  # 只发到这个渠道（命令回复）；None = 按各渠道的订阅规则发


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
    # 新版卡片：每个标的一张，见 render.card_from。为空时按旧版字段（lead_html + members + end_lines）渲染，
    # 兼容升级前存进数据库的消息和“异动过多”摘要。
    cards: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""  # V4 分组联动标题

    def text(self) -> str:
        if self.cards:
            from .render import cards_text

            return cards_text(self.cards, self.title)
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
