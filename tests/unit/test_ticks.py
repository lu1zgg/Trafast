"""V4 P2 逐笔层：大单门槛、分钟摘要、大单流向标签、单笔冲涨跌影子记录、影子报告。"""
import json
import math

from market_monitor_v3.config import Params
from market_monitor_v3.feeds.binance_trades import parse_agg_trade
from market_monitor_v3.render import to_plain
from market_monitor_v3.tick_report import report_lines, summarize
from market_monitor_v3.ticks import N_BUCKETS, TickHub, TickMinute, Trade, bucket_of, bucket_value, quantile
from tests.helpers import Harness, et_ts, flat_baselines, make_cfg, utc_ts

T0 = utc_ts(2026, 11, 24, 3, 0)
S = "BTCUSDT"


def hub(**ticks):
    cfg = make_cfg(stocks={"enabled": False}, ticks={"min_trades": 100, "flow_min_samples": 10, **ticks})
    h = Harness(cfg)
    hb = TickHub(cfg.ticks, Params(cfg), h.baselines, h.engine.hist.get, h.engine.session_for)
    counts = [0] * N_BUCKETS
    counts[bucket_of(100.0)] = 5000
    hb.load_hists([(S, "2026-11-23", "*", counts)])
    return hb, h


def test_histogram_quantiles():
    counts = [0] * N_BUCKETS
    counts[bucket_of(100)] = 98
    counts[bucket_of(50_000)] = 2
    assert math.isclose(quantile(counts, 0.5), bucket_value(bucket_of(100)))
    assert quantile(counts, 0.99) == bucket_value(bucket_of(50_000))
    assert quantile([0] * N_BUCKETS, 0.5) is None
    assert 90 < bucket_value(bucket_of(100)) < 110


def test_minute_summary_and_big_flow():
    hb, _ = hub()
    hb.load_minutes([TickMinute(S, T0 - 3600 + 60 * i, "CWD", 10, 0, 0, 1, 1000.0, 0.0) for i in range(30)])
    t = T0 + 5
    for i in range(5):
        hb.on_trade(Trade(S, t + i, 100.0, 200.0, +1))  # 每笔 $20,000：大单，主动买
    hb.on_trade(Trade(S, t + 6, 100.0, 1.0, -1))  # $100：不是大单
    net, ratio = hb.flow(S, T0 + 60)
    assert net == 100_000 and ratio > 3
    hb.roll(T0 + 60 + 6)
    mins, shadows, hists = hb.drain()
    (m,) = mins
    assert (m.n, m.big_n, m.buy, m.sell, m.big_buy) == (6, 5, 100_000, 100, 100_000)
    assert shadows == [] and hists and hists[0][0] == S


def test_flow_needs_baseline():
    hb, _ = hub(flow_min_samples=1000)
    hb.on_trade(Trade(S, T0 + 1, 100.0, 200.0, +1))
    assert hb.flow(S, T0 + 60) is None


def feed_jump(hb, after):
    t = T0
    for i in range(70):  # 一分多钟的平稳成交
        hb.on_trade(Trade(S, t + i, 100.0, 1.0, 1 if i % 2 else -1))
    t += 70
    hb.on_trade(Trade(S, t, 101.2, 10.0, +1))  # 跳 +1.2%，$1012 ≥ 5 × 中位数
    for i, p in enumerate(after):
        hb.on_trade(Trade(S, t + 2 + i, p, 1.0, +1))
    hb.roll(t + 25)
    return hb.drain()[1]


def test_jump_accepted_and_passes_gate():
    hb, _ = hub()
    (sh,) = feed_jump(hb, [101.1, 101.15, 101.1])
    assert sh.status == "accepted" and sh.direction == 1 and sh.n_after == 3
    assert sh.retain > 0.9 and math.isclose(sh.d1m, math.log(101.1 / 100))
    assert math.isclose(sh.gate, 0.005) and sh.gate_pass
    assert hb.stats["gate_pass"] == 1


def test_jump_rejected_when_price_returns():
    hb, _ = hub()
    (sh,) = feed_jump(hb, [100.1, 100.0, 100.05])
    assert sh.status == "rejected" and sh.retain < 0.6 and not sh.gate_pass


def test_small_or_thin_jump_ignored():
    hb, _ = hub()
    hb.on_trade(Trade(S, T0, 100.0, 1.0, 1))
    hb.on_trade(Trade(S, T0 + 1, 100.8, 10.0, 1))  # 0.8% < 1%
    hb.on_trade(Trade(S, T0 + 2, 102.0, 0.01, 1))  # 跳 1.2% 但只有 $1：零股
    hb.roll(T0 + 60)
    assert hb.drain()[1] == [] and hb.stats["jumps"] == 0


def test_us_extended_sessions_not_used():
    cfg = make_cfg(crypto={"enabled": False}, stocks={"symbols": ["MU.US"]}, ticks={"min_trades": 100})
    bl = flat_baselines({"MU.US": ["PRE", "REG", "POST", "ON"]}, 0.0005)
    h = Harness(cfg, bl)
    hb = TickHub(cfg.ticks, Params(cfg), bl, h.engine.hist.get, h.engine.session_for)
    t = et_ts(2026, 11, 24, 6, 0)  # 盘前
    counts = [0] * N_BUCKETS
    counts[bucket_of(600.0)] = 5000
    hb.load_hists([("MU.US", "2026-11-23", "PRE", counts)])
    hb.on_trade(Trade("MU.US", t, 600.0, 1.0, 0))
    hb.on_trade(Trade("MU.US", t + 1, 590.0, 10.0, 0))
    (sh,) = hb.drain()[1]
    assert sh.status == "ext_session" and sh.session == "PRE"


def test_engine_flow_tag_on_card():
    h = Harness()
    h.engine.flow = lambda sym, ts: (2e6, 4.0)
    t = h.seed(S, T0, 100.0)
    h.bars(S, t, [100.7, 100.65])
    text = to_plain(h.sends()[0].text)
    assert "5 分钟大单净买入 $2.00M｜常态的 4.0 倍" in text
    # 方向相反或倍数不够：只记在 reason 里，卡片不写
    h2 = Harness()
    h2.engine.flow = lambda sym, ts: (-2e6, -4.0)
    t = h2.seed(S, T0, 100.0)
    h2.bars(S, t, [100.7, 100.65])
    assert "大单" not in to_plain(h2.sends()[0].text)
    assert h2.signals[0].reason["flow_ratio"] == -4.0


def test_parse_agg_trade():
    msg = {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "97000.5", "q": "0.25", "T": 1795491000123, "m": True}}
    t = parse_agg_trade(msg)
    assert t.symbol == S and t.side == -1 and t.price == 97000.5 and math.isclose(t.ts, 1795491000.123)
    msg["data"]["m"] = False
    assert parse_agg_trade(msg).side == 1
    assert parse_agg_trade({"data": {"e": "kline"}}) is None


def test_shadow_summary():
    shadows = [
        {"symbol": S, "ts": 1000.0, "accept_ts": 1020.0, "direction": 1, "status": "accepted", "gate_pass": True},
        {"symbol": S, "ts": 5000.0, "accept_ts": 5020.0, "direction": -1, "status": "accepted", "gate_pass": True},
        {"symbol": S, "ts": 7000.0, "direction": 1, "status": "rejected", "gate_pass": False},
    ]
    signals = [
        {"symbol": S, "ts": 1080, "kind": "open", "direction": 1, "pattern": "spike"},
        {"symbol": S, "ts": 9000, "kind": "open", "direction": -1, "pattern": "spike"},
    ]
    s = summarize(shadows, signals, {S})
    assert (s.jumps, s.accepted, s.gate_pass, s.matched, s.unmatched) == (3, 2, 2, 1, 1)
    assert s.leads == [60.0] and s.spikes == 2 and s.missed == 1
    text = "\n".join(report_lines(s))
    assert "中位数早 60 秒" in text and "没有 K 线推送 1" in text


def test_tick_tables_roundtrip(tmp_path):
    from market_monitor_v3.store import Store

    hb, _ = hub()
    feed_jump(hb, [101.1, 101.15, 101.1])  # 只取直方图和分钟
    hb.on_trade(Trade(S, T0 + 200, 101.1, 1.0, 1))
    hb.roll(T0 + 400)
    mins, _, hists = hb.drain()
    st = Store(tmp_path / "t.db")
    st.open_sync()
    try:
        hb2, _ = hub()
        (sh,) = feed_jump(hb2, [101.1, 101.15, 101.1])
        st.tick_save(mins, [sh], hists)
        st.tick_save(mins, [], hists)  # 重复写不报错
        assert len(st.tick_minutes(T0 - 60)) == len(mins)
        assert {h[1] for h in st.tick_hists("2026-11-01")} >= {"2026-11-24"}
        (row,) = st.tick_shadows(T0 - 60)
        assert row["status"] == "accepted" and row["gate_pass"]
    finally:
        st.close_sync()


async def test_runtime_wires_tick_layer(tmp_path):
    from market_monitor_v3.app import Runtime
    from market_monitor_v3.config import Secrets
    from market_monitor_v3.engine import Engine
    from market_monitor_v3.store import Store

    cfg = make_cfg(
        stocks={"enabled": False},
        general={"state_dir": str(tmp_path), "control_socket": str(tmp_path / "c.sock")},
        ticks={"min_trades": 100},
    )
    rt = Runtime(cfg, None, Secrets("x", None), clock=lambda: T0 + 30)
    await rt.store.open()
    try:
        counts = [0] * N_BUCKETS
        counts[bucket_of(100.0)] = 5000
        await rt.store.call(Store.tick_save, [], [], [(S, "2026-11-23", "*", counts)])
        rt.engine = Engine(cfg, rt.params, rt.cal, rt.baselines, rt.sink)
        await rt._start_ticks()
        assert rt.binance_trades is not None and rt.engine.flow is not None
        rt.binance_trades.handle(
            json.dumps({"data": {"e": "aggTrade", "s": S, "p": "100", "q": "500", "T": (T0 + 1) * 1000, "m": False}})
        )
        assert rt.ticks.syms[S].big_thr is not None and rt.ticks.stats["big"] == 1
        rt.ticks.roll(T0 + 70)
        await rt.save_ticks(force=True)
        assert len(await rt.store.call(Store.tick_minutes, T0 - 60)) == 1
        assert "逐笔：" in rt.ticks_text(T0 + 30) and "大单门槛就绪：BTC" in rt.ticks_text(T0 + 30)
    finally:
        await rt.store.close()
