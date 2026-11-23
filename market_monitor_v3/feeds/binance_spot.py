"""Binance 现货 1 分钟 K 线：组合 WebSocket 流 + REST 补数。

- 以 K 线事件里的 x=true（已收盘）为准。
- 30 秒没有任何推送就重连；连接满 23 小时主动重连（Binance 单连接最长 24 小时）。
- 每次（重）连接后先用 REST 补齐上次收到之后的 K 线，补完再放出期间缓存的实时 K 线，保证顺序。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..config import CryptoCfg
from ..models import Bar
from ..sessions import crypto_session

log = logging.getLogger(__name__)

MAX_BACKFILL_S = 2 * 86400


def parse_kline_event(msg: dict[str, Any]) -> Bar | None:
    data = msg.get("data", msg)
    if not isinstance(data, dict) or data.get("e") != "kline":
        return None
    k = data.get("k") or {}
    if not k.get("x"):
        return None
    ts = int(k["t"]) // 1000
    return Bar(
        symbol=str(k.get("s") or data.get("s")).upper(),
        ts=ts - ts % 60,
        o=float(k["o"]),
        h=float(k["h"]),
        l=float(k["l"]),
        c=float(k["c"]),
        volume=float(k.get("v", 0.0)),
        turnover=float(k.get("q", 0.0)),
        session=crypto_session(ts),
        source="binance",
    )


def parse_rest_row(symbol: str, row: list[Any]) -> Bar:
    ts = int(row[0]) // 1000
    return Bar(
        symbol=symbol,
        ts=ts - ts % 60,
        o=float(row[1]),
        h=float(row[2]),
        l=float(row[3]),
        c=float(row[4]),
        volume=float(row[5]),
        turnover=float(row[7]),
        session=crypto_session(ts),
        source="binance-rest",
    )


class BinanceSpotFeed:
    name = "binance"
    market = "CRYPTO"

    def __init__(
        self,
        cfg: CryptoCfg,
        emit: Callable[[Bar], None],
        seen: Callable[[str, float], None],
        stale_s: float = 30.0,
        clock: Callable[[], float] = time.time,
        http: httpx.AsyncClient | None = None,
        ws_connect: Callable[..., Any] | None = None,
    ):
        self.cfg = cfg
        self.emit = emit
        self.seen = seen
        self.stale_s = stale_s
        self.clock = clock
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        if ws_connect is None:
            from websockets.asyncio.client import connect as ws_connect  # noqa: N813
        self.ws_connect = ws_connect
        self.symbols = [s.upper() for s in cfg.symbols]
        self.last_ts: dict[str, int] = {}
        self.connected = False
        self.reconnects = 0
        self._gap_tasks: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        streams = "/".join(f"{s.lower()}@kline_1m" for s in self.symbols)
        return f"{self.cfg.ws_url.rstrip('/')}/stream?streams={streams}"

    def _emit(self, bar: Bar) -> None:
        last = self.last_ts.get(bar.symbol, 0)
        if bar.ts <= last:
            return
        self.last_ts[bar.symbol] = bar.ts
        self.emit(bar)
        if last and bar.ts > last + 60 and bar.ts - last <= MAX_BACKFILL_S:
            # 连接中途丢了推送：后台补齐，补回来的 K 线只入库和进历史，不再检测
            t = asyncio.create_task(self._fill_gap(bar.symbol, last + 60, bar.ts))
            self._gap_tasks.add(t)
            t.add_done_callback(self._gap_tasks.discard)

    async def _fill_gap(self, symbol: str, start: int, end: int) -> None:
        try:
            for b in await self.fetch_klines(symbol, start, end, pause_s=0):
                self.emit(b)
            log.info("Binance 补缺口 %s：%d 分钟", symbol, (end - start) // 60)
        except Exception as e:  # noqa: BLE001
            log.warning("Binance 补缺口失败 %s：%s", symbol, type(e).__name__)

    # ---- REST ----
    async def fetch_klines(self, symbol: str, start_ts: int, end_ts: int, pause_s: float = 0.2) -> list[Bar]:
        """取 [start_ts, end_ts) 内已收盘的 K 线。"""
        out: list[Bar] = []
        cur = start_ts
        url = f"{self.cfg.rest_url.rstrip('/')}/api/v3/klines"
        while cur < end_ts:
            params = {
                "symbol": symbol,
                "interval": "1m",
                "startTime": cur * 1000,
                "endTime": end_ts * 1000 - 1,
                "limit": 1000,
            }
            r = await self.http.get(url, params=params)
            if r.status_code in (418, 429):
                wait = float(r.headers.get("Retry-After", "30"))
                log.warning("Binance REST 限频，等待 %.0f 秒", wait)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            now = self.clock()
            for row in rows:
                if int(row[6]) / 1000 <= now:  # 只要已收盘的
                    out.append(parse_rest_row(symbol, row))
            nxt = int(rows[-1][0]) // 1000 + 60
            if nxt <= cur:
                break
            cur = nxt
            if pause_s:
                await asyncio.sleep(pause_s)
        return [b for b in out if start_ts <= b.ts < end_ts]

    async def backfill_since_last(self) -> list[Bar]:
        now = int(self.clock())
        end = now - now % 60  # 当前分钟还没收盘
        bars: list[Bar] = []
        for sym in self.symbols:
            last = self.last_ts.get(sym)
            if last is None:
                continue
            start = max(last + 60, end - MAX_BACKFILL_S)
            if start >= end:
                continue
            try:
                got = await self.fetch_klines(sym, start, end)
                bars.extend(got)
                if got:
                    log.info("Binance 补数 %s：%d 根", sym, len(got))
            except Exception as e:  # noqa: BLE001
                log.warning("Binance 补数失败 %s：%s", sym, type(e).__name__)
        return bars

    async def warmup(self, insert: Callable[[list[Bar]], Any], first_ts: Callable[[str, int], Any], last_ts: Callable[[str], Any], days: int) -> int:
        """启动预热：保证库里有最近 days 天的 K 线（基线用）。insert/first_ts/last_ts 是存储的异步包装。"""
        now = int(self.clock())
        end = now - now % 60
        since = end - days * 86400
        total = 0
        for sym in self.symbols:
            first = await first_ts(sym, since)
            last = await last_ts(sym)
            ranges = []
            if first is None or first > since + 3600:
                ranges.append((since, first if first is not None else end))
            if last is not None and last + 60 < end:
                ranges.append((max(last + 60, since), end))
            for a, b in ranges:
                if a >= b:
                    continue
                bars = await self.fetch_klines(sym, a, b)
                if bars:
                    await insert(bars)
                    total += len(bars)
                log.info("Binance 预热 %s：%d 根", sym, len(bars))
            lt = await last_ts(sym)
            if lt is not None:
                self.last_ts[sym] = lt
        return total

    async def server_time(self) -> float:
        r = await self.http.get(f"{self.cfg.rest_url.rstrip('/')}/api/v3/time")
        r.raise_for_status()
        return r.json()["serverTime"] / 1000.0

    # ---- WebSocket ----
    def _flush(self, backfill: asyncio.Task[list[Bar]] | None, buffered: list[Bar]) -> None:
        """放出补数结果和期间缓存的实时 K 线。补数没完成就取消它，缺口交给 _emit 的后台补缺。"""
        missed: list[Bar] = []
        if backfill is not None:
            if backfill.done() and not backfill.cancelled() and backfill.exception() is None:
                missed = backfill.result()
            else:
                backfill.cancel()
        for b in sorted(missed + buffered, key=lambda x: (x.ts, x.symbol)):
            self._emit(b)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        rotate_s = self.cfg.rotate_hours * 3600
        while not stop.is_set():
            backfill: asyncio.Task[list[Bar]] | None = None
            buffered: list[Bar] = []
            try:
                async with self.ws_connect(
                    self.url, open_timeout=10, ping_interval=20, ping_timeout=20, close_timeout=3, max_size=2**20
                ) as ws:
                    self.connected = True
                    backoff = 1.0
                    started = self.clock()
                    log.info("Binance 已连接（%d 个币）", len(self.symbols))
                    backfill = asyncio.create_task(self.backfill_since_last())
                    while not stop.is_set():
                        if self.clock() - started > rotate_s:
                            log.info("Binance 连接满 %.0f 小时，主动重连", self.cfg.rotate_hours)
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_s)
                        except TimeoutError:
                            log.warning("Binance %.0f 秒没有数据，重连", self.stale_s)
                            break
                        self.seen(self.name, self.clock())
                        try:
                            bar = parse_kline_event(json.loads(raw))
                        except (ValueError, KeyError, TypeError):
                            log.warning("Binance 消息解析失败")
                            continue
                        if backfill is not None:
                            if bar is not None:
                                buffered.append(bar)
                            if backfill.done():
                                self._flush(backfill, buffered)
                                backfill, buffered = None, []
                            continue
                        if bar is not None:
                            self._emit(bar)
            except asyncio.CancelledError:
                if backfill is not None:
                    backfill.cancel()
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Binance 连接出错：%s %s", type(e).__name__, str(e)[:200])
            # 断线时补数还没放出：缓存的实时 K 线不能丢
            if backfill is not None or buffered:
                self._flush(backfill, buffered)
            self.connected = False
            if stop.is_set():
                break
            self.reconnects += 1
            try:
                await asyncio.wait_for(stop.wait(), backoff)
            except TimeoutError:
                pass
            backoff = min(60.0, backoff * 2)
