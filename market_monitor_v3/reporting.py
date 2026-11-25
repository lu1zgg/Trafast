"""每日报告（默认北京时间 09:05，美股盘后刚结束）。在存储线程里执行。"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import CRYPTO_SESSIONS, SESSION_LABELS, STOCK_SESSIONS, AppConfig, Params
from .models import display_name, market_of
from .patterns import PATTERN_LABELS, PATTERNS
from .render import pct
from .sessions import UsCalendar
from .store import Store

OUTCOME_LABELS = {
    "rejected": "没站稳（插针）",
    "wick_return": "插针回落",
    "retrace": "回吐",
    "thin": "成交太少",
    "stale_price": "疑似旧价（前收回报）",
    "cooldown": "冷却期",
    "muted": "静音",
    "expired_pending": "确认超时",
    "spike_rejected": "急拉急跌多确认没站稳",
    "type_off": "类型已关闭",
}


def max_15m_move(rows: list[tuple[int, float]]) -> float:
    closes = dict(rows)
    best = 0.0
    for ts, c in rows:
        c0 = closes.get(ts - 900)
        if c0 and c > 0:
            d = math.log(c / c0)
            if abs(d) > abs(best):
                best = d
    return best


def build_daily_report(store: Store, cfg: AppConfig, params: Params, cal: UsCalendar, start: float, end: float) -> str:
    c = store.c
    tz = ZoneInfo(cfg.health.report_tz)
    s_txt = datetime.fromtimestamp(start, tz).strftime("%m-%d %H:%M")
    e_txt = datetime.fromtimestamp(end, tz).strftime("%m-%d %H:%M")
    lines = [f"📊 <b>日报</b>（北京 {s_txt} → {e_txt}）", f"规则 {params.rule_version}"]

    # 推送
    pushed: dict[str, int] = defaultdict(int)
    levels: dict[str, int] = defaultdict(int)
    for r in c.execute(
        "SELECT session, level, COUNT(*) AS n FROM alerts WHERE ts>=? AND ts<? AND outcome='pushed' GROUP BY session, level",
        (int(start), int(end)),
    ):
        pushed[r["session"]] += r["n"]
        levels[r["level"]] += r["n"]
    us = sum(pushed[s] for s in STOCK_SESSIONS)
    cr = sum(pushed[s] for s in CRYPTO_SESSIONS)
    us_detail = "、".join(f"{SESSION_LABELS[s]} {pushed[s]}" for s in ("PRE", "REG", "POST", "ON") if pushed[s])
    lines.append(
        f"异动：美股 {us} 个" + (f"（{us_detail}）" if us_detail else "") + f"，加密 {cr} 个；"
        f"L3 {levels['L3']}、L2 {levels['L2']}、L1 {levels['L1']}"
    )

    # V4 形态类型（只统计，推送规则不变）
    kinds: dict[str, int] = defaultdict(int)
    for r in c.execute(
        "SELECT pattern, COUNT(*) AS n FROM alerts WHERE ts>=? AND ts<? AND outcome='pushed' AND pattern IS NOT NULL "
        "GROUP BY pattern",
        (int(start), int(end)),
    ):
        kinds[r["pattern"]] = r["n"]
    if kinds:
        lines.append("类型：" + "，".join(f"{PATTERN_LABELS[p]} {kinds[p]}" for p in PATTERNS if kinds.get(p)))

    # 消息
    sent: dict[str, dict[str, int]] = defaultdict(lambda: {"send": 0, "reply": 0})
    bad = defaultdict(int)
    for r in c.execute(
        "SELECT channel, kind, status, COUNT(*) AS n FROM outbox WHERE created_at>=? AND created_at<? "
        "AND kind IN ('send','reply') GROUP BY channel, kind, status",
        (start, end),
    ):
        if r["status"] == "sent":
            sent[r["channel"]][r["kind"]] += r["n"]
        elif r["status"] in ("failed", "expired", "unknown", "dropped"):
            bad[r["status"]] += r["n"]
    labels = {"telegram": "Telegram", "discord": "Discord"}
    if len(sent) <= 1:
        one = next(iter(sent.values()), {"send": 0, "reply": 0})
        lines.append(f"消息：首发 {one['send']} 条，回复 {one['reply']} 条")
    else:
        lines.append(
            "消息："
            + "；".join(f"{labels.get(ch, ch)} 首发 {v['send']} 条、回复 {v['reply']} 条" for ch, v in sorted(sent.items()))
        )

    # 被挡下的候选
    l0 = {
        r["outcome"]: r["n"]
        for r in c.execute(
            "SELECT outcome, COUNT(*) AS n FROM alerts WHERE ts>=? AND ts<? AND level='L0' GROUP BY outcome",
            (int(start), int(end)),
        )
    }
    blocked = [f"{OUTCOME_LABELS[k]} {l0[k]}" for k in OUTCOME_LABELS if l0.get(k)]
    if blocked:
        lines.append("挡下：" + "，".join(blocked))

    # 每个标的最大 15 分钟波动
    pushed_syms = {
        r["symbol"]
        for r in c.execute(
            "SELECT DISTINCT symbol FROM alerts WHERE ts>=? AND ts<? AND outcome='pushed'", (int(start), int(end))
        )
    }
    items = []
    for sym in cfg.all_symbols():
        rows = [
            (r["ts"], r["c"])
            for r in c.execute(
                "SELECT ts, c FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                (sym, int(start) - 900, int(end)),
            )
        ]
        if not rows:
            items.append(f"{display_name(sym)} 无数据")
            continue
        mv = max_15m_move(rows)
        mark = "✅" if sym in pushed_syms else "—"
        items.append(f"{display_name(sym)} {pct(mv)} {mark}")
    lines.append("最大 15 分钟波动（✅ 有推送）：")
    for i in range(0, len(items), 4):
        lines.append("  " + "｜".join(items[i : i + 4]))

    # 数据覆盖
    cov_parts = []
    if cfg.stocks.enabled:
        expected = 0
        t = int(start) - int(start) % 60
        while t < end:
            if cal.session_at(t) != "CLOSED":
                expected += 1
            t += 60
        real = synth = 0
        for r in c.execute(
            "SELECT synthetic, COUNT(*) AS n FROM bars_1m WHERE ts>=? AND ts<? AND symbol LIKE '%.US' "
            "AND session!='CLOSED' GROUP BY synthetic",
            (int(start), int(end)),
        ):
            if r["synthetic"]:
                synth = r["n"]
            else:
                real = r["n"]
        exp_all = expected * len(cfg.stocks.symbols)
        if exp_all:
            cov_parts.append(
                f"美股 {100 * (real + synth) / exp_all:.1f}%（其中补平 {100 * synth / exp_all:.1f}%）"
            )
        else:
            cov_parts.append("美股 休市")
    if cfg.crypto.enabled:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM bars_1m WHERE ts>=? AND ts<? AND symbol NOT LIKE '%.US'", (int(start), int(end))
        ).fetchone()["n"]
        exp = (end - start) / 60 * len(cfg.crypto.symbols)
        cov_parts.append(f"加密 {100 * n / exp:.1f}%" if exp else "加密 —")
    lines.append("数据覆盖：" + "，".join(cov_parts))

    restarts = c.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='start' AND ts>=? AND ts<?", (start, end)
    ).fetchone()["n"]
    lines.append(
        f"投递：失败 {bad['failed']}，过期 {bad['expired']}，未知 {bad['unknown']}，放弃 {bad['dropped']}；重启 {restarts} 次"
    )

    fb = {
        r["value"]: r["n"]
        for r in c.execute(
            "SELECT value, COUNT(*) AS n FROM feedback WHERE ts>=? AND ts<? GROUP BY value", (start, end)
        )
    }
    lines.append(f"反馈：👍 {fb.get(1, 0)} / 👎 {fb.get(0, 0)}")
    return "\n".join(lines)


__all__ = ["build_daily_report", "market_of"]
