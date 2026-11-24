"""趋势消息模板（Telegram HTML 子集，Discord 由渠道自己转换）。

    📈 🟢 NVDA 上涨趋势形成｜B 级
    现价 $172.40（15m 站稳 1 小时）
    ────────────────
    4h  中性偏多｜ADX 24
    1h  上涨结构（低点抬高）｜ADX 31
    15m 上涨结构（低点抬高）｜ADX 18
    ────────────────
    失效位 $167.80（1h 收盘跌破即结束，-2.7%）
    压力 $177.80–182.00 ★★ 4h+1h（+3.1%）
    支撑 $166.90–168.20 ★★★ 4h+成交密集·失效位（-2.8%）
    ────────────────
    北京 11-23 22:15｜美东 09:15
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import display_name, market_of
from ..render import BJ, DIVIDER, b, price, times_line, usd
from .levels import Levels, Zone
from .machine import TrendEvent

TF_NAMES = (("4h", "s4"), ("1h", "s1"), ("15m", "s15"))


def _dir_word(d: int) -> str:
    return "上涨" if d > 0 else "下跌"


def _chg(a: float, b_: float) -> str:
    x = (a / b_ - 1) * 100 if b_ else 0.0
    return f"{x:+.1f}%"


def _hours(sec: float) -> str:
    h = sec / 3600
    if h < 48:
        return f"{h:.0f} 小时"
    return f"{h / 24:.1f} 天"


def tf_lines(states: dict[str, dict[str, Any]], market: str) -> list[str]:
    out = []
    for name, key in TF_NAMES:
        st = states.get(key) or {}
        if not st:
            continue
        label = name if not (market == "US" and name == "4h") else "半日"
        out.append(f"{label:<4}{st.get('label', '')}｜ADX {st.get('adx', 0):.0f}")
    return out


def zone_text(z: Zone, ref: float, stop: float | None = None, atr1: float = 0.0) -> str:
    tag = z.sources()
    if stop is not None and z.lo - 0.1 * atr1 <= stop <= z.hi + 0.1 * atr1:
        tag += "·失效位"
    mid = (z.lo + z.hi) / 2
    return f"{usd(z.lo)}–{price(z.hi)} {z.stars()} {tag}（{_chg(mid, ref)}）"


def level_lines(lv: Levels | None, show: int, stop: float | None = None, strong_only: bool = True) -> list[str]:
    if lv is None:
        return []
    out = []
    for name, zs in (("压力", lv.res), ("支撑", lv.sup)):
        picked = [z for z in zs if z.strong or not strong_only][:show]
        if not picked:
            out.append(f"{name} 附近暂无强位")
            continue
        for i, z in enumerate(picked):
            out.append(("" if i else f"{name} ") + ("" if not i else "　　 ") + zone_text(z, lv.price, stop, lv.atr1))
    return out


def formed_text(ev: TrendEvent, lv: Levels | None, show: int, accept_bars: int) -> str:
    sym = display_name(ev.symbol)
    market = market_of(ev.symbol)
    icon = "📈 🟢" if ev.d > 0 else "📉 🔴"
    mins = accept_bars * 15
    stand = f"{mins // 60} 小时" if mins % 60 == 0 else f"{mins} 分钟"
    lines = [
        f"{icon} {b(f'{sym} {_dir_word(ev.d)}趋势形成')}｜{ev.grade} 级",
        f"现价 {b(usd(ev.price))}（15m 站稳 {stand}）",
        DIVIDER,
        *tf_lines({"s4": ev.s4, "s1": ev.s1, "s15": ev.s15}, market),
        DIVIDER,
        f"失效位 {b(usd(ev.stop))}（1h 收盘{'跌破' if ev.d > 0 else '突破'}即结束，{_chg(ev.stop, ev.price)}）",
        *level_lines(lv, show, ev.stop),
        DIVIDER,
        times_line(ev.t, market),
    ]
    return "\n".join(lines)


def ended_text(ev: TrendEvent, lv: Levels | None, show: int = 1) -> str:
    sym = display_name(ev.symbol)
    market = market_of(ev.symbol)
    if ev.reason == "stop":
        why = "跌破结构" if ev.d > 0 else "突破结构"
        detail = f"1h 收盘 {usd(ev.price)} {'<' if ev.d > 0 else '>'} 失效位 {usd(ev.stop)}"
    else:
        why = "动能反转"
        detail = f"1h 转为{'空' if ev.d > 0 else '多'}头、4h 结构不再{_dir_word(ev.d)}，现价 {usd(ev.price)}"
    best = "最高" if ev.d > 0 else "最低"
    lines = [
        f"⚪ {b(f'{sym} {_dir_word(ev.d)}趋势结束')}：{why}",
        detail,
        f"本段：形成于 {usd(ev.entry_price)}，{best} {usd(ev.peak)}，持续 {_hours(ev.t - ev.entry_t)}，形成后 {_chg(ev.price, ev.entry_price)}",
    ]
    if lv is not None:
        zs = lv.sup if ev.d > 0 else lv.res
        strong = [z for z in zs if z.strong][:show]
        if strong:
            lines.append(f"{'下方支撑' if ev.d > 0 else '上方压力'} " + zone_text(strong[0], lv.price))
    lines.append(times_line(ev.t, market))
    return "\n".join(lines)


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, BJ).strftime("%m-%d %H:%M")


def overview_text(snaps: list[dict[str, Any]]) -> str:
    lines = [b("趋势状态") + "（15m / 1h / 4h）"]
    if not snaps:
        lines.append("趋势监控没有启用，或者没有标的。")
    for s in snaps:
        name = display_name(s["symbol"])
        a = s.get("active")
        if not s.get("ready"):
            bars = s.get("bars", {})
            lines.append(f"{name}：数据不足（15m {bars.get('15m', 0)}、1h {bars.get('1h', 0)}、4h {bars.get('4h', 0)} 根）")
        elif a:
            icon = "📈" if a["d"] > 0 else "📉"
            quiet = f"，未推送：{a['quiet']}" if a.get("quiet") else ""
            lines.append(
                f"{icon} {name} {_dir_word(a['d'])}趋势｜形成于 {_ts(a['entry_t'])} {usd(a['entry_price'])}｜失效位 {usd(a['stop'])}{quiet}"
            )
        else:
            lines.append(f"— {name} 无趋势｜1h {s.get('s1', {}).get('label', '')}")
    lines.append("看单个标的：/trend 代码，例如 /trend NVDA")
    return "\n".join(lines)


def detail_text(s: dict[str, Any], lv: Levels | None, show: int = 2) -> str:
    name = display_name(s["symbol"])
    market = market_of(s["symbol"])
    lines = [b(f"{name} 趋势详情")]
    if s.get("price") is not None:
        lines.append(f"最近 15m 收盘 {usd(s['price'])}（{_ts(s['t'])}）")
    if not s.get("ready"):
        bars = s.get("bars", {})
        lines.append(f"数据不足，还不能判断（15m {bars.get('15m', 0)}、1h {bars.get('1h', 0)}、4h {bars.get('4h', 0)} 根）")
    a = s.get("active")
    if a:
        lines.append(
            f"{'📈' if a['d'] > 0 else '📉'} {_dir_word(a['d'])}趋势中：形成于 {_ts(a['entry_t'])} {usd(a['entry_price'])}，失效位 {b(usd(a['stop']))}"
            + (f"（这段没有推送：{a['quiet']}）" if a.get("quiet") else "")
        )
    elif s.get("ready"):
        lines.append("当前无趋势")
    lines.append(DIVIDER)
    for name_, key in TF_NAMES:
        st = s.get(key) or {}
        if st:
            label = "半日" if (market == "US" and name_ == "4h") else name_
            lines.append(f"{label:<4}{st['label']}｜结构 {st['s']:+d} 均线 {st['e']:+d} 强度 {st['a']:+d}｜ADX {st['adx']:.0f}")
    if lv is not None:
        lines.append(DIVIDER)
        lines += level_lines(lv, show, a["stop"] if a else None, strong_only=False)
        lines.append("★ 越多越强；只有含 4h 拐点或 3 种来源重叠的强位会出现在推送里")
    return "\n".join(lines)
