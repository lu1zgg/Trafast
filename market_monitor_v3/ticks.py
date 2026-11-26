"""V4 P2 逐笔层：逐笔成交的内存聚合、大单流向标签、单笔冲涨跌（影子模式）。

逐笔不落库。落库的只有：
- tick_1m：每分钟摘要（笔数、主动买额、主动卖额、大单笔数、大单主动买 / 卖额）；
- tick_hist：每个标的每天（美股按时段）的单笔成交额对数直方图，用来算大单门槛和单笔成交额中位数；
- tick_shadow：单笔冲涨跌的影子记录（只记录不推送），报告“比 K 线早多少秒、多少次后来没过入门门”。

规则（设计文档第 6、7 节）：
- 大单：单笔成交额 ≥ 该标的近 14 天（加密）/ 近 20 个同类时段（美股）单笔成交额的 99 分位。
- 大单流向：5 分钟大单净额 = 主动买大单额 − 主动卖大单额；与近期 |5 分钟净额| 中位数相比 ≥ 3 倍，
  且和价格异动同向，卡片加一行“大单净买入 / 净卖出”。只是证据，不触发、不升级。
- 单笔冲涨跌：|这一笔价 / 上一笔价 − 1| ≥ max(1%, 2 × 该时段 1 分钟地板)，单笔成交额 ≥ 中位数 × 5；
  之后 20 秒内 ≥ 3 笔、成交均价保留 ≥ 60% 算被接受；接受时用实时价算 1 分钟位移，看能不能过入门门。
  美股盘前、盘后、夜盘不做（这些时段的坏价正是单笔跳价）。本版本只影子运行。

同步代码、单线程：只在事件循环里调用，和引擎一样不需要锁。
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .baseline import Baselines
from .config import Params, TicksCfg
from .models import market_of
from .windows import SymbolHistory

# 单笔成交额直方图：以 2 为底的对数，每倍 4 格，覆盖 $1 到约 $4×10⁹
BUCKETS_PER_OCTAVE = 4
N_BUCKETS = 128
EXT_SESSIONS = ("PRE", "POST", "ON")
REFRESH_S = 600  # 大单门槛、中位数多久重算一次
MINUTE_GRACE_S = 5  # 分钟结束后再等几秒才定稿（逐笔可能晚到）


def bucket_of(notional: float) -> int:
    if notional <= 1.0:
        return 0
    return min(N_BUCKETS - 1, int(math.log2(notional) * BUCKETS_PER_OCTAVE))


def bucket_value(i: int) -> float:
    """格子的几何中点。"""
    return 2.0 ** ((i + 0.5) / BUCKETS_PER_OCTAVE)


def quantile(counts: list[int], q: float, upper: bool = False) -> float | None:
    """分位数所在格子的中点；upper = True 时取格子上沿（大单门槛用，宁可严一点）。"""
    total = sum(counts)
    if total <= 0:
        return None
    target = q * total
    acc = 0
    for i, c in enumerate(counts):
        acc += c
        if acc >= target:
            return 2.0 ** ((i + 1) / BUCKETS_PER_OCTAVE) if upper else bucket_value(i)
    return bucket_value(len(counts) - 1)


@dataclass(slots=True)
class Trade:
    symbol: str
    ts: float  # 成交时间（epoch 秒，带小数）
    price: float
    qty: float
    side: int  # +1 主动买，−1 主动卖，0 不知道
    session: str = ""

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class TickMinute:
    symbol: str
    ts: int  # 分钟开始
    session: str
    n: int = 0
    buy: float = 0.0  # 主动买成交额
    sell: float = 0.0
    big_n: int = 0
    big_buy: float = 0.0
    big_sell: float = 0.0

    @property
    def big_net(self) -> float:
        return self.big_buy - self.big_sell

    def row(self) -> tuple[Any, ...]:
        return (self.symbol, self.ts, self.session, self.n, self.buy, self.sell, self.big_n, self.big_buy, self.big_sell)


@dataclass(slots=True)
class TickShadow:
    """一次单笔跳价：从发现到接受 / 不接受。"""

    symbol: str
    ts: float  # 跳价那一笔的时间
    session: str
    direction: int
    prev: float
    price: float
    jump: float  # 对数跳幅（带方向）
    notional: float
    min_jump: float
    status: str = "pending"  # pending / accepted / rejected / ext_session
    accept_ts: float | None = None
    n_after: int = 0
    vwap: float | None = None
    retain: float | None = None
    d1m: float | None = None  # 接受时实时价相对 60 秒前的对数位移
    gate: float | None = None  # 当时的 1 分钟入门门槛
    gate_pass: bool = False
    _qty: float = 0.0
    _amt: float = 0.0
    _last: float = 0.0

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("_qty", "_amt", "_last"):
            d.pop(k, None)
        return d


def gate_1m(params: Params, baselines: Baselines, symbol: str, session: str) -> float | None:
    """1 分钟窗口的入门门槛：基线就绪时 max(地板, Z·σ)，否则直通线。"""
    st = params.sessions.get(session)
    if st is None:
        return None
    floor = st.floor[0] * params.mult(symbol)
    bl = baselines.get(symbol, session, 1)
    if bl is not None and bl.ready:
        return max(floor, st.z * bl.sigma)
    return st.bypass[0]


@dataclass
class _Sym:
    minutes: dict[int, TickMinute] = field(default_factory=dict)
    last_price: float = 0.0
    last_ts: float = 0.0
    secs: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=120))  # 每秒最后一笔价
    pending: TickShadow | None = None
    net5: deque[float] = field(default_factory=deque)  # 每分钟定稿时的 5 分钟大单净额
    big_thr: float | None = None
    median: float | None = None
    flow_med: float | None = None
    refreshed_at: float = -1e18
    refreshed_skey: str = ""
    hist_total: int = 0


class TickHub:
    def __init__(
        self,
        cfg: TicksCfg,
        params: Params,
        baselines: Baselines,
        hist: Callable[[str], SymbolHistory | None],
        session_for: Callable[[str, float], str],
    ):
        self.cfg = cfg
        self.params = params
        self.baselines = baselines
        self.hist = hist
        self.session_for = session_for
        self.syms: dict[str, _Sym] = {}
        # (symbol, 日期 YYYY-MM-DD, 时段键) → 直方图；加密时段键为 "*"
        self.hists: dict[tuple[str, str, str], list[int]] = {}
        self._dirty_hists: set[tuple[str, str, str]] = set()
        self.out_minutes: list[TickMinute] = []
        self.out_shadows: list[TickShadow] = []
        self.stats: dict[str, float] = {"trades": 0, "big": 0, "jumps": 0, "accepted": 0, "gate_pass": 0, "cpu_s": 0.0}
        self.first_trade_at: float | None = None

    # ---- 状态恢复 ----
    def load_hists(self, rows: list[tuple[str, str, str, list[int]]]) -> None:
        for sym, day, skey, counts in rows:
            c = list(counts)[:N_BUCKETS]
            c += [0] * (N_BUCKETS - len(c))
            self.hists[(sym, day, skey)] = c
        for s in {r[0] for r in rows}:
            self._sym(s).refreshed_at = -1e18

    def load_minutes(self, rows: list[TickMinute]) -> None:
        """用库里的分钟摘要重建每个标的的 5 分钟大单净额序列（大单流向的常态）。"""
        by_sym: dict[str, list[TickMinute]] = {}
        for m in rows:
            by_sym.setdefault(m.symbol, []).append(m)
        win = self.cfg.flow_window_min
        for sym, ms in by_sym.items():
            st = self._sym(sym)
            ms.sort(key=lambda m: m.ts)
            q: deque[TickMinute] = deque()
            for m in ms:
                q.append(m)
                while q and q[0].ts <= m.ts - 60 * win:
                    q.popleft()
                self._push_net(st, sum(x.big_net for x in q))
            st.refreshed_at = -1e18

    def _sym(self, symbol: str) -> _Sym:
        st = self.syms.get(symbol)
        if st is None:
            st = self.syms[symbol] = _Sym()
        return st

    def _push_net(self, st: _Sym, v: float) -> None:
        st.net5.append(v)
        cap = self.cfg.big_days_crypto * 1440
        while len(st.net5) > cap:
            st.net5.popleft()

    # ---- 门槛 ----
    @staticmethod
    def _skey(symbol: str, session: str) -> str:
        return session if market_of(symbol) == "US" else "*"

    def _refresh(self, symbol: str, session: str, now: float) -> None:
        st = self._sym(symbol)
        skey = self._skey(symbol, session)
        st.refreshed_at = now
        st.refreshed_skey = skey
        days = sorted({d for (s, d, k) in self.hists if s == symbol and k == skey}, reverse=True)
        keep = days[: self.cfg.big_sessions_us if skey != "*" else self.cfg.big_days_crypto]
        counts = [0] * N_BUCKETS
        for d in keep:
            for i, c in enumerate(self.hists[(symbol, d, skey)]):
                counts[i] += c
        st.hist_total = sum(counts)
        if st.hist_total >= self.cfg.min_trades:
            st.big_thr = quantile(counts, self.cfg.big_quantile, upper=True)
            st.median = quantile(counts, 0.5)
        else:
            st.big_thr = st.median = None
        nets = [abs(x) for x in st.net5 if x != 0.0]
        st.flow_med = statistics.median(nets) if len(nets) >= self.cfg.flow_min_samples else None

    # ---- 输入 ----
    def on_trade(self, t: Trade) -> None:
        if t.price <= 0 or t.qty <= 0:
            return
        sym = t.symbol
        st = self._sym(sym)
        if not t.session:
            t.session = self.session_for(sym, t.ts)
        if t.session == "CLOSED":
            return
        if self.first_trade_at is None:
            self.first_trade_at = t.ts
        self.stats["trades"] += 1
        if t.ts - st.refreshed_at >= REFRESH_S or st.refreshed_skey != self._skey(sym, t.session):
            self._refresh(sym, t.session, t.ts)
        notional = t.notional

        # 直方图
        day = datetime.fromtimestamp(t.ts, UTC).strftime("%Y-%m-%d")
        key = (sym, day, self._skey(sym, t.session))
        h = self.hists.get(key)
        if h is None:
            h = self.hists[key] = [0] * N_BUCKETS
        h[bucket_of(notional)] += 1
        self._dirty_hists.add(key)

        # 分钟摘要
        m_ts = int(t.ts) - int(t.ts) % 60
        m = st.minutes.get(m_ts)
        if m is None:
            m = st.minutes[m_ts] = TickMinute(sym, m_ts, t.session)
        m.n += 1
        if t.side > 0:
            m.buy += notional
        elif t.side < 0:
            m.sell += notional
        if st.big_thr is not None and notional >= st.big_thr:
            self.stats["big"] += 1
            m.big_n += 1
            if t.side > 0:
                m.big_buy += notional
            elif t.side < 0:
                m.big_sell += notional

        # 单笔冲涨跌
        p = st.pending
        if p is not None:
            if t.ts - p.ts <= self.cfg.accept_s:
                p.n_after += 1
                p._qty += t.qty
                p._amt += notional
                p._last = t.price
            else:
                self._finish(sym, st, t.ts)
        if st.pending is None and st.last_price > 0:
            self._check_jump(sym, st, t)

        st.last_price = t.price
        st.last_ts = t.ts
        sec = int(t.ts)
        if st.secs and st.secs[-1][0] == sec:
            st.secs[-1] = (sec, t.price)
        else:
            st.secs.append((sec, t.price))

    def _check_jump(self, sym: str, st: _Sym, t: Trade) -> None:
        if st.median is None:
            return
        j = math.log(t.price / st.last_price)
        min_jump = max(
            math.log1p(self.cfg.jump_min_pct / 100), self.cfg.jump_floor_mult * self.params.floor(sym, t.session, 1)
        )
        if abs(j) < min_jump or t.notional < self.cfg.jump_notional_mult * st.median:
            return
        self.stats["jumps"] += 1
        sh = TickShadow(sym, t.ts, t.session, 1 if j > 0 else -1, st.last_price, t.price, j, t.notional, min_jump)
        if market_of(sym) == "US" and t.session in EXT_SESSIONS:
            sh.status = "ext_session"
            self.out_shadows.append(sh)
            return
        st.pending = sh

    def _price_ago(self, st: _Sym, ts: float, ago: float) -> float | None:
        target = int(ts - ago)
        best = None
        for sec, px in st.secs:
            if sec <= target:
                best = px
            else:
                break
        return best

    def _finish(self, sym: str, st: _Sym, now: float) -> None:
        p = st.pending
        st.pending = None
        if p is None:
            return
        p.accept_ts = p.ts + self.cfg.accept_s
        if p.n_after > 0 and p._qty > 0:
            p.vwap = p._amt / p._qty
            p.retain = p.direction * math.log(p.vwap / p.prev) / abs(p.jump)
        ok = p.n_after >= self.cfg.accept_min_trades and p.retain is not None and p.retain >= self.cfg.accept_retain
        p.status = "accepted" if ok else "rejected"
        if ok:
            self.stats["accepted"] += 1
            last = p._last or p.price
            base = self._price_ago(st, p.accept_ts, 60)
            if base is None:
                h = self.hist(sym)
                b = h.last() if h is not None else None
                base = b.c if b is not None else None
            p.gate = gate_1m(self.params, self.baselines, sym, p.session)
            if base and base > 0:
                p.d1m = math.log(last / base)
                p.gate_pass = (
                    p.gate is not None and p.direction * p.d1m > 0 and abs(p.d1m) >= p.gate
                )
                if p.gate_pass:
                    self.stats["gate_pass"] += 1
        self.out_shadows.append(p)

    # ---- 时钟 ----
    def roll(self, now: float) -> None:
        """每秒调用：定稿已经结束的分钟、结束超过接受窗口的单笔跳价。"""
        win = self.cfg.flow_window_min
        for sym, st in self.syms.items():
            if st.pending is not None and now - st.pending.ts > self.cfg.accept_s:
                self._finish(sym, st, now)
            done = [ts for ts in st.minutes if ts + 60 + MINUTE_GRACE_S <= now]
            for ts in sorted(done):
                m = st.minutes[ts]
                self.out_minutes.append(m)
                net = sum(x.big_net for x in st.minutes.values() if ts - 60 * win < x.ts <= ts)
                self._push_net(st, net)
            # 只留最近 win+2 分钟在内存里（流向查询要用）
            for ts in [ts for ts in st.minutes if ts < now - 60 * (win + 2)]:
                del st.minutes[ts]

    # ---- 查询 ----
    def flow(self, symbol: str, end_ts: int) -> tuple[float, float] | None:
        """[end_ts − 5 分钟, end_ts) 内的大单净额和它相对常态的倍数（带符号）。没有数据或常态未就绪时 None。"""
        st = self.syms.get(symbol)
        if st is None or st.flow_med is None or st.flow_med <= 0:
            return None
        start = end_ts - 60 * self.cfg.flow_window_min
        ms = [m for m in st.minutes.values() if start <= m.ts < end_ts]
        if not ms:
            return None
        net = sum(m.big_net for m in ms)
        return net, net / st.flow_med

    # ---- 输出 ----
    def drain(self) -> tuple[list[TickMinute], list[TickShadow], list[tuple[str, str, str, list[int]]]]:
        mins, self.out_minutes = self.out_minutes, []
        shadows, self.out_shadows = self.out_shadows, []
        hists = [(k[0], k[1], k[2], list(self.hists[k])) for k in self._dirty_hists]
        self._dirty_hists = set()
        # 内存里只留最近 31 天的直方图
        if self.hists:
            days = sorted({k[1] for k in self.hists})
            cutoff = days[-1] if len(days) <= 31 else days[-31]
            for k in [k for k in self.hists if k[1] < cutoff]:
                del self.hists[k]
        return mins, shadows, hists

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"stats": dict(self.stats), "symbols": {}}
        for sym, st in sorted(self.syms.items()):
            out["symbols"][sym] = {
                "big_thr": st.big_thr,
                "median": st.median,
                "flow_med": st.flow_med,
                "hist_total": st.hist_total,
                "samples": len(st.net5),
                "last_ts": st.last_ts,
            }
        return out
