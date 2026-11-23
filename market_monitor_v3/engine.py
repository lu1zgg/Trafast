"""核心引擎（同步、单线程）。

实盘和回放共用：输入是 1 分钟 K 线和时钟 tick，输出写进 Sink。
只有核心协程调用它，所以不需要锁，处理顺序在回放里能原样重现。
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace

from .baseline import Baselines
from .config import AppConfig, Params
from .detector import evaluate
from .episodes import EpisodeManager
from .merge import Merger
from .models import Bar, Episode, RootMessage, market_of
from .sessions import UsCalendar, crypto_session
from .sink import Sink
from .windows import SymbolHistory

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        cfg: AppConfig,
        params: Params,
        cal: UsCalendar,
        baselines: Baselines,
        sink: Sink,
        alive: Callable[[str, float], bool] = lambda market, now: False,
    ):
        self.cfg = cfg
        self.params = params
        self.cal = cal
        self.baselines = baselines
        self.sink = sink
        self.alive = alive
        self.hist: dict[str, SymbolHistory] = {s: SymbolHistory() for s in cfg.all_symbols()}
        self.stock_symbols = list(cfg.stocks.symbols) if cfg.stocks.enabled else []
        self.merger = Merger(params, sink, cfg.telegram.feedback_buttons)
        self.episodes = EpisodeManager(params, self.merger, sink, self.is_muted, cfg.telegram.silent_extension_replies)
        self.mutes: dict[str, float] = {}
        self.stats: Counter[str] = Counter()
        self.last_bar_at: dict[str, float] = {}
        self._last_eval: dict[str, tuple[int, bool]] = {}
        self._last_gc = 0.0
        # 每只美股最近一次盘中（REG）收盘价，用来识别盘外按前收回报的旧价
        self.ref_close: dict[str, float] = {}

    # ---- 配置与状态 ----
    def set_params(self, params: Params) -> None:
        self.params = params
        self.merger.params = params
        self.episodes.params = params

    def session_for(self, symbol: str, ts: float) -> str:
        if market_of(symbol) == "US":
            return self.cal.session_at(ts)
        return crypto_session(ts)

    def is_muted(self, symbol: str, now: float) -> bool:
        for key in (symbol, "*"):
            until = self.mutes.get(key)
            if until is not None and until > now:
                return True
        return False

    def seed_history(self, bars: Iterable[Bar]) -> int:
        n = 0
        for b in sorted(bars, key=lambda x: x.ts):
            h = self.hist.get(b.symbol)
            if h is None:
                continue
            if not b.session:
                b.session = self.session_for(b.symbol, b.ts)
            h.add(b)
            self._note_ref(b)
            n += 1
        return n

    def set_ref_close(self, symbol: str, price: float | None) -> None:
        if price and price > 0:
            self.ref_close[symbol] = price

    def _note_ref(self, bar: Bar) -> None:
        if bar.session == "REG" and not bar.synthetic and bar.c > 0 and market_of(bar.symbol) == "US":
            self.ref_close[bar.symbol] = bar.c

    def _stale_print(self, bar: Bar, h: SymbolHistory) -> bool:
        """盘外这根 K 线收在上一交易日盘中收盘价上，而上一根离那里还很远：多半是前收参考价成交，不是真实价格。"""
        if bar.session == "REG" or market_of(bar.symbol) != "US":
            return False
        ref = self.ref_close.get(bar.symbol)
        prev = h.get(bar.ts - 60)
        if not ref or prev is None or prev.c <= 0 or bar.c <= 0:
            return False
        tol = self.params.rules.stale_price_tol
        if abs(math.log(bar.c / ref)) > tol:
            return False
        return abs(math.log(prev.c / ref)) > max(4 * tol, self.params.floor(bar.symbol, bar.session, 1))

    def restore(self, episodes: list[Episode], roots: list[RootMessage]) -> None:
        self.merger.restore(roots)
        self.episodes.restore(episodes)

    # ---- 输入 ----
    def on_bar(self, bar: Bar, now: float) -> None:
        sym = bar.symbol
        h = self.hist.get(sym)
        if h is None:
            self.stats["unknown_symbol"] += 1
            return
        if not bar.session:
            bar.session = self.session_for(sym, bar.ts)

        prev = h.get(bar.ts)
        if prev is not None:
            if prev.same_values(bar) or bar.synthetic:
                self.stats["dup_bars"] += 1
                return
            h.add(bar)
            self.sink.bar(bar)
            self.stats["revised_bars"] += 1
            # 补的平 K 线被真实 K 线覆盖：如果它还是最新一根、且之前没有候选，重新检测一次
            if not (
                prev.synthetic
                and bar.ts == h.last_ts
                and self.episodes.idle(sym)
                and self._last_eval.get(sym) == (bar.ts, False)
            ):
                return
        else:
            is_old = h.last_ts is not None and bar.ts < h.last_ts
            h.add(bar)
            self.sink.bar(bar)
            if is_old:
                self.stats["late_bars"] += 1
                return

        self.last_bar_at[sym] = now
        self.stats["synthetic_bars" if bar.synthetic else "bars"] += 1
        self._note_ref(bar)
        if bar.session == "CLOSED":
            return
        if bar.close_ts < now - self.params.merge.expiry_s:
            self.stats["stale_bars"] += 1
            return
        ev = evaluate(bar, h, self.params, self.baselines, self.cal.is_thin)
        if ev.candidate is not None and self._stale_print(bar, h):
            log.warning(
                "疑似旧价 %s %s %s：收 %.4f ≈ 前收 %.4f，上一根 %.4f，成交额 %.0f，来源 %s",
                sym, bar.session, bar.ts, bar.c, self.ref_close[sym], h.get(bar.ts - 60).c, bar.turnover, bar.source,
            )
            self.stats["stale_price"] += 1
            self.episodes.reject(ev.candidate, "stale_price")
            ev.candidate = None
            # 窗口里不留这根坏价，否则下一根“回到原价”会被当成反向异动，5/15 分钟窗口也会被污染 15 分钟
            pc = h.get(bar.ts - 60).c
            h.add(replace(bar, o=pc, h=pc, l=pc, c=pc))
        self._last_eval[sym] = (bar.ts, ev.candidate is not None)
        self.episodes.on_bar(bar, ev, now)

    def on_tick(self, now: float) -> None:
        if self.stock_symbols and self.cfg.stocks.synthesize_missing:
            self._synthesize(now)
        self.episodes.on_tick(now)
        self.merger.tick(now)
        if now - self._last_gc >= 60:
            self._last_gc = now
            active = {ep.root_key for ep in self.episodes.active() if ep.root_key}
            self.merger.gc(active)
            for key in [k for k, until in self.mutes.items() if until <= now]:
                del self.mutes[key]

    def _synthesize(self, now: float) -> None:
        """交易时段内某一分钟没有成交：收盘 grace 秒后补一根平 K 线，让窗口和真实时间对齐。"""
        if not self.alive("US", now):
            return
        grace = self.cfg.stocks.grace_s
        for sym in self.stock_symbols:
            h = self.hist[sym]
            last = h.last()
            if last is None:
                continue
            m = last.ts + 60
            n = 0
            while m + 60 + grace <= now and n < 60:
                sess = self.cal.session_at(m)
                if sess == "CLOSED":
                    break
                c = last.c
                self.on_bar(Bar(sym, m, c, c, c, c, 0.0, 0.0, sess, "synthetic", True), now)
                m += 60
                n += 1
