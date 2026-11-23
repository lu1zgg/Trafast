import math

import pytest

from market_monitor_v3.baseline import compute_symbol_baseline, robust_sigma
from market_monitor_v3.models import Bar, RootMessage, display_name
from market_monitor_v3.render import money, pct, to_plain
from market_monitor_v3.sessions import crypto_instance, crypto_session
from tests.helpers import random_walk, utc_ts


def test_pct_and_money():
    assert pct(math.log(1.024)) == "+2.4%"
    assert pct(math.log(0.9967)) == "-0.33%"
    assert money(3_200_000, "美元") == "320 万美元"
    assert money(1_000_000, "USDT") == "100 万 USDT"
    assert money(250_000_000, "USDT") == "2.50 亿 USDT"


def test_names_and_plain():
    assert display_name("NVDA.US") == "NVDA" and display_name("BTCUSDT") == "BTC"
    assert to_plain("<b>MU</b> &lt;x&gt;") == "MU <x>"
    r = RootMessage("k", "US", -1, "lead", ["MU.US", "SNDK.US"], members=["SNDK -1.9%"], end_lines=["MU 已结束"])
    assert r.text() == "lead\n同时：SNDK -1.9%\nMU 已结束"


def test_robust_sigma_floor_and_mad():
    assert robust_sigma([]) is None
    assert robust_sigma([0.0] * 10) == 1e-5
    rs = [0.001, -0.001] * 50
    assert robust_sigma(rs) == pytest.approx(max(1.4826 * 0.001, 0.8 * 0.001))


def test_compute_baseline_ready_and_split():
    start = utc_ts(2026, 11, 9, 0, 0)  # 周一
    bars = random_walk("BTCUSDT", start, 14 * 1440, 90000, 0.0006, seed=3)
    st = compute_symbol_baseline(bars, lambda b: crypto_session(b.ts), lambda b: crypto_instance(b.ts), 10_000, 2)
    assert st[("CWD", 1)].n_instances == 10 and st[("CWE", 1)].n_instances == 4
    assert st[("CWD", 1)].ready and st[("CWE", 15)].ready
    assert st[("CWD", 1)].sigma == pytest.approx(0.0006, rel=0.1)
    assert st[("CWD", 15)].sigma == pytest.approx(0.0006 * math.sqrt(15), rel=0.15)
    st2 = compute_symbol_baseline(bars[:1440], lambda b: crypto_session(b.ts), lambda b: crypto_instance(b.ts), 10_000, 2)
    assert not st2[("CWD", 1)].ready


def test_baseline_returns_stay_inside_instance():
    # 两个实例之间有缺口：跨实例的收益不算
    b = [Bar("X", 0, 1, 1, 1, 1), Bar("X", 60, 1, 1, 1, 1), Bar("X", 120, 2, 2, 2, 2)]
    st = compute_symbol_baseline(b, lambda x: "CWD", lambda x: "A" if x.ts < 120 else "B", 10, 1)
    assert st[("CWD", 1)].n_returns == 1
