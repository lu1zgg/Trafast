"""中文消息模板（Telegram HTML）。"""
from __future__ import annotations

import html
import math
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import SESSION_LABELS
from .models import LEVEL_NAMES, Candidate, Episode, display_name
from .sessions import ET

BJ = ZoneInfo("Asia/Shanghai")
UTC = UTC


def pct(logret: float, signed: bool = True) -> str:
    """对数位移转成普通涨跌幅显示。"""
    x = (math.exp(logret) - 1) * 100
    a = abs(x)
    digits = 2 if a < 1 else 1
    s = f"{x:+.{digits}f}%" if signed else f"{a:.{digits}f}%"
    return s


def price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.4f}"


def money(x: float, unit: str) -> str:
    sep = " " if unit.isascii() else ""
    if x >= 1e8:
        return f"{x / 1e8:.2f} 亿{sep}{unit}"
    if x >= 1e4:
        return f"{x / 1e4:.0f} 万{sep}{unit}"
    return f"{x:.0f} {unit}"


def arrow(direction: int) -> str:
    return "▲" if direction > 0 else "▼"


def times_line(ts: float, market: str) -> str:
    bj = datetime.fromtimestamp(ts, BJ).strftime("%m-%d %H:%M")
    if market == "US":
        other = "美东 " + datetime.fromtimestamp(ts, ET).strftime("%H:%M")
    else:
        other = "UTC " + datetime.fromtimestamp(ts, UTC).strftime("%H:%M")
    return f"北京 {bj}｜{other}"


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def lead_block(c: Candidate, level: str, retained: float | None, current_price: float, close_ts: int) -> str:
    """首条消息正文。c 是触发的候选，level 是最终级别，retained 为 None 表示走了快速通道。"""
    name = esc(display_name(c.symbol))
    p = c.primary
    unit = "美元" if c.market == "US" else "USDT"
    lines = [f"{arrow(c.direction)} <b>{name}</b> {pct(p.d)}｜{p.k} 分钟｜{level} {LEVEL_NAMES[level]}"]
    sess = SESSION_LABELS.get(c.session, c.session)
    sess_txt = f"（{sess}）" if c.market == "US" else ""
    lines.append(f"现价 {price(current_price)}{sess_txt}｜起点 {price(p.p0)}")
    if retained is None:
        lines.append("快速通道：位移很大，收盘即推")
    else:
        lines.append(f"站稳：下一分钟仍保留 {retained * 100:.0f}%")
    if len(c.hits) > 1:
        lines.append("多窗口：" + "，".join(f"{h.k} 分钟 {pct(h.d)}" for h in sorted(c.hits, key=lambda h: h.k)))
    if p.turnover > 0:
        t = f"成交额：{p.k} 分钟 {money(p.turnover, unit)}"
        if p.turnover_ratio is not None:
            t += f"，为该时段常态 {p.turnover_ratio:.1f} 倍"
        lines.append(t)
    lines.append(times_line(close_ts, c.market))
    return "\n".join(lines)


def member_item(c: Candidate) -> str:
    return f"{esc(display_name(c.symbol))} {pct(c.primary.d)}"


def joiner_reply(c: Candidate, level: str) -> str:
    return f"↳ 联动：{arrow(c.direction)} <b>{esc(display_name(c.symbol))}</b> {pct(c.primary.d)}｜{c.primary.k} 分钟｜{level} {LEVEL_NAMES[level]}"


def extension_reply(ep: Episode, move: float, minutes: int, upgrade: str | None) -> str:
    s = f"↳ 延伸：{esc(display_name(ep.symbol))} {pct(ep.direction * move)}（{minutes} 分钟）"
    if upgrade:
        s += f"，升级 {upgrade} {LEVEL_NAMES[upgrade]}"
    return s


REASONS = {"retrace": "", "idle": "", "reversal": "（反向剧烈波动，另起一条）"}


def end_line(ep: Episode, with_name: bool) -> str:
    name = f"{esc(display_name(ep.symbol))} " if with_name else ""
    mins = max(1, round(((ep.ended_at or ep.last_extreme_at) - ep.anchor_close_ts) / 60))
    end_move = ep.end_move if ep.end_move is not None else 0.0
    return (
        f"{name}已结束：最大 {pct(ep.direction * ep.extreme)}，收于 {pct(ep.direction * end_move)}，"
        f"持续 {mins} 分钟{REASONS.get(ep.end_reason or '', '')}"
    )


def summary_header(n: int) -> str:
    return f"⚠️ 1 分钟内异动过多，以下 {n} 组合并为一条："


def feedback_buttons(root_key: str) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "👍 有用", "callback_data": f"fb|{root_key}|1"},
            {"text": "👎 没用", "callback_data": f"fb|{root_key}|0"},
        ]
    ]


_TAG = re.compile(r"<[^>]+>")


def to_plain(text: str) -> str:
    return html.unescape(_TAG.sub("", text))
