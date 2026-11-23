import time

import httpx
import pytest

from market_monitor_v3 import outbox as outbox_mod
from market_monitor_v3.models import OutMsg
from market_monitor_v3.outbox import ChatRef, OutboxSender
from market_monitor_v3.store import Store
from market_monitor_v3.telegram import classify
from tests.fakes import FakeTelegram, err, ok


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "t.db")
    await store.open()
    fake = FakeTelegram()
    tg = fake.client()
    sender = OutboxSender(store, tg, ChatRef(-100), probe_interval_s=0.01)
    yield store, fake, sender
    await tg.aclose()
    await store.close()


def msg(key, kind="send", parent=None, level="L2", text="<b>MU</b> -2.4%", ttl=600, **kw):
    now = time.time()
    return OutMsg(key=key, kind=kind, text=text, created_at=now, expires_at=now + ttl, parent_key=parent, level=level, **kw)


async def step(store, sender, now=None):
    row = await store.call(Store.outbox_next, now or time.time())
    if row is None:
        return None
    await sender.handle(row)
    return row


async def status(store, key):
    def q(s, k):
        r = s.c.execute("SELECT * FROM outbox WHERE key=?", (k,)).fetchone()
        return dict(r) if r else None

    return await store.call(q, key)


async def test_reply_waits_for_root_then_threads_under_it(env):
    store, fake, sender = env
    await store.call(Store.enqueue, msg("g1"))
    await store.call(Store.enqueue, msg("r1", kind="reply", parent="g1", text="↳ 延伸"))
    await store.call(Store.outbox_mark, (await status(store, "g1"))["id"], "pending", next_at=time.time() + 100)
    assert await step(store, sender) is None  # 原消息在退避中，回复也要等
    await store.call(Store.outbox_mark, (await status(store, "g1"))["id"], "pending", next_at=0)
    await step(store, sender)
    await step(store, sender)
    root, reply = fake.sent()
    assert root["parse_mode"] == "HTML" and "reply_parameters" not in root
    root_id = (await status(store, "g1"))["tg_message_id"]
    assert reply["reply_parameters"] == {"message_id": root_id, "allow_sending_without_reply": True}


async def test_dedup_key_sends_once(env):
    store, fake, sender = env
    await store.call(Store.enqueue, msg("g1"))
    await store.call(Store.enqueue, msg("g1"))
    await step(store, sender)
    assert await step(store, sender) is None
    assert len(fake.sent()) == 1


async def test_parse_error_falls_back_to_plain_and_400_is_isolated(env):
    store, fake, sender = env
    fake.script = [err(400, "Bad Request: can't parse entities: unsupported start tag"), None, err(400, "Bad Request: message is too long")]
    fake.script[1] = lambda m, b: ok({"message_id": 7})
    await store.call(Store.enqueue, msg("a"))
    await store.call(Store.enqueue, msg("b"))
    await store.call(Store.enqueue, msg("c"))
    for _ in range(4):
        await step(store, sender)
    calls = fake.sent()
    assert "parse_mode" not in calls[1] and calls[1]["text"] == "MU -2.4%"
    assert (await status(store, "a"))["status"] == "sent"
    assert (await status(store, "b"))["status"] == "failed"
    assert (await status(store, "c"))["status"] == "sent"  # 一条 400 不影响后面的消息


async def test_429_waits_retry_after(env, monkeypatch):
    store, fake, sender = env
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(outbox_mod.asyncio, "sleep", fake_sleep)
    fake.script = [err(429, "Too Many Requests: retry after 3", retry_after=3)]
    await store.call(Store.enqueue, msg("a"))
    await step(store, sender)
    row = await status(store, "a")
    assert row["status"] == "pending" and row["next_at"] >= time.time() + 2 and slept == [3.0]


async def test_auth_error_pauses_then_probe_resumes(env):
    store, fake, sender = env
    fake.script = [err(403, "Forbidden: bot was kicked from the supergroup chat")]
    await store.call(Store.enqueue, msg("a"))
    await step(store, sender)
    assert sender.paused and (await status(store, "a"))["status"] == "pending"
    import asyncio

    await sender._probe(asyncio.Event())
    assert not sender.paused
    assert fake.calls[-1][0] == "getChat"
    assert await status(store, "a") is not None


async def test_timeout_resends_l2_with_note_but_not_l1(env):
    store, fake, sender = env
    fake.script = [httpx.ReadTimeout("read timed out"), None, httpx.ReadTimeout("read timed out")]
    fake.script[1] = lambda m, b: ok({"message_id": 9})
    await store.call(Store.enqueue, msg("l2", level="L2"))
    await store.call(Store.enqueue, msg("l1", level="L1"))
    await step(store, sender)  # l2 超时 → 标注后重发
    await step(store, sender)  # l2 重发成功（l2 的 id 更小，先取到）
    await step(store, sender)  # l1 超时 → 不重发
    sent = fake.sent()
    assert "可能重复" in sent[1]["text"]
    assert (await status(store, "l2"))["status"] == "sent"
    assert (await status(store, "l1"))["status"] == "unknown"


async def test_network_error_backoff(env):
    store, fake, sender = env
    fake.script = [httpx.ConnectError("boom"), err(502, "Bad Gateway")]
    await store.call(Store.enqueue, msg("a"))
    await step(store, sender)
    r1 = await status(store, "a")
    assert r1["status"] == "pending" and r1["attempts"] == 1 and r1["next_at"] > time.time() + 0.5
    await step(store, sender, now=r1["next_at"] + 0.01)
    r2 = await status(store, "a")
    assert r2["attempts"] == 2 and r2["next_at"] > time.time() + 1.5


async def test_migrated_group_switches_chat(env):
    store, fake, sender = env
    fake.script = [err(400, "Bad Request: group chat was upgraded to a supergroup chat", migrate_to_chat_id=-100999)]
    await store.call(Store.enqueue, msg("a"))
    await step(store, sender)
    await step(store, sender)
    assert sender.chat.chat_id == -100999
    assert fake.sent()[-1]["chat_id"] == -100999
    assert await store.call(Store.kv_get, "chat_id_override") == "-100999"


async def test_edit_superseded_dropped_and_expired(env):
    store, fake, sender = env
    await store.call(Store.enqueue, msg("g1"))
    await step(store, sender)
    await store.call(Store.enqueue, msg("g1:e1", kind="edit", parent="g1"))
    await store.call(Store.enqueue, msg("g1:e2", kind="edit", parent="g1"))
    await store.call(Store.enqueue, msg("gX:e1", kind="edit", parent="gX"))  # 原消息不存在
    await store.call(Store.enqueue, msg("old", ttl=-1))
    for _ in range(4):
        await step(store, sender)
    assert (await status(store, "g1:e1"))["status"] == "superseded"
    assert (await status(store, "g1:e2"))["status"] == "sent"
    assert (await status(store, "gX:e1"))["status"] == "dropped"
    assert (await status(store, "old"))["status"] == "expired"
    edit = fake.sent("editMessageText")[0]
    assert edit["message_id"] == (await status(store, "g1"))["tg_message_id"]


async def test_reply_with_failed_parent_goes_standalone(env):
    store, fake, sender = env
    await store.call(Store.enqueue, msg("g1"))
    await store.call(Store.outbox_mark, (await status(store, "g1"))["id"], "failed")
    await store.call(Store.enqueue, msg("r1", kind="reply", parent="g1"))
    await step(store, sender)
    assert "reply_parameters" not in fake.sent()[0]


def test_classify():
    assert classify(200, {"ok": True, "result": {"message_id": 1}}).ok
    assert classify(400, {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}).ok
    assert classify(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}).kind == "auth"
    assert classify(401, {"ok": False, "error_code": 401, "description": "Unauthorized"}).kind == "auth"
    assert classify(502, None).kind == "server"
    assert classify(400, {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}).kind == "edit_missing"


async def test_token_never_in_error_text(env):
    store, fake, sender = env
    from tests.fakes import TOKEN

    fake.script = [httpx.ConnectError(f"cannot connect to https://api.telegram.test/bot{TOKEN}/sendMessage")]
    res = await sender.tg.call("sendMessage", {})
    assert TOKEN not in res.description and "***" in res.description
