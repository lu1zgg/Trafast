"""V4 P3：API 密钥、/api/v1、异动统计、关键位突破、指标型自定义提醒。"""
import math

import pytest

from market_monitor_v3.apikeys import ApiKeyStore, FailGuard, hash_key
from market_monitor_v3.custom import CustomAlerts, CustomRule, ema_series, parse_rule, rsi_last2
from market_monitor_v3.models import Bar
from market_monitor_v3.render import to_plain
from market_monitor_v3.stats import build_stats, episodes_from, stats_lines
from market_monitor_v3.trend.breakout import BreakoutWatch, break_text, crossing
from market_monitor_v3.trend.levels import Levels, Zone
from market_monitor_v3.windows import SymbolHistory
from tests.helpers import Harness, utc_ts

T0 = utc_ts(2026, 11, 24, 3, 0)
S = "BTCUSDT"


# ---- API 密钥 ----
def test_api_keys_create_verify_revoke(tmp_path):
    ks = ApiKeyStore(tmp_path / "k.json")
    rec, key = ks.create("我的Mac", "write", now=100)
    assert key.startswith("tfk_") and len(key) > 40
    raw = (tmp_path / "k.json").read_text()
    assert key not in raw and hash_key(key) in raw  # 只存哈希
    assert (tmp_path / "k.json").stat().st_mode & 0o777 == 0o600
    other = ApiKeyStore(tmp_path / "k.json")  # 另一个进程（网页后台）读同一个文件
    got = other.verify(key, now=200)
    assert got is not None and got.id == rec.id and got.scope == "write" and got.last_used_at == 200
    assert other.verify(key[:-1] + ("A" if key[-1] != "A" else "B")) is None
    assert other.verify("tfk_") is None and other.verify(None) is None
    assert ks.revoke(rec.id) and not ks.revoke(rec.id)
    assert other.verify(key) is None and not other.is_active(rec.id)
    with pytest.raises(ValueError):
        ks.create("x", "admin")


def test_fail_guard_locks_after_ten():
    g = FailGuard(max_fail=10, lock_s=900)
    for _ in range(9):
        g.fail("1.2.3.4", 0)
    assert not g.locked("1.2.3.4", 0)
    g.fail("1.2.3.4", 0)
    assert g.locked("1.2.3.4", 1) and not g.locked("5.6.7.8", 1)
    assert not g.locked("1.2.3.4", 901)


# ---- /api/v1 ----
@pytest.fixture
def api(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from market_monitor_v3.web.api import mount_api

    calls = []
    state = {"online": True, "signals": [{"seq": 1, "symbol": S, "kind": "open"}, {"seq": 2, "symbol": S, "kind": "end"}]}

    async def monitor(cmd, **args):
        calls.append((cmd, args))
        if not state["online"]:
            return None
        if cmd == "status":
            return {"ok": True, "rule_version": "v4.1-x"}
        if cmd == "api_signals":
            if args.get("latest"):
                return {"ok": True, "items": [], "next": 0}
            items = [x for x in state["signals"] if x["seq"] > args["after"]]
            return {"ok": True, "items": items, "next": items[-1]["seq"] if items else args["after"]}
        if cmd == "api_quotes":
            return {"ok": True, "items": [{"symbol": S, "price": 1.0}]}
        if cmd == "alert_add":
            return {"ok": True, "message": "已添加"}
        return {"ok": True}

    ks = ApiKeyStore(tmp_path / "k.json")
    _, wkey = ks.create("w", "write")
    _, rkey = ks.create("r", "read")
    app = FastAPI()
    mount_api(app, ks, monitor, FailGuard(max_fail=3, lock_s=60))
    return {"c": TestClient(app), "w": wkey, "r": rkey, "calls": calls, "state": state, "ks": ks}


def hdr(key):
    return {"Authorization": f"Bearer {key}"}


def test_api_auth_scopes_and_errors(api):
    c = api["c"]
    r = c.get("/api/v1/ping")
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == "*"
    r = c.get("/api/v1/ping", headers=hdr(api["r"]))
    assert r.status_code == 200 and r.json()["key"]["scope"] == "read" and r.json()["api"] == 1
    assert r.json()["monitor"] is True and r.json()["rule_version"] == "v4.1-x"
    assert c.post("/api/v1/alerts", json={"text": "NVDA > 1"}, headers=hdr(api["r"])).status_code == 403
    r = c.post("/api/v1/alerts", json={"text": "NVDA > 1"}, headers=hdr(api["w"]))
    assert r.status_code == 200 and ("alert_add", {"text": "NVDA > 1", "by": "api:w"}) in api["calls"]
    assert c.post("/api/v1/alerts", content="x", headers=hdr(api["w"])).status_code == 400
    assert c.get("/api/v1/bars", headers=hdr(api["r"])).status_code == 400
    assert c.options("/api/v1/signals").status_code == 204  # 预检不需要密钥
    api["state"]["online"] = False
    assert c.get("/api/v1/signals", headers=hdr(api["r"])).status_code == 503
    assert c.get("/api/v1/ping", headers=hdr(api["r"])).json()["monitor"] is False
    for _ in range(3):
        c.get("/api/v1/ping", headers=hdr("tfk_wrongwrongwrongwrong"))
    assert c.get("/api/v1/ping", headers=hdr(api["r"])).status_code == 429  # 同一 IP 被封


def test_api_stream_auth_signals_and_revoke(api):
    c = api["c"]
    with c.websocket_connect("/api/v1/stream") as ws:
        ws.send_json({"type": "auth", "key": api["r"], "after": 0})
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["scope"] == "read"
        got = [ws.receive_json() for _ in range(3)]
        assert [g["type"] for g in got] == ["signal", "signal", "quotes"]
        assert got[0]["seq"] == 1 and got[1]["data"]["kind"] == "end"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
    with c.websocket_connect("/api/v1/stream") as ws:
        ws.send_json({"type": "auth", "key": "tfk_bad_bad_bad_bad"})
        with pytest.raises(Exception):  # noqa: B017
            ws.receive_json()


# ---- 异动统计 ----
def test_stats_weights_and_upgrades():
    sigs = [
        {"kind": "open", "episode_id": "a", "symbol": "MU.US", "direction": 1, "level": "L1", "pattern": "spike", "ts": 1000},
        {"kind": "upgrade", "episode_id": "a", "symbol": "MU.US", "direction": 1, "level": "L3", "ts": 1100},
        {"kind": "open", "episode_id": "b", "symbol": "STX.US", "direction": -1, "level": "L2", "pattern": "trend", "ts": 3000},
        {"kind": "end", "episode_id": "b", "symbol": "STX.US", "direction": -1, "level": "L2", "ts": 3500},
        {"kind": "open", "episode_id": "c", "symbol": S, "direction": -1, "level": "L1", "pattern": "trend", "ts": 3100},
    ]
    assert len(episodes_from(sigs)) == 3
    groups = {"MU.US": ["存储"], "STX.US": ["存储"], S: ["加密"]}
    st = build_stats(sigs, 3200, lambda s: groups[s], recent_s=600)
    t = st["today"]
    assert t["total"]["up"] == 3 and t["total"]["down"] == 3 and t["total"]["bull"] == 0.5
    assert t["groups"]["存储"]["up"] == 3 and t["groups"]["存储"]["down"] == 2
    assert t["markets"]["CRYPTO"]["down_n"] == 1
    assert st["recent"]["total"]["up_n"] == 0 and st["recent"]["total"]["down_n"] == 2
    text = "\n".join(stats_lines(st))
    assert "最近 10 分钟" in text and "存储：涨 1（3）跌 1（2）｜多 60%" in text


# ---- 关键位突破 ----
def lv(price, res=(), sup=(), atr=1.0):
    def z(lo, hi, kinds):
        zz = Zone(lo, hi, 3.0, 2, set(kinds))
        zz.score = 4.0
        return zz

    return Levels(price, atr, [z(*r) for r in res], [z(*x) for x in sup])


def test_breakout_needs_two_closes_and_dedups():
    w = BreakoutWatch()
    before = lv(99, res=[(100, 101, {"4h"})])
    assert w.on_candle(S, before, 101.1, 900) == []  # 没越过 0.25 ATR
    assert w.on_candle(S, before, 101.3, 1800) == []  # 越过：待确认
    (ev,) = w.on_candle(S, lv(101.3, sup=[(100, 101, {"4h"})]), 101.2, 2700)
    assert ev.d == 1 and ev.t == 2700 and ev.t0 == 1800 and ev.lo == 100
    assert "BTC 突破压力区" in to_plain(break_text(ev))
    # 4 小时内同一区间不再推
    w.on_candle(S, before, 101.4, 3600)
    assert w.on_candle(S, lv(101.4), 101.5, 4500) == []
    # 第二根回到区间里：不推
    w2 = BreakoutWatch()
    w2.on_candle(S, lv(101, sup=[(99, 100, {"4h", "1h"})]), 98.6, 900)
    assert w2.on_candle(S, lv(98.6), 99.5, 1800) == []
    # 弱位（只有 1h 一种来源）不看
    w3 = BreakoutWatch()
    w3.on_candle(S, lv(99, res=[(100, 101, {"1h"})]), 102, 900)
    assert w3.on_candle(S, lv(102), 102, 1800) == []


def test_crossing_and_card_line():
    levels = lv(100, res=[(101, 101.5, {"4h"})], sup=[(98, 98.5, {"4h", "1h", "VP"})])
    assert crossing(levels, 100.5, 102, 1).lo == 101
    assert crossing(levels, 100.5, 101.2, 1) is None  # 还在区间里
    assert crossing(levels, 99, 97.9, -1).hi == 98.5
    h = Harness()
    h.engine.levels_of = lambda sym: lv(100, res=[(100.2, 100.4, {"4h"})])
    t = h.seed(S, T0, 100.0)
    h.bars(S, t, [100.7, 100.65])
    text = to_plain(h.sends()[0].text)
    assert "突破强压力区 $100.20–100.40 ★" in text
    assert "level" in h.signals[0].tags


# ---- 指标型自定义提醒 ----
def resolve(s):
    return {"BTC": S, "NVDA": "NVDA.US"}.get(s.upper())


def test_parse_indicator_rules():
    r = parse_rule(["BTC", "1h", "ema", "20", "50", "金叉"], resolve)
    assert (r.kind, r.ind, r.tf, r.p1, r.p2, r.side, r.freq) == ("indicator", "ema", 60, 20, 50, 1, "always")
    assert r.describe() == "BTC 1h EMA20/EMA50 金叉（持续）"
    r = parse_rule(["NVDA", "15m", "rsi>70"], resolve)
    assert (r.ind, r.value, r.side, r.p1) == ("rsi", 70, 1, 14)
    r = parse_rule(["BTC", "1h", "macd"], resolve)
    assert r.ind == "macd" and r.side == 0 and "金叉或死叉" in r.describe()
    r = parse_rule(["BTC", "15m", "布林", "下"], resolve)
    assert r.ind == "boll" and r.side == -1
    assert isinstance(parse_rule(["BTC", "7m", "ema"], resolve), str)
    assert isinstance(parse_rule(["BTC", "1h", "ema", "50", "20"], resolve), str)
    assert isinstance(parse_rule(["BTC", "1h", "rsi", "70"], resolve), str)


def test_indicator_math():
    xs = [float(i) for i in range(1, 11)]
    e = ema_series(xs, 3)
    assert e[0] == 1 and math.isclose(e[1], 1.5)
    up = [100 + i for i in range(30)]
    assert rsi_last2(up)[1] == 100.0


def test_ema_cross_fires_on_tf_close():
    ca = CustomAlerts()
    rule = CustomRule(9, S, "indicator", 0.0, ind="ema", tf=5, p1=3, p2=6, side=1, freq="always", cooldown_min=0)
    ca.set_rules([rule])
    hist = SymbolHistory()
    out = []
    t = T0
    prices = [100 - 0.1 * i for i in range(30)] + [97 + 0.5 * i for i in range(10)]  # 先跌后涨：金叉
    # 历史：只喂到下跌段结束
    bars = []
    for p in prices[:30]:
        for _ in range(5):
            bars.append(Bar(S, t, p, p, p, p, 1, 1e6, "CWD", "t"))
            t += 60
    ca.seed_series(S, bars)
    assert len(ca.series[(S, 5)].closes) == 30
    for p in prices[30:]:
        for m in range(5):
            bar = Bar(S, t, p, p, p, p, 1, 1e6, "CWD", "t")
            hist.add(bar)
            ca.on_bar(bar, hist, bar.close_ts + 1, lambda r, msg: out.append(msg))
            if m < 4:
                assert len(out) <= 1
            t += 60
    assert len(out) == 1
    text = to_plain(out[0].text)
    assert text.startswith("🔔 BTC 5m EMA3/EMA6 金叉") and "自定义提醒 #9" in text
    # 只在 5 分钟收盘那一分钟触发
    assert int(out[0].key.split(":")[1]) % 300 == 0


async def test_backend_commands(tmp_path):
    from market_monitor_v3.app import Runtime
    from market_monitor_v3.config import Secrets
    from market_monitor_v3.engine import Engine
    from market_monitor_v3.store import Store
    from tests.helpers import make_cfg

    cfg = make_cfg(stocks={"enabled": False}, general={"state_dir": str(tmp_path)})
    h = Harness(cfg)
    h.collect = lambda: None  # 效果留在 sink 里，下面写进库
    t = h.seed(S, T0, 100.0)
    h.bars(S, t, [100.7, 100.65])
    rt = Runtime(cfg, None, Secrets("x", None), clock=lambda: t + 300)
    await rt.store.open()
    try:
        rt.engine = Engine(cfg, rt.params, rt.cal, rt.baselines, rt.sink)
        await rt.store.call(Store.insert_bars, [Bar(S, T0 + 60 * i, 100, 100, 100, 100, 1, 1e6, "CWD", "t") for i in range(10)])
        await rt.store.call(Store.apply_effects, h.sink.drain())
        res = await rt.control_handle({"cmd": "api_signals", "after": 0, "limit": 10})
        assert res["ok"] and res["items"] and res["items"][0]["kind"] == "open"
        nxt = res["next"]
        assert (await rt.control_handle({"cmd": "api_signals", "after": nxt}))["items"] == []
        latest = await rt.control_handle({"cmd": "api_signals", "latest": True, "limit": 1})
        assert latest["next"] == nxt
        bars = await rt.control_handle({"cmd": "api_bars", "symbol": "BTC", "tf": "1m", "limit": 5})
        assert len(bars["items"]) == 5 and bars["symbol"] == S
        assert not (await rt.control_handle({"cmd": "api_bars", "symbol": "BTC", "tf": "4h"}))["ok"]
        st = await rt.control_handle({"cmd": "api_stats"})
        assert st["ok"] and "today" in st
        syms = await rt.control_handle({"cmd": "api_symbols"})
        assert {x["symbol"] for x in syms["items"]} == set(cfg.crypto.symbols)
        ep = res["items"][0]["episode_id"]
        fb = await rt.control_handle({"cmd": "api_feedback", "episode_id": ep, "value": 1, "user": "api:3"})
        assert fb["ok"] and fb["root_key"]
        lv_ = await rt.control_handle({"cmd": "api_levels", "symbol": "BTC"})
        assert lv_["ok"] and lv_["enabled"] is False
    finally:
        await rt.store.close()

