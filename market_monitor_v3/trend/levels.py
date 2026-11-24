"""支撑位 / 压力位：多来源打分，相近的合并成区间，只把“强位”推给用户。

来源和权重（见方案文档）：
  4h 波段高低点 2.5（近 120 天）、前一日最高/最低 1.5、成交密集区 1.5–3（近 10 天 15m）、1h 波段高低点 1（近 30 天）
每个来源按半衰期打折；相距不到 tol × ATR(1h) 的合并成一个区间，得分 = 权重和 + 每多一种来源加 0.5。
强位：区间里有 4h 拐点，或者有 ≥3 种来源。回测里只有这两类的反应明显好于随机位置。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .series import Candle, Pivot

DAY = 86400


@dataclass
class Zone:
    lo: float
    hi: float
    w: float
    n: int
    kinds: set[str] = field(default_factory=set)
    wsum: float = 0.0  # Σ price × weight
    mid: float = 0.0
    score: float = 0.0

    @property
    def strong(self) -> bool:
        return "4h" in self.kinds or len(self.kinds) >= 3

    def stars(self) -> str:
        n = 3 if self.score >= 6 else 2 if self.score >= 3.5 else 1
        return "★" * n

    def sources(self) -> str:
        names = {"4h": "4h", "1h": "1h", "PDH": "前日高", "PDL": "前日低", "VP": "成交密集"}
        order = ["4h", "1h", "VP", "PDH", "PDL"]
        return "+".join(names[k] for k in order if k in self.kinds)


@dataclass
class Levels:
    price: float
    atr1: float
    res: list[Zone]  # 由近到远
    sup: list[Zone]


@dataclass(frozen=True)
class LevelParams:
    tol_atr: float = 0.4
    half_life_d: float = 15.0
    vp_days: float = 10.0
    min_score: float = 1.5
    pivots_1h_days: float = 30.0
    pivots_4h_days: float = 120.0


def prior_day_range(candles: Sequence[Candle], day_of) -> tuple[float, float] | None:  # noqa: ANN001
    """最后一根 K 线所在“交易日”之前那一天的最高/最低。day_of(ts) 返回日期键。"""
    if not candles:
        return None
    today = day_of(candles[-1].ts)
    prev_key = None
    hi, lo = float("-inf"), float("inf")
    for k in reversed(candles):
        key = day_of(k.ts)
        if key == today:
            continue
        if prev_key is None:
            prev_key = key
        if key != prev_key:
            break
        hi, lo = max(hi, k.h), min(lo, k.l)
    if prev_key is None:
        return None
    return hi, lo


def compute_levels(
    now: int,
    price: float,
    atr1: float,
    pivots_1h: Iterable[Pivot],
    pivots_4h: Iterable[Pivot],
    candles_15m: Sequence[Candle],
    day_of,  # noqa: ANN001
    p: LevelParams | None = None,
) -> Levels:
    p = p or LevelParams()
    cand: list[tuple[float, float, str]] = []

    def add(px: float, w: float, ts: float, kind: str) -> None:
        age = (now - ts) / DAY
        cand.append((px, w * 0.5 ** (age / p.half_life_d), kind))

    for pv in pivots_1h:
        if pv.confirmed_at <= now and now - pv.ts < p.pivots_1h_days * DAY:
            add(pv.price, 1.0, pv.ts, "1h")
    for pv in pivots_4h:
        if pv.confirmed_at <= now and now - pv.ts < p.pivots_4h_days * DAY:
            add(pv.price, 2.5, pv.ts, "4h")
    pr = prior_day_range(candles_15m, day_of)
    if pr is not None:
        add(pr[0], 1.5, now, "PDH")
        add(pr[1], 1.5, now, "PDL")
    if atr1 > 0 and candles_15m:
        bw = 0.25 * atr1
        bins: dict[int, float] = {}
        tot = 0.0
        for k in reversed(candles_15m):
            if k.ts <= now - p.vp_days * DAY:
                break
            m = (k.h + k.l + k.c) / 3
            key = round(m / bw)
            bins[key] = bins.get(key, 0.0) + k.v
            tot += k.v
        if bins and tot > 0:
            avg = tot / len(bins)
            for key in sorted(bins):
                v = bins[key]
                if v > 1.8 * avg and all(v >= bins.get(key + j, 0.0) for j in (-2, -1, 1, 2)):
                    add(key * bw, 1.5 * min(v / avg / 2, 2), now, "VP")

    cand.sort(key=lambda x: x[0])
    tol = p.tol_atr * atr1
    zones: list[Zone] = []
    for px, w, kind in cand:
        z = zones[-1] if zones else None
        if z is not None and px - z.hi <= tol:
            z.hi = max(z.hi, px)
            z.w += w
            z.n += 1
            z.kinds.add(kind)
            z.wsum += px * w
        else:
            zones.append(Zone(px, px, w, 1, {kind}, px * w))
    for z in zones:
        z.mid = z.wsum / z.w if z.w else (z.lo + z.hi) / 2
        z.score = z.w + 0.5 * (len(z.kinds) - 1)
        if z.hi - z.lo < 0.2 * atr1:
            z.lo = min(z.lo, z.mid - 0.1 * atr1)
            z.hi = max(z.hi, z.mid + 0.1 * atr1)
    ok = [z for z in zones if z.score >= p.min_score]
    res = sorted((z for z in ok if z.lo > price + 0.15 * atr1), key=lambda z: z.lo)
    sup = sorted((z for z in ok if z.hi < price - 0.15 * atr1), key=lambda z: -z.hi)
    return Levels(price, atr1, res, sup)
