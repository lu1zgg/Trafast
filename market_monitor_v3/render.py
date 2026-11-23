"""中文消息模板。

正文统一写成 Telegram 支持的 HTML 子集（<b> <i> <code> <a>），其他推送渠道再各自转换
（Discord 见 notifiers/discord.py 的 html_to_markdown）。

一条告警卡片的样子（Telegram 里 <b> 显示为加粗）：

    🔴 NVDA -1.9%｜1 分钟｜L3 剧烈
    现价 $134.03（盘前）｜起点 $136.66
    快速通道：位移很大，收盘即推
    ────────────────
    多窗口
    1 分钟 -1.9%｜5 分钟 -2.4%｜15 分钟 -2.7%
    ────────────────
    成交额
    1 分钟 $3.62M｜该时段常态的 2.1 倍
    ────────────────
    北京 11-23 17:38｜美东 04:38

联动消息里每个标的都用同样的卡片完整展开，事件结束后在各自卡片末尾补一行“已结束”。
"""
from __future__ import annotations

import html
import math
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import SESSION_LABELS
from .models import LEVEL_NAMES, Candidate, Episode, display_name
from .sessions import ET

BJ = ZoneInfo("Asia/Shanghai")
UTC = UTC

UP_ICON = "🟢"
DOWN_ICON = "🔴"
DIVIDER = "─" * 16
GROUP_DIVIDER = "━" * 16
# Telegram 单条消息上限 4096 字符；留出余量给“可能重复”之类的附注
MAX_TEXT = 3800


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


def usd(p: float) -> str:
    """价格：前面加美元符号。加密货币按 USDT 计价，1 USDT 约等于 1 美元，统一显示 $。"""
    return f"${price(p)}"


def compact(x: float) -> str:
    """成交额缩写：3_620_000 → 3.62M，845_300 → 845.30K，1_250_000_000 → 1.25B。"""
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:.2f}B"
    if a >= 1e6:
        return f"{x / 1e6:.2f}M"
    if a >= 1e3:
        return f"{x / 1e3:.2f}K"
    return f"{x:.2f}"


def money(x: float, unit: str = "$") -> str:
    """成交额显示：美元写成 $3.62M，其他单位写成 3.62M USDT。"""
    if unit in ("$", "美元", "USD"):
        return f"${compact(x)}"
    return f"{compact(x)} {unit}"


def arrow(direction: int) -> str:
    return "▲" if direction > 0 else "▼"


def icon(direction: int) -> str:
    """涨跌灯：绿灯上涨，红灯下跌。"""
    return UP_ICON if direction > 0 else DOWN_ICON


def b(s: str) -> str:
    return f"<b>{s}</b>"


def times_line(ts: float, market: str) -> str:
    bj = datetime.fromtimestamp(ts, BJ).strftime("%m-%d %H:%M")
    if market == "US":
        other = "美东 " + datetime.fromtimestamp(ts, ET).strftime("%H:%M")
    else:
        other = "UTC " + datetime.fromtimestamp(ts, UTC).strftime("%H:%M")
    return f"北京 {bj}｜{other}"


def esc(s: str) -> str:
    return html.escape(s, quote=False)


# ---- 告警卡片 ----
def card_from(c: Candidate, level: str, retained: float | None, current_price: float, close_ts: int, ep_id: str = "") -> dict[str, Any]:
    """把一次触发整理成可序列化的卡片数据。retained 为 None 表示走了快速通道。

    卡片存进 roots 表，事件结束或有新标的加入时整条消息按卡片重新渲染。
    """
    p = c.primary
    return {
        "ep": ep_id,
        "symbol": c.symbol,
        "market": c.market,
        "direction": c.direction,
        "level": level,
        "session": c.session,
        "k": p.k,
        "d": p.d,
        "price": current_price,
        "p0": p.p0,
        "retained": retained,
        "hits": [[h.k, h.d] for h in sorted(c.hits, key=lambda h: h.k)],
        "turnover": p.turnover,
        "turnover_ratio": p.turnover_ratio,
        "close_ts": close_ts,
        "end": None,
    }


def card_headline(card: dict[str, Any]) -> str:
    level = card["level"]
    return (
        f"{icon(card['direction'])} {b(esc(display_name(card['symbol'])))} {b(pct(card['d']))}"
        f"｜{card['k']} 分钟｜{level} {LEVEL_NAMES[level]}"
    )


def card_text(card: dict[str, Any]) -> str:
    """一个标的的完整卡片。"""
    lines = [card_headline(card)]
    sess = SESSION_LABELS.get(card["session"], card["session"])
    sess_txt = f"（{sess}）" if card["market"] == "US" else ""
    lines.append(f"现价 {b(usd(card['price']))}{sess_txt}｜起点 {b(usd(card['p0']))}")
    if card["retained"] is None:
        lines.append("快速通道：位移很大，收盘即推")
    else:
        kept = f"{card['retained'] * 100:.0f}%"
        lines.append(f"站稳：下一分钟仍保留 {b(kept)}")
    hits = card.get("hits") or []
    if len(hits) > 1:
        lines += [DIVIDER, "多窗口", "｜".join(f"{k} 分钟 {b(pct(d))}" for k, d in hits)]
    if card.get("turnover", 0) > 0:
        t = f"{card['k']} 分钟 {b(money(card['turnover']))}"
        if card.get("turnover_ratio") is not None:
            ratio = f"{card['turnover_ratio']:.1f} 倍"
            t += f"｜该时段常态的 {b(ratio)}"
        lines += [DIVIDER, "成交额", t]
    lines += [DIVIDER, times_line(card["close_ts"], card["market"])]
    if card.get("end"):
        lines.append(end_text(card["end"]))
    return "\n".join(lines)


def card_compact(card: dict[str, Any]) -> str:
    """消息太长时剩余标的的一行写法。"""
    s = f"{card_headline(card)}｜现价 {b(usd(card['price']))}"
    if card.get("end"):
        s += "｜" + end_text(card["end"])
    return s


def cards_text(cards: list[dict[str, Any]]) -> str:
    """整条联动消息：第一张卡片是触发最强的标的，其余在“同时异动”下面逐个完整展开。"""
    parts = [card_text(cards[0])]
    rest = cards[1:]
    if rest:
        parts += [GROUP_DIVIDER, b(f"同时异动（{len(rest)}）")]
        size = sum(_plain_len(p) + 1 for p in parts)
        compact_mode = False
        for i, card in enumerate(rest):
            item = card_compact(card) if compact_mode else card_text(card)
            if not compact_mode and size + _plain_len(item) + 1 > MAX_TEXT:
                compact_mode = True
                item = card_compact(card)
            if size + _plain_len(item) + 1 > MAX_TEXT:
                parts.append(f"……另有 {len(rest) - i} 个标的未展开")
                break
            if i and not compact_mode:
                parts.append(GROUP_DIVIDER)
                size += len(GROUP_DIVIDER) + 1
            parts.append(item)
            size += _plain_len(item) + 1
    return "\n".join(parts)


def _plain_len(s: str) -> int:
    """Telegram 的长度上限按去掉标签后的文字计算。"""
    return len(to_plain(s))


REASONS = {"retrace": "", "idle": "", "reversal": "（反向剧烈波动，另起一条）"}


def end_info(ep: Episode) -> dict[str, Any]:
    mins = max(1, round(((ep.ended_at or ep.last_extreme_at) - ep.anchor_close_ts) / 60))
    end_move = ep.end_move if ep.end_move is not None else 0.0
    return {
        "max": ep.direction * ep.extreme,
        "close": ep.direction * end_move,
        "minutes": mins,
        "reason": ep.end_reason or "",
    }


def end_text(info: dict[str, Any]) -> str:
    return (
        f"已结束：最大 {b(pct(info['max']))}，收于 {b(pct(info['close']))}，"
        f"持续 {b(str(info['minutes']))} 分钟{REASONS.get(info.get('reason') or '', '')}"
    )


# ---- 旧版文本（升级前存进数据库的消息仍按旧格式编辑；摘要消息也用一行一个标的的写法） ----
def lead_block(c: Candidate, level: str, retained: float | None, current_price: float, close_ts: int) -> str:
    return card_text(card_from(c, level, retained, current_price, close_ts))


def member_item(c: Candidate) -> str:
    return f"{icon(c.direction)} {b(esc(display_name(c.symbol)))} {b(pct(c.primary.d))}"


def joiner_reply(c: Candidate, level: str) -> str:
    return (
        f"↳ 联动升级：{icon(c.direction)} {b(esc(display_name(c.symbol)))} {b(pct(c.primary.d))}"
        f"｜{c.primary.k} 分钟｜{level} {LEVEL_NAMES[level]}｜现价 {b(usd(c.price))}"
    )


def extension_reply(ep: Episode, move: float, minutes: int, upgrade: str | None) -> str:
    s = (
        f"↳ 延伸：{icon(ep.direction)} {b(esc(display_name(ep.symbol)))} {b(pct(ep.direction * move))}"
        f"（{minutes} 分钟）｜现价 {b(usd(ep.last_price))}"
    )
    if upgrade:
        s += f"｜升级 {upgrade} {LEVEL_NAMES[upgrade]}"
    return s


def end_line(ep: Episode, with_name: bool) -> str:
    name = f"{icon(ep.direction)} {b(esc(display_name(ep.symbol)))} " if with_name else ""
    return name + end_text(end_info(ep))


def summary_header(n: int) -> str:
    return f"⚠️ 1 分钟内异动过多，以下 {n} 组合并为一条："


def summary_line(c: Candidate, level: str, current_price: float, others: list[str]) -> str:
    s = (
        f"{icon(c.direction)} {b(esc(display_name(c.symbol)))} {b(pct(c.primary.d))}"
        f"｜{c.primary.k} 分钟｜{level}｜现价 {b(usd(current_price))}"
    )
    if others:
        s += "\n    同时：" + "、".join(others)
    return s


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
