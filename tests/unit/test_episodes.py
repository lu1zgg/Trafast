import math

from tests.helpers import Harness, make_cfg, utc_ts

T0 = utc_ts(2026, 11, 24, 3, 0)
S = "BTCUSDT"


def h_seeded():
    h = Harness()
    t = h.seed(S, T0, 100.0)
    return h, t


def test_wick_is_rejected_and_return_leg_suppressed():
    h, t = h_seeded()
    h.bars(S, t, [100.7, 100.05, 100.0, 100.0])
    assert h.sends() == []
    assert "rejected" in h.outcomes() and "wick_return" in h.outcomes()


def test_held_move_is_pushed_after_confirmation():
    h, t = h_seeded()
    h.bar(S, t, 100.7)
    assert h.sends() == []  # 还在等确认
    h.bar(S, t + 60, 100.65)
    sends = h.sends()
    assert len(sends) == 1 and "L1 异动" in sends[0].text and "站稳：下一分钟仍保留" in sends[0].text
    assert sends[0].buttons[0][0]["callback_data"].startswith("fb|")


def test_fast_path_pushes_immediately():
    h, t = h_seeded()
    h.bar(S, t, 101.2)
    sends = h.sends()
    assert len(sends) == 1 and "L3 剧烈" in sends[0].text and "快速通道" in sends[0].text


def test_extension_reply_and_limits():
    h, t = h_seeded()
    t = h.bars(S, t, [100.7, 100.7])
    root = h.sends()[0].key
    p = 100.7
    for _ in range(40):  # 每分钟 +0.1%，持续走高
        p *= 1.001
        h.bar(S, t, p)
        t += 60
    replies = [o for o in h.outs if o.kind == "reply"]
    assert 1 <= len(replies) <= 3
    assert all(r.parent_key == root for r in replies)
    assert all("延伸" in r.text for r in replies)


def test_end_by_retrace_edits_root():
    h, t = h_seeded()
    t = h.bars(S, t, [100.7, 100.8, 100.9])
    h.bars(S, t, [100.6, 100.4, 100.3])
    edits = [o for o in h.outs if o.kind == "edit"]
    assert edits and "已结束" in edits[-1].text and edits[-1].parent_key == h.sends()[0].key
    assert edits[-1].silent


def test_end_by_idle_on_tick():
    h, t = h_seeded()
    t = h.bars(S, t, [100.7, 100.7])
    h.tick(t + 33 * 60)  # 没有新 K 线（例如休市），30 分钟后由时钟结束
    assert any(o.kind == "edit" and "已结束" in o.text for o in h.outs)


def test_cooldown_blocks_same_direction():
    h, t = h_seeded()
    t = h.bars(S, t, [100.7, 100.7, 100.3, 100.2])  # 推送后回撤结束
    t = h.bars(S, t, [100.2, 100.2])
    n = len(h.sends())
    h.bars(S, t, [100.85, 100.85])  # 同方向、不够 L3，冷却期内
    assert len(h.sends()) == n and "cooldown" in h.outcomes()


def test_mute_blocks_push():
    h, t = h_seeded()
    h.engine.mutes[S] = 1e12
    h.bars(S, t, [101.5, 101.5])
    assert h.sends() == [] and "muted" in h.outcomes()


def test_reversal_beyond_anchor_opens_new_episode():
    h, t = h_seeded()
    t = h.bars(S, t, [101.2, 101.2])  # 上涨 L3
    h.bars(S, t, [99.5, 99.5])  # 跌穿起点 1.2%（反向 L3）
    sends = h.sends()
    assert len(sends) == 2 and sends[1].text.startswith("🔴")
    assert any(o.kind == "edit" and "另起一条" in o.text for o in h.outs)


def test_stale_bar_is_not_detected():
    h, t = h_seeded()
    from market_monitor_v3.models import Bar

    h.engine.on_bar(Bar(S, t, 102, 102, 102, 102, 1, 1e6, "", "t"), now=t + 61 + 3600)
    h.collect()
    assert h.sends() == [] and h.engine.stats["stale_bars"] == 1


def test_duplicate_and_out_of_order_bars():
    h, t = h_seeded()
    h.bar(S, t, 100.0)
    h.bar(S, t, 100.0)  # 重复
    h.bar(S, t - 600, 120.0)  # 已有的旧分钟被改写：只更新历史，不检测
    h.bar(S, t - 7200, 120.0)  # 很久以前的 K 线乱序到达：只进历史，不检测
    st = h.engine.stats
    assert st["dup_bars"] == 1 and st["revised_bars"] == 1 and st["late_bars"] == 1
    assert h.sends() == []


def test_log_level_names():
    h, t = h_seeded()
    h.bars(S, t, [100.7, 100.7])
    d = [x for x in h.decisions if x.outcome == "pushed"][0]
    assert d.level == "L1" and d.k == 1 and math.isclose(d.d, math.log(1.007))
    assert d.rule_version.startswith("v4.1-")


def test_stock_synthetic_fill_and_late_real_bar_redetect():
    cfg = make_cfg(crypto={"enabled": False}, stocks={"symbols": ["MU.US"]})
    from tests.helpers import et_ts, flat_baselines

    bl = flat_baselines({"MU.US": ["PRE", "REG", "POST", "ON"]}, 0.0005)
    h = Harness(cfg, bl, alive=True)
    t = h.seed("MU.US", et_ts(2026, 11, 24, 10, 0), 600.0)
    # 10:30 这一分钟没有成交：收盘 grace 秒后补平 K 线
    h.tick(t + 60 + 7)
    assert h.engine.hist["MU.US"].get(t).synthetic
    # 真实 K 线晚到，而且有大幅位移：覆盖平 K 线并重新检测
    h.bar("MU.US", t, 612.0)
    assert len(h.sends()) == 1 and "MU" in h.sends()[0].text
