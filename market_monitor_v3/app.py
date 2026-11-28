"""组装各模块并运行：一个 asyncio 进程，一个核心协程处理 K 线，一个每秒时钟驱动计时。"""
from __future__ import annotations

import asyncio
import fcntl
import html
import json
import logging
import math
import os
import signal
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .baseline import Baselines, BaselineStat, compute_symbol_baseline
from .bot import BotPoller, _log_control
from .config import SESSION_LABELS, AppConfig, Params, Secrets, channel_routes, load_config
from .control import ControlServer
from .engine import Engine
from .health import Health, sd_notify
from .models import Bar, OutMsg, display_name, market_of, target_label
from .notifiers.discord import DiscordClient, DiscordGateway, DiscordNotifier
from .outbox import ChatRef, OutboxSender
from .render import arrow, pct
from .reporting import build_daily_report
from .sessions import ET, UsCalendar, commodity_session, crypto_session, utc_market_instance
from .sink import BufferSink
from .store import Store, bar_from_row, connect
from .telegram import TelegramClient
from .tradingview import Throttle
from .tradingview import card_text as tv_card_text
from .trend.engine import TrendEngine
from .trend.series import Candle

log = logging.getLogger("market_monitor_v3")
BJ = ZoneInfo("Asia/Shanghai")
UTC = UTC


def next_at(now: float, tz: ZoneInfo | timezone, hhmm: str) -> float:
    hh, mm = (int(x) for x in hhmm.split(":"))
    dt = datetime.fromtimestamp(now, tz)
    cand = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand.timestamp() <= now:
        cand += timedelta(days=1)
    return cand.timestamp()


def make_calendar(cfg: AppConfig) -> UsCalendar:
    hh, mm = (int(x) for x in cfg.rules.thin_pre_until.split(":"))
    return UsCalendar(
        cfg.calendar.extra_holidays,
        cfg.calendar.extra_early_closes,
        overnight=cfg.stocks.overnight,
        thin_pre_until_min=hh * 60 + mm,
    )


def compute_baselines_sync(
    db_path: Path, cfg: AppConfig, cal: UsCalendar, symbols: list[str], now: float
) -> dict[str, dict[tuple[str, int], BaselineStat]]:
    """在后台线程里用只读连接算基线，不挡核心协程。"""
    conn = connect(db_path, readonly=True)
    out: dict[str, dict[tuple[str, int], BaselineStat]] = {}
    try:
        r = cfg.rules
        for sym in symbols:
            if market_of(sym) == "US":
                since = int(now) - cfg.stocks.longbridge.warmup_days * 86400
                rows = conn.execute(
                    "SELECT * FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts", (sym, since, int(now))
                ).fetchall()
                bars = [bar_from_row(x) for x in rows]
                out[sym] = compute_symbol_baseline(
                    bars,
                    lambda b: b.session if b.session in ("PRE", "REG", "POST", "ON") else cal.session_at(b.ts),
                    lambda b: cal.instance_of(b.ts),
                    r.baseline_sessions,
                    r.baseline_min_sessions,
                )
            else:
                cm = market_of(sym) == "CMDTY"
                days = cfg.commodities.baseline_days if cm else r.crypto_baseline_days
                since = int(now) - days * 86400
                rows = conn.execute(
                    "SELECT * FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts", (sym, since, int(now))
                ).fetchall()
                bars = [bar_from_row(x) for x in rows]
                out[sym] = compute_symbol_baseline(
                    bars,
                    (lambda b: commodity_session(b.ts)) if cm else (lambda b: crypto_session(b.ts)),
                    (lambda b, s=sym: utc_market_instance(s, b.ts)),
                    10_000,
                    cfg.commodities.min_days if cm else r.crypto_min_days,
                )
    finally:
        conn.close()
    return out


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    def release(self) -> None:
        if self.fh is not None:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


class Runtime:
    def __init__(self, cfg: AppConfig, config_path: str | None, secrets: Secrets, clock: Any = time.time):
        self.cfg = cfg
        self.config_path = config_path
        self.secrets = secrets
        self.clock = clock
        self.stop = asyncio.Event()
        self.store = Store(cfg.db_path, routes=channel_routes(cfg))
        self.cal = make_calendar(cfg)
        self.params = Params(cfg)
        self.baselines = Baselines()
        self.sink = BufferSink()
        self.queue: asyncio.Queue[Bar] = asyncio.Queue(maxsize=10_000)
        self.chat = ChatRef(cfg.telegram.chat_id)
        self.started_at = clock()
        self.core_progress = clock()
        self.dropped_bars = 0
        self.health: Health | None = None
        self.engine: Engine | None = None
        self.trend: TrendEngine | None = None
        self.senders: dict[str, OutboxSender] = {}
        self.channel_ok: dict[str, bool] = {}
        self.tg: TelegramClient | None = None
        self.tg_poll: TelegramClient | None = None
        self.discord_gw: DiscordGateway | None = None
        self.control: ControlServer | None = None
        self.tv_throttle = Throttle(cfg.tradingview.max_per_min, cfg.tradingview.dedup_s)
        self.exit_code = 0
        self.binance: Any = None
        self.binance_fut: Any = None  # 大宗商品（Binance USDT-M 永续）
        self.lb: Any = None
        self.ticks: Any = None  # V4 P2 逐笔层（TickHub）
        self.binance_trades: Any = None
        self._ticks_saved_at = 0.0
        self.tasks: list[asyncio.Task[Any]] = []

    # ---- 启动 ----
    async def start(self) -> None:
        cfg = self.cfg
        if cfg.telegram.enabled and not self.secrets.telegram_token:
            raise SystemExit("缺少环境变量 TELEGRAM_BOT_TOKEN（写在 /etc/market-monitor/secrets.env，或在后台“推送渠道”里填）")
        if cfg.discord.enabled and not self.secrets.discord_token:
            raise SystemExit("缺少环境变量 DISCORD_BOT_TOKEN（写在 /etc/market-monitor/secrets.env，或在后台“推送渠道”里填）")
        await self.store.open()
        override = await self.store.call(Store.kv_get, "chat_id_override")
        if override:
            self.chat.chat_id = int(override)
        await self.store.call(Store.log_event, "start", f"{__version__} {self.params.rule_version}", self.clock())

        if cfg.telegram.enabled:
            assert self.secrets.telegram_token
            self.tg = TelegramClient(self.secrets.telegram_token, cfg.telegram.api_base, cfg.telegram.send_timeout_s)
            self.tg_poll = TelegramClient(self.secrets.telegram_token, cfg.telegram.api_base, 40.0)
            self.senders["telegram"] = OutboxSender(
                self.store, self.tg, self.chat, self.clock, on_auth_change=lambda ok: self._on_auth("telegram", ok)
            )
        if cfg.discord.enabled:
            assert self.secrets.discord_token
            dc = DiscordClient(self.secrets.discord_token, cfg.discord.api_base, cfg.discord.send_timeout_s)
            self.senders["discord"] = OutboxSender(
                self.store,
                clock=self.clock,
                on_auth_change=lambda ok: self._on_auth("discord", ok),
                notifier=DiscordNotifier(dc, cfg.discord.channel_id),
            )
            if cfg.discord.commands or cfg.discord.feedback_buttons:
                self.discord_gw = DiscordGateway(
                    self.secrets.discord_token,
                    DiscordClient(self.secrets.discord_token, cfg.discord.api_base, cfg.discord.send_timeout_s),
                    cfg.discord.channel_id,
                    on_command=self._discord_command,
                    on_feedback=self._feedback,
                    ops=self._ops,
                    commands_enabled=cfg.discord.commands,
                    feedback_enabled=cfg.discord.feedback_buttons,
                    clock=self.clock,
                )
        self.channel_ok = {ch: True for ch in self.senders}

        server_time = None
        if cfg.crypto.enabled:
            from .feeds.binance_spot import BinanceSpotFeed

            self.binance = BinanceSpotFeed(
                cfg.crypto, self._emit, self._seen, stale_s=cfg.health.binance_stale_s, clock=self.clock
            )
            server_time = self.binance.server_time
        self.health = Health(
            cfg, self.cal, self.store, self.notify_senders, self.clock, self.secrets.heartbeat_url, server_time
        )
        if self.binance is not None:
            self.health.register("binance", "CRYPTO")
        if cfg.commodities.enabled:
            from .feeds.binance_spot import BinanceFuturesFeed

            self.binance_fut = BinanceFuturesFeed(
                cfg.commodities, self._emit, self._seen, stale_s=cfg.health.binance_stale_s, clock=self.clock
            )
            self.health.register("binance-futures", "CMDTY")
        if cfg.stocks.enabled:
            from .feeds.longbridge import LongbridgeFeed

            self.health.register("longbridge", "US")
            h = self.health
            self.lb = LongbridgeFeed(
                cfg.stocks,
                self.cal,
                self._emit,
                self._seen,
                stale_s=lambda now: h.stale_threshold("longbridge", now),
                ops=h.ops,
                clock=self.clock,
            )

        self.engine = Engine(cfg, self.params, self.cal, self.baselines, self.sink, alive=self.health.is_alive)
        trend_syms = cfg.trend.symbols(cfg)
        if trend_syms:
            self.trend = TrendEngine(
                trend_syms,
                self.cal,
                cfg.trend.params(),
                self.sink.out,
                is_muted=self.engine.is_muted,
                buttons=cfg.telegram.feedback_buttons or cfg.discord.feedback_buttons,
                show=cfg.trend.sr_show,
            )
        if self.trend is not None:
            self.trend.level_breaks = cfg.trend.level_breaks
            self.engine.levels_of = self.trend.levels
        await self._restore()
        await self._start_ticks()

        for feed, days in ((self.binance, cfg.rules.crypto_baseline_days), (self.binance_fut, cfg.commodities.baseline_days)):
            if feed is None:
                continue
            try:
                n = await feed.warmup(
                    lambda bars: self.store.call(Store.insert_bars, bars),
                    lambda sym, since: self.store.call(Store.first_bar_ts, sym, since),
                    lambda sym: self.store.call(Store.last_bar_ts, sym),
                    days,
                )
                log.info("%s 预热完成，新增 %d 根 K 线", feed.label, n)
            except Exception as e:  # noqa: BLE001
                log.warning("%s 预热失败（只用直通线，直到基线够数）：%s", feed.label, type(e).__name__)
        await self.recompute_baselines(cfg.all_symbols())

    async def _restore(self) -> None:
        assert self.engine is not None
        now = self.clock()
        since = int(now) - 40 * 60
        for sym in self.cfg.all_symbols():
            bars = await self.store.call(Store.load_recent_bars, sym, since)
            if market_of(sym) == "US":
                self.engine.set_ref_close(sym, await self.store.call(Store.last_close, sym, "REG"))
            self.engine.seed_history(bars)
            last = await self.store.call(Store.last_bar_ts, sym)
            if last is not None:
                if self.binance is not None and market_of(sym) == "CRYPTO":
                    self.binance.last_ts[sym] = last
                if self.binance_fut is not None and market_of(sym) == "CMDTY":
                    self.binance_fut.last_ts[sym] = last
                if self.lb is not None and market_of(sym) == "US":
                    self.lb.last_ts[sym] = last
        eps = await self.store.call(Store.active_episodes)
        roots = await self.store.call(Store.load_roots, [e.root_key for e in eps if e.root_key])
        self.engine.restore(eps, roots)
        self.engine.mutes = await self.store.call(Store.mutes_load, now)
        await self._load_v4_state(now)
        if eps:
            log.info("恢复 %d 个进行中的事件", len(eps))

    async def _start_ticks(self) -> None:
        """V4 P2：逐笔层。加密走单独的 Binance aggTrade 连接，美股复用长桥连接订逐笔。"""
        tc = self.cfg.ticks
        if not tc.enabled or self.engine is None:
            return
        use_crypto = tc.crypto and self.cfg.crypto.enabled
        use_stocks = tc.stocks and self.lb is not None
        if not (use_crypto or use_stocks):
            return
        from .ticks import TickHub

        eng = self.engine
        self.ticks = TickHub(tc, self.params, self.baselines, eng.hist.get, eng.session_for)
        now = self.clock()
        since_day = datetime.fromtimestamp(now - 31 * 86400, UTC).strftime("%Y-%m-%d")
        self.ticks.load_hists(await self.store.call(Store.tick_hists, since_day))
        self.ticks.load_minutes(await self.store.call(Store.tick_minutes, now - tc.big_days_crypto * 86400))
        eng.flow = self.ticks.flow
        if use_crypto:
            from .feeds.binance_trades import BinanceTradeFeed

            self.binance_trades = BinanceTradeFeed(
                self.cfg.crypto, self.ticks.on_trade, stale_s=self.cfg.health.binance_stale_s, clock=self.clock
            )
        if use_stocks:
            self.lb.on_tick = self.ticks.on_trade
        log.info("逐笔层已启用（%s）", "、".join(x for x, on in (("加密", use_crypto), ("美股", use_stocks)) if on))

    async def save_ticks(self, force: bool = False) -> None:
        if self.ticks is None:
            return
        now = self.clock()
        if not force and now - self._ticks_saved_at < 10:
            return
        self._ticks_saved_at = now
        mins, shadows, hists = self.ticks.drain()
        if mins or shadows or hists:
            await self.store.call(Store.tick_save, mins, shadows, hists)

    async def _load_v4_state(self, now: float) -> None:
        """V4：类型开关、自定义提醒规则、当日涨跌幅的参考价。"""
        assert self.engine is not None
        raw = await self.store.call(Store.kv_get, "types_off")
        try:
            self.engine.types_off = {k: list(v) for k, v in json.loads(raw).items()} if raw else {}
        except (ValueError, AttributeError):
            log.warning("kv types_off 无法解析，忽略")
            self.engine.types_off = {}
        self.engine.custom.set_rules(await self.store.call(Store.custom_rules))
        await self._seed_indicator_series()
        today_et = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0)
        for sym in self.cfg.all_symbols():
            if market_of(sym) == "US":
                ref = await self.store.call(Store.last_close, sym, "REG", int(today_et.timestamp()))
                self.engine.custom.seed_ref(sym, ref)

    async def _seed_indicator_series(self, only: str | None = None) -> None:
        """指标提醒要几天的 1 分钟历史（例如 1h EMA50 要 150 小时）：从库里补。美股有休市时段，多取一倍。"""
        assert self.engine is not None
        now = int(self.clock())
        for sym, mins in self.engine.custom.needs_series().items():
            if only and sym != only:
                continue
            since = now - min(mins * 60 * 2, self.cfg.storage.bars_days * 86400)
            bars = await self.store.call(Store.load_recent_bars, sym, since)
            self.engine.custom.seed_series(sym, bars)

    async def recompute_baselines(self, symbols: list[str]) -> None:
        now = self.clock()
        stats = await asyncio.to_thread(compute_baselines_sync, self.cfg.db_path, self.cfg, self.cal, symbols, now)
        ready = []
        for sym, st in stats.items():
            self.baselines.set_symbol(sym, st, now)
            await self.store.call(Store.save_baseline, sym, st, int(now))
            if any(s.ready for s in st.values()):
                ready.append(display_name(sym))
        log.info("基线已更新：%s", "、".join(ready) or "暂无足够数据")

    # ---- 数据入口 ----
    def _emit(self, bar: Bar) -> None:
        try:
            self.queue.put_nowait(bar)
        except asyncio.QueueFull:
            self.dropped_bars += 1

    def _seen(self, name: str, now: float) -> None:
        if self.health is not None:
            self.health.seen(name, now)

    async def flush(self) -> None:
        if self.trend is not None:
            teff = self.trend.drain()
            if not teff.empty():
                await self.store.call(Store.apply_trend, teff)
        eff = self.sink.drain()
        if eff.empty():
            return
        if self.trend is not None:
            # 推送出去的价格异动已经写了穿过某个强位：4 小时内不再单独推这个关键位突破
            for sig in eff.signals:
                r = sig.reason or {}
                if sig.kind == "open" and "level" in sig.tags and r.get("lvl_lo") is not None:
                    self.trend.note_crossing(sig.symbol, r["lvl_lo"], r["lvl_hi"], sig.direction, sig.ts)
        await self.store.call(Store.apply_effects, eff)
        if eff.outs:
            self.notify_senders()

    def notify_senders(self) -> None:
        for s in self.senders.values():
            s.notify()

    async def _on_auth(self, channel: str, ok: bool) -> None:
        self.channel_ok[channel] = ok
        if self.health is not None:
            # 至少有一个渠道还能发，用户就还能收到告警和运维消息；全部暂停才让外部心跳报警
            self.health.telegram_ok = any(self.channel_ok.values())
            self.health._last_heartbeat = 0.0  # 立刻把状态报给外部心跳

    async def _ops(self, key: str, text: str) -> None:
        if self.health is not None:
            await self.health.ops(key, text)

    async def _feedback(self, root_key: str, user_id: int, value: int) -> None:
        await self.store.call(Store.feedback_set, root_key, user_id, value)

    async def _discord_command(self, cmd: str, args: list[str], user_id: int | None, who: str) -> str | None:
        from .bot import run_command

        reply = await run_command(self, cmd, args, user_id, f"discord:{who}", set(self.cfg.discord.admin_user_ids), self.clock)
        await self.store.call(_log_control, self.clock(), f"discord:{who}", "/" + " ".join([cmd, *args]), (reply or "")[:200])
        return reply

    # ---- 协程 ----
    async def core_loop(self) -> None:
        assert self.engine is not None
        while not self.stop.is_set():
            get = asyncio.create_task(self.queue.get())
            stopper = asyncio.create_task(self.stop.wait())
            done, _ = await asyncio.wait({get, stopper}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            if get not in done:
                get.cancel()
                break
            bar = get.result()
            try:
                self.engine.on_bar(bar, self.clock())
            except Exception:
                log.exception("处理 K 线出错：%s %s", bar.symbol, bar.ts)
            if self.trend is not None:
                try:
                    self.trend.on_bar(bar, self.clock())
                except Exception:
                    log.exception("趋势处理 K 线出错：%s %s", bar.symbol, bar.ts)
            self.core_progress = self.clock()
            await self.flush()

    async def clock_loop(self) -> None:
        assert self.engine is not None and self.health is not None
        loop = asyncio.get_running_loop()
        next_t = loop.time() + 1.0
        last_wd = 0.0
        while not self.stop.is_set():
            await asyncio.sleep(max(0.0, next_t - loop.time()))
            lag = loop.time() - next_t
            next_t = next_t + 1.0 if lag < 5 else loop.time() + 1.0
            now = self.clock()
            self.health.report_lag(max(0.0, lag), now)
            try:
                if self.lb is not None and self.cfg.stocks.bar_source == "trades":
                    self.lb.flush_trades(now)
                self.engine.on_tick(now)
                await self.flush()
                if self.ticks is not None:
                    self.ticks.roll(now)
                    await self.save_ticks()
            except Exception:
                log.exception("时钟 tick 出错")
            core_ok = self.queue.qsize() == 0 or now - self.core_progress < 30
            if core_ok and now - last_wd >= 10:
                sd_notify("WATCHDOG=1")
                last_wd = now

    async def scheduler_loop(self) -> None:
        cfg = self.cfg
        now = self.clock()
        t_report = next_at(now, ZoneInfo(cfg.health.report_tz), cfg.health.daily_report_time)
        t_maint = next_at(now, BJ, cfg.storage.maintenance_time)
        t_stock_bl = next_at(now, ET, "20:05")
        t_crypto_bl = next_at(now, UTC, "00:05")
        t_cm_bl = next_at(now, ET, "17:05")
        t_mutes = now
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), 10)
                break
            except TimeoutError:
                pass
            now = self.clock()
            try:
                if now >= t_mutes and self.engine is not None:
                    t_mutes = now + 10
                    self.engine.mutes = await self.store.call(Store.mutes_load, now)
                if now >= t_report:
                    t_report = next_at(now, ZoneInfo(cfg.health.report_tz), cfg.health.daily_report_time)
                    await self.send_report(now)
                if now >= t_stock_bl:
                    t_stock_bl = next_at(now, ET, "20:05")
                    if cfg.stocks.enabled:
                        await self.recompute_baselines(cfg.stocks.symbols)
                if now >= t_crypto_bl:
                    t_crypto_bl = next_at(now, UTC, "00:05")
                    if cfg.crypto.enabled:
                        await self.recompute_baselines(cfg.crypto.symbols)
                if now >= t_cm_bl:
                    # 大宗商品每天美东 17:05（每日休市那一小时里）重算
                    t_cm_bl = next_at(now, ET, "17:05")
                    if cfg.commodities.enabled:
                        await self.recompute_baselines(cfg.commodities.symbols)
                if now >= t_maint:
                    t_maint = next_at(now, BJ, cfg.storage.maintenance_time)
                    msg = await self.store.call(
                        Store.maintenance,
                        now,
                        cfg.storage.bars_days,
                        cfg.storage.l0_days,
                        cfg.storage.outbox_days,
                        Path(cfg.general.state_dir) / "backups",
                        cfg.storage.backup_keep,
                    )
                    log.info("每日维护：%s", msg)
            except Exception:
                log.exception("定时任务出错")

    async def lb_bootstrap(self) -> None:
        """长桥连上之后做一次历史预热，再重算美股基线。"""
        if self.lb is None:
            return
        while not self.stop.is_set() and not self.lb.connected:
            try:
                await asyncio.wait_for(self.stop.wait(), 5)
            except TimeoutError:
                pass
        if self.stop.is_set():
            return
        try:
            n = await self.lb.warmup(
                lambda bars: self.store.call(Store.insert_bars, bars),
                lambda sym, since: self.store.call(Store.first_bar_ts, sym, since),
                self.cfg.stocks.longbridge.warmup_days,
            )
            log.info("长桥预热完成，新增 %d 根 K 线", n)
        except Exception as e:  # noqa: BLE001
            log.warning("长桥预热失败：%s %s", type(e).__name__, str(e)[:200])
        await self.recompute_baselines(self.cfg.stocks.symbols)
        await self.trend_bootstrap("US")

    # ---- 趋势监控 ----
    async def trend_bootstrap(self, market: str) -> None:
        """补齐 15m 历史（库里没有的从交易所拉），静默回放重建状态，再开始实时判断。"""
        if self.trend is None:
            return
        now = int(self.clock())
        since = now - self.cfg.trend.history_days * 86400
        saved = await self.store.call(Store.trend_states)
        for sym in [s for s in self.trend.machines if market_of(s) == market]:
            try:
                await self._trend_seed_symbol(sym, since, now, saved.get(sym))
            except Exception:
                log.exception("趋势预热失败：%s", sym)
                # 失败也要开始实时处理，否则这个标的一直在缓存
                if sym not in self.trend.seeded:
                    self.trend.seed(sym, [], [], saved.get(sym), self.clock())
            await self.flush()
            await asyncio.sleep(0)

    async def _trend_seed_symbol(self, sym: str, since: int, now: int, saved: dict[str, Any] | None) -> None:
        assert self.trend is not None
        rows = await self.store.call(Store.load_trend_bars, sym, since)
        fetched: list[tuple[str, Candle]] = []
        first = rows[0][0] if rows else None
        last_end = rows[-1][1] if rows else None
        end15 = now - now % 900
        bk = self.trend.bucketer(sym)
        feed = {"CRYPTO": self.binance, "CMDTY": self.binance_fut}.get(market_of(sym))
        if feed is not None:
            gaps = []
            if first is None or first > since + 86400:
                gaps.append((since, first if first is not None else end15))
            if last_end is not None and last_end < end15:
                gaps.append((last_end, end15))
            for a, b in gaps:
                for r in await feed.fetch_rows(sym, "15m", a, b):
                    ts = int(r[0]) // 1000
                    fetched.append((sym, Candle(ts, ts + 900, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]))))
        elif market_of(sym) == "US" and self.lb is not None and (first is None or first > since + 3 * 86400):
            try:
                hist = await self.lb.history_15m(sym, since)
            except Exception as e:  # noqa: BLE001
                log.warning("长桥 15m 历史获取失败 %s：%s %s", sym, type(e).__name__, str(e)[:200])
                hist = []
            for ts, o, h, l, c, v in hist:
                key = bk.key15(ts)
                if key is not None and key[0] == ts and (first is None or ts < first):
                    fetched.append((sym, Candle(ts, key[1], o, h, l, c, v)))
        if fetched:
            await self.store.call(Store.insert_trend_bars, fetched)
            rows = await self.store.call(Store.load_trend_bars, sym, since)
        candles = [Candle.from_row(r) for r in rows]
        tail_from = candles[-1].end if candles else since
        minutes = await self.store.call(Store.load_recent_bars, sym, tail_from)
        n = self.trend.seed(sym, candles, minutes, saved, self.clock())
        m = self.trend.machines[sym]
        log.info(
            "趋势预热 %s：15m %d 根（新拉 %d），1m 补 %d 根，%s",
            display_name(sym), n, len(fetched), len(minutes), "可以判断" if m.ready else "数据还不够",
        )

    # ---- V4：类型开关 ----
    def resolve_target(self, s: str) -> str | None:
        t = s.strip()
        if t.lower() in ("all", "*", "全部"):
            return "*"
        for g in self.cfg.groups:
            if t == g or t.lower() == g.lower() or t.removeprefix("group:") == g:
                return f"group:{g}"
        return self.resolve_symbol(t)

    def types_text(self) -> str:
        from .patterns import PATTERN_LABELS, PATTERNS

        assert self.engine is not None
        off = self.engine.types_off
        glob = set(off.get("*", ()))
        lines = ["<b>异动类型开关</b>"]
        lines.append("　".join(f"{PATTERN_LABELS[p]} {'❌' if p in glob else '✅'}" for p in PATTERNS))
        for target, pats in sorted(off.items()):
            if target == "*" or not pats:
                continue
            name = target.replace("group:", "分组 ") if target.startswith("group:") else display_name(target)
            lines.append(f"{html.escape(name)} 关闭：" + "、".join(PATTERN_LABELS.get(p, p) for p in pats))
        lines.append("放量是标签，不能单独开关。分组：" + "；".join(
            f"{html.escape(g)}（{'、'.join(html.escape(m) for m in ms)}）" for g, ms in self.cfg.groups.items()
        ))
        return "\n".join(lines)

    async def set_type(self, target: str, pattern: str, off: bool) -> None:
        assert self.engine is not None
        cur = set(self.engine.types_off.get(target, ()))
        if off:
            cur.add(pattern)
        else:
            cur.discard(pattern)
        if cur:
            self.engine.types_off[target] = sorted(cur)
        else:
            self.engine.types_off.pop(target, None)
        await self.store.call(Store.kv_set, "types_off", json.dumps(self.engine.types_off, ensure_ascii=False))

    # ---- V4：自定义提醒 ----
    def alerts_text(self) -> str:
        assert self.engine is not None
        rules = sorted(self.engine.custom.rules.values(), key=lambda r: r.id)
        if not rules:
            return "还没有自定义提醒。例如 /alert NVDA &gt; 140、/alert BTC 5m -2%。"
        lines = ["<b>自定义提醒</b>"]
        for r in rules:
            state = "" if r.enabled else "（已触发，停用）"
            lines.append(f"#{r.id} {html.escape(r.describe())}{state}" + (f"｜已触发 {r.fired} 次" if r.fired else ""))
        lines.append("删除：/unalert 编号")
        return "\n".join(lines)

    async def add_alert(self, args: list[str], by: str) -> str:
        from .custom import USAGE, CustomRule, parse_rule

        assert self.engine is not None
        if not args:
            return USAGE
        rule = parse_rule(args, self.resolve_symbol)
        if not isinstance(rule, CustomRule):
            return rule
        if len(self.engine.custom.rules) >= 200:
            return "自定义提醒最多 200 条，先用 /unalert 删掉一些。"
        rule.created_by, rule.created_at = by, self.clock()
        await self.store.call(Store.custom_add, rule)
        self.engine.custom.rules[rule.id] = rule
        if rule.kind == "indicator":
            await self._seed_indicator_series(rule.symbol)
        return f"已添加自定义提醒 #{rule.id}：{html.escape(rule.describe())}"

    async def remove_alert(self, rule_id: int | None) -> int:
        assert self.engine is not None
        n = await self.store.call(Store.custom_delete, rule_id)
        if rule_id is None:
            self.engine.custom.rules.clear()
        else:
            self.engine.custom.rules.pop(rule_id, None)
        return n

    async def trend_text(self, args: list[str]) -> str:
        if self.trend is None:
            return "趋势监控没有启用（config.toml 的 [trend] enabled = false，或者没有标的）。"
        if not args:
            return self.trend.overview_text()
        sym = self.resolve_symbol(args[0])
        if sym is None or sym not in self.trend.machines:
            return f"没找到标的 {html.escape(args[0])}，或者它没有开启趋势监控。"
        return self.trend.detail_text(sym)

    async def send_report(self, now: float | None = None) -> None:
        now = now or self.clock()
        text = await self.report_text(now)
        day = datetime.fromtimestamp(now, ZoneInfo(self.cfg.health.report_tz)).date().isoformat()
        await self.store.call(
            Store.enqueue,
            OutMsg(key=f"report:{day}", kind="ops", text=text, created_at=now, expires_at=now + 6 * 3600, silent=True),
        )
        self.notify_senders()

    async def report_text(self, now: float | None = None) -> str:
        now = now or self.clock()
        return await self.store.call(build_daily_report, self.cfg, self.params, self.cal, now - 86400, now)

    # ---- Bot 钩子 ----
    async def status_text(self) -> str:
        assert self.engine is not None and self.health is not None
        now = self.clock()
        up = int(now - self.started_at)
        lines = [f"🟢 <b>V3 运行中</b> · 已运行 {up // 3600} 小时 {up % 3600 // 60} 分", f"规则 {self.params.rule_version}"]
        if self.cfg.stocks.enabled:
            sess = self.cal.session_at(now)
            age = self.health.age("longbridge", now)
            src = f"长桥 {age:.0f} 秒前有数据" if age is not None else "长桥尚无数据"
            et = datetime.fromtimestamp(now, ET).strftime("%H:%M")
            lines.append(f"美股：{SESSION_LABELS[sess]}（美东 {et}）｜{src}")
        if self.cfg.crypto.enabled:
            age = self.health.age("binance", now)
            src = f"Binance {age:.0f} 秒前有数据" if age is not None else "Binance 尚无数据"
            lines.append(f"加密：{SESSION_LABELS[crypto_session(now)]}｜{src}")
        if self.cfg.commodities.enabled:
            age = self.health.age("binance-futures", now)
            src = f"Binance 合约 {age:.0f} 秒前有数据" if age is not None else "Binance 合约尚无数据"
            sess = commodity_session(now)
            note = "" if sess == "CMO" else "（休市不检测）"
            lines.append(f"大宗商品：{SESSION_LABELS[sess]}{note}｜{src}")
        eps = self.engine.episodes.active()
        if eps:
            parts = []
            for ep in eps:
                mins = max(1, round((now - ep.anchor_close_ts) / 60))
                parts.append(f"{display_name(ep.symbol)} {arrow(ep.direction)} {pct(ep.direction * ep.extreme)}（{mins} 分钟）")
            lines.append("进行中：" + "，".join(parts))
        else:
            lines.append("进行中：无")
        lines.append(self.mutes_text())
        n, age = await self.store.call(Store.outbox_backlog, now)
        chans = "，".join(
            f"{s.notifier.label} {'正常' if not s.paused else '暂停（发送被拒）'}" for s in self.senders.values()
        )
        lines.append(f"推送：{chans}｜待发 {n} 条")
        not_ready = [display_name(s) for s in self.cfg.all_symbols() if not self.baselines.ready_sessions(s)]
        if not_ready:
            lines.append("基线未就绪（只用直通线）：" + "、".join(not_ready))
        tl = self.ticks_text(now)
        if tl:
            lines.append(tl)
        return "\n".join(lines)

    def ticks_info(self, now: float) -> dict[str, Any] | None:
        if self.ticks is None:
            return None
        info = self.ticks.status()
        bt = self.binance_trades
        if bt is not None:
            info["binance"] = {
                "connected": bt.connected,
                "reconnects": bt.reconnects,
                "messages": bt.messages,
                "cpu_s": bt.cpu_s,
                "max_lag_s": bt.max_lag_s,
            }
        if self.lb is not None:
            info["longbridge_ticks"] = self.lb.ticks
        info["uptime_s"] = now - self.started_at
        return info

    def ticks_text(self, now: float) -> str:
        info = self.ticks_info(now)
        if info is None:
            return ""
        st = info["stats"]
        up = max(1.0, info["uptime_s"])
        parts = [f"逐笔：{st['trades'] / up:.1f} 笔/秒"]
        bt = info.get("binance")
        if bt is not None:
            parts.append(("Binance 已连接" if bt["connected"] else "Binance 未连接") + f"，CPU {bt['cpu_s'] / up * 100:.1f}%")
        ready = [display_name(s) for s, v in info["symbols"].items() if v["big_thr"] is not None]
        parts.append("大单门槛就绪：" + ("、".join(ready) if ready else "暂无"))
        parts.append(f"单笔冲涨跌（影子）{int(st['jumps'])} 次，接受 {int(st['accepted'])}，过入门门 {int(st['gate_pass'])}")
        return "｜".join(parts)

    def params_text(self) -> str:
        rows = self.params.table()
        lines = [f"<b>当前门槛</b>（{self.params.label}档，{self.params.rule_version}）", "时段：地板 1/5/15 分钟｜Z｜直通 1/5/15 分钟"]
        lines += [f"{a}：{b}｜Z {c}｜{d}" for a, b, c, d in rows]
        mults = ", ".join(f"{display_name(k)}×{v:g}" for k, v in self.params.multipliers.items())
        if mults:
            lines.append(f"标的系数（乘在地板上）：{mults}")
        r = self.cfg.rules
        lines.append(f"站稳要求：下一分钟保留 ≥ {r.confirm_ratio:.0%}；score ≥ {r.fast_score:g} 或触发直通 → L3 立即推")
        if r.spike_extra_confirm:
            lines.append(f"急拉急跌（L1/L2）再多确认 {r.spike_extra_confirm} 分钟；L3 不等")
        lines.append("成交额放大升 L2：" + (f"≥ {r.l2_turnover_mult:g} 倍" if r.l2_turnover_mult > 0 else "关闭（V4 默认）"))
        return "\n".join(lines)

    def mutes_text(self) -> str:
        assert self.engine is not None
        now = self.clock()
        items = []
        for k, until in sorted(self.engine.mutes.items()):
            if until > now:
                name = target_label(k)
                items.append(f"{name} 至 {datetime.fromtimestamp(until, BJ).strftime('%m-%d %H:%M')}")
        return "静音：" + ("，".join(items) if items else "无")

    def resolve_symbol(self, s: str) -> str | None:
        u = s.strip().upper()
        syms = self.cfg.all_symbols()
        for cand in (u, u + ".US", u + "USDT"):
            if cand in syms:
                return cand
        return None

    def resolve_mute_target(self, s: str) -> str | None:
        """标的代码，或者整个市场（美股 / 加密 / 大宗商品，也接受 market:CMDTY 这种写法）。"""
        from .models import MARKET_ALIASES

        t = s.strip()
        m = MARKET_ALIASES.get(t.removeprefix("market:").upper())
        if m is not None:
            return f"market:{m}"
        return self.resolve_symbol(t)

    async def mute(self, target: str, until: float, by: str) -> None:
        assert self.engine is not None
        self.engine.mutes[target] = until
        await self.store.call(Store.mute_set, target, until, by)

    async def unmute(self, target: str | None) -> int:
        assert self.engine is not None
        n = await self.store.call(Store.mute_clear, target)
        if target is None:
            self.engine.mutes.clear()
        else:
            self.engine.mutes.pop(target, None)
        return n

    # ---- 热加载 ----
    @staticmethod
    def restart_reasons(old: AppConfig, new: AppConfig) -> list[str]:
        """这些变化要重启进程才生效（重启由 systemd 自动拉起，十几秒）。"""
        out = []
        if new.all_symbols() != old.all_symbols() or new.stocks.enabled != old.stocks.enabled or new.crypto.enabled != old.crypto.enabled:
            out.append("标的或市场开关")
        if new.telegram.model_dump(exclude={"route", "admin_user_ids"}) != old.telegram.model_dump(exclude={"route", "admin_user_ids"}):
            out.append("Telegram 设置")
        if new.discord.model_dump(exclude={"route", "admin_user_ids"}) != old.discord.model_dump(exclude={"route", "admin_user_ids"}):
            out.append("Discord 设置")
        if new.stocks.longbridge != old.stocks.longbridge or new.crypto.model_dump(exclude={"multipliers"}) != old.crypto.model_dump(exclude={"multipliers"}):
            out.append("数据源设置")
        if new.general != old.general or new.storage != old.storage or new.calendar != old.calendar:
            out.append("系统设置")
        keys = {"enabled", "disabled_symbols", "zigzag_atr", "adx_min", "history_days"}
        if new.trend.model_dump(include=keys) != old.trend.model_dump(include=keys):
            out.append("趋势监控的标的或打分参数")
        tkeys = {"enabled", "crypto", "stocks"}
        if new.ticks.model_dump(include=tkeys) != old.ticks.model_dump(include=tkeys):
            out.append("逐笔层开关")
        return out

    async def reload(self, auto_restart: bool = False) -> dict[str, Any]:
        assert self.engine is not None and self.health is not None
        if not self.config_path:
            return {"ok": False, "error": "没有配置文件路径"}
        old = self.params.rule_version
        try:
            new = load_config(self.config_path)
        except Exception as e:  # noqa: BLE001
            await self.health.ops(f"reload-bad:{int(self.clock())}", f"新配置无效，继续使用旧配置：{str(e)[:300]}")
            return {"ok": False, "error": str(e)[:500]}
        reasons = self.restart_reasons(self.cfg, new)
        self.cfg = new
        self.params = Params(new)
        self.engine.cfg = new
        self.engine.set_params(self.params)
        self.engine.merger.buttons = new.telegram.feedback_buttons or new.discord.feedback_buttons
        self.engine.flow_mult = new.ticks.flow_mult
        self.engine.flow_tag = new.ticks.flow_tag
        if self.ticks is not None:
            # 逐笔层的门槛参数可以热加载；开关和数据源的变化要重启
            self.ticks.cfg = new.ticks
            self.ticks.params = self.params
        if self.trend is not None:
            self.trend.set_params(new.trend.params(), new.trend.sr_show)
            self.trend.level_breaks = new.trend.level_breaks
            self.trend.buttons = self.engine.merger.buttons
        self.health.cfg = new
        self.health.hc = new.health
        # 订阅规则（每个渠道收哪些消息）可以直接换
        routes = channel_routes(new)
        if set(routes) == set(self.store.routes):
            self.store.routes = routes
        self.tv_throttle.max_per_min = new.tradingview.max_per_min
        self.tv_throttle.dedup_s = new.tradingview.dedup_s
        msg = f"配置已重载：{old} → {self.params.rule_version}"
        if reasons and auto_restart:
            msg += f"。{'、'.join(reasons)}有变化，监控马上自动重启"
        elif reasons:
            msg += f"。{'、'.join(reasons)}的变化要重启才生效：systemctl restart market-monitor"
        await self.health.ops(f"reload:{int(self.clock())}", msg)
        if reasons and auto_restart:
            self.request_restart()
        return {"ok": True, "rule_version": self.params.rule_version, "restart": bool(reasons and auto_restart), "restart_reasons": reasons}

    def request_restart(self, delay: float = 1.0) -> None:
        """正常退出，由 systemd（Restart=always）拉起新进程。"""
        log.warning("按请求重启")
        self.exit_code = 0
        asyncio.get_running_loop().call_later(delay, self.stop.set)

    # ---- 控制通道 ----
    async def control_handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "version": __version__, "rule_version": self.params.rule_version}
        if cmd == "status":
            return {"ok": True, **(await self.status_json())}
        if cmd == "reload":
            return await self.reload(auto_restart=bool(req.get("auto_restart", True)))
        if cmd == "restart":
            await self.store.call(_log_control, self.clock(), "web", "restart", "ok")
            self.request_restart()
            return {"ok": True}
        if cmd == "mute":
            target = "*" if req.get("target") in ("*", "all") else self.resolve_mute_target(str(req.get("target") or ""))
            if target is None:
                return {"ok": False, "error": "没找到标的"}
            until = self.clock() + max(60, min(7 * 86400, int(req.get("seconds") or 3600)))
            await self.mute(target, until, "web")
            return {"ok": True, "target": target, "until": until}
        if cmd == "unmute":
            t = req.get("target")
            n = await self.unmute(None if t in (None, "*", "all") else self.resolve_mute_target(str(t)) or str(t))
            return {"ok": True, "cleared": n}
        if cmd == "types":
            from .patterns import PATTERN_LABELS

            return {"ok": True, "types_off": self.engine.types_off, "labels": PATTERN_LABELS, "groups": self.cfg.groups}
        if cmd == "types_set":
            from .patterns import parse_pattern

            pattern = parse_pattern(str(req.get("pattern") or ""))
            target = self.resolve_target(str(req.get("target") or "*"))
            if pattern is None or target is None:
                return {"ok": False, "error": "类型或目标不对"}
            await self.set_type(target, pattern, bool(req.get("off")))
            return {"ok": True, "types_off": self.engine.types_off}
        if cmd == "alerts":
            return {"ok": True, "rules": [
                {**r.to_json(), "text": r.describe()} for r in sorted(self.engine.custom.rules.values(), key=lambda r: r.id)
            ]}
        if cmd == "alert_add":
            text = str(req.get("text") or "").strip()
            msg = await self.add_alert(text.split(), str(req.get("by") or "web"))
            ok = msg.startswith("已添加")
            return {"ok": ok, "message": html.unescape(msg)} if ok else {"ok": False, "error": html.unescape(msg)}
        if cmd == "alert_del":
            rid = req.get("id")
            n = await self.remove_alert(None if rid in (None, "all") else int(rid))
            return {"ok": True, "removed": n}
        if cmd == "test":
            ch = str(req.get("channel") or "")
            if ch not in self.senders:
                return {"ok": False, "error": f"渠道 {ch} 没有启用"}
            now = self.clock()
            text = req.get("text") or f"✅ 测试消息：{self.senders[ch].notifier.label} 推送正常（{datetime.fromtimestamp(now, BJ).strftime('%m-%d %H:%M:%S')}）"
            await self.store.call(
                Store.enqueue,
                OutMsg(key=f"test:{ch}:{int(now * 1000)}", kind="cmd", text=str(text)[:3500], created_at=now, expires_at=now + 300, channel=ch, direction=int(req.get("direction") or 0)),
            )
            self.notify_senders()
            return {"ok": True}
        if cmd == "tv_alert":
            return await self.tradingview_alert(req.get("alert") or {})
        if cmd == "trend":
            if self.trend is None:
                return {"ok": True, "enabled": False, "items": [], "events": []}
            events = await self.store.call(Store.trend_events_recent, self.clock() - 14 * 86400, 40)
            return {"ok": True, "enabled": True, "items": self.trend.status_json(), "events": events}
        if cmd == "ops":
            # 网页后台的安全提醒（登录、锁定）：发到运维消息
            assert self.health is not None
            text = str(req.get("text") or "")[:500]
            if text:
                await self.health.ops(f"web:{int(self.clock() * 1000)}", "🔐 后台：" + html.escape(text))
            return {"ok": True}
        from . import api_backend

        if cmd in api_backend.API_COMMANDS:
            return await api_backend.handle(self, cmd, req)
        return {"ok": False, "error": f"未知命令 {cmd}"}

    async def stats_text(self) -> str:
        from .api_backend import stats_json
        from .stats import stats_lines

        st = await stats_json(self, self.clock())
        return "\n".join(["<b>异动统计</b>（括号里按级别加权：L1 = 1，L2 = 2，L3 = 3）", *stats_lines(st)])

    async def tradingview_alert(self, alert: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.tradingview.enabled:
            return {"ok": False, "error": "TradingView 转发没有启用"}
        now = self.clock()
        allowed, info = self.tv_throttle.allow(alert, now)
        if not allowed:
            log.info("TradingView 告警未转发：%s", info)
            return {"ok": False, "error": info}
        text, direction = tv_card_text(alert, now)
        await self.store.call(
            Store.enqueue,
            OutMsg(key=f"tv:{int(now)}:{info}", kind="send", text=text, created_at=now, expires_at=now + 600, level="L2", market="TV", direction=direction),
        )
        await self.store.call(Store.log_event, "tradingview", f"{alert.get('ticker') or ''} {alert.get('action') or ''}".strip(), now)
        self.notify_senders()
        return {"ok": True}

    async def status_json(self) -> dict[str, Any]:
        assert self.engine is not None and self.health is not None
        now = self.clock()
        feeds = {}
        if self.cfg.stocks.enabled:
            feeds["longbridge"] = self.health.age("longbridge", now)
        if self.cfg.crypto.enabled:
            feeds["binance"] = self.health.age("binance", now)
        if self.cfg.commodities.enabled:
            feeds["binance-futures"] = self.health.age("binance-futures", now)
        n, age = await self.store.call(Store.outbox_backlog, now)
        return {
            "version": __version__,
            "rule_version": self.params.rule_version,
            "preset": self.params.label,
            "uptime_s": int(now - self.started_at),
            "us_session": SESSION_LABELS[self.cal.session_at(now)] if self.cfg.stocks.enabled else None,
            "feeds": feeds,
            "channels": {
                ch: {"label": s.notifier.label, "paused": s.paused, "reason": s.paused_reason, "last_ok_at": s.last_ok_at}
                for ch, s in self.senders.items()
            },
            "discord_gateway": None if self.discord_gw is None else self.discord_gw.connected,
            "backlog": n,
            "backlog_age_s": age,
            "episodes": [
                {
                    "symbol": display_name(ep.symbol),
                    "direction": ep.direction,
                    "move": (math.exp(ep.direction * ep.extreme) - 1) * 100,
                    "minutes": max(1, round((now - ep.anchor_close_ts) / 60)),
                    "level": ep.level,
                }
                for ep in self.engine.episodes.active()
            ],
            "mutes": {k: v for k, v in self.engine.mutes.items() if v > now},
            "baseline_not_ready": [display_name(s) for s in self.cfg.all_symbols() if not self.baselines.ready_sessions(s)],
            "ticks": self.ticks_info(now),
            "recent": await self.store.call(_recent_alerts, now - 3 * 86400),
        }

    # ---- 运行 ----
    async def run(self) -> None:
        await self.start()
        assert self.health is not None
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.stop.set)
        loop.add_signal_handler(signal.SIGINT, self.stop.set)
        loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.ensure_future(self.reload()))

        bot = None
        if self.cfg.telegram.enabled and self.cfg.telegram.commands and self.tg_poll is not None:
            bot = BotPoller(
                self.store,
                self.tg_poll,
                self.chat,
                self,
                self.cfg.telegram.admin_user_ids,
                self.notify_senders,
                self.clock,
                self.cfg.telegram.feedback_buttons,
            )
        coros = [
            self.core_loop(),
            self.clock_loop(),
            self.scheduler_loop(),
            self.health.run(self.stop),
        ]
        coros += [s.run(self.stop) for s in self.senders.values()]
        if bot is not None:
            coros.append(bot.run(self.stop))
        if self.discord_gw is not None:
            coros.append(self.discord_gw.run(self.stop))
        self.control = ControlServer(self.cfg.general.control_socket, self.control_handle)
        try:
            await self.control.start()
        except OSError as e:
            log.warning("控制通道打不开（%s），网页后台将无法连接", e)
            self.control = None
        if self.binance is not None:
            coros.append(self.binance.run(self.stop))
        if self.binance_fut is not None:
            coros.append(self.binance_fut.run(self.stop))
        if self.binance_trades is not None:
            coros.append(self.binance_trades.run(self.stop))
        if self.lb is not None:
            coros.append(self.lb.run(self.stop))
            coros.append(self.lb_bootstrap())
        if self.trend is not None:
            # 加密的趋势预热在后台补数据，不挡启动；美股在长桥预热之后做（没有长桥就只用库里的数据）
            coros.append(self.trend_bootstrap("CRYPTO"))
            coros.append(self.trend_bootstrap("CMDTY"))
            if self.lb is None:
                coros.append(self.trend_bootstrap("US"))
        self.tasks = [asyncio.create_task(c) for c in coros]

        if self.cfg.health.startup_message:
            markets = []
            if self.cfg.stocks.enabled:
                markets.append(f"美股 {len(self.cfg.stocks.symbols)} 只")
            if self.cfg.crypto.enabled:
                markets.append("加密 " + "、".join(display_name(s) for s in self.cfg.crypto.symbols))
            if self.cfg.commodities.enabled:
                markets.append("大宗商品 " + "、".join(display_name(s) for s in self.cfg.commodities.symbols))
            await self.health.ops(
                f"start:{int(self.clock())}",
                f"V3 {__version__} 已启动｜{self.params.label}档 {self.params.rule_version}｜" + "，".join(markets),
                ttl=1800,
            )
        sd_notify("READY=1")
        log.info("V3 %s 已启动，%s", __version__, self.params.rule_version)

        await self.stop.wait()
        sd_notify("STOPPING=1")
        log.info("正在停止…")
        await self._shutdown()

    async def _shutdown(self) -> None:
        # stop 已置位：数据源和时钟会自行退出；发件箱再给最多 10 秒把待发的发完
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            n, _ = await self.store.call(Store.outbox_backlog, self.clock())
            if n == 0 or (self.health is not None and not self.health.telegram_ok):
                break
            await asyncio.sleep(0.5)
        if self.control is not None:
            await self.control.close()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            await self.flush()
            await self.save_ticks(force=True)
        except Exception:  # noqa: BLE001
            log.exception("停止时写库失败")
        for s in self.senders.values():
            await s.notifier.aclose()
        if self.tg_poll is not None:
            await self.tg_poll.aclose()
        if self.discord_gw is not None:
            await self.discord_gw.client.aclose()
        await self.store.close()
        log.info("已停止")


def _recent_alerts(store: Store, since: float) -> list[dict[str, Any]]:
    rows = store.c.execute(
        "SELECT a.ts, a.symbol, a.session, a.level, a.k, a.d, a.episode_id FROM alerts a "
        "WHERE a.outcome='pushed' AND a.ts>=? ORDER BY a.ts DESC LIMIT 30",
        (int(since),),
    ).fetchall()
    out = []
    for r in rows:
        d = r["d"] or 0.0
        out.append(
            {
                "ts": r["ts"],
                "symbol": display_name(r["symbol"]),
                "session": SESSION_LABELS.get(r["session"] or "", r["session"]),
                "level": r["level"],
                "k": r["k"],
                "move": (math.exp(d) - 1) * 100,
            }
        )
    return out


def run_service(config_path: str) -> int:
    from .logsetup import setup_logging

    cfg = load_config(config_path)
    setup_logging(cfg.general.log_level)
    lock = SingleInstanceLock(Path(cfg.general.state_dir) / "monitor.lock")
    if not lock.acquire():
        log.error("已有另一个实例在运行（%s），退出", lock.path)
        return 2
    try:
        rt = Runtime(cfg, config_path, Secrets.from_env())
        asyncio.run(rt.run())
    finally:
        lock.release()
    return rt.exit_code


__all__ = ["Runtime", "run_service", "next_at", "make_calendar", "compute_baselines_sync"]
