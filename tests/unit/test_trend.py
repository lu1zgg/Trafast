"""趋势监控：单周期打分、状态机、分桶、支撑压力位、引擎（推送 / 静默回放 / 对账）、文案、存储、路由、命令。"""
import math
import random
from datetime import datetime

import pytest

from market_monitor_v3.bot import run_command
from market_monitor_v3.models import Bar
from market_monitor_v3.notifiers import Route
from market_monitor_v3.sessions import ET, UsCalendar
from market_monitor_v3.store import Store
from market_monitor_v3.trend.buckets import UsRegularBucketer, UtcBucketer
from market_monitor_v3.trend.engine import TrendEngine
from market_monitor_v3.trend.levels import LevelParams, compute_levels, prior_day_range
from market_monitor_v3.trend.machine import SymbolTrend, TrendParams
from market_monitor_v3.trend.render import ended_text, formed_text, overview_text
from market_monitor_v3.trend.series import Candle, TFAnalyzer

SYM = "BTCUSDT"
T0 = 1_767_225_600  # 2026-01-01 00:00 UTC


def series(n: int = 1200, seed: int = 7) -> list[Candle]:
    """先横盘，后面一段稳定下跌：在第 1114 根左右形成下跌趋势，1143 根左右收盘突破失效位结束。"""
    rnd = random.Random(seed)
    out, x = [], 100.0
    for i in range(n):
        r = 0.0012 * math.sin(i / 9) + rnd.gauss(0, 0.003)
        o, c = x, x * (1 + r)
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.0015)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.0015)))
        ts = T0 + i * 900
        out.append(Candle(ts, ts + 900, o, h, lo, c, 1e6 * (1 + rnd.random())))
        x = c
    return out


def run_machine(candles):
    m = SymbolTrend(SYM, UtcBucketer(), TrendParams())
    evs = []
    for i, k in enumerate(candles):
        for ev in m.on_candle15(k):
            evs.append((i, ev))
    return m, evs


def minutes_of(k: Candle, symbol: str = SYM) -> list[Bar]:
    """把一根 15m 拆成 15 根 1m：第一根走完整根的高低，后面平在收盘价，合成回来和原来一样。"""
    out = [Bar(symbol, k.ts, k.o, k.h, k.l, k.c, 0.0, k.v / 15)]
    out += [Bar(symbol, k.ts + j * 60, k.c, k.c, k.c, k.c, 0.0, k.v / 15) for j in range(1, 15)]
    return out


# ---- 单周期 ----
def test_analyzer_scores_trend_and_finds_pivots():
    up = TFAnalyzer(1.5, 20)
    x = 100.0
    st = None
    for i in range(300):
        r = 0.004 + 0.012 * math.sin(i / 5)
        o, c = x, x * (1 + r)
        st = up.add(Candle(T0 + i * 3600, T0 + (i + 1) * 3600, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1.0))
        x = c
    assert st is not None
    assert st.e == 1 and st.sc >= 2 and st.s == 1
    assert st.ll is not None and st.ll < st.close
    assert any(p.kind == "L" for p in up.pivots) and any(p.kind == "H" for p in up.pivots)
    assert "上涨" in st.label() or "多" in st.label()


# ---- 状态机 ----
def test_machine_forms_and_ends_a_trend():
    m, evs = run_machine(series())
    kinds = [(ev.kind, ev.d) for _, ev in evs]
    assert ("formed", -1) in kinds
    i_formed, formed = next((i, ev) for i, ev in evs if ev.kind == "formed")
    ended = next(ev for _, ev in evs if ev.kind == "ended")
    assert formed.stop > formed.price  # 下跌趋势的失效位在上方
    assert formed.quiet == ""
    assert formed.grade in ("A", "B")
    assert ended.entry_t == formed.entry_t and ended.reason in ("stop", "flip")
    assert ended.t > formed.t
    assert i_formed >= 960  # 4h 要 60 根才开始判断
    snap = m.snapshot()
    assert snap["ready"] and snap["bars"]["4h"] >= 60


def test_machine_not_ready_without_enough_history():
    m, evs = run_machine(series(900))
    assert not m.ready and evs == []


def test_quiet_trend_is_tracked_but_marked():
    p = TrendParams(max_ext_atr1h=0.01)  # 一定算“离均线太远”
    m = SymbolTrend(SYM, UtcBucketer(), p)
    evs = [ev for k in series() for ev in m.on_candle15(k)]
    formed = [ev for ev in evs if ev.kind == "formed"]
    assert formed and all("离均线太远" in ev.quiet for ev in formed)


# ---- 分桶 ----
def _et(y, mo, d, hh, mm):
    return int(datetime(y, mo, d, hh, mm, tzinfo=ET).timestamp())


def test_us_bucketer_regular_session_and_half_day_bars():
    bk = UsRegularBucketer(UsCalendar())
    assert bk.key15(_et(2026, 11, 23, 9, 0)) is None  # 盘前
    assert bk.key15(_et(2026, 11, 23, 16, 5)) is None  # 盘后
    assert bk.key15(_et(2026, 11, 22, 11, 0)) is None  # 周日
    s, e = bk.key15(_et(2026, 11, 23, 9, 47))
    assert (s, e) == (_et(2026, 11, 23, 9, 45), _et(2026, 11, 23, 10, 0))
    assert bk.hkey("1h", _et(2026, 11, 23, 10, 15)) == (_et(2026, 11, 23, 9, 30), _et(2026, 11, 23, 10, 30))
    assert bk.hkey("1h", _et(2026, 11, 23, 15, 45)) == (_et(2026, 11, 23, 15, 30), _et(2026, 11, 23, 16, 0))
    assert bk.hkey("4h", _et(2026, 11, 23, 13, 15)) == (_et(2026, 11, 23, 9, 30), _et(2026, 11, 23, 13, 30))
    assert bk.hkey("4h", _et(2026, 11, 23, 13, 30)) == (_et(2026, 11, 23, 13, 30), _et(2026, 11, 23, 16, 0))
    # 感恩节次日提前到 13:00 收盘
    assert bk.key15(_et(2026, 11, 27, 13, 15)) is None
    assert bk.hkey("4h", _et(2026, 11, 27, 12, 45)) == (_et(2026, 11, 27, 9, 30), _et(2026, 11, 27, 13, 0))


def test_utc_bucketer():
    bk = UtcBucketer()
    assert bk.key15(T0 + 1000) == (T0 + 900, T0 + 1800)
    assert bk.hkey("4h", T0 + 5 * 3600) == (T0 + 4 * 3600, T0 + 8 * 3600)


# ---- 支撑压力位 ----
def test_levels_split_above_and_below_price():
    m, _ = run_machine(series(1100))
    lv = m.levels()
    assert lv is not None and lv.atr1 > 0
    assert lv.res and lv.sup
    assert all(z.lo > lv.price - 1e-9 or z.hi > lv.price for z in lv.res)
    assert all(z.hi < lv.price + 1e-9 or z.lo < lv.price for z in lv.sup)
    assert all(z.stars().count("★") >= 1 for z in lv.res + lv.sup)
    # 离现价近的在前
    assert [z.lo for z in lv.res] == sorted(z.lo for z in lv.res)


def test_prior_day_range_and_empty_levels():
    cs = [Candle(T0 + i * 900, T0 + (i + 1) * 900, 10, 11 + (i % 96 == 5), 9, 10, 1) for i in range(150)]
    assert prior_day_range(cs, lambda t: t // 86400) == (12, 9)
    lv = compute_levels(cs[-1].end, 10.0, 0.0, [], [], [], lambda t: t // 86400, LevelParams())
    assert lv.res == [] and lv.sup == []


# ---- 引擎 ----
def _engine(out, **kw):
    return TrendEngine([SYM, "NVDA.US"], UsCalendar(), TrendParams(), out.append, **kw)


def test_engine_pushes_formed_and_replies_ended_from_live_minutes():
    cs = series()
    out = []
    eng = _engine(out)
    eng.seed(SYM, cs[:1000], now=cs[999].end)
    assert out == []  # 回放不推送
    eff = eng.drain()
    assert eff.states[SYM]["t"] == cs[999].end and eff.events == []
    for k in cs[1000:]:
        for bar in minutes_of(k):
            eng.on_bar(bar, bar.ts + 60)
    sends = [m for m in out if m.kind == "send"]
    replies = [m for m in out if m.kind == "reply"]
    assert len(sends) == 1 and sends[0].key.startswith(f"trend:{SYM}:")
    assert sends[0].buttons and sends[0].market == "CRYPTO" and sends[0].direction == -1
    assert "下跌趋势形成" in sends[0].text and "失效位" in sends[0].text
    assert len(replies) == 1 and replies[0].parent_key == sends[0].key and "趋势结束" in replies[0].text
    eff = eng.drain()
    assert [e[2] for e in eff.events] == ["formed", "ended"]
    assert len(eff.candles) == 200  # 1m 合成出来的 15m 都要落库
    assert eff.candles[0][1].ts == cs[1000].ts and eff.candles[0][1].end == cs[1000].end
    assert eff.states[SYM]["active"] is None


def test_engine_buffers_live_bars_until_seeded():
    cs = series(1010)
    out = []
    eng = _engine(out)
    for bar in minutes_of(cs[1005]):
        eng.on_bar(bar, bar.ts + 60)
    assert eng.machines[SYM].last_t == 0 and len(eng.pending[SYM]) == 15
    eng.seed(SYM, cs[:1005], now=cs[1005].end)
    assert eng.machines[SYM].last_t == cs[1005].end and eng.pending[SYM] == []


def test_engine_muted_or_stale_trend_is_not_pushed():
    cs = series()
    out = []
    eng = _engine(out, is_muted=lambda s, now: True)
    eng.seed(SYM, cs[:1000], now=cs[999].end)
    for k in cs[1000:]:
        for bar in minutes_of(k):
            eng.on_bar(bar, bar.ts + 60)
    assert out == []
    assert [e[2] for e in eng.drain().events] == ["formed", "ended"]  # 照样记录

    out2 = []
    eng2 = _engine(out2)
    eng2.seed(SYM, cs[:1000], now=cs[999].end)
    for k in cs[1000:]:
        for bar in minutes_of(k):
            eng2.on_bar(bar, bar.ts + 60 + 7200)  # 处理时已经过了两小时
    assert out2 == []


def test_reconcile_after_restart():
    cs = series()
    # 重启前推过一段下跌趋势；回放后这段已经结束 → 在原消息下补一句
    out = []
    eng = _engine(out)
    saved = {"active": {"d": -1, "entry_t": 123, "msg_key": "trend:BTCUSDT:123"}}
    eng.seed(SYM, cs, persisted=saved, now=cs[-1].end)
    assert len(out) == 1 and out[0].kind == "reply" and out[0].parent_key == "trend:BTCUSDT:123"
    assert "已结束" in out[0].text

    # 回放后仍是同一段趋势 → 接上原消息键，结束时回复它
    eng2 = _engine([])
    _, evs = run_machine(cs[:1120])
    formed = next(ev for _, ev in evs if ev.kind == "formed")
    saved = {"active": {"d": -1, "entry_t": formed.entry_t, "msg_key": "trend:BTCUSDT:x"}}
    eng2.seed(SYM, cs[:1120], persisted=saved, now=cs[1119].end)
    assert eng2.machines[SYM].active["msg_key"] == "trend:BTCUSDT:x"
    out3 = []
    eng2.out = out3.append
    for k in cs[1120:]:
        for bar in minutes_of(k):
            eng2.on_bar(bar, bar.ts + 60)
    assert [m.parent_key for m in out3 if m.kind == "reply"] == ["trend:BTCUSDT:x"]


def test_engine_ignores_us_bars_outside_regular_session():
    out = []
    eng = _engine(out)
    eng.seed("NVDA.US", [], now=0)
    ts = _et(2026, 11, 23, 8, 0)
    eng.on_bar(Bar("NVDA.US", ts, 1, 1, 1, 1, 0, 1), ts + 60)
    assert eng.open15["NVDA.US"] is None
    ts = _et(2026, 11, 23, 9, 30)
    eng.on_bar(Bar("NVDA.US", ts, 1, 1, 1, 1, 0, 1), ts + 60)
    assert eng.open15["NVDA.US"] is not None


def test_status_and_texts():
    cs = series()
    eng = _engine([])
    eng.seed(SYM, cs[:1120], now=cs[1119].end)
    items = {it["symbol"]: it for it in eng.status_json()}
    assert items[SYM]["seeded"] and items[SYM]["active"]["d"] == -1 and items[SYM]["res"]
    assert not items["NVDA.US"]["seeded"]
    ov = eng.overview_text()
    assert "下跌趋势" in ov and "数据不足" in ov
    detail = eng.detail_text(SYM)
    assert "趋势详情" in detail and "支撑" in detail and "压力" in detail


def test_render_formed_and_ended():
    m, evs = run_machine(series())
    formed = next(ev for _, ev in evs if ev.kind == "formed")
    ended = next(ev for _, ev in evs if ev.kind == "ended")
    t = formed_text(formed, m.levels(), 2, 4)
    assert t.startswith("📉 🔴") and "站稳 1 小时" in t and "1h 收盘突破即结束" in t and "北京" in t
    e = ended_text(ended, m.levels())
    assert "下跌趋势结束" in e and "形成于" in e
    assert "看单个标的" in overview_text([])


# ---- 存储、路由、命令 ----
async def test_store_trend_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    await store.open()
    try:
        cs = series()
        eng = _engine([])
        eng.seed(SYM, cs[:1000], now=cs[999].end)
        for k in cs[1000:]:
            for bar in minutes_of(k):
                eng.on_bar(bar, bar.ts + 60)
        await store.call(Store.apply_trend, eng.drain())
        rows = await store.call(Store.load_trend_bars, SYM, 0)
        assert len(rows) == 200
        k = Candle.from_row(rows[0])
        assert (k.ts, k.end, k.o, k.c) == (cs[1000].ts, cs[1000].end, cs[1000].o, pytest.approx(cs[1000].c))
        st = await store.call(Store.trend_states)
        assert st[SYM]["active"] is None and st[SYM]["last_end"]["-1"] > 0
        evs = await store.call(Store.trend_events_recent, 0, 10)
        assert [e["kind"] for e in evs] == ["ended", "formed"] and evs[1]["data"]["d"] == -1
    finally:
        await store.close()


def test_route_trend_flag():
    assert Route().accepts("send", "trend:BTCUSDT:1", None, "CRYPTO")
    assert not Route(trend=False).accepts("send", "trend:BTCUSDT:1", None, "CRYPTO")
    assert not Route(markets=("US",)).accepts("send", "trend:BTCUSDT:1", None, "CRYPTO")
    # 趋势不受告警开关和级别影响
    assert Route(alerts=False, min_level="L3").accepts("send", "trend:NVDA.US:1", None, "US")


class _Hooks:
    async def trend_text(self, args):
        return f"TREND {' '.join(args)}"


@pytest.mark.parametrize("args,expect", [([], "TREND "), (["NVDA"], "TREND NVDA")])
async def test_trend_command(args, expect):
    assert await run_command(_Hooks(), "trend", args, None, "u", set()) == expect
