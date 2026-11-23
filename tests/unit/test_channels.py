"""多渠道：发件箱分发、Discord 投递和交互、控制通道、TradingView。"""
import asyncio
import json
import time

import httpx
import pytest

from market_monitor_v3.control import ControlServer, ControlUnavailable, control_call
from market_monitor_v3.models import OutMsg
from market_monitor_v3.notifiers import Route
from market_monitor_v3.notifiers.discord import (
    COLOR_DOWN,
    DiscordClient,
    DiscordGateway,
    DiscordNotifier,
    classify,
    html_to_markdown,
)
from market_monitor_v3.outbox import ChatRef, OutboxSender
from market_monitor_v3.store import Store
from market_monitor_v3.tradingview import Throttle, card_text, parse_alert
from tests.fakes import FakeTelegram

DTOKEN = "MTIz.fake-discord-token.for-tests"


class FakeDiscord:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.script: list[httpx.Response] = []
        self.next_id = 9000

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == f"Bot {DTOKEN}"
        if self.script:
            return self.script.pop(0)
        if request.method == "POST" and request.url.path.endswith("/messages"):
            self.next_id += 1
            return httpx.Response(200, json={"id": str(self.next_id)})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})
        if request.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "1", "username": "trafast-bot"})
        if "/channels/" in request.url.path:
            return httpx.Response(200, json={"id": "555", "name": "alerts", "guild_id": "777"})
        return httpx.Response(204)

    def client(self) -> DiscordClient:
        return DiscordClient(DTOKEN, "https://discord.test/api/v10", transport=httpx.MockTransport(self.handler))

    def posts(self):
        return [(p, b) for m, p, b in self.calls if m == "POST" and p.endswith("/messages")]


def msg(key, kind="send", parent=None, level="L2", text="<b>MU</b> -2.4%", market="US", direction=-1, **kw):
    now = time.time()
    return OutMsg(
        key=key, kind=kind, text=text, created_at=now, expires_at=now + 600, parent_key=parent,
        level=level, market=market, direction=direction, **kw,
    )


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "t.db", routes={"telegram": Route(), "discord": Route(min_level="L2", markets=("US",))})
    await store.open()
    fd = FakeDiscord()
    ft = FakeTelegram()
    tg_sender = OutboxSender(store, ft.client(), ChatRef(-100))
    dc_sender = OutboxSender(store, notifier=DiscordNotifier(fd.client(), "555"))
    yield store, ft, fd, tg_sender, dc_sender
    await tg_sender.notifier.aclose()
    await dc_sender.notifier.aclose()
    await store.close()


async def rows(store):
    def q(s):
        return [dict(r) for r in s.c.execute("SELECT key, channel, kind, parent_key, status, ext_id FROM outbox ORDER BY id")]

    return await store.call(q)


async def drain(store, sender):
    while True:
        row = await store.call(Store.outbox_next, time.time(), sender.channel)
        if row is None:
            return
        await sender.handle(row)


def test_html_to_markdown_escapes_and_converts():
    md = html_to_markdown("🔴 <b>NVDA</b> <b>-1.9%</b>｜现价 <b>$134.03</b>\n# 不是标题 a*b_c &lt;x&gt;")
    assert md.startswith("🔴 **NVDA** **-1.9%**｜现价 **$134.03**")
    assert "\\# 不是标题" in md and "a\\*b\\_c <x>" in md
    assert html_to_markdown('<a href="https://x.test/a">图表</a>') == "[图表](https://x.test/a)"


def test_route_filters():
    r = Route(min_level="L2", markets=("US",), ops=False, tradingview=False)
    assert not r.accepts("send", "g1", "L1", "US")
    assert r.accepts("send", "g1", "L3", "US")
    assert not r.accepts("send", "g1", "L3", "CRYPTO")
    assert not r.accepts("ops", "ops:x", None, None) and r.accepts("ops", "report:2026-11-23", None, None)
    assert not r.accepts("send", "tv:1", "L2", "TV")


async def test_fanout_follows_each_channels_subscription(env):
    store, ft, fd, tg, dc = env
    await store.call(Store.enqueue, msg("g1", level="L3"))
    await store.call(Store.enqueue, msg("g2", level="L1"))  # Discord 只收 L2 以上
    await store.call(Store.enqueue, msg("g3", level="L3", market="CRYPTO"))  # Discord 只收美股
    await store.call(Store.enqueue, msg("g1:r1", kind="reply", parent="g1", text="↳ 延伸"))
    await store.call(Store.enqueue, msg("g2:e1", kind="edit", parent="g2", text="已结束"))
    await store.call(Store.enqueue, msg("cmd:1", kind="cmd", channel="telegram", text="ok"))
    got = {(r["key"], r["channel"]) for r in await rows(store)}
    assert ("g1", "telegram") in got and ("g1@discord", "discord") in got
    assert ("g2@discord", "discord") not in got and ("g3@discord", "discord") not in got
    assert ("g1:r1@discord", "discord") in got and ("g2:e1@discord", "discord") not in got
    assert ("cmd:1@discord", "discord") not in got and ("cmd:1", "telegram") in got
    reply = next(r for r in await rows(store) if r["key"] == "g1:r1@discord")
    assert reply["parent_key"] == "g1@discord"


async def test_discord_send_reply_edit_with_colors_and_buttons(env):
    store, ft, fd, tg, dc = env
    buttons = [[{"text": "👍 有用", "callback_data": "fb|g1|1"}, {"text": "👎 没用", "callback_data": "fb|g1|0"}]]
    await store.call(Store.enqueue, msg("g1", level="L3", buttons=buttons))
    await store.call(Store.enqueue, msg("g1:r1", kind="reply", parent="g1", text="↳ 延伸", silent=True))
    await store.call(Store.enqueue, msg("g1:e1", kind="edit", parent="g1", text="<b>NVDA</b> 已结束", buttons=buttons))
    await drain(store, dc)
    (p1, root), (p2, reply) = fd.posts()
    assert p1 == "/api/v10/channels/555/messages"
    assert root["embeds"][0]["color"] == COLOR_DOWN and root["embeds"][0]["description"] == "**MU** -2.4%"
    assert root["components"][0]["components"][0]["custom_id"] == "fb|g1|1"
    assert root["allowed_mentions"] == {"parse": []}
    assert reply["message_reference"]["message_id"] == "9001" and reply["flags"] == 1 << 12
    patch = [(p, b) for m, p, b in fd.calls if m == "PATCH"]
    assert patch and patch[0][0].endswith("/messages/9001") and "已结束" in patch[0][1]["embeds"][0]["description"]
    assert all(r["status"] == "sent" for r in await rows(store) if r["channel"] == "discord")
    # Telegram 那边互不影响，照常能发
    await drain(store, tg)
    assert len(ft.sent()) == 2


async def test_discord_errors_are_classified():
    assert classify(429, {"retry_after": 1.5}).kind == "rate"
    assert classify(403, {"code": 50013, "message": "Missing Permissions"}).kind == "auth"
    assert classify(404, {"code": 10003, "message": "Unknown Channel"}).kind == "auth"
    assert classify(404, {"code": 10008, "message": "Unknown Message"}, kind="edit").kind == "edit_missing"
    assert classify(502, None).kind == "server"
    assert classify(400, {"code": 50035, "message": "Invalid Form Body"}).kind == "bad_request"
    assert classify(200, {"id": "42"}).ext_id == "42"


async def test_discord_auth_failure_pauses_only_that_channel(env):
    store, ft, fd, tg, dc = env
    fd.script.append(httpx.Response(403, json={"code": 50001, "message": "Missing Access"}))
    await store.call(Store.enqueue, msg("g1", level="L3"))
    row = await store.call(Store.outbox_next, time.time(), "discord")
    await dc.handle(row)
    assert dc.paused and not tg.paused
    await drain(store, tg)
    assert len(ft.sent()) == 1


async def test_discord_check_reports_bot_and_channel():
    fd = FakeDiscord()
    n = DiscordNotifier(fd.client(), "555")
    ok, text = await n.check()
    assert ok and "trafast-bot" in text and "#alerts" in text
    await n.aclose()


async def test_gateway_button_and_slash_command():
    fd = FakeDiscord()
    got = {}

    async def on_cmd(cmd, args, uid, who):
        got["cmd"] = (cmd, args, uid)
        return "<b>已静音</b> MU"

    async def on_fb(key, uid, value):
        got["fb"] = (key, uid, value)

    async def ops(key, text):
        got["ops"] = text

    gw = DiscordGateway(DTOKEN, fd.client(), "555", on_cmd, on_fb, ops, ws_connect=lambda *a, **k: None)
    gw.application_id = "999"
    await gw.on_interaction(
        {"type": 3, "id": "1", "token": "t1", "channel_id": "555", "member": {"user": {"id": "42", "username": "a"}},
         "data": {"custom_id": "fb|g1|0"}}
    )
    assert got["fb"] == ("g1", 42, 0)
    cb = [b for m, p, b in fd.calls if p.endswith("/interactions/1/t1/callback")]
    assert cb[0]["type"] == 4 and cb[0]["data"]["flags"] == 64
    await gw.on_interaction(
        {"type": 2, "id": "2", "token": "t2", "channel_id": "555", "user": {"id": "42", "username": "a"},
         "data": {"name": "mute", "options": [{"name": "symbol", "value": "MU"}, {"name": "duration", "value": "2h"}]}}
    )
    assert got["cmd"] == ("mute", ["MU", "2h"], 42)
    edit = [b for m, p, b in fd.calls if m == "PATCH" and p.endswith("/webhooks/999/t2/messages/@original")]
    assert edit and edit[0]["content"] == "**已静音** MU"
    # 别的频道的按钮不认
    await gw.on_interaction({"type": 3, "id": "3", "token": "t3", "channel_id": "1", "user": {"id": "5"}, "data": {"custom_id": "fb|g1|1"}})
    assert got["fb"] == ("g1", 42, 0)
    await gw.client.aclose()


async def test_gateway_heartbeat_and_reconnect_frames():
    sent = []

    class WS:
        async def send(self, s):
            sent.append(json.loads(s))

    async def nop(*a):
        return None

    gw = DiscordGateway(DTOKEN, FakeDiscord().client(), "555", nop, nop, nop, ws_connect=lambda *a, **k: None)
    assert await gw.handle_frame(WS(), {"op": 0, "s": 7, "t": "GUILD_CREATE", "d": {}}) is None and gw.seq == 7
    await gw.handle_frame(WS(), {"op": 1})
    assert sent[-1] == {"op": 1, "d": 7}
    assert await gw.handle_frame(WS(), {"op": 7}) == "reconnect"
    assert gw.identify_payload()["d"]["intents"] == 1
    await gw.client.aclose()


async def test_control_socket_roundtrip(tmp_path):
    async def handler(req):
        return {"ok": True, "echo": req["cmd"], "x": req.get("x")}

    path = tmp_path / "c.sock"
    srv = ControlServer(path, handler)
    assert await srv.start()
    assert oct(path.stat().st_mode & 0o777) == "0o660"
    assert await control_call(path, "ping", x=3) == {"ok": True, "echo": "ping", "x": 3}
    await srv.close()
    with pytest.raises(ControlUnavailable):
        await control_call(path, "ping")


def test_tradingview_parse_and_card():
    a = parse_alert(b'{"ticker":"NVDA","exchange":"NASDAQ","price":134.03,"interval":"5","action":"sell","message":"<\xe8\xb7\x8c\xe7\xa0\xb4>"}')
    text, d = card_text(a, 1795400000)
    assert d == -1 and text.startswith("🔴 <b>NVDA</b> · NASDAQ｜TradingView 告警")
    assert "价格 <b>$134.03</b>｜周期 5 分钟｜信号 <b>sell</b>" in text and "&lt;跌破&gt;" in text
    plain = parse_alert(b"RSI over 70 on BTCUSDT")
    t2, d2 = card_text(plain, 1795400000)
    assert d2 == 0 and t2.startswith("🔔 <b>TradingView</b>") and "RSI over 70" in t2


def test_tradingview_throttle():
    th = Throttle(max_per_min=2, dedup_s=60)
    a = {"ticker": "A"}
    assert th.allow(a, 0)[0] and not th.allow(a, 1)[0]
    assert th.allow({"ticker": "B"}, 2)[0] and not th.allow({"ticker": "C"}, 3)[0]
    assert th.allow({"ticker": "C"}, 61)[0]


async def test_concurrent_control_calls(tmp_path):
    async def handler(req):
        await asyncio.sleep(0.01)
        return {"ok": True, "n": req["n"]}

    srv = ControlServer(tmp_path / "c.sock", handler)
    await srv.start()
    res = await asyncio.gather(*(control_call(tmp_path / "c.sock", "x", n=i) for i in range(10)))
    assert [r["n"] for r in res] == list(range(10))
    await srv.close()
