"""V4 P3 关键位突破 / 跌破（设计文档第 8 节）。

- 只看强位（区间里有 4h 拐点，或 ≥ 3 种来源），和趋势监控推送里列的一样。
- 15 分钟收盘越过区间边缘 ≥ 0.25 × ATR(1h) 记为“待确认”；下一根 15 分钟收盘仍在区间外面才推。
- 同一区间（同方向、区间有重叠）4 小时内不重复；一条价格异动刚穿过这个区间时，异动卡片上加一行，
  这里也记下来，不再另发。
- 美股和趋势监控一样只用正常盘的 15 分钟 K 线（由分桶器保证）。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import display_name, market_of
from ..render import DIVIDER, b, price, times_line, usd
from .levels import Levels, Zone

ATR_MULT = 0.25
DEDUP_S = 4 * 3600


@dataclass
class LevelBreak:
    symbol: str
    d: int  # +1 突破压力，−1 跌破支撑
    lo: float
    hi: float
    stars: str
    src: str
    t: int  # 确认那根 15m 的收盘时间
    t0: int  # 第一次越过的那根 15m 的收盘时间
    price: float
    atr1: float

    def to_json(self) -> dict:
        return {
            "symbol": self.symbol, "d": self.d, "lo": self.lo, "hi": self.hi, "stars": self.stars, "src": self.src,
            "t": self.t, "t0": self.t0, "price": self.price, "atr1": self.atr1,
        }


@dataclass
class _Pending:
    d: int
    zone: Zone
    t0: int
    atr1: float


class BreakoutWatch:
    def __init__(self, atr_mult: float = ATR_MULT, dedup_s: int = DEDUP_S):
        self.atr_mult = atr_mult
        self.dedup_s = dedup_s
        self.pending: dict[str, list[_Pending]] = {}
        self.recent: dict[str, list[tuple[float, float, int, float]]] = {}  # (lo, hi, d, t)

    def note(self, symbol: str, lo: float, hi: float, d: int, t: float) -> None:
        lst = [r for r in self.recent.get(symbol, []) if t - r[3] < self.dedup_s]
        lst.append((lo, hi, d, t))
        self.recent[symbol] = lst

    def is_dup(self, symbol: str, lo: float, hi: float, d: int, t: float) -> bool:
        return any(
            rd == d and t - rt < self.dedup_s and lo <= rhi and hi >= rlo for rlo, rhi, rd, rt in self.recent.get(symbol, [])
        )

    def on_candle(self, symbol: str, before: Levels | None, close: float, end: int) -> list[LevelBreak]:
        """before 是这根 15m 收盘之前的强位（压力在价格上方，支撑在下方）。返回这根确认的突破。"""
        out: list[LevelBreak] = []
        for p in self.pending.pop(symbol, []):
            z = p.zone
            still = close > z.hi if p.d > 0 else close < z.lo
            if still and not self.is_dup(symbol, z.lo, z.hi, p.d, end):
                out.append(LevelBreak(symbol, p.d, z.lo, z.hi, z.stars(), z.sources(), end, p.t0, close, p.atr1))
                self.note(symbol, z.lo, z.hi, p.d, end)
        if before is None or before.atr1 <= 0:
            return out
        gap = self.atr_mult * before.atr1
        crossed: list[tuple[int, Zone]] = []
        crossed += [(1, z) for z in before.res if z.strong and close >= z.hi + gap]
        crossed += [(-1, z) for z in before.sup if z.strong and close <= z.lo - gap]
        if crossed:
            # 一根 K 线穿过好几个区间时只跟最强的那个
            d, z = max(crossed, key=lambda x: x[1].score)
            if not self.is_dup(symbol, z.lo, z.hi, d, end):
                self.pending[symbol] = [_Pending(d, z, end, before.atr1)]
        return out


def crossing(lv: Levels | None, p0: float, p1: float, direction: int) -> Zone | None:
    """一段价格异动（从 p0 到 p1）是否整个穿过了一个强位：返回最强的那个。"""
    if lv is None:
        return None
    lo_p, hi_p = min(p0, p1), max(p0, p1)
    zones = [*lv.res, *lv.sup]
    if direction > 0:
        hit = [z for z in zones if z.strong and p0 < z.lo and p1 > z.hi]
    else:
        hit = [z for z in zones if z.strong and p0 > z.hi and p1 < z.lo]
    hit = [z for z in hit if lo_p < z.lo and z.hi < hi_p]
    return max(hit, key=lambda z: z.score) if hit else None


def level_word(d: int) -> str:
    return "突破压力区" if d > 0 else "跌破支撑区"


def break_text(ev: LevelBreak) -> str:
    sym = display_name(ev.symbol)
    icon = "🧱 🟢" if ev.d > 0 else "🧱 🔴"
    edge = ev.hi if ev.d > 0 else ev.lo
    dist = (ev.price / edge - 1) * 100
    lines = [
        f"{icon} {b(f'{sym} {level_word(ev.d)}')} {usd(ev.lo)}–{price(ev.hi)} {ev.stars}",
        f"现价 {b(usd(ev.price))}（离区间{'上沿' if ev.d > 0 else '下沿'} {dist:+.1f}%）｜来源 {ev.src}",
        "两根 15 分钟收盘都在区间外，算站稳",
        DIVIDER,
        times_line(ev.t, market_of(ev.symbol)),
    ]
    return "\n".join(lines)
