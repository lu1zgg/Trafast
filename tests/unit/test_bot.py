import pytest

from market_monitor_v3.bot import BotPoller, parse_duration
from market_monitor_v3.outbox import ChatRef
from market_monitor_v3.store import Store
from tests.fakes import FakeTelegram

CHAT = -100123


class Hooks:
    def __init__(self):
        self.muted = {}

    async def status_text(self):
        return "STATUS"

    def params_text(self):
        return "PARAMS"

    def mutes_text(self):
        return "MUTES"

    def resolve_symbol(self, s):
        s = s.upper()
        for c in (s, s + ".US", s + "USDT"):
            if c in ("MU.US", "BTCUSDT"):
                return c
        return None

    async def mute(self, target, until, by):
        self.muted[target] = until

    async def unmute(self, target):
        n = len(self.muted) if target is None else int(target in self.muted)
        if target is None:
            self.muted.clear()
        else:
            self.muted.pop(target, None)
        return n

    async def report_text(self):
        return "REPORT"


@pytest.fixture
async def bot(tmp_path):
    store = Store(tmp_path / "b.db")
    await store.open()
    fake = FakeTelegram()
    tg = fake.client()
    hooks = Hooks()
    b = BotPoller(store, tg, ChatRef(CHAT), hooks, admin_user_ids=[42], notify_sender=lambda: None, clock=lambda: 1000.0)
    yield b, store, fake, hooks
    await tg.aclose()
    await store.close()


def upd(uid, text, chat=CHAT, user=42):
    return {"update_id": uid, "message": {"message_id": 500 + uid, "chat": {"id": chat}, "from": {"id": user, "username": "louis"}, "text": text}}


async def outbox(store):
    return await store.call(lambda s: [dict(r) for r in s.c.execute("SELECT * FROM outbox ORDER BY id")])


async def test_commands_reply_in_thread(bot):
    b, store, fake, hooks = bot
    await b.handle_update(upd(1, "/status@MyMonitorBot"))
    rows = await outbox(store)
    assert rows[0]["text"] == "STATUS" and rows[0]["kind"] == "cmd" and rows[0]["reply_to_tg"] == 501


async def test_other_chat_ignored(bot):
    b, store, fake, hooks = bot
    await b.handle_update(upd(1, "/status", chat=-999))
    await b.handle_update(upd(2, "hello"))
    assert await outbox(store) == []


async def test_mute_unmute_and_admin_check(bot):
    b, store, fake, hooks = bot
    await b.handle_update(upd(1, "/mute mu 2h"))
    assert hooks.muted["MU.US"] == 1000.0 + 7200
    await b.handle_update(upd(2, "/mute MU 2h", user=7))
    await b.handle_update(upd(3, "/mute XYZ"))
    await b.handle_update(upd(4, "/unmute all"))
    texts = [r["text"] for r in await outbox(store)]
    assert "已静音 MU，" in texts[0]
    assert "没有权限" in texts[1]
    assert "没找到标的" in texts[2]
    assert "已取消全部静音" in texts[3] and hooks.muted == {}


async def test_feedback_callback(bot):
    b, store, fake, hooks = bot
    await b.handle_update({"update_id": 9, "callback_query": {"id": "cq1", "data": "fb|g123CuBTC|1", "from": {"id": 42}, "message": {"chat": {"id": CHAT}}}})
    rows = await store.call(lambda s: [dict(r) for r in s.c.execute("SELECT * FROM feedback")])
    assert rows[0]["root_key"] == "g123CuBTC" and rows[0]["value"] == 1
    assert fake.calls[-1][0] == "answerCallbackQuery" and "有用" in fake.calls[-1][1]["text"]


async def test_poll_loop_saves_offset(bot):
    import asyncio

    b, store, fake, hooks = bot
    stop = asyncio.Event()

    def updates(method, body):
        from tests.fakes import ok

        stop.set()
        return ok([upd(77, "/help")])

    fake.script = [updates]
    await b.run(stop)
    assert await store.call(Store.kv_get, "tg_update_offset") == "78"
    assert "可用命令" in (await outbox(store))[0]["text"]


def test_parse_duration():
    assert parse_duration(None) == 3600
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200
    assert parse_duration("30d") == 7 * 86400
    assert parse_duration("abc") is None
