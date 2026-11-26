"""V4 P3：监控进程这一侧的 /api/v1 数据（经本机控制通道给网页后台）。

网页后台进程不直接读数据库，所有数据都从这里拿；返回值都要能 JSON 序列化。
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .config import SESSION_LABELS
from .models import display_name, market_of
from .stats import build_stats
from .store import Store

if TYPE_CHECKING:
    from .app import Runtime

API_COMMANDS = {"api_signals", "api_symbols", "api_quotes", "api_bars", "api_levels", "api_stats", "api_feedback"}


def day_start(now: float, tz: str) -> float:
    d = datetime.fromtimestamp(now, ZoneInfo(tz))
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


async def stats_json(rt: "Runtime", now: float) -> dict[str, Any]:
    start = day_start(now, rt.cfg.health.report_tz)
    sigs = await rt.store.call(Store.signals_between, start, now + 60)
    return build_stats(sigs, now, rt.cfg.groups_of)


def _quote(rt: "Runtime", sym: str, now: float) -> dict[str, Any]:
    assert rt.engine is not None
    h = rt.engine.hist.get(sym)
    last = h.last() if h is not None else None
    ref = rt.engine.custom.day_ref(sym)
    price = last.c if last is not None else None
    return {
        "symbol": sym,
        "name": display_name(sym),
        "price": price,
        "ts": last.close_ts if last is not None else None,
        "session": last.session if last is not None else None,
        "day_ref": ref,
        "day_chg": (price / ref - 1) if price and ref else None,
    }


async def handle(rt: "Runtime", cmd: str, req: dict[str, Any]) -> dict[str, Any]:
    assert rt.engine is not None
    now = rt.clock()
    if cmd == "api_signals":
        limit = max(1, min(500, int(req.get("limit") or 100)))
        sym = rt.resolve_symbol(str(req["symbol"])) if req.get("symbol") else None
        if req.get("symbol") and sym is None:
            return {"ok": False, "error": "没找到标的"}
        if req.get("latest"):
            items = await rt.store.call(Store.api_signals_latest, limit, sym)
            nxt = await rt.store.call(Store.max_signal_seq)
            return {"ok": True, "items": items, "next": nxt}
        after = max(0, int(req.get("after") or 0))
        items = await rt.store.call(Store.api_signals, after, limit, sym, int(req.get("since") or 0))
        return {"ok": True, "items": items, "next": items[-1]["seq"] if items else after}

    if cmd == "api_symbols":
        muted = {k for k, v in rt.engine.mutes.items() if v > now}
        items = []
        for sym in rt.cfg.all_symbols():
            q = _quote(rt, sym, now)
            q.update(
                market=market_of(sym),
                groups=rt.cfg.groups_of(sym),
                muted=sym in muted or "*" in muted,
                session_label=SESSION_LABELS.get(q["session"] or "", ""),
            )
            items.append(q)
        return {"ok": True, "items": items, "groups": rt.cfg.groups}

    if cmd == "api_quotes":
        return {"ok": True, "items": [_quote(rt, s, now) for s in rt.cfg.all_symbols()]}

    if cmd == "api_bars":
        sym = rt.resolve_symbol(str(req.get("symbol") or ""))
        if sym is None:
            return {"ok": False, "error": "没找到标的"}
        tf = str(req.get("tf") or "1m")
        limit = max(1, min(2000, int(req.get("limit") or 300)))
        if tf == "1m":
            bars = await rt.store.call(Store.bars_last, sym, limit)
            items = [{"t": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.turnover, "s": b.session} for b in bars]
        elif tf == "15m":
            rows = await rt.store.call(Store.trend_bars_last, sym, limit)
            items = [{"t": r[0], "o": r[2], "h": r[3], "l": r[4], "c": r[5], "v": r[6]} for r in rows]
        else:
            return {"ok": False, "error": "tf 只支持 1m、15m"}
        return {"ok": True, "symbol": sym, "tf": tf, "items": items}

    if cmd == "api_levels":
        sym = rt.resolve_symbol(str(req.get("symbol") or ""))
        if sym is None:
            return {"ok": False, "error": "没找到标的"}
        if rt.trend is None or sym not in rt.trend.machines:
            return {"ok": True, "symbol": sym, "enabled": False, "res": [], "sup": []}
        m = rt.trend.machines[sym]
        lv = m.levels() if m.tf1.state else None

        def zones(zs: list[Any]) -> list[dict[str, Any]]:
            return [{"lo": z.lo, "hi": z.hi, "stars": z.stars(), "src": z.sources(), "strong": z.strong, "score": z.score} for z in zs[:5]]

        return {
            "ok": True,
            "symbol": sym,
            "enabled": True,
            "trend": m.snapshot(),
            "atr1h": lv.atr1 if lv else None,
            "res": zones(lv.res) if lv else [],
            "sup": zones(lv.sup) if lv else [],
        }

    if cmd == "api_stats":
        return {"ok": True, **(await stats_json(rt, now))}

    if cmd == "api_feedback":
        root = await rt.store.call(Store.episode_root, str(req.get("episode_id") or ""))
        if not root:
            return {"ok": False, "error": "没找到这条信号的消息"}
        value = 1 if int(req.get("value") or 0) else 0
        user = str(req.get("user") or "api:0")
        uid = -int(user.split(":", 1)[1]) if user.split(":", 1)[-1].isdigit() else -1
        await rt.store.call(Store.feedback_set, root, uid, value)
        return {"ok": True, "root_key": root}

    return {"ok": False, "error": f"未知命令 {cmd}"}
