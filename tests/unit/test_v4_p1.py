"""V4 P1：类型卡片、急拉急跌多确认、类型开关、分组联动、自定义提醒。"""
from market_monitor_v3.bot import run_command
from market_monitor_v3.custom import CustomAlerts, CustomRule, parse_rule
from market_monitor_v3.models import Bar
from market_monitor_v3.render import to_plain
from tests.helpers import Harness, utc_ts

T0 = utc_ts(2026, 11, 24, 3, 0)  # 周二 UTC
S = "BTCUSDT"


def noisy(h, t, n=6):
    """振幅 0.45% 的来回震荡（每步都低于 0.5% 地板）：之后的 1 分钟跳价算急拉急跌，不算盘整突破。"""
    return h.bars(S, t, [100, 100.45] * n + [100])


def test_breakout_card_has_type_and_reason():
    h = Harness()
    t = h.seed(S, T0, 100.0)
    h.bars(S, t, [100.7, 100.65])
    text = to_plain(h.sends()[0].text)
    first, _, third = text.split("\n")[:3]
    assert first.startswith("🟢 BTC +0.70%｜盘整突破｜L1")
    assert third.startswith("异动前 10 分钟振幅 0.00%，之后 1 分钟涨 0.70%")


def test_spike_waits_two_more_bars():
    h = Harness()
    t = noisy(h, h.seed(S, T0, 100.0))
    h.bar(S, t, 100.8)  # 急拉，L2
    h.bar(S, t + 60, 100.75)  # V3 在这里就推了
    h.bar(S, t + 120, 100.7)
    assert h.sends() == []
    h.bar(S, t + 180, 100.72)
    sends = h.sends()
    assert len(sends) == 1
    text = to_plain(sends[0].text)
    assert "｜急拉｜L2" in text.split("\n")[0] and "站稳：之后 3 分钟都保留" in text
    assert "1 分钟内涨 0.80%" in text


def test_spike_dropped_if_any_extra_bar_fails():
    h = Harness()
    t = noisy(h, h.seed(S, T0, 100.0))
    h.bars(S, t, [100.8, 100.75, 100.2, 100.2, 100.2])
    assert h.sends() == []
    assert "spike_rejected" in h.outcomes()


def test_spike_fast_lane_l3_not_delayed():
    h = Harness()
    t = h.seed(S, T0, 100.0)
    t = h.bars(S, t, [100, 100.8] * 6 + [100])  # 振幅 0.8%，比 1.3% 的一半大：算急拉
    h.bar(S, t, 101.3)
    assert len(h.sends()) == 1 and "｜急拉｜L3" in h.sends()[0].text


def test_type_off_is_recorded_not_pushed():
    h = Harness()
    h.engine.types_off = {"*": ["breakout"]}
    t = h.seed(S, T0, 100.0)
    h.bars(S, t, [100.7, 100.65])
    assert h.sends() == [] and "type_off" in h.outcomes()
    # 只关某个分组：BTC 在默认的“加密”分组里
    h2 = Harness()
    h2.engine.types_off = {"group:加密": ["breakout"]}
    t = h2.seed(S, T0, 100.0)
    h2.bars(S, t, [100.7, 100.65])
    assert h2.sends() == []


def test_group_linkage_title():
    h = Harness()
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for s in syms:
        t = h.seed(s, T0, 100.0)
    h.multi(t, {s: 102.0 for s in syms})
    sends = h.sends()
    assert len(sends) == 1
    assert to_plain(sends[0].text).split("\n")[0] == "⚡ 加密板块联动上涨（3 只）"


# ---- 自定义提醒 ----
def resolve(s):
    s = s.upper()
    for c in (s, s + ".US", s + "USDT"):
        if c in ("NVDA.US", "BTCUSDT"):
            return c
    return None


def test_parse_rules():
    r = parse_rule(["NVDA", ">", "140"], resolve)
    assert r.kind == "price_above" and r.value == 140 and r.freq == "once" and r.symbol == "NVDA.US"
    r = parse_rule(["NVDA<120.5", "always", "touch"], resolve)
    assert r.kind == "price_below" and r.value == 120.5 and r.freq == "always" and r.touch
    r = parse_rule(["btc", "5m", "-2%"], resolve)
    assert r.kind == "move" and r.window == 5 and r.value == -0.02 and not r.both and r.freq == "daily"
    r = parse_rule(["BTC", "15m", "±1.5%"], resolve)
    assert r.both and abs(r.value - 0.015) < 1e-12
    r = parse_rule(["NVDA", "当日", "5%"], resolve)
    assert r.kind == "day_move" and r.both
    r = parse_rule(["NVDA", "5m", "$20M"], resolve)
    assert r.kind == "turnover" and r.value == 20e6
    assert isinstance(parse_rule(["AAPL", ">", "1"], resolve), str)
    assert isinstance(parse_rule(["NVDA", "7m", "2%"], resolve), str)
    assert r.describe() == "NVDA 5m 成交额 ≥ $20M（每天一次）"


def test_price_rule_fires_once_on_close():
    h = Harness()
    t = h.seed(S, T0, 100.0)
    h.engine.custom.set_rules([CustomRule(1, S, "price_above", 100.3)])
    h.bar(S, t, 100.2)
    h.bar(S, t + 60, 100.35)
    h.bar(S, t + 120, 100.2)
    h.bar(S, t + 180, 100.4)
    fired = [m for m in h.outs if m.key.startswith("c1:")]
    assert len(fired) == 1
    text = to_plain(fired[0].text)
    assert text.startswith("🔔 BTC 上穿 $100.30") and "自定义提醒 #1" in text
    assert not h.engine.custom.rules[1].enabled


def test_move_rule_rearms_and_respects_cooldown():
    h = Harness()
    t = h.seed(S, T0, 100.0)
    rule = CustomRule(2, S, "move", -0.003, window=1, freq="always", cooldown_min=0)
    h.engine.custom.set_rules([rule])
    h.bars(S, t, [99.6, 99.3, 99.3, 98.9])
    fired = [m for m in h.outs if m.key.startswith("c2:")]
    # 99.6（-0.4%）触发；99.3 仍在条件里但没重新上膛；持平后上膛；98.9 再触发
    assert len(fired) == 2 and rule.fired == 2


def test_day_move_uses_previous_day_close():
    ca = CustomAlerts()
    ca.set_rules([CustomRule(3, S, "day_move", 0.05, both=True, freq="daily")])
    day1 = utc_ts(2026, 11, 23, 23, 58)
    from market_monitor_v3.windows import SymbolHistory

    hist = SymbolHistory()
    out = []
    for i, c in enumerate([100.0, 100.0, 104.0, 105.5, 106.0]):
        bar = Bar(S, day1 + 60 * i, c, c, c, c, 1, 1e6, "CWD", "t")
        hist.add(bar)
        ca.on_bar(bar, hist, bar.close_ts + 1, lambda r, m: out.append(m))
    assert ca.day_ref(S) == 100.0
    assert len(out) == 1 and "当日涨 5.5%" in to_plain(out[0].text) and "昨收 $100.00" in to_plain(out[0].text)


class TypeHooks:
    def __init__(self):
        self.off = {}

    def types_text(self):
        return f"TYPES {sorted((k, tuple(v)) for k, v in self.off.items())}"

    def resolve_target(self, s):
        return {"NVDA": "NVDA.US", "存储": "group:存储"}.get(s)

    async def set_type(self, target, pattern, off):
        cur = set(self.off.get(target, ()))
        (cur.add if off else cur.discard)(pattern)
        self.off[target] = sorted(cur)


async def test_types_command():
    hk = TypeHooks()
    out = await run_command(hk, "types", ["off", "急拉急跌", "存储"], 1, "u", set())
    assert "已关闭分组 存储的「急拉急跌」" in out and hk.off == {"group:存储": ["spike"]}
    out = await run_command(hk, "types", ["off", "平台跳水"], 1, "u", set())
    assert hk.off["*"] == ["breakout"]
    out = await run_command(hk, "types", ["off", "乱写"], 1, "u", set())
    assert out.startswith("没有这个类型")
    out = await run_command(hk, "types", ["on", "spike", "存储"], 2, "u", {1})
    assert out.startswith("没有权限")


def test_custom_rules_persist_and_fire_state_written_back(tmp_path):
    from market_monitor_v3.store import Store

    store = Store(tmp_path / "c.db")
    store.open_sync()
    try:
        rid = store.custom_add(CustomRule(0, S, "price_above", 100.3))
        h = Harness()
        t = h.seed(S, T0, 100.0)
        h.engine.custom.set_rules(store.custom_rules())
        h.engine.on_bar(Bar(S, t, 100.4, 100.4, 100.4, 100.4, 1, 1e6, "", "t"), t + 61)
        store.apply_effects(h.sink.drain())
        (r,) = store.custom_rules()
        assert r.id == rid and not r.enabled and r.fired == 1
        assert store.custom_delete(rid) == 1 and store.custom_rules() == []
    finally:
        store.close_sync()
