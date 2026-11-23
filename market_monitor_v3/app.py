"""组装各模块并运行：一个 asyncio 进程，一个核心协程处理 K 线，一个每秒时钟驱动计时。"""
from __future__ import annotations

import asyncio
import fcntl
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
from .models import Bar, OutMsg, display_name, market_of
from .notifiers.discord import DiscordClient, DiscordGateway, DiscordNotifier
from .outbox import ChatRef, OutboxSender
from .render import arrow, pct
from .reporting import build_daily_report
from .sessions import ET, UsCalendar, crypto_instance, crypto_session
from .sink import BufferSink
from .store import Store, bar_from_row, connect
from .telegram import TelegramClient
from .tradingview import Throttle
from .tradingview import card_text as tv_card_text

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
        self.senders: dict[str, OutboxSender] = {}
        self.channel_ok: dict[str, bool] = {}
        self.tg: TelegramClient | None = None
        self.tg_poll: TelegramClient | None = None
        self.discord_gw: DiscordGateway | None = None
        self.control: ControlServer | None = None
        self.tv_throttle = Throttle(cfg.tradingview.max_per_min, cfg.tradingview.dedup_s)
        self.exit_code = 0
        self.binance: Any = None
        self.lb: Any = None
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
            target = "*" if req.get("target") in ("*", "all") else self.resolve_symbol(str(req.get("target") or ""))
            if target is None:
                return {"ok": False, "error": "没找到标的"}
            until = self.clock() + max(60, min(7 * 86400, int(req.get("seconds") or 3600)))
            await self.mute(target, until, "web")
            return {"ok": True, "target": target, "until": until}
        if cmd == "unmute":
            t = req.get("target")
            n = await self.unmute(None if t in (None, "*", "all") else self.resolve_symbol(str(t)) or str(t))
            return {"ok": True, "cleared": n}
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
        return {"ok": False, "error": f"未知命令 {cmd}"}

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
        if self.control is not None:
            await self.control.close()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            await self.flush()
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
