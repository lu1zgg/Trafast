"""健康监控：数据新鲜度、事件循环卡顿、发件箱积压、时钟偏差、凭据到期、外部心跳、systemd 看门狗。

该不该有数据看交易日历：周末和休市日美股没有推送是正常的，不算故障。
断流和恢复成对发送，同一次故障只发一对。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from .config import AppConfig
from .models import OutMsg
from .sessions import UsCalendar
from .store import Store

log = logging.getLogger(__name__)

FEED_LABELS = {"longbridge": "长桥行情", "binance": "Binance 行情"}


def sd_notify(msg: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False


@dataclass
class FeedState:
    name: str
    market: str
    last_msg_at: float | None = None
    stale_since: float | None = None
    alerted: bool = False
    outages: int = 0


class Health:
    def __init__(
        self,
        cfg: AppConfig,
        cal: UsCalendar,
        store: Store,
        notify_sender: Callable[[], None],
        clock: Callable[[], float] = time.time,
        heartbeat_url: str | None = None,
        server_time: Callable[[], Awaitable[float]] | None = None,
    ):
        self.cfg = cfg
        self.hc = cfg.health
        self.cal = cal
        self.store = store
        self.notify_sender = notify_sender
        self.clock = clock
        self.heartbeat_url = heartbeat_url
        self.server_time = server_time
        self.feeds: dict[str, FeedState] = {}
        self.started_at = clock()
        self.loop_lag = 0.0
        self._lag_since: float | None = None
        self._lag_alerted = False
        self.telegram_ok = True
        self.clock_offset: float | None = None
        self._last_backlog_alert = 0.0
        self._last_clock_check = 0.0
        self._last_heartbeat = 0.0
        self._token_warned: date | None = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    # ---- 数据源 ----
    def register(self, name: str, market: str) -> None:
        self.feeds[name] = FeedState(name, market)

    def seen(self, name: str, now: float | None = None) -> None:
        st = self.feeds.get(name)
        if st is not None:
            st.last_msg_at = now if now is not None else self.clock()

    def expected(self, name: str, now: float) -> bool:
        st = self.feeds[name]
        if st.market == "US":
            return self.cal.session_at(now - 60) != "CLOSED"
        return True

    def stale_threshold(self, name: str, now: float) -> float:
        st = self.feeds[name]
        if st.market == "US":
            return self.hc.lb_stale_reg_s if self.cal.session_at(now) == "REG" else self.hc.lb_stale_other_s
        return self.hc.binance_stale_s

    def is_alive(self, market: str, now: float) -> bool:
        for st in self.feeds.values():
            if st.market == market and st.last_msg_at is not None:
                if now - st.last_msg_at <= self.stale_threshold(st.name, now):
                    return True
        return False

    def age(self, name: str, now: float) -> float | None:
        st = self.feeds.get(name)
        if st is None or st.last_msg_at is None:
            return None
        return now - st.last_msg_at

    # ---- 运维消息 ----
    async def ops(self, key: str, text: str, ttl: float = 3600) -> None:
        now = self.clock()
        await self.store.call(
            Store.enqueue,
            OutMsg(key=f"ops:{key}", kind="ops", text="⚙️ 运维：" + text, created_at=now, expires_at=now + ttl),
        )
        await self.store.call(Store.log_event, "ops", text[:300], now)
        self.notify_sender()

    # ---- 主循环 ----
    def report_lag(self, lag: float, now: float) -> None:
        self.loop_lag = lag
        if lag > self.hc.loop_lag_s:
            if self._lag_since is None:
                self._lag_since = now
        else:
            self._lag_since = None

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("健康检查出错")
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except TimeoutError:
                pass
        await self._http.aclose()

    async def check_once(self) -> None:
        now = self.clock()
        await self._check_feeds(now)
        await self._check_lag(now)
        await self._check_outbox(now)
        await self._heartbeat(now)
        await self._check_clock(now)
        await self._check_token(now)

    async def _check_feeds(self, now: float) -> None:
        for st in self.feeds.values():
            label = FEED_LABELS.get(st.name, st.name)
            if not self.expected(st.name, now):
                if st.alerted:
                    st.alerted = False
                    st.stale_since = None
                continue
            age = now - st.last_msg_at if st.last_msg_at is not None else now - self.started_at
            thr = self.stale_threshold(st.name, now)
            if age > thr:
                if st.stale_since is None:
                    st.stale_since = now - age + thr
                if not st.alerted and now - st.stale_since >= self.hc.stale_alert_after_s:
                    st.alerted = True
                    st.outages += 1
                    mins = max(1, round((now - st.stale_since + thr) / 60))
                    await self.ops(
                        f"stale:{st.name}:{int(st.stale_since)}",
                        f"{label}中断约 {mins} 分钟，正在自动重连。",
                    )
            else:
                if st.alerted:
                    mins = max(1, round((now - (st.stale_since or now)) / 60))
                    await self.ops(f"recovered:{st.name}:{int(now)}", f"{label}已恢复，中断约 {mins} 分钟。")
                st.alerted = False
                st.stale_since = None

    async def _check_lag(self, now: float) -> None:
        if self._lag_since is not None and now - self._lag_since >= self.hc.loop_lag_alert_s and not self._lag_alerted:
            self._lag_alerted = True
            await self.ops(f"lag:{int(now)}", f"事件循环卡顿：每秒定时器延迟 {self.loop_lag:.1f} 秒，已持续 1 分钟以上。")
        elif self._lag_since is None:
            self._lag_alerted = False

    async def _check_outbox(self, now: float) -> None:
        if now - self._last_backlog_alert < 3600:
            return
        n, age = await self.store.call(Store.outbox_backlog, now)
        if n > self.hc.outbox_backlog_n or age > self.hc.outbox_backlog_age_s:
            self._last_backlog_alert = now
            log.warning("发件箱积压：%d 条，最早的等了 %.0f 秒", n, age)
            if self.telegram_ok:
                await self.ops(f"backlog:{int(now)}", f"发件箱积压 {n} 条，最早一条已等待 {age / 60:.0f} 分钟。")

    async def _heartbeat(self, now: float) -> None:
        if not self.heartbeat_url or now - self._last_heartbeat < self.hc.heartbeat_interval_s:
            return
        self._last_heartbeat = now
        url = self.heartbeat_url.rstrip("/") + ("" if self.telegram_ok else "/fail")
        try:
            await self._http.get(url)
        except httpx.HTTPError as e:
            log.warning("外部心跳失败：%s", type(e).__name__)

    async def _check_clock(self, now: float) -> None:
        if self.server_time is None or now - self._last_clock_check < self.hc.clock_check_minutes * 60:
            return
        self._last_clock_check = now
        try:
            t0 = time.time()
            server = await self.server_time()
            t1 = time.time()
        except Exception as e:  # noqa: BLE001
            log.warning("对时失败：%s", type(e).__name__)
            return
        self.clock_offset = server - (t0 + t1) / 2
        if abs(self.clock_offset) > self.hc.clock_max_offset_s:
            day = datetime.fromtimestamp(now).strftime("%Y%m%d")
            await self.ops(
                f"clock:{day}",
                f"本机时钟和 Binance 服务器相差 {self.clock_offset:+.2f} 秒，请检查 chrony。",
            )

    async def _check_token(self, now: float) -> None:
        exp = self.hc.longbridge_token_expires
        if exp is None or not self.cfg.stocks.enabled:
            return
        today = datetime.fromtimestamp(now, ZoneInfo(self.hc.report_tz)).date()
        days_left = (exp - today).days
        if days_left <= self.hc.token_warn_days and self._token_warned != today:
            self._token_warned = today
            await self.ops(
                f"token:{today.isoformat()}",
                f"长桥 Access Token 还有 {days_left} 天到期（{exp.isoformat()}），请到长桥开发者中心更新，"
                "然后改 secrets.env 和 config.toml 里的到期日并重启。",
                ttl=6 * 3600,
            )
