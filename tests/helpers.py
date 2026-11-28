"""测试辅助：配置、合成 K 线、手动基线。"""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime

from market_monitor_v3.baseline import Baselines, BaselineStat
from market_monitor_v3.config import WINDOWS, AppConfig, Params
from market_monitor_v3.engine import Engine
from market_monitor_v3.models import Bar
from market_monitor_v3.sessions import ET, UsCalendar, crypto_session, utc_market_session
from market_monitor_v3.sink import BufferSink

UTC = UTC


def make_cfg(**over) -> AppConfig:
    raw = {"telegram": {"chat_id": -100123}}
    for k, v in over.items():
        raw[k] = v
    return AppConfig.model_validate(raw)


def et_ts(y, mo, d, h, mi) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=ET).timestamp())


def utc_ts(y, mo, d, h, mi) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp())


def flat_baselines(symbols_sessions: dict[str, list[str]], sigma1: float, turnover_median: float = 1e6) -> Baselines:
    """σ_k = σ_1·√k，所有标的、时段一样，已就绪。"""
    bl = Baselines()
    for sym, sessions in symbols_sessions.items():
        stats = {}
        for s in sessions:
            for k in WINDOWS:
                stats[(s, k)] = BaselineStat(sigma1 * math.sqrt(k), turnover_median * k, 20, 1000, True)
        bl.set_symbol(sym, stats, 0)
    return bl


class Harness:
    """引擎 + 缓冲 sink，按分钟喂收盘价。"""

    def __init__(self, cfg: AppConfig | None = None, baselines: Baselines | None = None, alive: bool = True):
        self.cfg = cfg or make_cfg(stocks={"enabled": False})
        self.params = Params(self.cfg)
        self.cal = UsCalendar()
        self.baselines = baselines or flat_baselines({s: ["CWD", "CWE"] for s in self.cfg.crypto.symbols}, 0.0006)
        self.sink = BufferSink()
        self.engine = Engine(self.cfg, self.params, self.cal, self.baselines, self.sink, alive=lambda m, n: alive)
        self.outs = []
        self.decisions = []
        self.signals = []

    def seed(self, symbol: str, start_ts: int, price: float, n: int = 30, turnover: float = 1e6) -> int:
        bars = []
        for i in range(n):
            ts = start_ts + 60 * i
            bars.append(Bar(symbol, ts, price, price, price, price, 1, turnover, self._sess(symbol, ts), "t"))
        self.engine.seed_history(bars)
        return start_ts + 60 * n

    def _sess(self, symbol: str, ts: int) -> str:
        return self.cal.session_at(ts) if symbol.endswith(".US") else utc_market_session(symbol, ts)

    def bar(self, symbol: str, ts: int, close: float, turnover: float = 1e6, ticks=(3, 10, 30)) -> None:
        now = ts + 61
        self.engine.on_bar(Bar(symbol, ts, close, close, close, close, 1, turnover, "", "t"), now)
        for dt in ticks:
            self.engine.on_tick(now + dt)
        self.collect()

    def bars(self, symbol: str, ts: int, closes: list[float], **kw) -> int:
        for i, c in enumerate(closes):
            self.bar(symbol, ts + 60 * i, c, **kw)
        return ts + 60 * len(closes)

    def multi(self, ts: int, closes: dict[str, float], ticks=(3, 10, 30)) -> None:
        now = ts + 61
        for sym, c in closes.items():
            self.engine.on_bar(Bar(sym, ts, c, c, c, c, 1, 1e6, "", "t"), now)
        for dt in ticks:
            self.engine.on_tick(now + dt)
        self.collect()

    def tick(self, now: float) -> None:
        self.engine.on_tick(now)
        self.collect()

    def collect(self) -> None:
        eff = self.sink.drain()
        self.outs.extend(eff.outs)
        self.decisions.extend(eff.decisions)
        self.signals.extend(eff.signals)

    def sends(self):
        return [o for o in self.outs if o.kind == "send"]

    def kinds(self):
        return [o.kind for o in self.outs]

    def outcomes(self):
        return [d.outcome for d in self.decisions]


def random_walk(symbol: str, start_ts: int, n: int, price: float, sigma: float, seed: int, turnover=1e6, session_fn=None):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        price *= math.exp(rnd.gauss(0, sigma))
        ts = start_ts + 60 * i
        s = session_fn(ts) if session_fn else crypto_session(ts)
        out.append(Bar(symbol, ts, price, price, price, price, 1, turnover * (0.5 + rnd.random()), s, "t"))
    return out
