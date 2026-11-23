"""波动基线：每个标的、每个时段、每个窗口的稳健 σ 和成交额中位数。

σ = max(1.4826 × MAD, 0.8 × 平均绝对值, 1e-5)，样本是最近 N 个同类时段实例里
所有 k 分钟对数收益（逐分钟滑动，两端在同一个时段实例内）。
美股取最近 20 个同类时段；加密货币取最近 14 天，工作日和周末分开。
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from statistics import median

from .config import WINDOWS
from .models import Bar


@dataclass(slots=True)
class BaselineStat:
    sigma: float
    turnover_median: float | None
    n_instances: int
    n_returns: int
    ready: bool


def robust_sigma(rs: list[float]) -> float | None:
    if not rs:
        return None
    med = median(rs)
    mad = median([abs(r - med) for r in rs])
    mean_abs = sum(abs(r) for r in rs) / len(rs)
    return max(1.4826 * mad, 0.8 * mean_abs, 1e-5)


def compute_symbol_baseline(
    bars: Iterable[Bar],
    session_of: Callable[[Bar], str],
    instance_of: Callable[[Bar], str],
    keep_instances: int,
    min_instances: int,
) -> dict[tuple[str, int], BaselineStat]:
    """bars 需按时间升序。返回 {(时段, k): BaselineStat}。"""
    instances: dict[str, dict[int, Bar]] = defaultdict(dict)
    inst_session: dict[str, str] = {}
    inst_first: dict[str, int] = {}
    for b in bars:
        s = session_of(b)
        if s == "CLOSED" or b.c <= 0:
            continue
        key = instance_of(b)
        instances[key][b.ts] = b
        inst_session[key] = s
        inst_first.setdefault(key, b.ts)

    by_session: dict[str, list[str]] = defaultdict(list)
    for key in sorted(instances, key=lambda x: inst_first[x]):
        by_session[inst_session[key]].append(key)

    out: dict[tuple[str, int], BaselineStat] = {}
    for sess, keys in by_session.items():
        chosen = keys[-keep_instances:]
        for k in WINDOWS:
            rets: list[float] = []
            turns: list[float] = []
            for key in chosen:
                m = instances[key]
                for ts, b in m.items():
                    b0 = m.get(ts - 60 * k)
                    if b0 is None or b0.c <= 0:
                        continue
                    rets.append(math.log(b.c / b0.c))
                    t = 0.0
                    for j in range(k):
                        bj = m.get(ts - 60 * j)
                        if bj is not None:
                            t += bj.turnover
                    if t > 0:
                        turns.append(t)
            sigma = robust_sigma(rets)
            if sigma is None:
                continue
            out[(sess, k)] = BaselineStat(
                sigma=sigma,
                turnover_median=median(turns) if turns else None,
                n_instances=len(chosen),
                n_returns=len(rets),
                ready=len(chosen) >= min_instances,
            )
    return out


class Baselines:
    def __init__(self) -> None:
        self._d: dict[tuple[str, str, int], BaselineStat] = {}
        self.computed_at: dict[str, float] = {}

    def get(self, symbol: str, session: str, k: int) -> BaselineStat | None:
        return self._d.get((symbol, session, k))

    def set_symbol(self, symbol: str, stats: dict[tuple[str, int], BaselineStat], when: float) -> None:
        for key in [key for key in self._d if key[0] == symbol]:
            del self._d[key]
        for (sess, k), st in stats.items():
            self._d[(symbol, sess, k)] = st
        self.computed_at[symbol] = when

    def items(self) -> Iterable[tuple[tuple[str, str, int], BaselineStat]]:
        return self._d.items()

    def ready_sessions(self, symbol: str) -> list[str]:
        return sorted({s for (sym, s, k), st in self._d.items() if sym == symbol and st.ready and k == 5})
