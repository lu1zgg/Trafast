"""趋势监控接入运行时：1 分钟 K 线 → 15m，喂给每个标的的状态机，产生推送和要落库的数据。

和异动引擎一样是同步、单线程的，只在核心协程里调用。
- 启动：每个标的先用历史 15m K 线静默回放（seed），重建指标和状态，再和库里存的状态对账；
  回放完成前收到的实时 1m K 线先缓存，完成后按顺序补处理。
- 推送：形成 = 新消息（带 👍/👎），结束 = 回复在形成消息下面。静音的标的不推。
  追远 / 高潮量的趋势照样跟踪，但整段不推；超过 30 分钟的旧事件（补历史时算出来的）也不推。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..models import Bar, OutMsg, display_name, market_of
from ..render import feedback_buttons
from .breakout import BreakoutWatch, LevelBreak, break_text
from .buckets import Bucketer, UsRegularBucketer, UtcBucketer
from .levels import Levels
from .machine import SymbolTrend, TrendEvent, TrendParams
from .render import detail_text, ended_text, formed_text, overview_text
from .series import Candle

log = logging.getLogger(__name__)

STALE_S = 1800  # 事件发生超过这么久才处理到（补数据、重启），就不推了
MAX_PENDING = 600  # 回放完成前最多缓存多少根 1m K 线（每个标的）


@dataclass
class _Open:
    start: int
    end: int
    o: float
    h: float
    l: float
    c: float
    v: float
    minutes: set[int] = field(default_factory=set)
    last_ts: int = 0

    def candle(self) -> Candle:
        return Candle(self.start, self.end, self.o, self.h, self.l, self.c, self.v)


@dataclass
class TrendEffects:
    candles: list[tuple[str, Candle]] = field(default_factory=list)
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[tuple[int, str, str, int, float, dict[str, Any]]] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.candles or self.states or self.events)


class TrendEngine:
    def __init__(
        self,
        symbols: Iterable[str],
        cal: Any,
        params: TrendParams,
        out: Callable[[OutMsg], None],
        is_muted: Callable[[str, float], bool] = lambda s, now: False,
        buttons: bool = True,
        show: int = 2,
    ):
        self.p = params
        self.out = out
        self.is_muted = is_muted
        self.buttons = buttons
        self.show = show
        self._us = UsRegularBucketer(cal)
        self._utc = UtcBucketer()
        self.machines: dict[str, SymbolTrend] = {s: SymbolTrend(s, self.bucketer(s), params) for s in symbols}
        self.open15: dict[str, _Open | None] = {s: None for s in self.machines}
        self.seeded: set[str] = set()
        self.pending: dict[str, list[Bar]] = {s: [] for s in self.machines}
        self._eff = TrendEffects()
        self._quiet = False  # 静默回放中：不推送、不记事件
        self.breakouts = BreakoutWatch()  # V4 P3 关键位突破
        self.level_breaks = True

    def bucketer(self, symbol: str) -> Bucketer:
        return self._us if market_of(symbol) == "US" else self._utc

    def set_params(self, params: TrendParams, show: int | None = None) -> None:
        self.p = params
        for m in self.machines.values():
            m.set_params(params)
        if show is not None:
            self.show = show

    # ---- 启动 ----
    def seed(self, symbol: str, candles: Iterable[Candle], minutes: Iterable[Bar] = (), persisted: dict[str, Any] | None = None, now: float = 0.0) -> int:
        """历史 15m 静默回放，再补当前未收盘的 1m，最后和库里的状态对账、处理缓存的实时 K 线。"""
        m = self.machines.get(symbol)
        if m is None:
            return 0
        n = 0
        self._quiet = True
        try:
            for k in sorted(candles, key=lambda x: x.ts):
                if k.ts < m.last_t:
                    continue
                self._on_candle(symbol, k, now, store=False)
                n += 1
            for bar in sorted(minutes, key=lambda x: x.ts):
                self._on_minute(bar, now)
        finally:
            self._quiet = False
        self._reconcile(symbol, persisted or {}, now)
        self.seeded.add(symbol)
        buffered, self.pending[symbol] = self.pending[symbol], []
        for bar in buffered:
            self._on_minute(bar, now)
        self._eff.states[symbol] = self.state_json(symbol)
        return n

    def _reconcile(self, symbol: str, saved: dict[str, Any], now: float) -> None:
        m = self.machines[symbol]
        old = saved.get("active")
        cur = m.active
        if old and cur and old.get("d") == cur["d"] and old.get("entry_t") == cur["entry_t"]:
            cur["msg_key"] = old.get("msg_key")
            return
        if old and old.get("msg_key"):
            # 停机期间这段趋势已经结束（或者重算后不成立）：在原消息下补一句
            word = "上涨" if old["d"] > 0 else "下跌"
            last = m.tf15.last
            px = f"，现价 ${last.c:,.2f}" if last else ""
            text = f"⚪ <b>{display_name(symbol)} {word}趋势已结束</b>（服务重启期间，按最新数据重算{px}）"
            self.out(
                OutMsg(
                    key=f"trend-end:{symbol}:{old.get('entry_t')}",
                    kind="reply",
                    text=text,
                    created_at=now,
                    expires_at=now + 3600,
                    parent_key=old["msg_key"],
                    market=market_of(symbol),
                    silent=True,
                )
            )

    # ---- 实时 ----
    def on_bar(self, bar: Bar, now: float) -> None:
        sym = bar.symbol
        if sym not in self.machines:
            return
        if sym not in self.seeded:
            buf = self.pending[sym]
            buf.append(bar)
            if len(buf) > MAX_PENDING:
                del buf[: len(buf) - MAX_PENDING]
            return
        self._on_minute(bar, now)

    def _on_minute(self, bar: Bar, now: float) -> None:
        sym = bar.symbol
        m = self.machines[sym]
        key = m.bk.key15(bar.ts)
        if key is None:
            return
        start, end = key
        if end <= m.last_t:
            return  # 这根 15m 已经算过了
        op = self.open15[sym]
        if op is not None and op.start != start:
            if start < op.start:
                return  # 迟到的旧 K 线
            self._close_open(sym, now)
            op = None
        if op is None:
            op = _Open(start, end, bar.o, bar.h, bar.l, bar.c, 0.0)
            self.open15[sym] = op
        if bar.ts not in op.minutes:
            op.minutes.add(bar.ts)
            op.v += bar.turnover
            op.h, op.l = max(op.h, bar.h), min(op.l, bar.l)
            if bar.ts >= op.last_ts:
                op.c, op.last_ts = bar.c, bar.ts
            if bar.ts == start:
                op.o = bar.o
        if bar.ts + 60 >= end:
            self._close_open(sym, now)

    def _close_open(self, sym: str, now: float) -> None:
        op = self.open15[sym]
        self.open15[sym] = None
        if op is None or not op.minutes:
            return
        self._on_candle(sym, op.candle(), now, store=True)

    def _on_candle(self, sym: str, k: Candle, now: float, store: bool) -> None:
        m = self.machines[sym]
        if store:
            self._eff.candles.append((sym, k))
        before = m.levels() if (self.level_breaks and not self._quiet and m.tf1.state is not None) else None
        evs = m.on_candle15(k)
        for ev in evs:
            self._handle(sym, ev, now)
        if self.level_breaks and not self._quiet:
            for br in self.breakouts.on_candle(sym, before, k.c, k.end):
                self._level_break(br, now)
        if evs and not self._quiet:
            self._eff.states[sym] = self.state_json(sym)

    def _handle(self, sym: str, ev: TrendEvent, now: float) -> None:
        m = self.machines[sym]
        if self._quiet:
            return
        self._eff.events.append((ev.t, sym, ev.kind, ev.d, ev.price, ev.to_json()))
        market = market_of(sym)
        if ev.kind == "formed":
            a = m.active
            assert a is not None
            why = None
            if ev.quiet:
                why = "quiet"
            elif now - ev.t > STALE_S:
                why = "stale"
            elif self.is_muted(sym, now):
                why = "muted"
            if why:
                log.info("趋势形成未推送 %s %s：%s %s", display_name(sym), "涨" if ev.d > 0 else "跌", why, ev.quiet)
                return
            key = f"trend:{sym}:{ev.entry_t}"
            a["msg_key"] = key
            self.out(
                OutMsg(
                    key=key,
                    kind="send",
                    text=formed_text(ev, m.levels(), self.show, self.p.accept_bars),
                    created_at=now,
                    expires_at=now + STALE_S,
                    buttons=feedback_buttons(key) if self.buttons else None,
                    market=market,
                    direction=ev.d,
                )
            )
        else:
            key = ev.msg_key
            if not key or now - ev.t > STALE_S:
                return
            self.out(
                OutMsg(
                    key=f"trend-end:{sym}:{ev.entry_t}",
                    kind="reply",
                    text=ended_text(ev, m.levels()),
                    created_at=now,
                    expires_at=now + 3600,
                    parent_key=key,
                    market=market,
                )
            )

    def _level_break(self, br: LevelBreak, now: float) -> None:
        self._eff.events.append((br.t, br.symbol, "level", br.d, br.price, br.to_json()))
        if now - br.t > STALE_S or self.is_muted(br.symbol, now):
            return
        key = f"trend-level:{br.symbol}:{br.t}:{br.d}"
        self.out(
            OutMsg(
                key=key,
                kind="send",
                text=break_text(br),
                created_at=now,
                expires_at=now + STALE_S,
                buttons=feedback_buttons(key) if self.buttons else None,
                market=market_of(br.symbol),
                direction=br.d,
            )
        )

    def note_crossing(self, sym: str, lo: float, hi: float, d: int, t: float) -> None:
        """价格异动的卡片已经写了穿过这个强位：4 小时内不再单独推关键位突破。"""
        self.breakouts.note(sym, lo, hi, d, t)
        pend = self.breakouts.pending.get(sym)
        if pend:
            self.breakouts.pending[sym] = [p for p in pend if not (p.d == d and p.zone.lo <= hi and p.zone.hi >= lo)]

    # ---- 落库 ----
    def state_json(self, sym: str) -> dict[str, Any]:
        m = self.machines[sym]
        return {"active": dict(m.active) if m.active else None, "last_end": {str(k): v for k, v in m.last_end.items()}, "t": m.last_t}

    def drain(self) -> TrendEffects:
        eff, self._eff = self._eff, TrendEffects()
        return eff

    # ---- 查询 ----
    def snapshots(self) -> list[dict[str, Any]]:
        return [m.snapshot() for m in self.machines.values()]

    def levels(self, sym: str) -> Levels | None:
        m = self.machines.get(sym)
        return m.levels() if m else None

    def overview_text(self) -> str:
        return overview_text(self.snapshots())

    def detail_text(self, sym: str) -> str:
        m = self.machines[sym]
        return detail_text(m.snapshot(), m.levels() if m.ready or m.tf1.state else None)

    def status_json(self) -> list[dict[str, Any]]:
        out = []
        for m in self.machines.values():
            s = m.snapshot()
            lv = m.levels() if m.tf1.state else None
            if lv is not None:
                s["res"] = [{"lo": z.lo, "hi": z.hi, "stars": z.stars(), "src": z.sources(), "strong": z.strong} for z in lv.res[:3]]
                s["sup"] = [{"lo": z.lo, "hi": z.hi, "stars": z.stars(), "src": z.sources(), "strong": z.strong} for z in lv.sup[:3]]
            s["name"] = display_name(m.symbol)
            s["seeded"] = m.symbol in self.seeded
            out.append(s)
        return out
