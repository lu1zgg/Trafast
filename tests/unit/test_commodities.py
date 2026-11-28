"""v4.3 大宗商品：Binance TradFi 永续（XAU / XAG / BZ / CL）。"""
from __future__ import annotations

import subprocess

import pytest
from pydantic import ValidationError

from market_monitor_v3.bot import run_command
from market_monitor_v3.config import AppConfig, Params, load_config
from market_monitor_v3.feeds.binance_spot import BinanceFuturesFeed, BinanceSpotFeed, parse_kline_event
from market_monitor_v3.merge import MARKET_KEY
from market_monitor_v3.migrate import migrate_text
from market_monitor_v3.models import display_name, market_of, target_label
from market_monitor_v3.replay import key_market
from market_monitor_v3.sessions import commodity_instance, commodity_session, utc_market_session
from market_monitor_v3.web.app import normalize_symbol
from tests.helpers import Harness, et_ts, flat_baselines, make_cfg

XAU = "XAUUSDT"


# ---- 识别和时段 ----
def test_market_of_and_names():
    assert market_of("XAUUSDT") == "CMDTY" and market_of("CLUSDT") == "CMDTY"
    assert market_of("BTCUSDT") == "CRYPTO" and market_of("NVDA.US") == "US"
    assert display_name("BZUSDT") == "BZ"
    assert target_label("market:CMDTY") == "大宗商品" and target_label("*") == "全部"
    # 配置里登记的新品种也算大宗商品
    make_cfg(commodities={"symbols": ["XAUUSDT", "NATGASUSDT"]})
    assert market_of("NATGASUSDT") == "CMDTY"


@pytest.mark.parametrize(
    ("when", "sess"),
    [
        ((2026, 11, 28, 12, 0), "CMC"),  # 周六
        ((2026, 11, 29, 17, 59), "CMC"),  # 周日开盘前
        ((2026, 11, 29, 18, 0), "CMO"),  # 周日 18:00 开盘
        ((2026, 12, 2, 12, 0), "CMO"),  # 周三白天
        ((2026, 12, 2, 17, 30), "CMC"),  # 每日休市
        ((2026, 12, 2, 18, 0), "CMO"),
        ((2026, 12, 4, 16, 59), "CMO"),  # 周五收盘前
        ((2026, 12, 4, 17, 0), "CMC"),  # 周五收盘
    ],
)
def test_commodity_session(when, sess):
    ts = et_ts(*when)
    assert commodity_session(ts) == sess
    assert utc_market_session(XAU, ts) == sess


def test_commodity_instance_rolls_at_18_et():
    assert commodity_instance(et_ts(2026, 12, 2, 12, 0)) == "CMO:2026-12-02"
    assert commodity_instance(et_ts(2026, 12, 2, 19, 0)) == "CMO:2026-12-03"
    assert commodity_instance(et_ts(2026, 11, 29, 18, 30)) == "CMO:2026-11-30"


# ---- 配置 ----
def test_defaults_and_params():
    cfg = make_cfg()
    assert cfg.commodities.enabled and cfg.commodities.symbols == ["XAUUSDT", "XAGUSDT", "BZUSDT", "CLUSDT"]
    assert set(cfg.all_symbols()) >= {"XAUUSDT", "CLUSDT"}
    assert "CMDTY" in cfg.telegram.route.markets
    p = Params(cfg)
    assert p.sessions["CMO"].floor == pytest.approx((0.003, 0.006, 0.010))
    assert p.sessions["CMO"].z == 5
    assert "CMC" not in p.sessions  # 休市不检测
    assert p.floor("XAGUSDT", "CMO", 5) == pytest.approx(0.012)
    assert p.floor("CLUSDT", "CMO", 1) == pytest.approx(0.0054)
    assert p.floor(XAU, "CMC", 1) == float("inf")


def test_commodity_in_crypto_list_is_rejected():
    with pytest.raises(ValidationError, match="只能留一个"):
        make_cfg(crypto={"symbols": ["BTCUSDT", "XAUUSDT"]})
    with pytest.raises(ValidationError, match="大宗商品合约"):
        make_cfg(crypto={"symbols": ["BTCUSDT", "XAUUSDT"]}, commodities={"symbols": ["CLUSDT"]})
    with pytest.raises(ValidationError, match="BTC"):
        make_cfg(commodities={"symbols": ["BTC"]})


def test_only_commodities_enabled_is_valid():
    cfg = make_cfg(stocks={"enabled": False}, crypto={"enabled": False})
    assert cfg.all_symbols() == ["XAUUSDT", "XAGUSDT", "BZUSDT", "CLUSDT"]


# ---- 数据源 ----
def test_futures_feed_urls_and_session():
    cfg = make_cfg()
    f = BinanceFuturesFeed(cfg.commodities, lambda b: None, lambda n, t: None)
    assert f.url == "wss://fstream.binance.com/market/stream?streams=xauusdt@kline_1m/xagusdt@kline_1m/bzusdt@kline_1m/clusdt@kline_1m"
    assert f.klines_path == "/fapi/v1/klines" and f.name == "binance-futures"
    assert BinanceSpotFeed.klines_path == "/api/v3/klines"
    ts = et_ts(2026, 11, 28, 12, 0)  # 周六
    msg = {"stream": "xauusdt@kline_1m", "data": {"e": "kline", "s": "XAUUSDT", "k": {
        "t": ts * 1000, "s": "XAUUSDT", "o": "3650", "h": "3651", "l": "3649", "c": "3650.5", "v": "10", "q": "36500", "x": True}}}
    bar = parse_kline_event(msg, f.session_fn)
    assert bar is not None and bar.session == "CMC" and bar.c == 3650.5


# ---- 检测：开市推，休市不推 ----
def _harness():
    cfg = make_cfg(stocks={"enabled": False}, crypto={"enabled": False})
    bl = flat_baselines({s: ["CMO"] for s in cfg.commodities.symbols}, 0.0003)
    return Harness(cfg, bl)


def test_open_session_move_is_pushed():
    h = _harness()
    t = h.seed(XAU, et_ts(2026, 12, 2, 10, 0), 3650.0)
    h.bars(XAU, t, [3650 * 1.006, 3650 * 1.006, 3650 * 1.006, 3650 * 1.006])
    sends = h.sends()
    assert sends and MARKET_KEY["CMDTY"] in sends[0].key and key_market(sends[0].key) == "CMDTY"
    assert "XAU" in sends[0].text and "美东" in sends[0].text


def test_weekend_wick_is_not_detected():
    h = _harness()
    t = h.seed(XAU, et_ts(2026, 11, 28, 10, 0), 3650.0)  # 周六
    h.bars(XAU, t, [3650 * 1.02] * 4)
    assert h.sends() == []


def test_market_mute_blocks_commodities_only():
    h = _harness()
    h.engine.mutes["market:CMDTY"] = 2e9
    assert h.engine.is_muted(XAU, 1.8e9) and not h.engine.is_muted("BTCUSDT", 1.8e9)


# ---- Bot ----
class _Hooks:
    def __init__(self):
        self.muted = {}

    def resolve_symbol(self, s):
        return {"XAU": XAU}.get(s.upper())

    async def mute(self, target, until, by):
        self.muted[target] = until


async def test_bot_mute_market():
    hooks = _Hooks()
    txt = await run_command(hooks, "mute", ["大宗商品", "2h"], 1, "louis", set(), clock=lambda: 1000.0)
    assert hooks.muted == {"market:CMDTY": 1000.0 + 7200} and "已静音 大宗商品" in txt
    txt = await run_command(hooks, "mute", ["xau"], 1, "louis", set(), clock=lambda: 1000.0)
    assert XAU in hooks.muted and "已静音 XAU" in txt


# ---- 网页 ----
def test_normalize_symbol_commodity():
    assert normalize_symbol("CMDTY", "xau") == "XAUUSDT"
    assert normalize_symbol("CMDTY", "CLUSDT") == "CLUSDT"


# ---- 升级迁移 ----
def test_migrate_old_example_config(tmp_path):
    old = subprocess.run(
        ["git", "show", "8556860:deploy/config.example.toml"], capture_output=True, text=True, check=False
    ).stdout
    if not old:
        pytest.skip("没有 git 历史")
    new, notes = migrate_text(old)
    assert len(notes) == 3  # 两个渠道 + [commodities]
    p = tmp_path / "config.toml"
    p.write_text(new)
    cfg: AppConfig = load_config(p)
    assert cfg.commodities.enabled and cfg.telegram.route.markets == ["US", "CRYPTO", "CMDTY"]
    assert cfg.commodities.multipliers["XAGUSDT"] == 2.0
    assert "# 异动监控 V3 配置" in new  # 注释保留
    again, notes2 = migrate_text(new)
    assert again == new and notes2 == []


def test_migrate_respects_markets_without_crypto():
    text = '[telegram]\nchat_id = -1\n[telegram.route]\nmarkets = ["US"]\n'
    new, _ = migrate_text(text)
    assert 'markets = ["US"]\n' in new and "[commodities]" in new
