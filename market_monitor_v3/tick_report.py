"""V4 P2 单笔冲涨跌的影子报告：逐笔接受比 K 线推送早多少秒、多少次后来没有过入门门。

输入是 tick_shadow 和 signals 两张表里的记录（都是 JSON 字典），纯函数，CLI 和日报共用。
“推送时间”用 SignalEvent.ts（做出推送决定的那根 K 线的收盘时间），实际发出还要再加几秒。
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .models import display_name

MATCH_BEFORE_S = 120  # K 线信号最多比跳价早这么久仍算同一段（逐笔晚于 K 线）
MATCH_AFTER_S = 600  # 逐笔接受之后这么久内出现同向 K 线信号算“后来过了入门门”


@dataclass
class ShadowSummary:
    jumps: int = 0
    ext_session: int = 0
    accepted: int = 0
    rejected: int = 0
    gate_pass: int = 0
    matched: int = 0  # 过入门门且后来有同向 K 线推送
    unmatched: int = 0  # 过入门门但后来没有 K 线推送（提前推就是一条多余的）
    leads: list[float] = field(default_factory=list)  # 比 K 线推送早多少秒（负数 = 更晚）
    missed: int = 0  # K 线推送的急拉急跌，之前 5 分钟内没有逐笔接受
    spikes: int = 0  # K 线推送的急拉急跌总数（有逐笔覆盖的标的）
    by_symbol: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0, 0]))

    @property
    def lead_median(self) -> float | None:
        return statistics.median(self.leads) if self.leads else None


def summarize(shadows: list[dict[str, Any]], signals: list[dict[str, Any]], covered: set[str]) -> ShadowSummary:
    s = ShadowSummary()
    opens = [g for g in signals if g.get("kind") == "open"]
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in opens:
        by_sym[g["symbol"]].append(g)
    accepted_at: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for sh in shadows:
        s.jumps += 1
        st = sh.get("status")
        row = s.by_symbol[sh["symbol"]]
        row[0] += 1
        if st == "ext_session":
            s.ext_session += 1
            continue
        if st != "accepted":
            s.rejected += 1
            continue
        s.accepted += 1
        acc = float(sh.get("accept_ts") or sh["ts"])
        accepted_at[sh["symbol"]].append((acc, int(sh["direction"])))
        if not sh.get("gate_pass"):
            continue
        s.gate_pass += 1
        row[1] += 1
        cands = [
            g
            for g in by_sym.get(sh["symbol"], [])
            if int(g["direction"]) == int(sh["direction"]) and sh["ts"] - MATCH_BEFORE_S <= g["ts"] <= acc + MATCH_AFTER_S
        ]
        if cands:
            g = min(cands, key=lambda x: abs(x["ts"] - acc))
            s.matched += 1
            row[2] += 1
            s.leads.append(g["ts"] - acc)
        else:
            s.unmatched += 1
    for g in opens:
        if g["symbol"] not in covered or g.get("pattern") != "spike":
            continue
        s.spikes += 1
        if not any(
            d == int(g["direction"]) and g["ts"] - 300 <= t <= g["ts"] for t, d in accepted_at.get(g["symbol"], [])
        ):
            s.missed += 1
    return s


def report_lines(s: ShadowSummary, detail: bool = False) -> list[str]:
    if s.jumps == 0 and s.spikes == 0:
        return ["单笔冲涨跌（影子）：没有记录"]
    lines = [
        f"单笔冲涨跌（影子）：跳价 {s.jumps} 次，市场接受 {s.accepted}，其中过入门门 {s.gate_pass}"
        + (f"；美股盘外不做 {s.ext_session}" if s.ext_session else "")
    ]
    if s.gate_pass:
        lead = s.lead_median
        lead_txt = f"，中位数早 {lead:.0f} 秒" if lead is not None else ""
        lines.append(f"过入门门的 {s.gate_pass} 次：之后有 K 线推送 {s.matched}{lead_txt}；没有 K 线推送 {s.unmatched}（提前推会多出来的）")
    if s.spikes:
        lines.append(f"K 线推送的急拉急跌 {s.spikes} 次，逐笔没有提前接受 {s.missed}")
    if detail:
        for sym, (n, gp, m) in sorted(s.by_symbol.items()):
            lines.append(f"  {display_name(sym)}：跳价 {n}，过入门门 {gp}，后来有推送 {m}")
    return lines
