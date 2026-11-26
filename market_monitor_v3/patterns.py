"""V4 形态分类和标签（纯函数）。

分类只决定信号叫什么、卡片怎么写，不改变是否触发：入门门仍然是 detector 里的 V3 规则。
按下面的顺序取第一个符合的类型（A = 主窗口起点那根 K 线，d = 主窗口位移）：

1. V 反转（v_reversal）：A 之前 15 分钟走的是反方向，且幅度 ≥ 该标的 15 分钟地板。
2. 盘整突破（breakout）：A 之前 10 根 K 线的振幅 ln(最高/最低) ≤ 0.5·|d|。
3. 急拉急跌（spike）：主窗口就是 1 分钟，或者最后 1 分钟走了 ≥ 50% 的位移。
4. 其余为持续推进（trend）。

放量（vol）是标签，不是类型：最近 3 根 1 分钟 K 线每根成交额 ≥ 3 倍该时段 1 分钟中位数，且 3 根平均 ≥ 6 倍。
回测（2026-11-25，13 个标的 60 天）里放量与否不改变回吐率，所以它只写在卡片上，不参与级别。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .baseline import Baselines
from .config import Params
from .models import Bar, Candidate
from .windows import SymbolHistory

PATTERNS = ("v_reversal", "breakout", "spike", "trend")
PATTERN_LABELS = {
    "v_reversal": "V 反转",
    "breakout": "盘整突破",
    "spike": "急拉急跌",
    "trend": "持续推进",
}
# 卡片标题里按方向的叫法（上涨, 下跌）
PATTERN_NAMES = {
    "v_reversal": ("低位反弹", "高位回落"),
    "breakout": ("盘整突破", "平台跳水"),
    "spike": ("急拉", "急跌"),
    "trend": ("持续上涨", "持续下跌"),
}
TAG_LABELS = {"vol": "放量", "big_buy": "大单净买入", "big_sell": "大单净卖出"}
# /types 和网页后台接受的写法 → 类型
PATTERN_ALIASES = {
    **{p: p for p in PATTERNS},
    **{v: k for k, v in PATTERN_LABELS.items()},
    **{name: k for k, pair in PATTERN_NAMES.items() for name in pair},
    "v反转": "v_reversal", "反转": "v_reversal", "突破": "breakout", "急拉急跌": "spike", "推进": "trend",
}

PRIOR_LEG_MIN = 15  # V 反转看起点之前多少分钟
PRE_BARS = 10  # 盘整看起点之前多少根
BREAKOUT_AMP_RATIO = 0.5
SPIKE_LAST_SHARE = 0.5
VOL_BARS = 3
VOL_BAR_MULT = 3.0
VOL_MEAN_MULT = 6.0


@dataclass(slots=True)
class PatternInfo:
    pattern: str
    tags: list[str] = field(default_factory=list)
    # 分类依据，进 SignalEvent 和回放报告；数值都是对数收益（与方向同号为正）
    prior_leg: float | None = None
    pre_amp: float | None = None
    last_share: float | None = None
    vol_ratio: float | None = None

    def reason(self) -> dict[str, float | None]:
        return {
            "prior_leg": self.prior_leg,
            "pre_amp": self.pre_amp,
            "last_share": self.last_share,
            "vol_ratio": self.vol_ratio,
        }


def classify(cand: Candidate, bar: Bar, hist: SymbolHistory, params: Params, baselines: Baselines) -> PatternInfo:
    p = cand.primary
    absd = abs(p.d)
    direction = cand.direction
    anchor_ts = p.anchor_close_ts - 60
    anchor = hist.get(anchor_ts)

    prior_leg: float | None = None
    b15 = hist.get(anchor_ts - 60 * PRIOR_LEG_MIN)
    if anchor is not None and b15 is not None and anchor.c > 0 and b15.c > 0:
        prior_leg = direction * math.log(anchor.c / b15.c)

    pre_amp: float | None = None
    hi, lo = -math.inf, math.inf
    for q in range(1, PRE_BARS + 1):
        b = hist.get(anchor_ts - 60 * q)
        if b is None or b.l <= 0:
            hi = -math.inf
            break
        hi = max(hi, b.h)
        lo = min(lo, b.l)
    if hi > 0 and lo > 0 and math.isfinite(hi):
        pre_amp = math.log(hi / lo)

    last_share: float | None = None
    prev = hist.get(bar.ts - 60)
    if prev is not None and prev.c > 0 and absd > 0:
        last_share = direction * math.log(bar.c / prev.c) / absd

    fl15 = params.floor(cand.symbol, cand.session, 15)
    if prior_leg is not None and prior_leg <= -fl15:
        pattern = "v_reversal"
    elif pre_amp is not None and pre_amp <= BREAKOUT_AMP_RATIO * absd:
        pattern = "breakout"
    elif p.k == 1 or (last_share is not None and last_share >= SPIKE_LAST_SHARE):
        pattern = "spike"
    else:
        pattern = "trend"

    tags: list[str] = []
    vol_ratio = volume_ratio(bar, hist, baselines)
    if vol_ratio is not None and vol_ratio[0] >= VOL_BAR_MULT and vol_ratio[1] >= VOL_MEAN_MULT:
        tags.append("vol")
    return PatternInfo(
        pattern=pattern,
        tags=tags,
        prior_leg=prior_leg,
        pre_amp=pre_amp,
        last_share=last_share,
        vol_ratio=None if vol_ratio is None else vol_ratio[1],
    )


def volume_ratio(bar: Bar, hist: SymbolHistory, baselines: Baselines) -> tuple[float, float] | None:
    """最近 3 根 K 线相对该时段 1 分钟成交额中位数的倍数：(最小一根, 平均)。基线没有中位数时返回 None。"""
    bl = baselines.get(bar.symbol, bar.session, 1)
    med = bl.turnover_median if bl is not None else None
    if not med:
        return None
    ratios = []
    for j in range(VOL_BARS):
        b = hist.get(bar.ts - 60 * j)
        if b is None or b.synthetic:
            return None
        ratios.append(b.turnover / med)
    return min(ratios), sum(ratios) / len(ratios)


def pattern_label(pattern: str | None) -> str:
    return PATTERN_LABELS.get(pattern or "", "")


def pattern_name(pattern: str | None, direction: int) -> str:
    pair = PATTERN_NAMES.get(pattern or "")
    if pair is None:
        return ""
    return pair[0] if direction > 0 else pair[1]


def parse_pattern(s: str) -> str | None:
    t = s.strip()
    return PATTERN_ALIASES.get(t) or PATTERN_ALIASES.get(t.lower()) or PATTERN_ALIASES.get(t.replace(" ", ""))


def _p(x: float) -> str:
    v = abs(math.exp(abs(x)) - 1) * 100
    return f"{v:.{2 if v < 1 else 1}f}%"


def reason_text(pattern: str, direction: int, k: int, d: float, reason: dict[str, float | None]) -> str:
    """卡片上“为什么是这个类型”的一行。d 是主窗口的对数位移。"""
    up = direction > 0
    move = ("涨" if up else "跌") + " " + _p(d)
    if pattern == "breakout" and reason.get("pre_amp") is not None:
        return f"异动前 {PRE_BARS} 分钟振幅 {_p(reason['pre_amp'])}，之后 {k} 分钟{move}"
    if pattern == "v_reversal" and reason.get("prior_leg") is not None:
        before = ("跌" if up else "涨") + " " + _p(reason["prior_leg"])
        return f"此前 {PRIOR_LEG_MIN} 分钟{before}，随后 {k} 分钟{move}"
    if pattern == "spike":
        if k == 1:
            return f"1 分钟内{move}"
        share = reason.get("last_share")
        tail = f"，最后 1 分钟占 {share * 100:.0f}%" if share is not None else ""
        return f"{k} 分钟{move}{tail}"
    if pattern == "trend":
        return f"{k} 分钟内逐步{move}"
    return ""
