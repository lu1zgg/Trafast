"""回放：把历史 1 分钟 K 线按真实顺序喂给同一个引擎，用虚拟时钟驱动，统计会发多少条消息。

基线和实盘一样每天重算一次（美股美东 20:05，加密 UTC 00:05），只用重算时刻之前的数据。
"""
from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .baseline import Baselines, compute_symbol_baseline
from .config import ALL_SESSIONS, SESSION_LABELS, AppConfig, Params
from .engine import Engine
from .models import Bar, Decision, OutMsg, SignalEvent, market_of
from .patterns import PATTERN_LABELS, PATTERNS
from .render import to_plain
from .sessions import ET, UsCalendar, utc_market_instance, utc_market_session

UTC = UTC


class CollectSink:
    def __init__(self) -> None:
        self.outs: list[OutMsg] = []
        self.decisions: list[Decision] = []
        self.signals: list[SignalEvent] = []

    def bar(self, b: Bar) -> None:
        pass

    def decision(self, d: Decision) -> None:
        self.decisions.append(d)

    def episode(self, ep) -> None:  # noqa: ANN001
        pass

    def root(self, r) -> None:  # noqa: ANN001
        pass

    def out(self, m: OutMsg) -> None:
        self.outs.append(m)

    def signal(self, ev: SignalEvent) -> None:
        self.signals.append(ev)

    def custom(self, rule) -> None:  # noqa: ANN001
        pass


@dataclass
class ReplayResult:
    start_ts: int
    end_ts: int
    rule_version: str
    outs: list[OutMsg]
    decisions: list[Decision]
    stats: dict[str, float] = field(default_factory=dict)
    signals: list[SignalEvent] = field(default_factory=list)
    # 首发信号 id → 30 分钟内是否回到起点（回吐）；None = 数据不够 30 分钟
    giveback: dict[str, bool | None] = field(default_factory=dict)

    def messages(self) -> list[OutMsg]:
        return [m for m in self.outs if m.kind in ("send", "reply")]

    def golden(self) -> list[dict[str, str]]:
        return [
            {"kind": m.kind, "key": m.key, "parent": m.parent_key or "", "first_line": to_plain(m.text).split("\n")[0]}
            for m in self.outs
        ]

    def summary(self) -> str:
        days = max(1e-9, (self.end_ts - self.start_ts) / 86400)
        sends = [m for m in self.outs if m.kind == "send"]
        replies = [m for m in self.outs if m.kind == "reply"]
        edits = [m for m in self.outs if m.kind == "edit"]
        us_sends = sum(1 for m in sends if key_market(m.key) == "US")
        pushed = [d for d in self.decisions if d.outcome == "pushed"]
        by_sess = Counter(d.session for d in pushed)
        by_level = Counter(d.level for d in pushed)
        l0 = Counter(d.outcome for d in self.decisions if d.level == "L0")
        s = datetime.fromtimestamp(self.start_ts, UTC).strftime("%Y-%m-%d %H:%M")
        e = datetime.fromtimestamp(self.end_ts, UTC).strftime("%Y-%m-%d %H:%M")
        weekdays = self.stats.get("us_weekdays", 0) or 0
        lines = [
            f"回放 {s} → {e} UTC（{days:.1f} 天），规则 {self.rule_version}",
            f"消息：首发 {len(sends)} 条（美股 {us_sends}，加密 {sum(1 for m in sends if key_market(m.key) == 'CRYPTO')}，大宗商品 {sum(1 for m in sends if key_market(m.key) == 'CMDTY')}），回复 {len(replies)} 条，静默编辑 {len(edits)} 次",
            f"平均每天：首发 {len(sends) / days:.1f} 条，加密 {sum(1 for m in sends if key_market(m.key) == 'CRYPTO') / days:.1f} 条"
            + (f"；美股每个交易日 {us_sends / weekdays:.1f} 条" if weekdays else ""),
            "推送的异动：" + "，".join(f"{k} {by_level[k]}" for k in ("L3", "L2", "L1")),
            "按时段：" + "，".join(
                f"{SESSION_LABELS[k]} {by_sess[k]}" for k in ALL_SESSIONS if by_sess[k]
            ),
            "挡下的候选：" + ("，".join(f"{k} {v}" for k, v in l0.most_common()) or "无"),
        ]
        lines.extend(self.pattern_lines())
        return "\n".join(lines)

    def pattern_lines(self) -> list[str]:
        """V4：首发信号按形态类型统计条数和 30 分钟回吐率（回到起点价算回吐）。"""
        opens = [ev for ev in self.signals if ev.kind == "open"]
        if not opens:
            return []
        out = []
        for market, label in (("US", "美股"), ("CRYPTO", "加密"), ("CMDTY", "大宗商品")):
            evs = [ev for ev in opens if ev.market == market]
            if not evs:
                continue
            parts = []
            for p in PATTERNS:
                sel = [ev for ev in evs if ev.pattern == p]
                if not sel:
                    continue
                known = [self.giveback.get(ev.id) for ev in sel if self.giveback.get(ev.id) is not None]
                gb = f"，回吐 {100 * sum(known) / len(known):.0f}%" if known else ""
                parts.append(f"{PATTERN_LABELS[p]} {len(sel)}{gb}")
            vol = sum(1 for ev in evs if "vol" in ev.tags)
            out.append(f"按类型（{label}）：" + "；".join(parts) + f"；带放量标签 {vol}")
        return out


def key_market(key: str) -> str:
    """首发消息的键形如 g{收盘时间}{U|C}{u|d}{代码}；摘要消息以 s 开头。"""
    if key.startswith("g"):
        rest = key[1:].lstrip("0123456789")
        return {"U": "US", "M": "CMDTY"}.get(rest[:1], "CRYPTO")
    return "MIX"


def _next_at(ts: int, tz, hour: int, minute: int) -> int:  # noqa: ANN001
    dt = datetime.fromtimestamp(ts, tz)
    cand = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand.timestamp() <= ts:
        cand = cand + timedelta(days=1)
    return int(cand.timestamp())


def compute_baselines_at(
    cfg: AppConfig,
    cal: UsCalendar,
    bars_by_symbol: dict[str, list[Bar]],
    ts_index: dict[str, list[int]],
    at_ts: int,
    baselines: Baselines,
    symbols: Iterable[str],
) -> None:
    r = cfg.rules
    for sym in symbols:
        bars = bars_by_symbol.get(sym, [])
        idx = ts_index[sym]
        hi = bisect.bisect_left(idx, at_ts)
        if market_of(sym) == "US":
            lo = bisect.bisect_left(idx, at_ts - cfg.stocks.longbridge.warmup_days * 86400)
            stats = compute_symbol_baseline(
                bars[lo:hi],
                lambda b: b.session or cal.session_at(b.ts),
                lambda b: cal.instance_of(b.ts),
                r.baseline_sessions,
                r.baseline_min_sessions,
            )
        else:
            cm = market_of(sym) == "CMDTY"
            days = cfg.commodities.baseline_days if cm else r.crypto_baseline_days
            lo = bisect.bisect_left(idx, at_ts - days * 86400)
            stats = compute_symbol_baseline(
                bars[lo:hi],
                lambda b, s=sym: utc_market_session(s, b.ts),
                lambda b, s=sym: utc_market_instance(s, b.ts),
                10_000,
                cfg.commodities.min_days if cm else r.crypto_min_days,
            )
        baselines.set_symbol(sym, stats, at_ts)


def run_replay(
    cfg: AppConfig,
    params: Params,
    cal: UsCalendar,
    bars: Iterable[Bar],
    start_ts: int,
    end_ts: int,
    progress: Callable[[str], None] | None = None,
) -> ReplayResult:
    by_sym: dict[str, list[Bar]] = defaultdict(list)
    for b in bars:
        if not b.session:
            b.session = cal.session_at(b.ts) if market_of(b.symbol) == "US" else utc_market_session(b.symbol, b.ts)
        by_sym[b.symbol].append(b)
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: x.ts)
    ts_index = {sym: [b.ts for b in lst] for sym, lst in by_sym.items()}
    symbols = [s for s in cfg.all_symbols() if s in by_sym]
    stocks = [s for s in symbols if market_of(s) == "US"]
    cryptos = [s for s in symbols if market_of(s) != "US"]

    start_ts -= start_ts % 60
    baselines = Baselines()
    compute_baselines_at(cfg, cal, by_sym, ts_index, start_ts, baselines, symbols)
    next_stock_bl = _next_at(start_ts, ET, 20, 5)
    next_crypto_bl = _next_at(start_ts, UTC, 0, 5)

    sink = CollectSink()
    engine = Engine(cfg, params, cal, baselines, sink, alive=lambda market, now: True)
    seed = []
    for sym in symbols:
        idx = ts_index[sym]
        lo = bisect.bisect_left(idx, start_ts - 40 * 60)
        hi = bisect.bisect_left(idx, start_ts)
        seed.extend(by_sym[sym][lo:hi])
    for sym in stocks:
        idx = ts_index[sym]
        hi = bisect.bisect_left(idx, start_ts)
        for b in reversed(by_sym[sym][:hi]):
            if b.session == "REG" and not b.synthetic:
                engine.set_ref_close(sym, b.c)
                break
    engine.seed_history(seed)

    minute_bars: dict[int, list[Bar]] = defaultdict(list)
    for sym in symbols:
        idx = ts_index[sym]
        lo = bisect.bisect_left(idx, start_ts)
        hi = bisect.bisect_left(idx, end_ts)
        for b in by_sym[sym][lo:hi]:
            minute_bars[b.ts].append(b)

    mc = cfg.merge
    offsets = sorted({1.0, cfg.stocks.grace_s + 1.5, mc.fast_batch_delay_s + 1.5, mc.batch_delay_s + 1.5, 30.0})
    t = start_ts
    us_days: set[str] = set()
    while t < end_ts:
        close = t + 60
        if close >= next_stock_bl and stocks:
            compute_baselines_at(cfg, cal, by_sym, ts_index, next_stock_bl, baselines, stocks)
            next_stock_bl = _next_at(next_stock_bl, ET, 20, 5)
        if close >= next_crypto_bl and cryptos:
            compute_baselines_at(cfg, cal, by_sym, ts_index, next_crypto_bl, baselines, cryptos)
            next_crypto_bl = _next_at(next_crypto_bl, UTC, 0, 5)
            if progress:
                progress(datetime.fromtimestamp(close, UTC).strftime("%Y-%m-%d"))
        now = close + 1.0
        for b in sorted(minute_bars.get(t, []), key=lambda x: x.symbol):
            engine.on_bar(b, now)
            if market_of(b.symbol) == "US" and cal.session_at(b.ts) in ("PRE", "REG", "POST"):
                us_days.add(datetime.fromtimestamp(b.ts, ET).date().isoformat())
        for off in offsets:
            engine.on_tick(close + off)
        t += 60

    stats = {"us_weekdays": float(len(us_days))}
    stats.update({f"engine_{k}": float(v) for k, v in engine.stats.items()})
    giveback = {
        ev.id: gave_back(by_sym.get(ev.symbol, []), ts_index.get(ev.symbol, []), ev)
        for ev in sink.signals
        if ev.kind == "open"
    }
    return ReplayResult(
        start_ts, end_ts, params.rule_version, sink.outs, sink.decisions, stats, sink.signals, giveback
    )


def gave_back(bars: list[Bar], idx: list[int], ev: SignalEvent, minutes: int = 30) -> bool | None:
    """首发后 minutes 分钟内，收盘价是否回到起点价（或越过）。之后的数据不足 minutes 分钟时返回 None。"""
    if ev.anchor <= 0:
        return None
    start = ev.ts  # 首发 K 线的收盘时间 = 下一根的开始时间
    lo = bisect.bisect_left(idx, start)
    hi = bisect.bisect_left(idx, start + minutes * 60)
    if not idx or idx[-1] < start + (minutes - 1) * 60:
        return None
    for b in bars[lo:hi]:
        if b.c > 0 and ev.direction * math.log(b.c / ev.anchor) <= 0:
            return True
    return False


def max_move(bars: list[Bar], k: int = 15) -> float:
    closes = {b.ts: b.c for b in bars}
    best = 0.0
    for b in bars:
        c0 = closes.get(b.ts - 60 * k)
        if c0:
            d = math.log(b.c / c0)
            if abs(d) > abs(best):
                best = d
    return best
