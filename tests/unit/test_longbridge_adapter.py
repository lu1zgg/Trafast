"""长桥适配器：不连网，只测转换逻辑（真实 SDK 已安装时也核对枚举）。"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from market_monitor_v3.feeds.longbridge import LongbridgeFeed, TradeBarBuilder, map_session
from market_monitor_v3.sessions import UsCalendar
from tests.helpers import et_ts, make_cfg


class FakeTS:
    class Intraday: ...
    class Pre: ...
    class Post: ...
    class Overnight: ...


FAKE_SDK = SimpleNamespace(TradeSession=FakeTS)


def feed(**stock_over):
    cfg = make_cfg(stocks={"enabled": True, **stock_over})
    out = []
    f = LongbridgeFeed(cfg.stocks, UsCalendar(overnight=cfg.stocks.overnight), out.append, lambda n, t: None, stale_s=lambda now: 60, sdk=FAKE_SDK)
    return f, out


def candle(ts, close, session):
    dt = datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)  # SDK 返回不带时区的 UTC
    return SimpleNamespace(timestamp=dt, open=close, high=close, low=close, close=close, volume=100, turnover=close * 100, trade_session=session)


def test_map_session():
    assert map_session(FAKE_SDK, FakeTS.Pre) == "PRE"
    assert map_session(FAKE_SDK, FakeTS.Overnight) == "ON"
    assert map_session(FAKE_SDK, "TradeSession.Post") == "POST"
    assert map_session(FAKE_SDK, None) is None


def test_to_bar_and_session():
    f, _ = feed()
    ts = et_ts(2026, 11, 24, 21, 15)
    b = f.to_bar("MU.US", candle(ts, 612.5, FakeTS.Overnight))
    assert b.ts == ts and b.session == "ON" and b.turnover == 61250 and b.source == "longbridge"


def test_ts_is_close_and_overnight_disabled():
    f, _ = feed(longbridge={"ts_is_close": True})
    ts = et_ts(2026, 11, 24, 10, 1)
    assert f.to_bar("MU.US", candle(ts, 1, FakeTS.Intraday)).ts == ts - 60
    f2, _ = feed(overnight=False)
    assert f2.to_bar("MU.US", candle(et_ts(2026, 11, 24, 21, 0), 1, FakeTS.Overnight)) is None


def test_confirmed_only_and_dedup():
    f, out = feed()
    ts = et_ts(2026, 11, 24, 10, 0)
    f._on_candle("MU.US", SimpleNamespace(is_confirmed=False, candlestick=candle(ts, 1, FakeTS.Intraday)))
    f._on_candle("MU.US", SimpleNamespace(is_confirmed=True, candlestick=candle(ts, 1, FakeTS.Intraday)))
    f._on_candle("MU.US", SimpleNamespace(is_confirmed=True, candlestick=candle(ts, 1, FakeTS.Intraday)))
    assert len(out) == 1


def test_trade_bar_builder():
    b = TradeBarBuilder()
    t = et_ts(2026, 11, 24, 21, 0)
    b.add("MU.US", t + 5, 600, 10, "ON")
    b.add("MU.US", t + 30, 605, 5, "ON")
    b.add("MU.US", t + 59, 603, 1, "ON")
    assert b.flush(t + 60, 5) == []
    bars = b.flush(t + 66, 5)
    assert len(bars) == 1
    x = bars[0]
    assert (x.o, x.h, x.l, x.c, x.volume) == (600, 605, 600, 603, 16)


def test_real_sdk_enums_if_installed():
    sdk = pytest.importorskip("longbridge.openapi")
    assert map_session(sdk, sdk.TradeSession.Pre) == "PRE"
    assert map_session(sdk, sdk.TradeSession.Intraday) == "REG"
    assert map_session(sdk, sdk.TradeSession.Overnight) == "ON"
    for name in ("AsyncQuoteContext", "PushCandlestickMode", "TradeSessions", "Period", "AdjustType", "SecurityListCategory"):
        assert hasattr(sdk, name)
    assert hasattr(sdk.TradeSessions, "All") and hasattr(sdk.PushCandlestickMode, "Confirmed")
