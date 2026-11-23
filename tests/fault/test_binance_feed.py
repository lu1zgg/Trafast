"""故障测试：本地 WebSocket 服务器模拟断线、重复、未收盘、丢推送，REST 用 MockTransport 补数。"""
import asyncio
import json
import time

import httpx
from websockets.asyncio.server import serve

from market_monitor_v3.config import CryptoCfg
from market_monitor_v3.feeds.binance_spot import BinanceSpotFeed, parse_kline_event


def kmsg(ts, close, closed=True, sym="BTCUSDT"):
    return json.dumps(
        {
            "stream": f"{sym.lower()}@kline_1m",
            "data": {
                "e": "kline",
                "s": sym,
                "k": {"t": ts * 1000, "T": ts * 1000 + 59999, "s": sym, "o": "1", "h": "1", "l": "1", "c": str(close), "v": "2", "q": "3", "x": closed},
            },
        }
    )


def rest_rows(bars, start_ms, end_ms):
    return [
        [ts * 1000, "1", "1", "1", str(c), "2", ts * 1000 + 59999, "3", 1, "0", "0", "0"]
        for ts, c in bars
        if start_ms <= ts * 1000 <= end_ms
    ]


def test_parse_kline_event_only_closed():
    assert parse_kline_event(json.loads(kmsg(1800000000, 5.0, closed=False))) is None
    b = parse_kline_event(json.loads(kmsg(1800000000, 5.0)))
    assert b.ts == 1800000000 and b.c == 5.0 and b.turnover == 3.0


async def test_disconnect_duplicates_gaps_and_backfill():
    now = int(time.time())
    m = now - now % 60 - 10 * 60  # 10 分钟前
    rest_bars = [(m + 60 * i, 100 + i) for i in range(10)]
    connections = []

    async def handler(ws):
        connections.append(ws)
        if len(connections) == 1:
            await ws.send(kmsg(m, 100))
            await ws.send(kmsg(m + 60, 101, closed=False))  # 未收盘的更新：忽略
            await ws.send(kmsg(m, 100))  # 重复
            await ws.send(kmsg(m + 120, 102))  # 跳过了 m+60：中途丢推送
            await asyncio.sleep(0.2)
            await ws.close()  # 断线
        else:
            await ws.send(kmsg(m + 180, 103))
            for _ in range(50):
                await asyncio.sleep(0.05)
                await ws.send(kmsg(m + 180, 103, closed=False))

    def rest(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        return httpx.Response(200, json=rest_rows(rest_bars, int(q["startTime"]), int(q["endTime"])))

    got = []
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = CryptoCfg(symbols=["BTCUSDT"], ws_url=f"ws://127.0.0.1:{port}", rest_url="http://rest.test")
        http = httpx.AsyncClient(transport=httpx.MockTransport(rest))
        feed = BinanceSpotFeed(cfg, got.append, lambda n, t: None, stale_s=5, http=http)
        stop = asyncio.Event()
        task = asyncio.create_task(feed.run(stop))
        for _ in range(100):
            await asyncio.sleep(0.1)
            if {b.ts for b in got} >= {ts for ts, _ in rest_bars}:
                break
        stop.set()
        await asyncio.wait_for(task, 5)
        await http.aclose()

    ts_list = [b.ts for b in got]
    assert len(connections) >= 2  # 断线后重连了
    assert len(ts_list) == len(set(ts_list))  # 没有重复
    assert set(ts_list) == {ts for ts, _ in rest_bars}  # 丢的 m+60 和断线期间的都补齐了
    # 除了中途补缺口那一根，其余按时间顺序到达
    late = [ts for i, ts in enumerate(ts_list) if i and ts < ts_list[i - 1]]
    assert late == [m + 60]


async def test_stale_connection_triggers_reconnect():
    connections = []

    async def handler(ws):
        connections.append(ws)
        await asyncio.sleep(10)  # 一直不发数据

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = CryptoCfg(symbols=["BTCUSDT"], ws_url=f"ws://127.0.0.1:{port}", rest_url="http://rest.test")
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        feed = BinanceSpotFeed(cfg, lambda b: None, lambda n, t: None, stale_s=0.3, http=http)
        stop = asyncio.Event()
        task = asyncio.create_task(feed.run(stop))
        await asyncio.sleep(2.5)
        stop.set()
        await asyncio.wait_for(task, 5)
        await http.aclose()
    assert len(connections) >= 2 and feed.reconnects >= 1
