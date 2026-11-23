"""2026-11-23 盘前误报回归：GOOGL、STX、META 在 07:36 同时出现一根收在上周五盘中收盘价的 K 线，
L3 快速通道直接推了 -5%，下一分钟价格就回到原位。"""
from market_monitor_v3.models import Bar
from tests.helpers import Harness, et_ts, flat_baselines, make_cfg

S = "GOOGL.US"
FRI_CLOSE = 265.29
PRE = 279.50


def harness(**rules):
    cfg = make_cfg(stocks={"enabled": True, "symbols": [S, "STX.US"]}, crypto={"symbols": ["BTCUSDT"]}, rules=rules)
    bl = flat_baselines({S: ["PRE", "REG", "POST", "ON"], "STX.US": ["PRE", "REG", "POST", "ON"]}, 0.0006)
    h = Harness(cfg, bl)
    h.engine.mutes.clear()
    return h


def seed_friday_close(h, sym=S, price=FRI_CLOSE):
    t = et_ts(2026, 11, 20, 15, 59)
    h.engine.seed_history([Bar(sym, t, price, price, price, price, 1, 1e7, "REG", "t")])


def premarket(h, closes, start=(7, 20), sym=S):
    t = et_ts(2026, 11, 23, *start)
    return h.bars(sym, t, closes, turnover=3e5)


def test_bar_at_prior_close_is_stale_not_pushed():
    h = harness()
    seed_friday_close(h)
    t = premarket(h, [PRE] * 16)
    h.bars(S, t, [FRI_CLOSE, PRE - 0.05, PRE])
    assert h.sends() == []
    assert "stale_price" in h.outcomes()
    assert h.engine.stats["stale_price"] == 1


def test_premarket_fast_move_waits_one_bar_and_spike_is_rejected():
    """不等于前收的单根坏价：快速通道在盘前也要确认，下一分钟回去了就不推。"""
    h = harness()
    t = premarket(h, [PRE] * 16)
    h.bars(S, t, [262.0, PRE - 0.05, PRE])
    assert h.sends() == []
    assert "rejected" in h.outcomes()


def test_premarket_real_crash_still_pushed_as_l3_after_one_bar():
    h = harness()
    seed_friday_close(h)
    t = premarket(h, [PRE] * 16)
    t = h.bars(S, t, [262.0])
    assert h.sends() == []  # 第一分钟只是待确认
    h.bars(S, t, [261.5, 261.0])
    sends = h.sends()
    assert len(sends) == 1 and "L3" in sends[0].text


def test_regular_session_fast_path_unchanged():
    h = harness()
    t = et_ts(2026, 11, 23, 10, 0)
    t = h.bars(S, t, [PRE] * 16, turnover=5e6)
    h.bars(S, t, [262.0], turnover=5e6)
    assert len(h.sends()) == 1  # 盘中收盘即推


def test_fast_confirm_sessions_configurable():
    h = harness(fast_confirm_sessions=[])
    t = premarket(h, [PRE] * 16)
    h.bars(S, t, [262.0])
    assert len(h.sends()) == 1


def test_two_symbols_stale_at_same_minute_produce_nothing():
    h = harness()
    seed_friday_close(h)
    seed_friday_close(h, "STX.US", 457.01)
    t = et_ts(2026, 11, 23, 7, 20)
    for i in range(16):
        h.multi(t + 60 * i, {S: PRE, "STX.US": 482.22})
    t += 16 * 60
    h.multi(t, {S: FRI_CLOSE, "STX.US": 457.01})
    h.multi(t + 60, {S: PRE, "STX.US": 482.2})
    assert h.sends() == []
    assert h.outcomes().count("stale_price") == 2
