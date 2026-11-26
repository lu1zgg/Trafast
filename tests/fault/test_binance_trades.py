"""故障测试：逐笔连接断线重连、坏消息不打断处理。"""
import asyncio
import json

from websockets.asyncio.server import serve

from market_monitor_v3.config import CryptoCfg
from market_monitor_v3.feeds.binance_trades import BinanceTradeFeed


def tmsg(i, price=100.0, maker=False):
    return json.dumps(
        {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "p": str(price), "q": "0.5", "T": 1795491000000 + i, "m": maker}}
    )


async def test_trades_reconnect_and_bad_messages():
    got = []
    connections = []

    async def handler(ws):
        connections.append(ws)
        if len(connections) == 1:
            await ws.send(tmsg(1))
            await ws.send("not json")
            await ws.send(tmsg(2, maker=True))
            await asyncio.sleep(0.1)
            await ws.close()
        else:
            await ws.send(tmsg(3))
            await asyncio.sleep(1)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = CryptoCfg(symbols=["BTCUSDT"], ws_url=f"ws://127.0.0.1:{port}")
        feed = BinanceTradeFeed(cfg, got.append, stale_s=0.5)
        assert feed.url.endswith("/stream?streams=btcusdt@aggTrade")
        stop = asyncio.Event()
        task = asyncio.create_task(feed.run(stop))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(got) >= 3:
                break
        stop.set()
        await asyncio.wait_for(task, 5)
    assert [t.side for t in got] == [1, -1, 1]
    assert feed.reconnects >= 1 and feed.errors == 1 and feed.messages == 3
