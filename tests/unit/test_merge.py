from tests.helpers import Harness, make_cfg, utc_ts

T0 = utc_ts(2026, 11, 24, 3, 0)


def seeded(cfg=None):
    h = Harness(cfg)
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        t = h.seed(s, T0, 100.0)
    return h, t


def test_same_minute_same_direction_merged():
    h, t = seeded()
    h.multi(t, {"BTCUSDT": 101.6, "ETHUSDT": 101.9, "SOLUSDT": 100.0})
    sends = h.sends()
    assert len(sends) == 1
    text = sends[0].text
    assert "同时异动（1）" in text
    # 联动标的也用完整卡片展开，不再缩成一行
    assert text.count("现价 <b>$") == 2 and text.count("多窗口") + text.count("成交额") >= 2


def test_late_joiner_is_edited_in():
    h, t = seeded()
    h.multi(t, {"BTCUSDT": 101.6, "ETHUSDT": 100.0, "SOLUSDT": 100.0})
    h.multi(t + 60, {"BTCUSDT": 101.6, "ETHUSDT": 100.0, "SOLUSDT": 100.0})
    h.multi(t + 120, {"BTCUSDT": 101.6, "ETHUSDT": 102.0, "SOLUSDT": 100.0})
    assert len(h.sends()) == 1
    edits = [o for o in h.outs if o.kind == "edit"]
    assert edits and "ETH" in edits[-1].text and edits[-1].silent


def test_opposite_directions_not_merged():
    h, t = seeded()
    h.multi(t, {"BTCUSDT": 101.6, "ETHUSDT": 98.0, "SOLUSDT": 100.0})
    assert len(h.sends()) == 2


def test_global_rate_limit_merges_overflow_into_summary():
    cfg = make_cfg(stocks={"enabled": False}, merge={"max_new_per_min": 1})
    h, t = seeded(cfg)
    h.multi(t, {"BTCUSDT": 101.6, "ETHUSDT": 98.0, "SOLUSDT": 100.0})
    assert len(h.sends()) == 1
    h.tick(t + 61 + 70)
    sends = h.sends()
    assert len(sends) == 2 and "合并为一条" in sends[1].text
