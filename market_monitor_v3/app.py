"""组装各模块并运行：一个 asyncio 进程，一个核心协程处理 K 线，一个每秒时钟驱动计时。"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .baseline import Baselines, BaselineStat, compute_symbol_baseline
from .bot import BotPoller
from .config import SESSION_LABELS, AppConfig, Params, Secrets, load_config
from .engine import Engine
from .health import Health, sd_notify
from .models import Bar, OutMsg, display_name, market_of
from .outbox import ChatRef, OutboxSender
from .render import arrow, pct
from .reporting import build_daily_report
from .sessions import ET, UsCalendar, crypto_instance, crypto_session
from .sink import BufferSink
from .store import Store, bar_from_row, connect
from .telegram import TelegramClient

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
                since = int(now) - r.crypto_baseline_days * 86400
                rows = conn.execute(
                    "SELECT * FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts", (sym, since, int(now))
                ).fetchall()
                bars = [bar_from_row(x) for x in rows]
                out[sym] = compute_symbol_baseline(
                    bars,
                    lambda b: crypto_session(b.ts),
                    lambda b: crypto_instance(b.ts),
                    10_000,
                    r.crypto_min_days,
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
        self.store = Store(cfg.db_path)
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
        self.sender: OutboxSender | None = None
        self.binance: Any = None
        self.lb: Any = None
        self.tasks: list[asyncio.Task[Any]] = []

    # ---- 启动 ----
    async def start(self) -> None:
        cfg = self.cfg
        if not self.secrets.telegram_token:
            raise SystemExit("缺少环境变量 TELEGRAM_BOT_TOKEN（写在 /etc/market-monitor/secrets.env）")
        await self.store.open()
        override = await self.store.call(Store.kv_get, "chat_id_override")
        if override:
            self.chat.chat_id = int(override)
        await self.store.call(Store.log_event, "start", f"{__version__} {self.params.rule_version}", self.clock())

        self.tg = TelegramClient(self.secrets.telegram_token, cfg.telegram.api_base, cfg.telegram.send_timeout_s)
        self.tg_poll = TelegramClient(self.secrets.telegram_token, cfg.telegram.api_base, 40.0)
        self.sender = OutboxSender(self.store, self.tg, self.chat, self.clock, on_auth_change=self._on_tg_auth)

        server_time = None
        if cfg.crypto.enabled:
            from .feeds.binance_spot import BinanceSpotFeed

            self.binance = BinanceSpotFeed(
                cfg.crypto, self._emit, self._seen, stale_s=cfg.health.binance_stale_s, clock=self.clock
            )
            server_time = self.binance.server_time
        self.health = Health(
            cfg, self.cal, self.store, self.sender.notify, self.clock, self.secrets.heartbeat_url, server_time
        )
        if self.binance is not None:
            self.health.register("binance", "CRYPTO")
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
        await self._restore()

        if self.binance is not None:
            try:
                n = await self.binance.warmup(
                    lambda bars: self.store.call(Store.insert_bars, bars),
                    lambda sym, since: self.store.call(Store.first_bar_ts, sym, since),
                    lambda sym: self.store.call(Store.last_bar_ts, sym),
                    cfg.rules.crypto_baseline_days,
                )
                log.info("Binance 预热完成，新增 %d 根 K 线", n)
            except Exception as e:  # noqa: BLE001
                log.warning("Binance 预热失败（只用直通线，直到基线够数）：%s", type(e).__name__)
        await self.recompute_baselines(cfg.all_symbols())

    async def _restore(self) -> None:
        assert self.engine is not None
        now = self.clock()
        since = int(now) - 40 * 60
        for sym in self.cfg.all_symbols():
            bars = await self.store.call(Store.load_recent_bars, sym, since)
            self.engine.seed_history(bars)
            last = await self.store.call(Store.last_bar_ts, sym)
            if last is not None:
                if self.binance is not None and market_of(sym) == "CRYPTO":
                    self.binance.last_ts[sym] = last
                if self.lb is not None and market_of(sym) == "US":
                    self.lb.last_ts[sym] = last
        eps = await self.store.call(Store.active_episodes)
        roots = await self.store.call(Store.load_roots, [e.root_key for e in eps if e.root_key])
        self.engine.restore(eps, roots)
        self.engine.mutes = await self.store.call(Store.mutes_load, now)
        if eps:
            log.info("恢复 %d 个进行中的事件", len(eps))

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
        eff = self.sink.drain()
        if eff.empty():
            return
        await self.store.call(Store.apply_effects, eff)
        if eff.outs and self.sender is not None:
            self.sender.notify()

    async def _on_tg_auth(self, ok: bool) -> None:
        if self.health is not None:
            self.health.telegram_ok = ok
            self.health._last_heartbeat = 0.0  # 立刻把状态报给外部心跳

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

    async def send_report(self, now: float | None = None) -> None:
        now = now or self.clock()
        text = await self.report_text(now)
        day = datetime.fromtimestamp(now, ZoneInfo(self.cfg.health.report_tz)).date().isoformat()
        await self.store.call(
            Store.enqueue,
            OutMsg(key=f"report:{day}", kind="ops", text=text, created_at=now, expires_at=now + 6 * 3600, silent=True),
        )
        if self.sender is not None:
            self.sender.notify()

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
        tg = "正常" if self.health.telegram_ok else "暂停（发送被拒）"
        lines.append(f"Telegram：{tg}｜待发 {n} 条")
        not_ready = [display_name(s) for s in self.cfg.all_symbols() if not self.baselines.ready_sessions(s)]
        if not_ready:
            lines.append("基线未就绪（只用直通线）：" + "、".join(not_ready))
        return "\n".join(lines)

    def params_text(self) -> str:
        rows = self.params.table()
        lines = [f"<b>当前门槛</b>（{self.params.label}档，{self.params.rule_version}）", "时段：地板 1/5/15 分钟｜Z｜直通 1/5/15 分钟"]
        lines += [f"{a}：{b}｜Z {c}｜{d}" for a, b, c, d in rows]
        mults = ", ".join(f"{display_name(k)}×{v:g}" for k, v in self.params.multipliers.items())
        if mults:
            lines.append(f"标的系数（乘在地板上）：{mults}")
        r = self.cfg.rules
        lines.append(f"站稳要求：下一分钟保留 ≥ {r.confirm_ratio:.0%}；score ≥ {r.fast_score:g} 或触发直通 → L3 立即推")
        return "\n".join(lines)

    def mutes_text(self) -> str:
        assert self.engine is not None
        now = self.clock()
        items = []
        for k, until in sorted(self.engine.mutes.items()):
            if until > now:
                name = "全部" if k == "*" else display_name(k)
                items.append(f"{name} 至 {datetime.fromtimestamp(until, BJ).strftime('%m-%d %H:%M')}")
        return "静音：" + ("，".join(items) if items else "无")

    def resolve_symbol(self, s: str) -> str | None:
        u = s.strip().upper()
        syms = self.cfg.all_symbols()
        for cand in (u, u + ".US", u + "USDT"):
            if cand in syms:
                return cand
        return None

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
    async def reload(self) -> None:
        assert self.engine is not None and self.health is not None
        if not self.config_path:
            return
        old = self.params.rule_version
        try:
            new = load_config(self.config_path)
        except Exception as e:  # noqa: BLE001
            await self.health.ops(f"reload-bad:{int(self.clock())}", f"新配置无效，继续使用旧配置：{str(e)[:300]}")
            return
        restart_needed = (
            new.all_symbols() != self.cfg.all_symbols()
            or str(new.telegram.chat_id) != str(self.cfg.telegram.chat_id)
            or new.stocks.enabled != self.cfg.stocks.enabled
            or new.crypto.enabled != self.cfg.crypto.enabled
        )
        self.cfg = new
        self.params = Params(new)
        self.engine.cfg = new
        self.engine.set_params(self.params)
        self.engine.merger.buttons = new.telegram.feedback_buttons
        self.health.cfg = new
        self.health.hc = new.health
        msg = f"配置已重载：{old} → {self.params.rule_version}"
        if restart_needed:
            msg += "。标的、群组或市场开关的变化要重启才生效：systemctl restart market-monitor"
        await self.health.ops(f"reload:{int(self.clock())}", msg)

    # ---- 运行 ----
    async def run(self) -> None:
        await self.start()
        assert self.sender is not None and self.health is not None
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.stop.set)
        loop.add_signal_handler(signal.SIGINT, self.stop.set)
        loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.ensure_future(self.reload()))

        bot = None
        if self.cfg.telegram.commands:
            bot = BotPoller(
                self.store,
                self.tg_poll,
                self.chat,
                self,
                self.cfg.telegram.admin_user_ids,
                self.sender.notify,
                self.clock,
                self.cfg.telegram.feedback_buttons,
            )
        coros = [
            self.core_loop(),
            self.clock_loop(),
            self.scheduler_loop(),
            self.sender.run(self.stop),
            self.health.run(self.stop),
        ]
        if bot is not None:
            coros.append(bot.run(self.stop))
        if self.binance is not None:
            coros.append(self.binance.run(self.stop))
        if self.lb is not None:
            coros.append(self.lb.run(self.stop))
            coros.append(self.lb_bootstrap())
        self.tasks = [asyncio.create_task(c) for c in coros]

        if self.cfg.health.startup_message:
            markets = []
            if self.cfg.stocks.enabled:
                markets.append(f"美股 {len(self.cfg.stocks.symbols)} 只")
            if self.cfg.crypto.enabled:
                markets.append("加密 " + "、".join(display_name(s) for s in self.cfg.crypto.symbols))
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
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            await self.flush()
        except Exception:  # noqa: BLE001
            log.exception("停止时写库失败")
        await self.tg.aclose()
        await self.tg_poll.aclose()
        await self.store.close()
        log.info("已停止")


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
    return 0


__all__ = ["Runtime", "run_service", "next_at", "make_calendar", "compute_baselines_sync"]
