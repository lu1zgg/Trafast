"""Binance 现货逐笔（aggTrade 组合流），给 V4 P2 逐笔层用。

和 K 线分开一条连接：逐笔量大，断了也不影响 K 线和告警；逐笔不补数（断线期间的分钟摘要缺几笔无所谓）。
aggTrade 的 m = true 表示买方是挂单方，也就是这笔是主动卖。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from ..config import CryptoCfg
from ..sessions import crypto_session
from ..ticks import Trade

log = logging.getLogger(__name__)


def parse_agg_trade(msg: dict[str, Any]) -> Trade | None:
    data = msg.get("data", msg)
    if not isinstance(data, dict) or data.get("e") != "aggTrade":
        return None
    ts = int(data["T"]) / 1000.0
    return Trade(
        symbol=str(data["s"]).upper(),
        ts=ts,
        price=float(data["p"]),
        qty=float(data["q"]),
        side=-1 if data.get("m") else 1,
        session=crypto_session(ts),
    )


class BinanceTradeFeed:
    name = "binance-trades"

    def __init__(
        self,
        cfg: CryptoCfg,
        on_trade: Callable[[Trade], None],
        stale_s: float = 30.0,
        clock: Callable[[], float] = time.time,
        ws_connect: Callable[..., Any] | None = None,
    ):
        self.cfg = cfg
        self.on_trade = on_trade
        self.stale_s = stale_s
        self.clock = clock
        if ws_connect is None:
            from websockets.asyncio.client import connect as ws_connect  # noqa: N813
        self.ws_connect = ws_connect
        self.symbols = [s.upper() for s in cfg.symbols]
        self.connected = False
        self.reconnects = 0
        self.messages = 0
        self.errors = 0
        self.cpu_s = 0.0  # 解析和聚合累计用掉的 CPU 时间（在 VPS 上看逐笔的开销）
        self.max_lag_s = 0.0  # 成交时间到收到的最大延迟（最近一分钟）
        self._lag_window_start = 0.0

    @property
    def url(self) -> str:
        streams = "/".join(f"{s.lower()}@aggTrade" for s in self.symbols)
        return f"{self.cfg.ws_url.rstrip('/')}/stream?streams={streams}"

    def handle(self, raw: str | bytes) -> None:
        t0 = time.perf_counter()
        try:
            trade = parse_agg_trade(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            self.errors += 1
            return
        if trade is not None:
            self.messages += 1
            now = self.clock()
            if now - self._lag_window_start >= 60:
                self._lag_window_start = now
                self.max_lag_s = 0.0
            self.max_lag_s = max(self.max_lag_s, now - trade.ts)
            try:
                self.on_trade(trade)
            except Exception:  # noqa: BLE001
                self.errors += 1
                log.exception("逐笔处理出错：%s", trade.symbol)
        self.cpu_s += time.perf_counter() - t0

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        rotate_s = self.cfg.rotate_hours * 3600
        while not stop.is_set():
            try:
                async with self.ws_connect(
                    self.url, open_timeout=10, ping_interval=20, ping_timeout=20, close_timeout=3, max_size=2**20
                ) as ws:
                    self.connected = True
                    backoff = 1.0
                    started = self.clock()
                    log.info("Binance 逐笔已连接（%d 个币）", len(self.symbols))
                    while not stop.is_set():
                        if self.clock() - started > rotate_s:
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_s)
                        except TimeoutError:
                            log.warning("Binance 逐笔 %.0f 秒没有数据，重连", self.stale_s)
                            break
                        self.handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Binance 逐笔连接出错：%s %s", type(e).__name__, str(e)[:200])
            self.connected = False
            if stop.is_set():
                break
            self.reconnects += 1
            try:
                await asyncio.wait_for(stop.wait(), backoff)
            except TimeoutError:
                pass
            backoff = min(60.0, backoff * 2)
