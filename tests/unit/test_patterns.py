"""V4 形态分类：四种类型的判定顺序、放量标签，以及分类不改变触发和级别。"""
from market_monitor_v3.config import Params
from market_monitor_v3.detector import evaluate
from market_monitor_v3.models import Bar
from market_monitor_v3.sessions import UsCalendar
from tests.helpers import Harness, flat_baselines, make_cfg, utc_ts
from market_monitor_v3.windows import SymbolHistory

CAL = UsCalendar()
P = Params(make_cfg())
T0 = utc_ts(2026, 11, 24, 3, 0)  # 周二 UTC，加密工作日时段
S = "BTCUSDT"
BL = flat_baselines({S: ["CWD", "CWE"]}, 0.0006)


def run(closes, turnovers=None):
    h = SymbolHistory()
    turnovers = turnovers or [1e6] * len(closes)
    bar = None
    for i, (c, tv) in enumerate(zip(closes, turnovers)):
        bar = Bar(S, T0 + 60 * i, c, c, c, c, 1, tv, "CWD", "t")
        h.add(bar)
    return evaluate(bar, h, P, BL, CAL.is_thin).candidate


def test_flat_range_then_jump_is_breakout():
    c = run([100] * 20 + [100.7])
    assert c.pattern == "breakout" and c.reason["pre_amp"] == 0


def test_one_minute_jump_out_of_noise_is_spike():
    noisy = [100, 100.6] * 10 + [100]
    c = run(noisy + [101.0])
    assert c.primary.k == 1 and c.pattern == "spike"


def test_bounce_after_15m_drop_is_v_reversal():
    drop = [102 - 2 * i / 15 for i in range(16)]  # 15 分钟从 102 跌到 100（约 -2%）
    c = run([102] * 5 + drop + [100.8])
    assert c.direction == 1 and c.pattern == "v_reversal" and c.reason["prior_leg"] < -0.015


def test_steady_5m_climb_is_trend():
    base = [100, 100.7] * 8 + [100]
    climb = [100 * 1.0025 ** i for i in range(1, 6)]
    c = run(base + climb)
    assert c.primary.k == 5 and c.pattern == "trend"
    assert c.reason["last_share"] < 0.5


def test_vol_tag_needs_three_heavy_bars():
    closes = [100] * 20 + [100.7]
    heavy = [1e6] * 18 + [7e6, 7e6, 7e6]
    assert "vol" in run(closes, heavy).tags
    one_bar = [1e6] * 20 + [20e6]  # 只有最后一根放量：不算
    assert "vol" not in run(closes, one_bar).tags
    light = [1e6] * 18 + [3.5e6, 3.5e6, 3.5e6]  # 每根 ≥3 倍但平均不到 6 倍
    assert "vol" not in run(closes, light).tags


def test_volume_does_not_change_level():
    # 成交额放大不再升级（V4 去掉了 turnover → L2），放量也只是标签
    c = run([100] * 20 + [100.6], [1e6] * 17 + [8e6] * 4)
    assert c.level == "L1" and "vol" in c.tags


def test_engine_emits_signal_events_and_tags_decisions():
    h = Harness()
    t = h.seed(S, T0, 100.0)
    h.bar(S, t, 101.2)  # L3 快速通道
    for i in range(1, 12):
        h.bar(S, t + 60 * i, 101.2 * 1.0015 ** i)
    for i in range(12, 20):
        h.bar(S, t + 60 * i, 100.0)  # 回撤过半，事件结束
    kinds = [ev.kind for ev in h.signals]
    assert kinds[0] == "open" and kinds[-1] == "end" and "extend" in kinds
    opened = h.signals[0]
    assert opened.pattern == "breakout" and opened.level == "L3" and opened.direction == 1
    assert opened.id == f"{opened.episode_id}:open" and opened.anchor == 100.0
    pushed = [d for d in h.decisions if d.outcome == "pushed"]
    assert pushed and pushed[0].pattern == "breakout"
    assert len({ev.id for ev in h.signals}) == len(h.signals)


def test_store_writes_signals_and_migrates_old_alerts_table(tmp_path):
    import sqlite3

    from market_monitor_v3.store import Store

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, symbol TEXT NOT NULL, session TEXT, "
        "level TEXT, outcome TEXT, k INTEGER, d REAL, threshold REAL, sigma REAL, floor REAL, bypass REAL, "
        "score REAL, retained REAL, turnover_ratio REAL, episode_id TEXT, rule_version TEXT)"
    )
    old.commit()
    old.close()
    h = Harness()
    t = h.seed(S, T0, 100.0)
    h.engine.on_bar(Bar(S, t, 101.2, 101.2, 101.2, 101.2, 1, 1e6, "", "t"), t + 61)
    eff = h.sink.drain()
    store = Store(db)
    store.open_sync()
    try:
        store.apply_effects(eff)
        row = store.c.execute("SELECT pattern FROM alerts WHERE outcome='pushed'").fetchone()
        assert row["pattern"] == "breakout"
        sig = store.signals_recent(0)
        assert sig and sig[0]["kind"] == "open" and sig[0]["pattern"] == "breakout"
    finally:
        store.close_sync()
