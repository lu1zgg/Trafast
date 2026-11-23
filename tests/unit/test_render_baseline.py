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
    assert money(3_620_000) == "$3.62M"
    assert money(845_300) == "$845.30K"
    assert money(1_250_000_000) == "$1.25B"
    assert money(512) == "$512.00"
    assert money(1_000_000, "USDT") == "1.00M USDT"


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


def _card(symbol="NVDA.US", direction=-1, **kw):
    card = {
        "ep": "e1",
        "symbol": symbol,
        "market": "US" if symbol.endswith(".US") else "CRYPTO",
        "direction": direction,
        "level": "L3",
        "session": "PRE",
        "k": 1,
        "d": math.log(134.03 / 136.66),
        "price": 134.03,
        "p0": 136.66,
        "retained": None,
        "hits": [[1, math.log(0.981)], [5, math.log(0.976)], [15, math.log(0.973)]],
        "turnover": 3_620_000,
        "turnover_ratio": 2.1,
        "close_ts": utc_ts(2026, 11, 23, 9, 38),
        "end": None,
    }
    card.update(kw)
    return card


def test_card_template():
    from market_monitor_v3.render import DIVIDER, card_text

    text = card_text(_card())
    lines = text.split("\n")
    assert lines[0] == "🔴 <b>NVDA</b> <b>-1.9%</b>｜1 分钟｜L3 剧烈"
    assert lines[1] == "现价 <b>$134.03</b>（盘前）｜起点 <b>$136.66</b>"
    assert lines.count(DIVIDER) == 3
    assert "1 分钟 <b>-1.9%</b>｜5 分钟 <b>-2.4%</b>｜15 分钟 <b>-2.7%</b>" in lines
    assert "1 分钟 <b>$3.62M</b>｜该时段常态的 <b>2.1 倍</b>" in lines
    assert lines[-1] == "北京 11-23 17:38｜美东 04:38"
    up = card_text(_card(direction=1, d=math.log(1.02), retained=0.72, hits=[[1, math.log(1.02)]]))
    assert up.startswith("🟢") and "多窗口" not in up and "仍保留 <b>72%</b>" in up


def test_group_lists_every_member_in_full_and_stays_under_limit():
    from market_monitor_v3.render import MAX_TEXT, cards_text, to_plain

    cards = [_card(), _card("INTC.US", d=math.log(0.982), end={"max": -0.018, "close": -0.002, "minutes": 3, "reason": "idle"})]
    text = cards_text(cards)
    assert "同时异动（1）" in text and text.count("现价") == 2 and text.count("成交额") == 2
    assert "INTC" in to_plain(text) and "已结束：最大 <b>-1.8%</b>" in text
    many = cards_text([_card(f"S{i}.US") for i in range(40)])
    assert len(to_plain(many)) <= MAX_TEXT and "S1</b>" in many and "个标的未展开" in many
