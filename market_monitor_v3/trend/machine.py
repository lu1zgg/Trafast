"""单个标的的趋势状态机：无趋势 / 上涨趋势 / 下跌趋势，只在“形成”和“结束”时产生事件。

每收一根 15m K 线调用 on_candle15()。15m 收盘时如果恰好也收了 1h / 4h，先更新大周期再判断。

形成（以上涨为例，全部满足）：
  1. 1h 同向趋势：结构 +1、均线 +1、总分 ≥ 2
  2. 4h 不反向：总分 ≥ min_4h，且 4h 结构不是 -1
  3. 15m 总分 ≥ 2，且 1–3 连续 accept 根 15m 都成立（站稳）
  4. 失效位离现价不超过 hard_risk × ATR(4h)
  5. 同方向上次结束后冷却 cooldown 小时
满足以上就进入趋势状态。下面三条只决定推不推（不满足的照样跟踪到结束，但整段都不推送）：
  不追远：(价 − 1h EMA20) ≤ max_ext × ATR(1h)，且 (价 − 失效位) ≤ max_risk × ATR(4h)
  不在高潮量里：最近 4 根 1h 成交额均值 ≤ climax × 之前 1h 成交额中位数
失效位 = 1h 最近确认的波段低点（下跌取高点），每根 1h 收盘时只往有利方向移动。
结束：1h 收盘跌破失效位（stop），或 1h 总分反向到 −2 且 4h 结构已不同向（flip）。
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from .buckets import Bucketer
from .levels import LevelParams, Levels, compute_levels
from .series import Candle, TFAnalyzer, TFState


@dataclass(frozen=True)
class TrendParams:
    zigzag_atr: float = 1.5
    adx_min: float = 20.0
    accept_bars: int = 4
    min_4h_score: int = 1
    cooldown_h: float = 4.0
    max_ext_atr1h: float = 2.5
    max_risk_atr4h: float = 1.8
    hard_risk_atr4h: float = 2.5  # 超过这个距离连趋势都不算（结构太远，失效位没意义）
    climax_mult: float = 2.5
    min_15m: int = 200
    min_1h: int = 60
    min_4h: int = 60
    levels: LevelParams = LevelParams()


@dataclass
class TrendEvent:
    kind: str  # formed / ended
    symbol: str
    d: int
    t: int  # 触发时刻（那根 15m 的收盘时间）
    price: float
    entry_t: int
    entry_price: float
    stop: float  # 当前失效位
    stop0: float  # 形成时的失效位
    grade: str = "B"
    quiet: str = ""  # 非空 = 这段趋势不推送（原因）
    msg_key: str | None = None  # 形成消息的发件箱键（结束消息回复它）
    reason: str = ""  # ended：stop / flip
    peak: float = 0.0  # 形成后最有利的价格
    s15: dict[str, Any] = field(default_factory=dict)
    s1: dict[str, Any] = field(default_factory=dict)
    s4: dict[str, Any] = field(default_factory=dict)
    ext: float = 0.0
    vr: float | None = None
    atr1: float = 0.0
    atr4: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _brief(st: TFState | None) -> dict[str, Any]:
    if st is None:
        return {}
    return {"s": st.s, "e": st.e, "a": st.a, "sc": st.sc, "adx": round(st.adx, 1), "label": st.label()}


@dataclass
class _Group:
    start: int
    end: int
    o: float
    h: float
    l: float
    c: float
    v: float

    def candle(self) -> Candle:
        return Candle(self.start, self.end, self.o, self.h, self.l, self.c, self.v)


DEFAULT_PARAMS = TrendParams()


class SymbolTrend:
    def __init__(self, symbol: str, bucketer: Bucketer, p: TrendParams = DEFAULT_PARAMS):
        self.symbol = symbol
        self.bk = bucketer
        self.p = p
        self.tf15 = TFAnalyzer(p.zigzag_atr, p.adx_min, keep=2000)
        self.tf1 = TFAnalyzer(p.zigzag_atr, p.adx_min, keep=800)
        self.tf4 = TFAnalyzer(p.zigzag_atr, p.adx_min, keep=400)
        self._groups: dict[str, _Group | None] = {"1h": None, "4h": None}
        self._v1: deque[float] = deque(maxlen=720)  # 1h 成交额，算高潮量
        self.active: dict[str, Any] | None = None
        self.last_end: dict[int, int] = {1: -(10**12), -1: -(10**12)}
        self.streak: dict[int, int] = {1: 0, -1: 0}
        self.last_t = 0

    # ---- 状态 ----
    @property
    def ready(self) -> bool:
        return self.tf15.n >= self.p.min_15m and self.tf1.n >= self.p.min_1h and self.tf4.n >= self.p.min_4h

    def set_params(self, p: TrendParams) -> None:
        """只换形成/结束的门槛；折线和 ADX 参数变了要重建（由引擎重新回放历史）。"""
        self.p = p

    def levels(self) -> Levels | None:
        s1 = self.tf1.state
        last = self.tf15.last
        if s1 is None or last is None:
            return None
        return compute_levels(
            last.end, last.c, s1.atr, self.tf1.pivots, self.tf4.pivots, self.tf15.candles, self.bk.day_of, self.p.levels
        )

    def vol_ratio(self) -> float | None:
        if len(self._v1) < 30:
            return None
        recent = list(self._v1)[-4:]
        hist = list(self._v1)[:-4]
        med = statistics.median(hist) if hist else 0.0
        if med <= 0:
            return None
        return (sum(recent) / len(recent)) / med

    # ---- 输入 ----
    def on_candle15(self, k: Candle) -> list[TrendEvent]:
        s15 = self.tf15.add(k)
        new_h = False
        for tf, an in (("1h", self.tf1), ("4h", self.tf4)):
            s, e = self.bk.hkey(tf, k.ts)
            g = self._groups[tf]
            if g is not None and g.start != s:
                self._close(tf, an, g)
                g = None
            if g is None:
                g = _Group(s, e, k.o, k.h, k.l, k.c, k.v)
            else:
                g.h, g.l, g.c, g.v = max(g.h, k.h), min(g.l, k.l), k.c, g.v + k.v
            self._groups[tf] = g
            if k.end >= e:
                self._close(tf, an, g)
                self._groups[tf] = None
                if tf == "1h":
                    new_h = True
        self.last_t = k.end
        if not self.ready:
            return []
        return self._evaluate(k, s15, new_h)

    def _close(self, tf: str, an: TFAnalyzer, g: _Group) -> None:
        an.add(g.candle())
        if tf == "1h":
            self._v1.append(g.v)

    def _evaluate(self, k: Candle, s15: TFState, new_h: bool) -> list[TrendEvent]:
        s1, s4 = self.tf1.state, self.tf4.state
        assert s1 is not None and s4 is not None
        t, x = k.end, k.c
        out: list[TrendEvent] = []
        a = self.active
        if a is not None:
            d = a["d"]
            if new_h:
                if d > 0 and s1.ll is not None and s1.ll > a["stop"] and s1.ll < x:
                    a["stop"] = s1.ll
                if d < 0 and s1.lh is not None and s1.lh < a["stop"] and s1.lh > x:
                    a["stop"] = s1.lh
                brk = s1.close < a["stop"] if d > 0 else s1.close > a["stop"]
                flip = s1.sc * d <= -2 and s4.s != d
                if brk or flip:
                    out.append(self._event("ended", d, k, s15, s1, s4, reason="stop" if brk else "flip"))
                    self.last_end[d] = t
                    self.active = None
            if self.active is not None:
                a["peak"] = max(a["peak"], k.h) if d > 0 else min(a["peak"], k.l)
                return out
        for d in (1, -1):
            c1 = s1.s == d and s1.e == d and s1.sc * d >= 2
            c4 = s4.sc * d >= self.p.min_4h_score and s4.s != -d
            c15 = s15.sc * d >= 2
            self.streak[d] = self.streak[d] + 1 if (c1 and c4 and c15) else 0
            if self.streak[d] < self.p.accept_bars or t - self.last_end[d] < self.p.cooldown_h * 3600:
                continue
            stop = s1.ll if d > 0 else s1.lh
            if stop is None or (stop - x) * d >= 0:
                continue
            risk = abs(x - stop)
            if risk > self.p.hard_risk_atr4h * s4.atr:
                continue
            # 跑远了或者在高潮量里：照样记为趋势（这一段不再重复触发），但不推送。
            # 回测：如果在这里直接跳过，状态机会在同一段行情后面更差的位置再次触发（+0.24R 对 +0.59R）。
            ext = (x - s1.e20) * d / (s1.atr or 1)
            vr = self.vol_ratio()
            quiet = []
            if risk > self.p.max_risk_atr4h * s4.atr:
                quiet.append("离失效位太远")
            if ext > self.p.max_ext_atr1h:
                quiet.append("离均线太远")
            if vr is not None and vr > self.p.climax_mult:
                quiet.append("高潮量")
            grade = "A" if (s4.s == d and s4.e == d) else "B"
            self.active = {
                "d": d,
                "entry_t": t,
                "entry_price": x,
                "stop": stop,
                "stop0": stop,
                "grade": grade,
                "peak": k.h if d > 0 else k.l,
                "msg_key": None,
                "quiet": "、".join(quiet),
            }
            ev = self._event("formed", d, k, s15, s1, s4)
            ev.ext, ev.vr = ext, vr
            out.append(ev)
            self.streak = {1: 0, -1: 0}
            break
        return out

    def _event(self, kind: str, d: int, k: Candle, s15: TFState, s1: TFState, s4: TFState, reason: str = "") -> TrendEvent:
        a = self.active or {}
        return TrendEvent(
            kind=kind,
            symbol=self.symbol,
            d=d,
            t=k.end,
            price=k.c,
            entry_t=a.get("entry_t", k.end),
            entry_price=a.get("entry_price", k.c),
            stop=a.get("stop", 0.0),
            stop0=a.get("stop0", 0.0),
            grade=a.get("grade", "B"),
            quiet=a.get("quiet", ""),
            msg_key=a.get("msg_key"),
            reason=reason,
            peak=a.get("peak", k.c),
            s15=_brief(s15),
            s1=_brief(s1),
            s4=_brief(s4),
            atr1=s1.atr,
            atr4=s4.atr,
        )

    # ---- 查询 ----
    def snapshot(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ready": self.ready,
            "t": self.last_t,
            "price": self.tf15.last.c if self.tf15.last else None,
            "s15": _brief(self.tf15.state),
            "s1": _brief(self.tf1.state),
            "s4": _brief(self.tf4.state),
            "active": dict(self.active) if self.active else None,
            "bars": {"15m": self.tf15.n, "1h": self.tf1.n, "4h": self.tf4.n},
        }
