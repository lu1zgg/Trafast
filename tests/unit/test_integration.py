"""引擎 → 写库 → 发件箱 → Telegram 的完整链路，以及重启恢复。"""
import time

import pytest

from market_monitor_v3.models import Bar
from market_monitor_v3.outbox import ChatRef, OutboxSender
from market_monitor_v3.store import Store
from tests.fakes import FakeTelegram
from tests.helpers import Harness, utc_ts

S = "BTCUSDT"


async def pump(h: Harness, store: Store, sender: OutboxSender):
    eff = h.sink.drain()
    await store.call(Store.apply_effects, eff)
    now = max([time.time()] + [m.created_at for m in eff.outs])
    while True:
        row = await store.call(Store.outbox_next, now)
        if row is None:
            break
        await sender.handle(row)


@pytest.fixture
async def chain(tmp_path):
    store = Store(tmp_path / "i.db")
    await store.open()
    fake = FakeTelegram()
    tg = fake.client()
    sender = OutboxSender(store, tg, ChatRef(-100))
    yield store, fake, sender
    await tg.aclose()
    await store.close()


def feed(h, ts, c):
    now = ts + 61
    h.engine.on_bar(Bar(S, ts, c, c, c, c, 1, 1e6, "", "t"), now)
    for dt in (3, 10, 30):
        h.engine.on_tick(now + dt)


async def test_alert_reply_and_end_edit_thread_correctly(chain, tmp_path):
    store, fake, sender = chain
    h = Harness()
    t0 = utc_ts(2026, 11, 24, 3, 0)
    t = h.seed(S, t0, 100.0)
    feed(h, t, 101.2)  # L3
    await pump(h, store, sender)
    root = fake.sent()[0]
    root_id = 101
    assert "BTC" in root["text"] and root["reply_markup"]["inline_keyboard"][0][0]["text"] == "👍 有用"
    p = 101.2
    for i in range(1, 12):
        p *= 1.0015
        feed(h, t + 60 * i, p)
    await pump(h, store, sender)
    replies = [b for b in fake.sent() if "reply_parameters" in b]
    assert replies and all(r["reply_parameters"]["message_id"] == root_id for r in replies)
    for i in range(12, 16):
        feed(h, t + 60 * i, 100.6)  # 回撤过半 → 结束
    await pump(h, store, sender)
    edit = fake.sent("editMessageText")[-1]
    assert edit["message_id"] == root_id and "已结束" in edit["text"] and "reply_markup" in edit

    # 告警全部落库
    n = await store.call(lambda s: s.c.execute("SELECT COUNT(*) FROM alerts WHERE outcome='pushed'").fetchone()[0])
    assert n == 1


async def test_restart_restores_active_episode(chain):
    store, fake, sender = chain
    h = Harness()
    t0 = utc_ts(2026, 11, 24, 3, 0)
    t = h.seed(S, t0, 100.0)
    feed(h, t, 101.2)
    await pump(h, store, sender)
    for i in range(1, 30):
        await store.call(Store.insert_bars, [Bar(S, t0 + 60 * i, 100, 100, 100, 100, 1, 1e6, "CWD", "t")])
    await store.call(Store.insert_bars, [Bar(S, t, 101.2, 101.2, 101.2, 101.2, 1, 1e6, "CWD", "t")])

    # “重启”：新引擎从库里恢复
    h2 = Harness()
    bars = await store.call(Store.load_recent_bars, S, t - 40 * 60)
    h2.engine.seed_history(bars)
    eps = await store.call(Store.active_episodes)
    roots = await store.call(Store.load_roots, [e.root_key for e in eps])
    h2.engine.restore(eps, roots)
    assert len(eps) == 1 and roots
    p = 101.2
    for i in range(1, 12):
        p *= 1.0015
        feed(h2, t + 60 * i, p)
    await pump(h2, store, sender)
    replies = [b for b in fake.sent() if "reply_parameters" in b]
    assert replies and replies[0]["reply_parameters"]["message_id"] == 101
