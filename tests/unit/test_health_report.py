import logging
import socket

import pytest

from market_monitor_v3.config import Params
from market_monitor_v3.health import Health, sd_notify
from market_monitor_v3.logsetup import RedactFilter
from market_monitor_v3.reporting import build_daily_report
from market_monitor_v3.sessions import UsCalendar
from market_monitor_v3.store import Store
from tests.helpers import Harness, et_ts, make_cfg, utc_ts


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "h.db")
    await s.open()
    yield s
    await s.close()


async def ops_texts(store):
    return await store.call(lambda s: [r["text"] for r in s.c.execute("SELECT text FROM outbox WHERE kind='ops' ORDER BY id")])


async def test_stale_feed_alert_and_recovery_pair(store):
    clock = Clock(et_ts(2026, 11, 24, 10, 0))  # 盘中
    h = Health(make_cfg(), UsCalendar(), store, lambda: None, clock)
    h.register("longbridge", "US")
    h.seen("longbridge", clock.t)
    clock.t += 30
    await h.check_once()
    assert await ops_texts(store) == []
    clock.t += 60 + 180  # 超过 60 秒阈值后再过 3 分钟
    await h.check_once()
    await h.check_once()
    texts = await ops_texts(store)
    assert len(texts) == 1 and "长桥行情中断" in texts[0]
    h.seen("longbridge", clock.t)
    await h.check_once()
    texts = await ops_texts(store)
    assert len(texts) == 2 and "已恢复" in texts[1]


async def test_no_alert_when_market_closed(store):
    clock = Clock(et_ts(2026, 11, 21, 12, 0))  # 周六
    h = Health(make_cfg(), UsCalendar(), store, lambda: None, clock)
    h.register("longbridge", "US")
    clock.t += 3600
    await h.check_once()
    assert await ops_texts(store) == []
    assert not h.is_alive("US", clock.t)


def test_sd_notify(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert sd_notify("WATCHDOG=1")
    assert srv.recv(64) == b"WATCHDOG=1"
    srv.close()


def test_log_redaction(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:SECRETSECRETSECRETSECRET")
    f = RedactFilter()
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "POST https://api.telegram.org/bot999999:SECRETSECRETSECRETSECRET/sendMessage %s", ("x",), None)
    f.filter(rec)
    assert "SECRET" not in rec.getMessage()
    rec2 = logging.LogRecord("x", logging.INFO, "f", 1, "url bot123456:ABCDEFGHIJKLMNOPQRSTUVWX", None, None)
    f.filter(rec2)
    assert "ABCDEFG" not in rec2.getMessage()


async def test_daily_report_contents(store):
    h = Harness()
    t0 = utc_ts(2026, 11, 24, 3, 0)
    t = h.seed("BTCUSDT", t0, 100.0)
    h.bars("BTCUSDT", t, [100.7, 100.05, 100.0])  # 插针，被挡
    h.bars("BTCUSDT", t + 180, [100.0, 101.2, 101.2])  # L3
    eff = h.sink.drain()
    eff.decisions = h.decisions
    eff.outs = h.outs
    from market_monitor_v3.models import Bar

    bars = [Bar("BTCUSDT", t0 + 60 * i, 100, 100, 100, 100, 1, 1e6, "CWD", "t") for i in range(30)]
    bars.append(Bar("BTCUSDT", t + 240, 101.2, 101.2, 101.2, 101.2, 1, 1e6, "CWD", "t"))
    eff.bars = bars
    await store.call(Store.apply_effects, eff)
    cfg = make_cfg(stocks={"enabled": False})
    text = await store.call(build_daily_report, cfg, Params(cfg), UsCalendar(), t0 - 3600, t + 3600)
    assert "日报" in text and "加密 1 个" in text and "没站稳（插针） 1" in text
    assert "BTC +1.2% ✅" in text
