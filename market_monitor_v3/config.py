"""配置：config.toml 经 pydantic 校验；三档预设；rule_version 自动计算。

密钥（Telegram token、长桥凭据、心跳地址）只从环境变量读，不进配置文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WINDOWS: tuple[int, int, int] = (1, 5, 15)
STOCK_SESSIONS = ("PRE", "REG", "POST", "ON")
CRYPTO_SESSIONS = ("CWD", "CWE")  # 加密货币：UTC 工作日 / 周末
SESSION_LABELS = {
    "PRE": "盘前",
    "REG": "盘中",
    "POST": "盘后",
    "ON": "夜盘",
    "CWD": "工作日",
    "CWE": "周末",
    "CLOSED": "休市",
}
RULE_MAJOR = "v4.0"

Triple = tuple[float, float, float]

# 地板（百分比），三档共用
STOCK_FLOORS_PCT: dict[str, Triple] = {
    "REG": (0.4, 0.8, 1.2),
    "PRE": (0.6, 1.2, 1.8),
    "POST": (0.6, 1.2, 1.8),
    "ON": (0.8, 1.5, 2.2),
}
CRYPTO_FLOORS_PCT: Triple = (0.5, 1.0, 1.5)

# 三档预设。灵敏档已包含 louis 2026-11-23 的决定：盘前、盘后、夜盘 15 分钟直通线 2.5%。
PRESETS: dict[str, dict] = {
    "sensitive": {
        "label": "灵敏",
        "stock_z": {"REG": 5, "PRE": 6, "POST": 6, "ON": 6},
        "stock_bypass": {
            "REG": (1.5, 2.5, 3.0),
            "PRE": (1.5, 2.5, 2.5),
            "POST": (1.5, 2.5, 2.5),
            "ON": (1.5, 2.5, 2.5),
        },
        "crypto_z": {"CWD": 5, "CWE": 6},
        "crypto_bypass": (1.5, 2.5, 3.5),
    },
    "balanced": {
        "label": "均衡",
        "stock_z": {"REG": 6, "PRE": 7, "POST": 7, "ON": 7},
        "stock_bypass": {s: (2.0, 2.5, 3.0) for s in STOCK_SESSIONS},
        "crypto_z": {"CWD": 6, "CWE": 7},
        "crypto_bypass": (1.5, 2.5, 3.5),
    },
    "conservative": {
        "label": "克制",
        "stock_z": {"REG": 7, "PRE": 8, "POST": 8, "ON": 8},
        "stock_bypass": {s: (2.0, 3.0, 4.0) for s in STOCK_SESSIONS},
        "crypto_z": {"CWD": 7, "CWE": 8},
        "crypto_bypass": (1.5, 2.5, 3.5),
    },
}

DEFAULT_STOCKS = [
    "NVDA.US",
    "GOOGL.US",
    "MU.US",
    "SNDK.US",
    "META.US",
    "STX.US",
    "INTC.US",
    "BE.US",
    "TSLA.US",
    "SPCX.US",
]
DEFAULT_CRYPTO = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_hhmm(v: str) -> str:
    if not _HHMM.match(v):
        raise ValueError(f"时间格式应为 HH:MM，收到 {v!r}")
    return v


class GeneralCfg(_M):
    state_dir: str = "/var/lib/market-monitor"
    display_tz: str = "Asia/Shanghai"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # 本机控制通道（网页后台用它重载配置、查状态）；目录由 systemd 的 RuntimeDirectory 建
    control_socket: str = "/run/market-monitor/control.sock"


class RouteCfg(_M):
    """一个推送渠道收哪些消息。"""

    alerts: bool = True  # 异动告警
    min_level: Literal["L1", "L2", "L3"] = "L1"  # 只收这个级别及以上
    markets: list[Literal["US", "CRYPTO"]] = Field(default_factory=lambda: ["US", "CRYPTO"])
    tradingview: bool = True  # TradingView 转发的告警
    ops: bool = True  # 运维消息
    reports: bool = True  # 日报
    trend: bool = True  # 趋势形成 / 结束（也按 markets 筛选）

    def to_route(self):  # noqa: ANN201
        from .notifiers import Route

        return Route(
            alerts=self.alerts,
            min_level=self.min_level,
            markets=tuple(self.markets),
            tradingview=self.tradingview,
            ops=self.ops,
            reports=self.reports,
            trend=self.trend,
        )


class TelegramCfg(_M):
    enabled: bool = True
    chat_id: int | str = 0  # 群组 ID（负数）；启用时必填
    admin_user_ids: list[int] = Field(default_factory=list)  # 空 = 群内任何人都可用修改类命令
    commands: bool = True
    feedback_buttons: bool = True
    silent_extension_replies: bool = False
    api_base: str = "https://api.telegram.org"
    send_timeout_s: float = 10.0
    route: RouteCfg = Field(default_factory=RouteCfg)

    @model_validator(mode="after")
    def _need_chat(self) -> "TelegramCfg":
        if self.enabled and str(self.chat_id).strip() in ("", "0"):
            raise ValueError("启用 Telegram 时要填 chat_id（群组 ID）")
        return self


class DiscordCfg(_M):
    enabled: bool = False
    channel_id: str = ""  # 频道 ID：Discord 设置里打开开发者模式，右键频道“复制频道 ID”
    admin_user_ids: list[int] = Field(default_factory=list)  # 空 = 频道里任何人都可用 /mute
    commands: bool = True  # 注册斜杠命令 /status /mute 等
    feedback_buttons: bool = True
    api_base: str = "https://discord.com/api/v10"
    send_timeout_s: float = 10.0
    route: RouteCfg = Field(default_factory=RouteCfg)

    @field_validator("channel_id", mode="before")
    @classmethod
    def _as_str(cls, v: object) -> str:
        return str(v).strip()

    @model_validator(mode="after")
    def _need_channel(self) -> "DiscordCfg":
        if self.enabled and not self.channel_id.isdigit():
            raise ValueError("启用 Discord 时要填 channel_id（一串数字）")
        return self


class TradingViewCfg(_M):
    """接收 TradingView 告警 webhook，按同样的模板转发到各推送渠道。webhook 地址里的密钥写在 secrets.env。"""

    enabled: bool = False
    max_per_min: int = Field(20, ge=1, le=600)  # 每分钟最多转发多少条，超出的丢弃并记日志
    dedup_s: int = Field(60, ge=0)  # 同样内容在这段时间内只转发一次


class WebCfg(_M):
    """网页管理后台（单独的 market-monitor-web 服务）。"""

    listen_host: str = "127.0.0.1"  # 只听本机，由 Caddy 反向代理出 HTTPS
    listen_port: int = Field(8787, ge=1, le=65535)
    session_hours: float = Field(12, gt=0, le=168)
    cookie_secure: bool = True  # 只通过 HTTPS（或本机 SSH 隧道）访问时保持 true
    login_max_failures: int = Field(5, ge=1)
    login_lock_min: int = Field(15, ge=1)


class SessionOverride(_M):
    floor_pct: Triple | None = None
    z: float | None = None
    bypass_pct: Triple | None = None


class RulesCfg(_M):
    preset: Literal["sensitive", "balanced", "conservative"] = "sensitive"
    confirm_ratio: float = Field(0.6, gt=0, le=1)
    fast_score: float = Field(2.0, gt=1)
    l2_score: float = Field(1.5, ge=1)
    # V4 去掉了“成交额 ≥ 常态 N 倍 → L2”（回测里 90%–100% 的告警都满足，级别没有区分度）。
    # 0 = 关闭；想恢复 V3 的行为填 3。
    l2_turnover_mult: float = Field(0.0, ge=0)
    # 急拉急跌（L1 / L2）在普通确认之后再等几根 K 线，每根都保留 ≥ confirm_ratio 才推；L3 不等。0 = 不多等
    spike_extra_confirm: int = Field(2, ge=0, le=5)
    thin_turnover_frac: float = Field(0.25, ge=0, le=1)
    thin_pre_until: str = "07:00"  # 美东时间；之前的盘前按稀薄时段处理
    # 这些时段里，快速通道（L3）也要等下一根 K 线确认。盘外成交稀薄，单根坏价（例如按上一交易日收盘价
    # 回报的“前收参考价”成交）会直接拉出 -5% 的假 K 线；多等 1 分钟就能挡掉。盘中仍然收盘即推。
    fast_confirm_sessions: list[str] = Field(default_factory=lambda: ["PRE", "POST", "ON"])
    # 盘外 K 线收盘价和上一交易日盘中收盘价几乎一样（差距 ≤ 这个比例），而上一根还离得很远：
    # 判定为前收参考价回报的旧价，不算异动
    stale_price_tol: float = Field(0.0005, ge=0, le=0.01)
    baseline_sessions: int = Field(20, ge=3)
    baseline_min_sessions: int = Field(5, ge=1)
    crypto_baseline_days: int = Field(14, ge=2)
    crypto_min_days: int = Field(2, ge=1)
    overrides: dict[str, SessionOverride] = Field(default_factory=dict)

    _v_thin = field_validator("thin_pre_until")(_check_hhmm)

    @field_validator("fast_confirm_sessions")
    @classmethod
    def _known_confirm_sessions(cls, v: list[str]) -> list[str]:
        bad = [k for k in v if k not in STOCK_SESSIONS + CRYPTO_SESSIONS]
        if bad:
            raise ValueError(f"未知时段 {bad}，可用 {STOCK_SESSIONS + CRYPTO_SESSIONS}")
        return v

    @field_validator("overrides")
    @classmethod
    def _known_sessions(cls, v: dict[str, SessionOverride]) -> dict[str, SessionOverride]:
        bad = [k for k in v if k not in STOCK_SESSIONS + CRYPTO_SESSIONS]
        if bad:
            raise ValueError(f"未知时段 {bad}，可用 {STOCK_SESSIONS + CRYPTO_SESSIONS}")
        return v


class EpisodeCfg(_M):
    extend_floors: float = Field(1.0, gt=0)  # 再走多少个 5 分钟地板才算延伸
    end_retrace: float = Field(0.5, gt=0, le=1)  # 回撤超过最大位移的这个比例即结束
    idle_end_min: int = Field(30, ge=5)
    cooldown_min: int = Field(10, ge=0)
    max_replies: int = Field(3, ge=0)
    reply_gap_min: int = Field(5, ge=0)
    pending_max_wait_min: int = Field(3, ge=1)


class MergeCfg(_M):
    window_s: int = Field(180, ge=0)
    batch_delay_s: float = Field(8, ge=0)
    fast_batch_delay_s: float = Field(2, ge=0)
    max_new_per_min: int = Field(6, ge=1)
    expiry_s: int = Field(600, ge=60)
    # 分组联动：同一分组在这段时间内同向确认的标的并进同一条消息；达到 group_min 只时标题写分组名
    group_window_s: int = Field(300, ge=0)
    group_min: int = Field(3, ge=2)


class LongbridgeCfg(_M):
    auth: Literal["apikey", "oauth"] = "apikey"
    oauth_callback_port: int | None = None
    ts_is_close: bool = False  # K 线时间戳是否为收盘时间（接通后用 monitor lb-check 核对）
    naive_tz: str = "UTC"  # SDK 返回不带时区的时间时按此时区解释
    subscribe_quotes: bool = True
    check_overnight_list: bool = True
    refresh_calendar: bool = True
    warmup_days: int = Field(35, ge=1)
    history_pause_s: float = Field(0.6, ge=0)


class StocksCfg(_M):
    enabled: bool = True
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_STOCKS))
    multipliers: dict[str, float] = Field(default_factory=dict)
    overnight: bool = True
    synthesize_missing: bool = True
    grace_s: float = Field(6, ge=1, le=30)
    bar_source: Literal["candles", "trades"] = "candles"
    longbridge: LongbridgeCfg = Field(default_factory=LongbridgeCfg)

    @field_validator("symbols")
    @classmethod
    def _us_symbols(cls, v: list[str]) -> list[str]:
        out = [s.strip().upper() for s in v]
        bad = [s for s in out if not s.endswith(".US")]
        if bad:
            raise ValueError(f"美股代码需写成长桥格式，例如 NVDA.US：{bad}")
        if len(set(out)) != len(out):
            raise ValueError("美股代码有重复")
        return out


class CryptoCfg(_M):
    enabled: bool = True
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_CRYPTO))
    multipliers: dict[str, float] = Field(default_factory=lambda: {"ETHUSDT": 1.2, "SOLUSDT": 1.4})
    ws_url: str = "wss://stream.binance.com:9443"
    rest_url: str = "https://api.binance.com"
    rotate_hours: float = Field(23, gt=0, le=23.9)
    grace_s: float = Field(5, ge=1, le=30)

    @field_validator("symbols")
    @classmethod
    def _crypto_symbols(cls, v: list[str]) -> list[str]:
        out = [s.strip().upper() for s in v]
        if any(s.endswith(".US") for s in out):
            raise ValueError("加密货币代码不能以 .US 结尾")
        return out


class HealthCfg(_M):
    daily_report_time: str = "09:05"  # 按 report_tz
    report_tz: str = "Asia/Shanghai"
    lb_stale_reg_s: int = 60
    lb_stale_other_s: int = 300
    binance_stale_s: int = 30
    stale_alert_after_s: int = 180
    outbox_backlog_n: int = 20
    outbox_backlog_age_s: int = 120
    loop_lag_s: float = 1.0
    loop_lag_alert_s: int = 60
    clock_check_minutes: int = 60
    clock_max_offset_s: float = 1.0
    heartbeat_interval_s: int = 60
    longbridge_token_expires: date | None = None
    token_warn_days: int = 7
    startup_message: bool = True

    _v_report = field_validator("daily_report_time")(_check_hhmm)


class CalendarCfg(_M):
    extra_holidays: list[date] = Field(default_factory=list)
    extra_early_closes: list[date] = Field(default_factory=list)


class StorageCfg(_M):
    db_file: str = "monitor.db"
    bars_days: int = 120
    l0_days: int = 90
    outbox_days: int = 30
    backup_keep: int = 7
    maintenance_time: str = "12:30"  # 北京时间，夜盘清淡时段

    _v_maint = field_validator("maintenance_time")(_check_hhmm)


class TrendCfg(_M):
    """多周期趋势监控（15m / 1h / 4h），规则见方案文档和 trend/machine.py。

    美股只用正常盘 K 线判定，也只在正常盘推送；“4h”是每天两根（09:30–13:30、13:30–16:00）。
    """

    enabled: bool = True
    disabled_symbols: list[str] = Field(default_factory=list)  # 这些标的不做趋势监控
    # 单周期打分（改了要重启：历史要按新参数重算）
    zigzag_atr: float = Field(1.5, ge=0.5, le=5)  # 波段高低点确认：收盘回撤超过几倍 ATR
    adx_min: float = Field(20, ge=5, le=60)
    # 形成 / 结束（热加载）
    accept_bars: int = Field(4, ge=1, le=16)  # 连续几根 15m 满足条件才算站稳
    min_4h_score: int = Field(1, ge=0, le=3)
    cooldown_h: float = Field(4, ge=0, le=48)  # 同方向趋势结束后多久内不再推“形成”
    hard_risk_atr4h: float = Field(2.5, gt=0, le=10)  # 失效位离现价超过几倍 ATR(4h) 就不算趋势
    # 推送过滤：不满足的照样跟踪，但整段不推（热加载）
    max_risk_atr4h: float = Field(1.8, gt=0, le=10)
    max_ext_atr1h: float = Field(2.5, gt=0, le=10)  # 离 1h EMA20 超过几倍 ATR(1h) 算追远
    climax_mult: float = Field(2.5, gt=1, le=20)  # 最近 4 小时成交额是常态中位数的几倍算高潮量
    # 支撑压力位（热加载）
    sr_tol_atr: float = Field(0.4, gt=0, le=2)
    sr_half_life_d: float = Field(15, gt=0, le=120)
    sr_vp_days: float = Field(10, gt=0, le=60)
    sr_show: int = Field(2, ge=1, le=4)  # 推送里上下各列几个强位
    # 数据
    history_days: int = Field(130, ge=40, le=365)  # 启动时补齐多少天的 15m 历史

    @field_validator("disabled_symbols")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s.strip()]

    def params(self):  # noqa: ANN201
        from .trend.levels import LevelParams
        from .trend.machine import TrendParams

        return TrendParams(
            zigzag_atr=self.zigzag_atr,
            adx_min=self.adx_min,
            accept_bars=self.accept_bars,
            min_4h_score=self.min_4h_score,
            cooldown_h=self.cooldown_h,
            max_ext_atr1h=self.max_ext_atr1h,
            max_risk_atr4h=self.max_risk_atr4h,
            hard_risk_atr4h=self.hard_risk_atr4h,
            climax_mult=self.climax_mult,
            levels=LevelParams(tol_atr=self.sr_tol_atr, half_life_d=self.sr_half_life_d, vp_days=self.sr_vp_days),
        )

    def symbols(self, cfg: "AppConfig") -> list[str]:
        if not self.enabled:
            return []
        off = set(self.disabled_symbols)
        return [s for s in cfg.all_symbols() if s not in off]


DEFAULT_GROUPS: dict[str, list[str]] = {
    "存储": ["MU", "SNDK", "STX"],
    "芯片": ["NVDA", "INTC"],
    "平台与汽车": ["GOOGL", "META", "TSLA"],
    "能源与太空": ["BE", "SPCX"],
    "加密": ["BTC", "ETH", "SOL"],
}


class WeekendPerpCfg(_M):
    enabled: bool = False  # louis 决定关闭；本版本未实现该模块


class AppConfig(_M):
    general: GeneralCfg = Field(default_factory=GeneralCfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    rules: RulesCfg = Field(default_factory=RulesCfg)
    episode: EpisodeCfg = Field(default_factory=EpisodeCfg)
    merge: MergeCfg = Field(default_factory=MergeCfg)
    stocks: StocksCfg = Field(default_factory=StocksCfg)
    crypto: CryptoCfg = Field(default_factory=CryptoCfg)
    health: HealthCfg = Field(default_factory=HealthCfg)
    calendar: CalendarCfg = Field(default_factory=CalendarCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    weekend_perp: WeekendPerpCfg = Field(default_factory=WeekendPerpCfg)
    discord: DiscordCfg = Field(default_factory=DiscordCfg)
    tradingview: TradingViewCfg = Field(default_factory=TradingViewCfg)
    web: WebCfg = Field(default_factory=WebCfg)
    trend: TrendCfg = Field(default_factory=TrendCfg)
    # 自选分组：分组名 → 标的（写简称即可，如 NVDA、BTC）。只影响联动标题、统计和按分组开关类型，不影响触发
    groups: dict[str, list[str]] = Field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_GROUPS.items()})

    @model_validator(mode="after")
    def _check(self) -> "AppConfig":
        if not (self.telegram.enabled or self.discord.enabled):
            raise ValueError("Telegram 和 Discord 至少启用一个推送渠道")
        if self.weekend_perp.enabled:
            raise ValueError("周末合约模块在本版本未实现，请保持 weekend_perp.enabled = false")
        if not (self.stocks.enabled or self.crypto.enabled):
            raise ValueError("美股和加密货币至少启用一个")
        return self

    @property
    def db_path(self) -> Path:
        p = Path(self.storage.db_file)
        return p if p.is_absolute() else Path(self.general.state_dir) / p

    def groups_of(self, symbol: str) -> list[str]:
        """标的所在的分组（按简称匹配，不区分大小写）。"""
        from .models import display_name

        name = display_name(symbol).upper()
        full = symbol.upper()
        return [g for g, members in self.groups.items() if any(m.upper() in (name, full) for m in members)]

    def all_symbols(self) -> list[str]:
        out: list[str] = []
        if self.stocks.enabled:
            out += self.stocks.symbols
        if self.crypto.enabled:
            out += self.crypto.symbols
        return out


def load_config(path: str | Path) -> AppConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return AppConfig.model_validate(raw)


@dataclass(frozen=True)
class SessionThresholds:
    floor: Triple  # 小数，例如 0.004；未乘标的系数
    z: float
    bypass: Triple  # 小数


class Params:
    """把预设和覆盖项展开成每个时段的门槛表，并计算 rule_version。"""

    def __init__(self, cfg: AppConfig):
        preset = PRESETS[cfg.rules.preset]
        self.preset = cfg.rules.preset
        self.label: str = preset["label"]
        self.sessions: dict[str, SessionThresholds] = {}
        for s in STOCK_SESSIONS:
            self.sessions[s] = self._resolve(
                STOCK_FLOORS_PCT[s], preset["stock_z"][s], preset["stock_bypass"][s], cfg.rules.overrides.get(s)
            )
        for s in CRYPTO_SESSIONS:
            self.sessions[s] = self._resolve(
                CRYPTO_FLOORS_PCT, preset["crypto_z"][s], preset["crypto_bypass"], cfg.rules.overrides.get(s)
            )
        self.multipliers: dict[str, float] = {}
        self.multipliers.update({k.upper(): v for k, v in cfg.stocks.multipliers.items()})
        self.multipliers.update({k.upper(): v for k, v in cfg.crypto.multipliers.items()})
        self.rules = cfg.rules
        self.episode = cfg.episode
        self.merge = cfg.merge
        self.rule_version = self._version(cfg)

    @staticmethod
    def _resolve(floor_pct: Triple, z: float, bypass_pct: Triple, ov: SessionOverride | None) -> SessionThresholds:
        if ov is not None:
            floor_pct = ov.floor_pct or floor_pct
            z = ov.z if ov.z is not None else z
            bypass_pct = ov.bypass_pct or bypass_pct
        return SessionThresholds(
            floor=tuple(x / 100 for x in floor_pct),  # type: ignore[arg-type]
            z=float(z),
            bypass=tuple(x / 100 for x in bypass_pct),  # type: ignore[arg-type]
        )

    def _version(self, cfg: AppConfig) -> str:
        payload = {
            "sessions": {k: [v.floor, v.z, v.bypass] for k, v in sorted(self.sessions.items())},
            "mult": dict(sorted(self.multipliers.items())),
            "rules": cfg.rules.model_dump(mode="json", exclude={"preset"}),
            "episode": cfg.episode.model_dump(mode="json"),
            "merge": cfg.merge.model_dump(mode="json"),
            "overnight": cfg.stocks.overnight,
            "groups": {k: sorted(v) for k, v in sorted(cfg.groups.items())},
        }
        h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]
        return f"{RULE_MAJOR}-{self.label}-{h}"

    def mult(self, symbol: str) -> float:
        return self.multipliers.get(symbol.upper(), 1.0)

    def floor(self, symbol: str, session: str, k: int) -> float:
        st = self.sessions.get(session)
        if st is None:
            return float("inf")
        return st.floor[WINDOWS.index(k)] * self.mult(symbol)

    def table(self) -> list[tuple[str, str, str, str]]:
        """给 /params 和 check-config 用的可读门槛表。"""
        rows = []
        for s, st in self.sessions.items():
            rows.append(
                (
                    SESSION_LABELS[s],
                    " / ".join(f"{x * 100:.1f}%" for x in st.floor),
                    f"{st.z:g}",
                    " / ".join(f"{x * 100:.1f}%" for x in st.bypass),
                )
            )
        return rows


@dataclass(frozen=True)
class Secrets:
    telegram_token: str | None
    heartbeat_url: str | None
    discord_token: str | None = None
    tradingview_secret: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
            heartbeat_url=os.environ.get("HEARTBEAT_URL") or None,
            discord_token=os.environ.get("DISCORD_BOT_TOKEN") or None,
            tradingview_secret=os.environ.get("TRADINGVIEW_WEBHOOK_SECRET") or None,
        )


SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "TRADINGVIEW_WEBHOOK_SECRET",
    "HEARTBEAT_URL",
    "LONGBRIDGE_APP_KEY",
    "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN",
    "LONGBRIDGE_OAUTH_CLIENT_ID",
)


def channel_routes(cfg: "AppConfig") -> dict:
    """启用的推送渠道及订阅规则，给发件箱分发用。"""
    routes = {}
    if cfg.telegram.enabled:
        routes["telegram"] = cfg.telegram.route.to_route()
    if cfg.discord.enabled:
        routes["discord"] = cfg.discord.route.to_route()
    return routes
