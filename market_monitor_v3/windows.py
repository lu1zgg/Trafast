"""每个标的保留最近几十根 1 分钟 K 线，按时间戳取 P_{t-k}。

只按“时间”而不是“第几根”取起点：缺了某一分钟（且没补平 K 线）时，
跨过缺口的窗口直接无效，不会把“5 根 K 线”误当成“5 分钟”。
"""
from __future__ import annotations

import math
from collections import deque

from .models import Bar


class SymbolHistory:
    def __init__(self, keep: int = 40):
        self.keep = keep
        self._bars: dict[int, Bar] = {}
        self._order: deque[int] = deque()
        self.last_ts: int | None = None

    def get(self, ts: int) -> Bar | None:
        return self._bars.get(ts)

    def last(self) -> Bar | None:
        return None if self.last_ts is None else self._bars.get(self.last_ts)

    def add(self, bar: Bar) -> Bar | None:
        """加入或覆盖一根 K 线，返回被覆盖的旧 K 线。"""
        prev = self._bars.get(bar.ts)
        self._bars[bar.ts] = bar
        if prev is None:
            if self.last_ts is None or bar.ts > self.last_ts:
                self._order.append(bar.ts)
                self.last_ts = bar.ts
            else:
                # 乱序到达的旧 K 线：插入后重排（很少发生）
                self._order = deque(sorted(self._order + deque([bar.ts])))
            cutoff = (self.last_ts or bar.ts) - self.keep * 60
            while self._order and self._order[0] < cutoff:
                old = self._order.popleft()
                self._bars.pop(old, None)
        return prev

    def turnover(self, end_ts: int, k: int) -> float:
        total = 0.0
        for j in range(k):
            b = self._bars.get(end_ts - 60 * j)
            if b is not None:
                total += b.turnover
        return total

    def move_turnover(self, end_ts: int, k: int) -> float | None:
        """价格是在哪几根 K 线上走出来的：按每根的 |收益| 加权的每分钟成交额，再乘 k。

        单根稀薄成交拉出的跳价，结果约等于那一根的成交额 × k，不会被窗口里其他正常 K 线摊平。
        """
        num = den = 0.0
        for j in range(k):
            b = self._bars.get(end_ts - 60 * j)
            prev = self._bars.get(end_ts - 60 * (j + 1))
            if b is None or prev is None or b.c <= 0 or prev.c <= 0:
                continue
            w = abs(math.log(b.c / prev.c))
            num += w * b.turnover
            den += w
        if den <= 0:
            return None
        return num / den * k

    def __len__(self) -> int:
        return len(self._bars)
