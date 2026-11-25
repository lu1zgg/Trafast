"""触发规则（纯函数）。

d_k = ln(P_t / P_{t-k})，k = 1, 5, 15 分钟，P 是 1 分钟收盘价。
候选：|d| ≥ max(F·m, Z·σ)（基线就绪时）或 |d| ≥ A（大幅直通，不依赖基线）。
窗口跨时段时，用两端时段里更严的门槛。
稀薄时段（夜盘、早盘前）还要求窗口成交额不低于该时段中位数的 25%，
并且“走出这段位移的那几根 K 线”（按 |收益| 加权）的成交额也不低于这个比例。
得分 = |d| / 门槛；只由直通线触发时 = 2·|d| / A。
score ≥ 2 或触发直通 → 快速通道 L3；否则 score ≥ 1.5、多窗口同向或成交额 ≥ 3 倍常态 → L2；其余 L1。
候选最后交给 patterns.classify 打上形态类型和放量标签（V4），不改变是否触发和级别。
"""
from __future__ import annotations

import math
from collections.abc import Callable

from .baseline import Baselines
from .config import WINDOWS, Params
from .models import Bar, Candidate, Evaluation, WindowHit, market_of
from .patterns import classify
from .windows import SymbolHistory


def evaluate(
    bar: Bar,
    hist: SymbolHistory,
    params: Params,
    baselines: Baselines,
    is_thin: Callable[[float, str], bool],
) -> Evaluation:
    sym = bar.symbol
    mult = params.mult(sym)
    rules = params.rules
    hits: list[WindowHit] = []
    thin_blocked: list[WindowHit] = []
    warmup = False
    if bar.c <= 0:
        return Evaluation()

    for i, k in enumerate(WINDOWS):
        b0 = hist.get(bar.ts - 60 * k)
        if b0 is None or b0.c <= 0:
            continue
        d = math.log(bar.c / b0.c)
        if d == 0.0:
            continue
        sessions = [s for s in {bar.session, b0.session} if s in params.sessions]
        if not sessions:
            continue

        floor = 0.0
        bypass = 0.0
        threshold: float | None = 0.0
        sigma: float | None = None
        for s in sessions:
            st = params.sessions[s]
            f = st.floor[i] * mult
            floor = max(floor, f)
            bypass = max(bypass, st.bypass[i])
            bl = baselines.get(sym, s, k)
            if bl is None or not bl.ready:
                threshold = None
                continue
            t = max(f, st.z * bl.sigma)
            if threshold is not None and t >= threshold:
                threshold = t
                sigma = bl.sigma
        if threshold is None:
            warmup = True

        ad = abs(d)
        by_z = threshold is not None and ad >= threshold
        by_bypass = ad >= bypass
        if not (by_z or by_bypass):
            continue

        turnover = hist.turnover(bar.ts, k)
        bl_now = baselines.get(sym, bar.session, k)
        med = bl_now.turnover_median if bl_now is not None else None
        ratio = turnover / med if med else None
        score = ad / threshold if by_z and threshold else 2.0 * ad / bypass

        hit = WindowHit(
            k=k,
            d=d,
            p0=b0.c,
            anchor_close_ts=b0.close_ts,
            threshold=threshold,
            floor=floor,
            sigma=sigma,
            bypass=bypass,
            by_z=by_z,
            by_bypass=by_bypass,
            score=score,
            turnover=turnover,
            turnover_ratio=ratio,
        )
        if is_thin(bar.ts, bar.session) and med:
            need = rules.thin_turnover_frac * med
            moved = hist.move_turnover(bar.ts, k)
            if turnover < need or (moved is not None and moved < need):
                thin_blocked.append(hit)
                continue
        hits.append(hit)

    if not hits:
        return Evaluation(None, thin_blocked)

    primary = max(hits, key=lambda h: h.score)
    direction = 1 if primary.d > 0 else -1
    same = [h for h in hits if (h.d > 0) == (direction > 0)]
    fast = any(h.by_bypass or h.score >= rules.fast_score for h in same)
    if fast:
        level = "L3"
    elif (
        primary.score >= rules.l2_score
        or len(same) >= 2
        or (primary.turnover_ratio is not None and primary.turnover_ratio >= rules.l2_turnover_mult)
    ):
        level = "L2"
    else:
        level = "L1"
    cand = Candidate(
        symbol=sym,
        market=market_of(sym),
        bar_ts=bar.ts,
        close_ts=bar.close_ts,
        session=bar.session,
        price=bar.c,
        direction=direction,
        hits=same,
        primary=primary,
        score=primary.score,
        fast=fast,
        level=level,
        warmup=warmup,
    )
    info = classify(cand, bar, hist, params, baselines)
    cand.pattern = info.pattern
    cand.tags = info.tags
    cand.reason = info.reason()
    return Evaluation(cand, thin_blocked)
