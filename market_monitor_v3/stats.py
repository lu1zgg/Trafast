"""V4 P3 异动统计：每个市场、每个分组按级别加权（L1 = 1，L2 = 2，L3 = 3）累计涨、跌异动。

只统计，不推送：写进日报、/stats、桌面版和 /api/v1/stats。
一段行情只算一次，级别取它推送过的最高级别（首发或升级）。
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from .models import MARKET_LABELS, market_of

WEIGHTS = {"L1": 1, "L2": 2, "L3": 3}


def episodes_from(signals: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 open / upgrade 信号合并成每段行情一条：symbol、direction、最高级别、类型、首发时间。"""
    eps: dict[str, dict[str, Any]] = {}
    for g in signals:
        if g.get("kind") not in ("open", "upgrade"):
            continue
        eid = g.get("episode_id") or g.get("id")
        cur = eps.get(eid)
        if cur is None:
            if g.get("kind") != "open":
                continue
            eps[eid] = {
                "symbol": g["symbol"], "direction": int(g["direction"]), "level": g.get("level") or "L1",
                "pattern": g.get("pattern"), "ts": g["ts"],
            }
        elif WEIGHTS.get(g.get("level") or "", 0) > WEIGHTS.get(cur["level"], 0):
            cur["level"] = g["level"]
    return list(eps.values())


def tally(eps: Iterable[dict[str, Any]], groups_of: Callable[[str], list[str]]) -> dict[str, Any]:
    def blank() -> dict[str, Any]:
        return {"up": 0, "down": 0, "up_n": 0, "down_n": 0}

    markets: dict[str, dict[str, Any]] = defaultdict(blank)
    groups: dict[str, dict[str, Any]] = defaultdict(blank)
    symbols: dict[str, dict[str, Any]] = defaultdict(blank)
    patterns: dict[str, dict[str, Any]] = defaultdict(blank)
    total = blank()
    for e in eps:
        w = WEIGHTS.get(e["level"], 1)
        side = "up" if e["direction"] > 0 else "down"
        buckets = [total, markets[market_of(e["symbol"])], symbols[e["symbol"]]]
        buckets += [groups[g] for g in groups_of(e["symbol"])]
        if e.get("pattern"):
            buckets.append(patterns[e["pattern"]])
        for b in buckets:
            b[side] += w
            b[side + "_n"] += 1
    for b in [total, *markets.values(), *groups.values(), *symbols.values(), *patterns.values()]:
        s = b["up"] + b["down"]
        b["bull"] = b["up"] / s if s else None  # 多头占比（加权）
    return {"total": total, "markets": dict(markets), "groups": dict(groups), "symbols": dict(symbols), "patterns": dict(patterns)}


def build_stats(signals_today: list[dict[str, Any]], now: float, groups_of: Callable[[str], list[str]], recent_s: int = 1800) -> dict[str, Any]:
    eps = episodes_from(signals_today)
    recent = [e for e in eps if e["ts"] >= now - recent_s]
    return {"today": tally(eps, groups_of), "recent": tally(recent, groups_of), "recent_min": recent_s // 60, "t": now}


def _side(b: dict[str, Any]) -> str:
    bull = b.get("bull")
    ratio = "—" if bull is None else f"多 {bull * 100:.0f}%"
    return f"涨 {b['up_n']}（{b['up']}）跌 {b['down_n']}（{b['down']}）｜{ratio}"


def tally_lines(t: dict[str, Any], top_groups: int = 6) -> list[str]:
    """一个统计窗口的几行：总计、各市场、异动最多的几个分组。"""
    lines = [f"合计：{_side(t['total'])}"]
    for m, b in sorted(t["markets"].items()):
        lines.append(f"{MARKET_LABELS.get(m, m)}：{_side(b)}")
    gs = sorted(t["groups"].items(), key=lambda kv: -(kv[1]["up"] + kv[1]["down"]))[:top_groups]
    lines += [f"{g}：{_side(b)}" for g, b in gs]
    return lines


def stats_lines(st: dict[str, Any], top_groups: int = 6) -> list[str]:
    """给 /stats 用：最近 30 分钟和今天。括号里是按级别加权的分数。"""
    lines = []
    for key, title in (("recent", f"最近 {st['recent_min']} 分钟"), ("today", "今天")):
        t = st[key]
        if not t["total"]["up_n"] and not t["total"]["down_n"]:
            lines.append(f"{title}：没有异动")
            continue
        lines.append(f"{title}")
        lines += ["  " + x for x in tally_lines(t, top_groups)]
    return lines
