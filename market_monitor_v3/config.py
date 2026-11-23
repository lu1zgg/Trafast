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
RULE_MAJOR = "v3.0"

Triple = tuple[float, float, float]

# 地板（百分比），三档共用
STOCK_FLOORS_PCT: dict[str, Triple] = {
    "REG": (0.4, 0.8, 1.2),
    "PRE": (0.6, 1.2, 1.8),
    "POST": (0.6, 1.2, 1.8),
    "ON": (0.8, 1.5, 2.2),
}
CRYPTO_FLOORS_PCT: Triple = (0.5, 1.0, 1.5)

# 三档预设。灵敏档：盘前、盘后、夜盘 15 分钟直通线 2.5%。
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


class TelegramCfg(_M):
    chat_id: int | str
    admin_user_ids: list[int] = Field(default_factory=list)  # 空 = 群内任何人都可用修改类命令
    commands: bool = True
    feedback_buttons: bool = True
    silent_extension_replies: bool = False
    api_base: str = "https://api.telegram.org"
    send_timeout_s: float = 10.0


class SessionOverride(_M):
    floor_pct: Triple | None = None
    z: float | None = None
    bypass_pct: Triple | None = None


class RulesCfg(_M):
    preset: Literal["sensitive", "balanced", "conservative"] = "sensitive"
    confirm_ratio: float = Field(0.6, gt=0, le=1)
    fast_score: float = Field(2.0, gt=1)
    l2_score: float = Field(1.5, ge=1)
    l2_turnover_mult: float = Field(3.0, gt=0)
    thin_turnover_frac: float = Field(0.25, ge=0, le=1)
    thin_pre_until: str = "07:00"  # 美东时间；之前的盘前按稀薄时段处理
    baseline_sessions: int = Field(20, ge=3)
    baseline_min_sessions: int = Field(5, ge=1)
    crypto_baseline_days: int = Field(14, ge=2)
    crypto_min_days: int = Field(2, ge=1)
    overrides: dict[str, SessionOverride] = Field(default_factory=dict)

    _v_thin = field_validator("thin_pre_until")(_check_hhmm)

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


class WeekendPerpCfg(_M):
    enabled: bool = False  # 本版本未实现该模块


class AppConfig(_M):
    general: GeneralCfg = Field(default_factory=GeneralCfg)
    telegram: TelegramCfg
    rules: RulesCfg = Field(default_factory=RulesCfg)
    episode: EpisodeCfg = Field(default_factory=EpisodeCfg)
    merge: MergeCfg = Field(default_factory=MergeCfg)
    stocks: StocksCfg = Field(default_factory=StocksCfg)
    crypto: CryptoCfg = Field(default_factory=CryptoCfg)
    health: HealthCfg = Field(default_factory=HealthCfg)
    calendar: CalendarCfg = Field(default_factory=CalendarCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    weekend_perp: WeekendPerpCfg = Field(default_factory=WeekendPerpCfg)

    @model_validator(mode="after")
    def _check(self) -> "AppConfig":
        if self.weekend_perp.enabled:
            raise ValueError("周末合约模块在本版本未实现，请保持 weekend_perp.enabled = false")
        if not (self.stocks.enabled or self.crypto.enabled):
            raise ValueError("美股和加密货币至少启用一个")
        return self

    @property
    def db_path(self) -> Path:
        p = Path(self.storage.db_file)
        return p if p.is_absolute() else Path(self.general.state_dir) / p

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

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
            heartbeat_url=os.environ.get("HEARTBEAT_URL") or None,
        )


SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "HEARTBEAT_URL",
    "LONGBRIDGE_APP_KEY",
    "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN",
    "LONGBRIDGE_OAUTH_CLIENT_ID",
)
