"""单一周期的增量指标：EMA20/50、Wilder ATR14 与 ADX14、ATR 折线（波段高低点）和结构打分。

每收一根 K 线调用一次 add()，返回这根 K 线收盘时的周期状态 TFState。
只用标准库，回测和实盘共用同一份代码。

打分规则（见方案文档“单周期趋势判定”）：
  结构 s：+1 低点抬高且（高点抬高或收盘站上最近高点）且收盘在最近低点之上；-1 镜像；其余 0
  均线 e：+1 收盘 > EMA20 > EMA50 且 EMA50 比 5 根前高；-1 镜像
  强度 a：ADX ≥ 门槛时按 +DI / -DI 取 ±1，否则 0
  总分 sc = s + e + a
波段高低点：价格从极值“收盘”回撤超过 zz × ATR 才确认，位置取影线极值。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class Candle:
    ts: int  # 开始时间（epoch 秒）
    end: int  # 结束时间（不含）
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0  # 成交额

    def to_row(self) -> tuple[int, int, float, float, float, float, float]:
        return (self.ts, self.end, self.o, self.h, self.l, self.c, self.v)

    @classmethod
    def from_row(cls, r: tuple | list) -> "Candle":
        return cls(int(r[0]), int(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6]))


@dataclass(slots=True)
class Pivot:
    kind: str  # "H" / "L"
    price: float
    ts: int  # 极值所在 K 线的开始时间
    confirmed_at: int  # 确认它的那根 K 线的结束时间


@dataclass(slots=True)
class TFState:
    s: int = 0
    e: int = 0
    a: int = 0
    sc: int = 0
    ll: float | None = None  # 最近确认的波段低点
    lh: float | None = None  # 最近确认的波段高点
    atr: float = 0.0
    adx: float = 0.0
    e20: float = 0.0
    e50: float = 0.0
    close: float = 0.0
    end: int = 0  # 这根 K 线的结束时间

    def label(self, d: int = 1) -> str:
        """给人看的简短描述。"""
        if self.s == 1 and self.e == 1:
            return "上涨结构（低点抬高）"
        if self.s == -1 and self.e == -1:
            return "下跌结构（高点降低）"
        if self.sc >= 1:
            return "中性偏多"
        if self.sc <= -1:
            return "中性偏空"
        return "震荡"


class _Wilder:
    """和原型一致：前 n 个值取累计平均，之后 x_t = x_{t-1} + (v - x_{t-1}) / n。"""

    __slots__ = ("n", "i", "s", "v")

    def __init__(self, n: int):
        self.n = n
        self.i = 0
        self.s = 0.0
        self.v = 0.0

    def add(self, x: float) -> float:
        if self.i < self.n:
            self.s += x
            self.v = self.s / (self.i + 1)
        else:
            self.v = self.v + (x - self.v) / self.n
        self.i += 1
        return self.v


class _Ema:
    __slots__ = ("k", "v", "started")

    def __init__(self, n: int):
        self.k = 2 / (n + 1)
        self.v = 0.0
        self.started = False

    def add(self, x: float) -> float:
        if not self.started:
            self.v = x
            self.started = True
        else:
            self.v = x * self.k + self.v * (1 - self.k)
        return self.v


@dataclass
class TFAnalyzer:
    zz: float = 1.5  # 折线确认幅度（× ATR）
    adx_min: float = 20.0
    keep: int = 2000  # 保留最近多少根 K 线（支撑压力的成交密集区要用）
    pivot_keep_s: int = 130 * 86400

    candles: deque[Candle] = field(init=False)
    pivots: deque[Pivot] = field(init=False)
    state: TFState | None = field(init=False, default=None)
    n: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.candles = deque(maxlen=self.keep)
        self.pivots = deque()
        self._atr = _Wilder(14)
        self._pdm = _Wilder(14)
        self._mdm = _Wilder(14)
        self._adx = _Wilder(14)
        self._e20 = _Ema(20)
        self._e50 = _Ema(50)
        self._e50_hist: deque[float] = deque(maxlen=6)
        self._prev: Candle | None = None
        # 折线状态
        self._dir = 1
        self._eh = 0.0
        self._eh_ts = 0
        self._el = 0.0
        self._el_ts = 0
        # 最近两个确认的高点 / 低点
        self._H: deque[float] = deque(maxlen=2)
        self._L: deque[float] = deque(maxlen=2)

    @property
    def last(self) -> Candle | None:
        return self.candles[-1] if self.candles else None

    def add(self, k: Candle) -> TFState:
        p = self._prev or k
        tr = max(k.h - k.l, abs(k.h - p.c), abs(k.l - p.c))
        up, dn = k.h - p.h, p.l - k.l
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        atr = self._atr.add(tr)
        sp = self._pdm.add(pdm)
        sm = self._mdm.add(mdm)
        pdi = 100 * sp / (atr or 1)
        mdi = 100 * sm / (atr or 1)
        dx = 100 * abs(pdi - mdi) / ((pdi + mdi) or 1)
        adx = self._adx.add(dx)
        e20 = self._e20.add(k.c)
        e50 = self._e50.add(k.c)
        self._e50_hist.append(e50)

        # 折线：第一根只初始化
        if self.n == 0:
            self._dir, self._eh, self._eh_ts, self._el, self._el_ts = 1, k.h, k.ts, k.l, k.ts
        else:
            th = self.zz * atr
            if self._dir == 1:
                if k.h > self._eh:
                    self._eh, self._eh_ts = k.h, k.ts
                if k.c <= self._eh - th:
                    self._confirm(Pivot("H", self._eh, self._eh_ts, k.end))
                    self._dir, self._el, self._el_ts = -1, k.l, k.ts
            else:
                if k.l < self._el:
                    self._el, self._el_ts = k.l, k.ts
                if k.c >= self._el + th:
                    self._confirm(Pivot("L", self._el, self._el_ts, k.end))
                    self._dir, self._eh, self._eh_ts = 1, k.h, k.ts

        x = k.c
        s = 0
        lh = self._H[-1] if self._H else None
        ll = self._L[-1] if self._L else None
        if len(self._H) == 2 and len(self._L) == 2:
            ph, pl = self._H[0], self._L[0]
            assert lh is not None and ll is not None
            if ll > pl and (lh > ph or x > lh) and x > ll:
                s = 1
            elif lh < ph and (ll < pl or x < ll) and x < lh:
                s = -1
        sl = e50 - self._e50_hist[0] if len(self._e50_hist) == 6 else 0.0
        e = 0
        if x > e20 > e50 and sl > 0:
            e = 1
        elif x < e20 < e50 and sl < 0:
            e = -1
        a = 0
        if adx >= self.adx_min:
            a = 1 if pdi > mdi else -1
        st = TFState(s, e, a, s + e + a, ll, lh, atr, adx, e20, e50, x, k.end)
        self.candles.append(k)
        self._prev = k
        self.state = st
        self.n += 1
        return st

    def _confirm(self, pv: Pivot) -> None:
        (self._H if pv.kind == "H" else self._L).append(pv.price)
        self.pivots.append(pv)
        while self.pivots and pv.confirmed_at - self.pivots[0].ts > self.pivot_keep_s:
            self.pivots.popleft()
