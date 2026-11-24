"""趋势规则回测：把 15m K 线按顺序喂给状态机，统计每条信号的结果，并和随机入场对照。

指标（R = 入场价到形成时失效位的距离）：
  hit1   先到 +1R，还是先收盘跌破形成时的失效位
  exit_r 按“趋势结束”离场的收益，单位 R（失效位跟随上移）
  r24    24 小时后顺方向的涨跌幅（%）
随机对照：随机时间、随机方向入场，失效位同样取当时 1h 最近波段点，离场规则完全相同。
只用标准库，可以在任何 Python 里跑（包括浏览器里的 Pyodide）。
"""
from __future__ import annotations

import random
import statistics
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .buckets import Bucketer
from .machine import DEFAULT_PARAMS, SymbolTrend, TrendEvent, TrendParams
from .series import Candle


@dataclass
class Trade:
    symbol: str
    d: int
    i: int  # 入场的 15m 序号
    t: int
    price: float
    stop0: float
    grade: str
    exit_i: int
    exit_price: float
    why: str
    hit1: int | None = None
    exit_r: float = 0.0
    r24: float | None = None
    ext: float = 0.0
    vr: float | None = None

    @property
    def risk(self) -> float:
        return abs(self.price - self.stop0)


@dataclass
class SymbolRun:
    symbol: str
    candles: list[Candle]
    trades: list[Trade]
    events: list[TrendEvent]
    stops_1h: list[tuple[int, float | None, float | None, float]]  # (1h 结束时间, ll, lh, 1h 收盘)


def _hit(candles: Sequence[Candle], i: int, d: int, stop: float) -> int | None:
    x = candles[i].c
    r = abs(x - stop)
    for k in range(i + 1, len(candles)):
        fav = candles[k].h - x if d > 0 else x - candles[k].l
        if fav >= r:
            return 1
        if (candles[k].c < stop) if d > 0 else (candles[k].c > stop):
            return 0
    return None


def run_symbol(
    symbol: str, candles: list[Candle], bucketer: Bucketer, p: TrendParams = DEFAULT_PARAMS, include_quiet: bool = False
) -> SymbolRun:
    m = SymbolTrend(symbol, bucketer, p)
    events: list[TrendEvent] = []
    trades: list[Trade] = []
    open_tr: Trade | None = None
    stops: list[tuple[int, float | None, float | None, float]] = []
    last_h = 0
    for i, k in enumerate(candles):
        evs = m.on_candle15(k)
        s1 = m.tf1.state
        if s1 is not None and s1.end != last_h:
            last_h = s1.end
            stops.append((s1.end, s1.ll, s1.lh, s1.close))
        for ev in evs:
            events.append(ev)
            if ev.kind == "formed":
                if ev.quiet and not include_quiet:
                    open_tr = None
                    continue
                open_tr = Trade(symbol, ev.d, i, ev.t, ev.price, ev.stop0, ev.grade, -1, 0.0, "open", ext=ev.ext, vr=ev.vr)
            elif open_tr is not None:
                open_tr.exit_i, open_tr.exit_price, open_tr.why = i, ev.price, ev.reason
                trades.append(open_tr)
                open_tr = None
    if open_tr is not None:
        open_tr.exit_i, open_tr.exit_price = len(candles) - 1, candles[-1].c
        trades.append(open_tr)
    for tr in trades:
        tr.hit1 = _hit(candles, tr.i, tr.d, tr.stop0)
        tr.exit_r = tr.d * (tr.exit_price - tr.price) / (tr.risk or 1)
        j = tr.i + 96
        tr.r24 = tr.d * (candles[j].c / tr.price - 1) * 100 if j < len(candles) else None
    return SymbolRun(symbol, candles, trades, events, stops)


def random_trades(run: SymbolRun, n: int, seed: int, warm: int = 1200) -> list[Trade]:
    """随机入场 + 和信号完全一样的跟随失效位离场。"""
    rnd = random.Random(seed)
    c = run.candles
    ends = [s[0] for s in run.stops_1h]
    out: list[Trade] = []
    if len(c) < warm + 200 or not ends:
        return out
    for _ in range(n):
        i = rnd.randrange(warm, len(c) - 100)
        d = 1 if rnd.random() < 0.5 else -1
        t = c[i].end
        j = bisect_right(ends, t) - 1
        if j < 0:
            continue
        _, ll, lh, _ = run.stops_1h[j]
        stop = ll if d > 0 else lh
        if stop is None or (c[i].c - stop) * d <= 0:
            continue
        tr = Trade(run.symbol, d, i, t, c[i].c, stop, "R", -1, 0.0, "open")
        cur = stop
        jj = j + 1
        exit_i = len(c) - 1
        for k in range(i + 1, len(c)):
            # 每根 1h 收盘时更新跟随失效位并检查
            while jj < len(run.stops_1h) and run.stops_1h[jj][0] <= c[k].end:
                _, l2, h2, close1 = run.stops_1h[jj]
                if d > 0 and l2 is not None and cur < l2 < c[k].c:
                    cur = l2
                if d < 0 and h2 is not None and c[k].c < h2 < cur:
                    cur = h2
                jj += 1
                if (close1 < cur) if d > 0 else (close1 > cur):
                    exit_i = k
                    break
            if exit_i != len(c) - 1:
                break
        tr.exit_i, tr.exit_price, tr.why = exit_i, c[exit_i].c, "stop"
        tr.hit1 = _hit(c, i, d, stop)
        tr.exit_r = d * (tr.exit_price - tr.price) / (tr.risk or 1)
        tr.r24 = d * (c[i + 96].c / tr.price - 1) * 100 if i + 96 < len(c) else None
        out.append(tr)
    return out


@dataclass
class Summary:
    n: int = 0
    hit1: float | None = None
    exit_r: float | None = None
    exit_r_med: float | None = None
    r24: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        f = lambda x, fmt: "—" if x is None else format(x, fmt)  # noqa: E731
        return f"n={self.n}  先到+1R {f(self.hit1 and self.hit1 * 100, '.1f')}%  平均 {f(self.exit_r, '+.2f')}R  中位 {f(self.exit_r_med, '+.2f')}R  24h {f(self.r24, '+.2f')}%"


def summarize(trades: Sequence[Trade]) -> Summary:
    if not trades:
        return Summary()
    hits = [t.hit1 for t in trades if t.hit1 is not None]
    rs = [t.exit_r for t in trades]
    r24 = [t.r24 for t in trades if t.r24 is not None]
    return Summary(
        n=len(trades),
        hit1=sum(hits) / len(hits) if hits else None,
        exit_r=sum(rs) / len(rs),
        exit_r_med=statistics.median(rs),
        r24=sum(r24) / len(r24) if r24 else None,
    )


def report(runs: Sequence[SymbolRun], baseline_per_symbol: int = 150) -> dict[str, Any]:
    trades = [t for r in runs for t in r.trades]
    base = [t for k, r in enumerate(runs) for t in random_trades(r, baseline_per_symbol, seed=1000 + k)]
    days = sum(max(0.0, (r.candles[-1].end - r.candles[0].ts) / 86400) for r in runs if r.candles) or 1.0
    return {
        "signals": summarize(trades),
        "random": summarize(base),
        "grade_A": summarize([t for t in trades if t.grade == "A"]),
        "grade_B": summarize([t for t in trades if t.grade == "B"]),
        "up": summarize([t for t in trades if t.d > 0]),
        "down": summarize([t for t in trades if t.d < 0]),
        "per_symbol_week": {r.symbol: len(r.trades) / (days / len(runs)) * 7 for r in runs},
    }
