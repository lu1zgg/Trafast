"""金标回放用的确定性合成行情：随机游走 + 注入的几类典型事件。

时间段：2026-04-06（周一）到 2026-04-23，期间没有 NYSE 休市日。
回放窗口：2026-04-18 00:00 UTC → 2026-04-23 00:00 UTC（含一个加密周末、美股 4/20–4/22 三个交易日）。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from market_monitor_v3.models import Bar
from market_monitor_v3.sessions import UsCalendar, crypto_session
from tests.helpers import et_ts, utc_ts

DATA_START = utc_ts(2026, 4, 6, 0, 0)
DATA_END = utc_ts(2026, 4, 23, 6, 0)
REPLAY_START = utc_ts(2026, 4, 18, 0, 0)
REPLAY_END = utc_ts(2026, 4, 23, 0, 0)

CRYPTO = {"BTCUSDT": 75000.0, "ETHUSDT": 3200.0, "SOLUSDT": 150.0}
STOCKS = {"NVDA.US": 180.0, "MU.US": 110.0}

# 每分钟对数收益率的标准差、每分钟成交额
STOCK_SIGMA = {"PRE": 0.0006, "REG": 0.0008, "POST": 0.0006, "ON": 0.0005}
STOCK_TURNOVER = {"PRE": 3e5, "REG": 5e6, "POST": 3e5, "ON": 6e4}
CRYPTO_SIGMA = {"CWD": 0.0006, "CWE": 0.0004}
CRYPTO_TURNOVER = {"CWD": 2e6, "CWE": 8e5}


@dataclass
class Event:
    """在 [t0, t0 + 60·len(path)) 内给对数价格叠加 path[i]；之后保持最后一个值（persist）或归零。"""

    name: str
    symbol: str
    t0: int
    path: list[float]
    persist: bool = True
    turnover_mult: list[float] = field(default_factory=list)

    def offset(self, ts: int) -> float:
        i = (ts - self.t0) // 60
        if i < 0:
            return 0.0
        if i < len(self.path):
            return self.path[i]
        return self.path[-1] if self.persist else 0.0

    def tmult(self, ts: int) -> float:
        i = (ts - self.t0) // 60
        if 0 <= i < len(self.turnover_mult):
            return self.turnover_mult[i]
        return 1.0


def ramp(total: float, n: int) -> list[float]:
    return [total * (i + 1) / n for i in range(n)]


L = math.log

EVENTS = [
    # 1. 周末插针：BTC 一根 +0.9%，下一根收回。不该推送，回落段也不该当成下跌异动。
    Event("wick", "BTCUSDT", utc_ts(2026, 4, 18, 10, 0), [L(1.009)], persist=False, turnover_mult=[3.0, 2.0]),
    # 2. 站稳的台阶：ETH 3 分钟涨 1.5% 后横住。应推送。
    Event("held_step", "ETHUSDT", utc_ts(2026, 4, 20, 14, 0), ramp(L(1.015), 3), turnover_mult=[3, 3, 3, 2, 2]),
    # 3. 三个币同一分钟一起跌：应合并成一条联动消息。
    Event("corr_btc", "BTCUSDT", utc_ts(2026, 4, 21, 2, 0), ramp(L(0.988), 2), turnover_mult=[4, 4, 2]),
    Event("corr_eth", "ETHUSDT", utc_ts(2026, 4, 21, 2, 0), ramp(L(0.984), 2), turnover_mult=[4, 4, 2]),
    Event("corr_sol", "SOLUSDT", utc_ts(2026, 4, 21, 2, 0), ramp(L(0.978), 2), turnover_mult=[4, 4, 2]),
    # 4. 盘中大跌再回吐：NVDA 5 分钟跌 3.5%（超过 5 分钟直推线 2.5%），再跌到 -4.5%，然后收回大半。
    Event(
        "big_move",
        "NVDA.US",
        et_ts(2026, 4, 21, 10, 30),
        ramp(L(0.965), 5)
        + [L(0.965)] * 3
        + [L(0.965) + x for x in ramp(L(0.955) - L(0.965), 4)]
        + [L(0.955)] * 4
        + [L(0.955) + x for x in ramp(L(0.985) - L(0.955), 10)],
        turnover_mult=[5] * 12 + [3] * 10,
    ),
    # 5. 只放量不动价：MU 成交额 10 倍、价格不动。不该推送。
    Event("volume_only", "MU.US", et_ts(2026, 4, 20, 11, 0), [0.0] * 5, turnover_mult=[10] * 5),
    # 6. 夜盘稀薄成交：MU 一根 +2.2% 并停在那里，但成交额只有夜盘中位数的 5%。不该推送。
    Event("on_thin", "MU.US", et_ts(2026, 4, 21, 22, 0), [L(1.022)], turnover_mult=[0.05] * 30),
    # 7. 盘前台阶：NVDA 盘前 7:30 两分钟涨 2% 并站稳，成交正常。应推送。
    Event("pre_step", "NVDA.US", et_ts(2026, 4, 22, 7, 30), ramp(L(1.02), 2), turnover_mult=[4, 4, 2, 2]),
]


def _session(cal: UsCalendar, sym: str, ts: int) -> str:
    return cal.session_at(ts) if sym.endswith(".US") else crypto_session(ts)


def generate(seed: int = 20261123) -> list[Bar]:
    cal = UsCalendar()
    bars: list[Bar] = []
    for idx, (sym, p0) in enumerate({**CRYPTO, **STOCKS}.items()):
        rng = random.Random(seed * 100 + idx)
        evs = [e for e in EVENTS if e.symbol == sym]
        logp = math.log(p0)
        prev_close = p0
        for ts in range(DATA_START, DATA_END, 60):
            sess = _session(cal, sym, ts)
            if sess == "CLOSED":
                continue
            sigma = (STOCK_SIGMA if sym.endswith(".US") else CRYPTO_SIGMA)[sess]
            tov = (STOCK_TURNOVER if sym.endswith(".US") else CRYPTO_TURNOVER)[sess]
            logp += rng.gauss(0.0, sigma)
            off = sum(e.offset(ts) for e in evs)
            close = math.exp(logp + off)
            mult = 1.0
            for e in evs:
                mult *= e.tmult(ts)
            turnover = tov * mult * math.exp(rng.gauss(0.0, 0.3))
            o = prev_close
            h = max(o, close) * (1 + abs(rng.gauss(0.0, sigma / 3)))
            lo = min(o, close) * (1 - abs(rng.gauss(0.0, sigma / 3)))
            bars.append(Bar(sym, ts, o, h, lo, close, turnover / close, turnover, sess, "synth"))
            prev_close = close
    return bars


def event(name: str) -> Event:
    return next(e for e in EVENTS if e.name == name)
