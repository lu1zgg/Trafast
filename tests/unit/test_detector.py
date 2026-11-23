import math

from market_monitor_v3.baseline import Baselines
from market_monitor_v3.config import Params
from market_monitor_v3.detector import evaluate
from market_monitor_v3.models import Bar
from market_monitor_v3.sessions import UsCalendar
from market_monitor_v3.windows import SymbolHistory
from tests.helpers import et_ts, flat_baselines, make_cfg, utc_ts

CAL = UsCalendar()
P = Params(make_cfg())
T0 = utc_ts(2026, 11, 24, 3, 0)  # 周二 UTC


def hist_with(symbol, closes, start=T0, session="CWD", turnover=1e6):
    h = SymbolHistory()
    for i, c in enumerate(closes):
        h.add(Bar(symbol, start + 60 * i, c, c, c, c, 1, turnover, session, "t"))
    return h


def run(symbol, closes, bl, session="CWD", last_turnover=1e6, start=T0, sessions=None):
    h = hist_with(symbol, closes[:-1], start, session)
    ts = start + 60 * (len(closes) - 1)
    bar = Bar(symbol, ts, closes[-1], closes[-1], closes[-1], closes[-1], 1, last_turnover, session, "t")
    h.add(bar)
    return evaluate(bar, h, P, bl, CAL.is_thin)


BL = flat_baselines({"BTCUSDT": ["CWD", "CWE"], "NVDA.US": ["PRE", "REG", "POST", "ON"]}, 0.0006)


def test_small_move_below_floor_is_ignored():
    ev = run("BTCUSDT", [100] * 20 + [100.4], BL)
    assert ev.candidate is None


def test_floor_candidate_l1():
    ev = run("BTCUSDT", [100] * 20 + [100.7], BL)
    c = ev.candidate
    assert c is not None and c.level == "L1" and not c.fast and c.direction == 1
    assert c.primary.k == 1 and c.primary.threshold == 0.005
    assert abs(c.score - math.log(1.007) / 0.005) < 1e-9


def test_fast_path_when_score_ge_2():
    ev = run("BTCUSDT", [100] * 20 + [101.2], BL)
    assert ev.candidate.fast and ev.candidate.level == "L3"


def test_two_windows_make_l2():
    closes = [100] * 16 + [100.1, 100.2, 100.3, 100.4, 101.1]
    c = run("BTCUSDT", closes, BL).candidate
    assert {h.k for h in c.hits} >= {1, 5} and c.level == "L2"


def test_turnover_makes_l2():
    c = run("BTCUSDT", [100] * 20 + [100.6], BL, last_turnover=3.5e6).candidate
    assert c.level == "L2" and c.primary.turnover_ratio >= 3


def test_volume_only_spike_never_triggers():
    ev = run("BTCUSDT", [100] * 20 + [100.05], BL, last_turnover=50e6)
    assert ev.candidate is None


def test_z_rule_dominates_floor_when_volatile():
    bl = flat_baselines({"BTCUSDT": ["CWD"]}, 0.002)  # Zσ = 1% > 地板 0.5%
    assert run("BTCUSDT", [100] * 20 + [100.8], bl).candidate is None
    assert run("BTCUSDT", [100] * 20 + [101.05], bl).candidate.primary.threshold == 5 * 0.002


def test_warmup_only_bypass():
    empty = Baselines()
    assert run("BTCUSDT", [100] * 20 + [100.9], empty).candidate is None
    c = run("BTCUSDT", [100] * 20 + [101.6], empty).candidate
    assert c.fast and c.primary.by_bypass and c.warmup


def test_window_across_session_boundary_uses_stricter_threshold():
    # 09:26–09:31：起点在盘前，用盘前 5 分钟地板 1.2%，而不是盘中的 0.8%
    sym = "NVDA.US"
    start = et_ts(2026, 11, 24, 9, 10)
    h = SymbolHistory()
    for i in range(22):
        ts = start + 60 * i
        h.add(Bar(sym, ts, 100, 100, 100, 100, 1, 1e6, CAL.session_at(ts), "t"))
    ts = et_ts(2026, 11, 24, 9, 31)
    for i, c in enumerate([100.25, 100.5, 100.75, 101.0]):
        b = Bar(sym, ts - 60 * (3 - i), c, c, c, c, 1, 1e6, CAL.session_at(ts - 60 * (3 - i)), "t")
        h.add(b)
    bar = h.get(ts)
    assert bar.session == "REG" and h.get(ts - 300).session == "PRE"
    ev = evaluate(bar, h, P, BL, CAL.is_thin)
    assert ev.candidate is None  # 1.0% < 盘前 1.2%


def test_thin_session_needs_turnover():
    sym = "NVDA.US"
    start = et_ts(2026, 11, 24, 1, 0)  # 夜盘
    closes = [100] * 20 + [101.0]
    blocked = run(sym, closes, BL, session="ON", last_turnover=1e4, start=start)
    assert blocked.candidate is None and blocked.thin_blocked
    ok = run(sym, closes, BL, session="ON", last_turnover=1e6, start=start)
    assert ok.candidate is not None


def test_move_turnover_follows_the_jump_bar():
    h = SymbolHistory()
    t = 1_800_000_000 - 1_800_000_000 % 60
    closes = [100.0, 100.01, 100.0, 102.0, 102.01]
    tovs = [1e5, 1e5, 1e5, 1e3, 1e5]  # 跳价那一根几乎没成交
    for i, (c, v) in enumerate(zip(closes, tovs, strict=True)):
        h.add(Bar("MU.US", t + 60 * i, c, c, c, c, 1, v, "ON", "t"))
    end = t + 60 * 4
    assert h.turnover(end, 4) > 3e5  # 整个窗口的成交额被正常 K 线撑起来
    moved = h.move_turnover(end, 4)
    assert moved is not None and moved < 0.1 * h.turnover(end, 4)  # 但走出位移的那根很薄
    assert h.move_turnover(t, 1) is None  # 没有前一根，算不出
