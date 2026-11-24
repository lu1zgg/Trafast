"""长桥 OpenAPI 美股行情（按 longbridge Python SDK 5.5 编写，尚未用真实账户接通验证）。

- AsyncQuoteContext，原生 asyncio；Config 里打开夜盘（enable_overnight）并用 confirmed 推送模式（收盘才推）。
- subscribe_candlesticks(symbol, Period.Min_1, TradeSessions.All)：盘前、盘中、盘后、夜盘全部时段。
- 订阅 Quote 只用来判断连接是否活着；可选用逐笔成交自己合成 K 线（bar_source = "trades"）。
- 断线：长时间没有推送就重建连接并重新订阅，再用 candlesticks() 补齐断线期间的 K 线。
- 预热：history_candlesticks_by_offset 往回翻页，取最近 warmup_days 天（基线用）。
- 每次连接后：检查 10 只股票是否都在夜盘名单里；用 trading_days 刷新休市日历。

接通时需要用 `monitor lb-check` 核对的三点（见 README）：
1. K 线推送是否覆盖盘前、盘后、夜盘；
2. K 线时间戳是开盘时间还是收盘时间、带不带时区（config: ts_is_close / naive_tz）；
3. trade_session 的取值能否对上。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import StocksCfg
from ..models import Bar
from ..sessions import UsCalendar

log = logging.getLogger(__name__)

SKIP_TRADE_TYPES = {"P", "W"}  # 前一交易日参考价、均价成交：不代表当前价格


class LongbridgeNotConfigured(RuntimeError):
    pass


def load_sdk() -> Any:
    try:
        import longbridge.openapi as sdk  # type: ignore[import-not-found]

        return sdk
    except ImportError:
        pass
    try:
        import longport.openapi as sdk  # type: ignore[import-not-found]

        return sdk
    except ImportError as e:
        raise LongbridgeNotConfigured("没有安装长桥 SDK：pip install 'market-monitor-v3[longbridge]'") from e


def map_session(sdk: Any, value: Any) -> str | None:
    ts_cls = getattr(sdk, "TradeSession", None)
    if ts_cls is not None:
        for attr, code in (("Overnight", "ON"), ("Pre", "PRE"), ("Post", "POST"), ("Intraday", "REG")):
            v = getattr(ts_cls, attr, None)
            if v is not None and (value is v or value == v):
                return code
    name = str(value).lower()
    for key, code in (("overnight", "ON"), ("pre", "PRE"), ("post", "POST"), ("intraday", "REG"), ("normal", "REG")):
        if key in name:
            return code
    return None


class TradeBarBuilder:
    """逐笔成交合成 1 分钟 K 线（K 线推送不覆盖某些时段时的后备方案）。"""

    def __init__(self) -> None:
        self.cur: dict[str, dict[int, list[Any]]] = {}

    def add(self, symbol: str, ts: float, price: float, volume: float, session: str | None) -> None:
        m = int(ts) - int(ts) % 60
        bars = self.cur.setdefault(symbol, {})
        b = bars.get(m)
        if b is None:
            bars[m] = [price, price, price, price, volume, price * volume, session]
        else:
            b[1] = max(b[1], price)
            b[2] = min(b[2], price)
            b[3] = price
            b[4] += volume
            b[5] += price * volume

    def flush(self, now: float, grace: float) -> list[Bar]:
        out: list[Bar] = []
        for sym, bars in self.cur.items():
            for m in sorted(bars):
                if m + 60 + grace <= now:
                    o, h, lo, c, v, t, s = bars.pop(m)
                    out.append(Bar(sym, m, o, h, lo, c, v, t, s or "", "longbridge-trades"))
        return sorted(out, key=lambda b: (b.ts, b.symbol))


class LongbridgeFeed:
    name = "longbridge"
    market = "US"

    def __init__(
        self,
        cfg: StocksCfg,
        cal: UsCalendar,
        emit: Callable[[Bar], None],
        seen: Callable[[str, float], None],
        stale_s: Callable[[float], float],
        ops: Callable[[str, str], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
        sdk: Any | None = None,
    ):
        self.cfg = cfg
        self.lb = cfg.longbridge
        self.cal = cal
        self.emit = emit
        self.seen = seen
        self.stale_s = stale_s
        self.ops = ops
        self.clock = clock
        self._sdk = sdk
        self.symbols = [s.upper() for s in cfg.symbols]
        self.ctx: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.last_ts: dict[str, int] = {}
        self.last_push_at: float | None = None
        self.connected = False
        self.reconnects = 0
        self.session_mismatch = 0
        self._buffering = False
        self._buffer: list[Bar] = []
        self._trades = TradeBarBuilder()
        self._naive_tz = ZoneInfo(self.lb.naive_tz)
        self._last_daily: date | None = None

    @property
    def sdk(self) -> Any:
        if self._sdk is None:
            self._sdk = load_sdk()
        return self._sdk

    # ---- 转换 ----
    def to_bar(self, symbol: str, c: Any) -> Bar | None:
        dt: datetime = c.timestamp
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._naive_tz)
        ts = int(dt.timestamp())
        if self.lb.ts_is_close:
            ts -= 60
        ts -= ts % 60
        cal_sess = self.cal.session_at(ts)
        sess = map_session(self.sdk, getattr(c, "trade_session", None)) or cal_sess
        if sess != cal_sess and cal_sess != "CLOSED":
            self.session_mismatch += 1
        if sess == "ON" and not self.cfg.overnight:
            return None
        return Bar(
            symbol=symbol.upper(),
            ts=ts,
            o=float(c.open),
            h=float(c.high),
            l=float(c.low),
            c=float(c.close),
            volume=float(c.volume),
            turnover=float(c.turnover),
            session=sess,
            source="longbridge",
        )

    def _to_naive(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, UTC).astimezone(self._naive_tz).replace(tzinfo=None)

    def _emit(self, bar: Bar) -> None:
        if self._buffering:
            self._buffer.append(bar)
            return
        if bar.ts <= self.last_ts.get(bar.symbol, 0):
            return
        self.last_ts[bar.symbol] = bar.ts
        self.emit(bar)

    # ---- SDK 回调（可能在 SDK 线程里被调用，统一切回事件循环） ----
    def _cb_candle(self, symbol: str, event: Any) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._on_candle, symbol, event)

    def _cb_quote(self, symbol: str, event: Any) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._on_push)

    def _cb_trades(self, symbol: str, event: Any) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._on_trades, symbol, event)

    def _on_push(self) -> None:
        now = self.clock()
        self.last_push_at = now
        self.seen(self.name, now)

    def _on_candle(self, symbol: str, event: Any) -> None:
        self._on_push()
        if not getattr(event, "is_confirmed", True):
            return
        try:
            bar = self.to_bar(symbol, event.candlestick)
        except Exception:  # noqa: BLE001
            log.exception("长桥 K 线解析失败 %s", symbol)
            return
        if bar is not None:
            self._emit(bar)

    def _on_trades(self, symbol: str, event: Any) -> None:
        self._on_push()
        for t in getattr(event, "trades", []) or []:
            if getattr(t, "trade_type", "") in SKIP_TRADE_TYPES:
                continue
            dt: datetime = t.timestamp
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self._naive_tz)
            sess = map_session(self.sdk, getattr(t, "trade_session", None))
            self._trades.add(symbol.upper(), dt.timestamp(), float(t.price), float(t.volume), sess)

    def flush_trades(self, now: float) -> None:
        """bar_source = trades 时由运行时每秒调用。"""
        for b in self._trades.flush(now, self.cfg.grace_s):
            if not b.session:
                b.session = self.cal.session_at(b.ts)
            if b.session == "ON" and not self.cfg.overnight:
                continue
            self._emit(b)

    # ---- 连接 ----
    async def _build_config(self) -> Any:
        sdk = self.sdk
        kwargs = dict(
            enable_overnight=self.cfg.overnight,
            push_candlestick_mode=sdk.PushCandlestickMode.Confirmed,
            enable_print_quote_packages=False,
        )
        if self.lb.auth == "apikey":
            key = os.environ.get("LONGBRIDGE_APP_KEY")
            secret = os.environ.get("LONGBRIDGE_APP_SECRET")
            token = os.environ.get("LONGBRIDGE_ACCESS_TOKEN")
            if not (key and secret and token):
                raise LongbridgeNotConfigured(
                    "缺少 LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN，请写进 secrets.env"
                )
            return sdk.Config.from_apikey(key, secret, token, **kwargs)
        client_id = os.environ.get("LONGBRIDGE_OAUTH_CLIENT_ID")
        if not client_id:
            raise LongbridgeNotConfigured("缺少 LONGBRIDGE_OAUTH_CLIENT_ID，请写进 secrets.env")
        oauth = await sdk.OAuthBuilder(client_id, self.lb.oauth_callback_port).build_async(
            lambda url: log.warning("长桥需要重新授权，请执行 sudo mm lb-login")
        )
        return sdk.Config.from_oauth(oauth, **kwargs)

    async def connect(self) -> None:
        sdk = self.sdk
        self.loop = asyncio.get_running_loop()
        config = await self._build_config()
        ctx = sdk.AsyncQuoteContext.create(config, self.loop)
        self._buffering = True
        self._buffer = []
        try:
            if self.lb.subscribe_quotes:
                ctx.set_on_quote(self._cb_quote)
                await ctx.subscribe(self.symbols, [sdk.SubType.Quote])
            if self.cfg.bar_source == "trades":
                ctx.set_on_trades(self._cb_trades)
                await ctx.subscribe(self.symbols, [sdk.SubType.Trade])
            else:
                ctx.set_on_candlestick(self._cb_candle)
                for sym in self.symbols:
                    await ctx.subscribe_candlesticks(sym, sdk.Period.Min_1, sdk.TradeSessions.All)
            self.ctx = ctx
            missed = await self.backfill_since_last()
        finally:
            self._buffering = False
        for b in sorted(missed + self._buffer, key=lambda x: (x.ts, x.symbol)):
            self._emit(b)
        self._buffer = []
        self.connected = True
        self.last_push_at = self.clock()
        log.info("长桥已连接，订阅 %d 只股票（%s）", len(self.symbols), self.cfg.bar_source)
        await self.daily_checks(force=True)

    async def backfill_since_last(self) -> list[Bar]:
        """短时间断线：candlesticks() 取最近 N 根（≤1000），不依赖翻页参数的语义。"""
        sdk = self.sdk
        now = self.clock()
        end = int(now) - int(now) % 60
        out: list[Bar] = []
        for sym in self.symbols:
            last = self.last_ts.get(sym)
            if last is None or last + 60 >= end:
                continue
            count = min(1000, (end - last) // 60 + 5)
            try:
                rows = await self.ctx.candlesticks(
                    sym, sdk.Period.Min_1, int(count), sdk.AdjustType.NoAdjust, sdk.TradeSessions.All
                )
            except Exception as e:  # noqa: BLE001
                log.warning("长桥补数失败 %s：%s", sym, type(e).__name__)
                continue
            for r in rows:
                b = self.to_bar(sym, r)
                if b is not None and last < b.ts < end:
                    out.append(b)
            if out:
                log.info("长桥补数 %s：%d 根", sym, sum(1 for b in out if b.symbol == sym))
        return out

    async def warmup(
        self,
        insert: Callable[[list[Bar]], Awaitable[Any]],
        first_ts: Callable[[str, int], Awaitable[int | None]],
        days: int,
    ) -> int:
        """往回翻页取历史 1 分钟 K 线（全部时段），直到覆盖最近 days 天。"""
        sdk = self.sdk
        if self.ctx is None:
            self.loop = asyncio.get_running_loop()
            self.ctx = sdk.AsyncQuoteContext.create(await self._build_config(), self.loop)
        now = int(self.clock())
        since = now - days * 86400
        total = 0
        for sym in self.symbols:
            first = await first_ts(sym, since)
            if first is not None and first <= since + 86400:
                continue
            cursor = first if first is not None else now
            pages = 0
            while cursor > since and pages < 200:
                pages += 1
                rows = await self.ctx.history_candlesticks_by_offset(
                    sym,
                    sdk.Period.Min_1,
                    sdk.AdjustType.NoAdjust,
                    False,
                    1000,
                    self._to_naive(cursor),
                    sdk.TradeSessions.All,
                )
                bars = [b for b in (self.to_bar(sym, r) for r in rows) if b is not None and b.ts < cursor]
                if not bars:
                    break
                await insert(bars)
                total += len(bars)
                cursor = min(b.ts for b in bars)
                await asyncio.sleep(self.lb.history_pause_s)
            log.info("长桥预热 %s：%d 页", sym, pages)
        return total

    async def history_15m(self, symbol: str, since: int) -> list[tuple[int, float, float, float, float, float]]:
        """趋势监控用：往回翻页取正常盘 15 分钟 K 线（ts 为开始时间），直到覆盖 since。

        优先只取正常盘（TradeSessions.Intraday）；SDK 没有这个选项时取全部时段，由调用方按日历过滤。
        """
        sdk = self.sdk
        if self.ctx is None:
            self.loop = asyncio.get_running_loop()
            self.ctx = sdk.AsyncQuoteContext.create(await self._build_config(), self.loop)
        sessions = getattr(sdk.TradeSessions, "Intraday", None) or sdk.TradeSessions.All
        out: dict[int, tuple[int, float, float, float, float, float]] = {}
        cursor = int(self.clock())
        pages = 0
        while cursor > since and pages < 60:
            pages += 1
            rows = await self.ctx.history_candlesticks_by_offset(
                symbol, sdk.Period.Min_15, sdk.AdjustType.NoAdjust, False, 1000, self._to_naive(cursor), sessions
            )
            got = []
            for r in rows:
                dt: datetime = r.timestamp
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=self._naive_tz)
                ts = int(dt.timestamp())
                if self.lb.ts_is_close:
                    ts -= 900
                ts -= ts % 60
                if ts < cursor:
                    got.append((ts, float(r.open), float(r.high), float(r.low), float(r.close), float(r.turnover)))
            if not got:
                break
            for g in got:
                out[g[0]] = g
            cursor = min(g[0] for g in got)
            await asyncio.sleep(self.lb.history_pause_s)
        return [out[k] for k in sorted(out)]

    async def daily_checks(self, force: bool = False) -> None:
        today = datetime.fromtimestamp(self.clock(), ZoneInfo("America/New_York")).date()
        if not force and self._last_daily == today:
            return
        self._last_daily = today
        sdk = self.sdk
        if self.lb.refresh_calendar:
            try:
                end = today + timedelta(days=28)
                resp = await self.ctx.trading_days(sdk.Market.US, today, end)
                changed = self.cal.update_from_exchange(
                    today, end, set(resp.trading_days), set(resp.half_trading_days)
                )
                if changed:
                    log.info("按长桥交易日历更新了 %d 天", changed)
            except Exception as e:  # noqa: BLE001
                log.warning("刷新交易日历失败：%s", type(e).__name__)
        if self.cfg.overnight and self.lb.check_overnight_list:
            try:
                secs = await self.ctx.security_list(sdk.Market.US, sdk.SecurityListCategory.Overnight)
                names = {str(s.symbol).upper().removesuffix(".US") for s in secs}
                missing = [s for s in self.symbols if s.removesuffix(".US") not in names]
                if missing and self.ops is not None:
                    await self.ops(
                        f"overnight-missing:{today.isoformat()}",
                        "这些股票不在长桥夜盘名单里，夜盘不会有数据：" + "、".join(missing),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("查询夜盘名单失败：%s", type(e).__name__)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 5.0
        while not stop.is_set():
            try:
                await self.connect()
                backoff = 5.0
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), 5)
                    except TimeoutError:
                        pass
                    now = self.clock()
                    if self.cfg.bar_source == "trades":
                        self.flush_trades(now)
                    if self.cal.session_at(now - 60) == "CLOSED":
                        self.last_push_at = now  # 休市时没有推送是正常的
                        continue
                    if self.last_push_at is not None and now - self.last_push_at > self.stale_s(now):
                        log.warning("长桥 %.0f 秒没有推送，重建连接", now - self.last_push_at)
                        break
                    await self.daily_checks()
            except asyncio.CancelledError:
                raise
            except LongbridgeNotConfigured as e:
                log.error("长桥未配置：%s", e)
                if self.ops is not None:
                    day = datetime.fromtimestamp(self.clock(), UTC).date().isoformat()
                    await self.ops(f"lb-not-configured:{day}", f"长桥行情未启动：{e}")
                try:
                    await asyncio.wait_for(stop.wait(), 3600)
                except TimeoutError:
                    pass
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("长桥连接出错：%s %s", type(e).__name__, str(e)[:200])
            self.connected = False
            self.ctx = None
            if stop.is_set():
                break
            self.reconnects += 1
            try:
                await asyncio.wait_for(stop.wait(), backoff)
            except TimeoutError:
                pass
            backoff = min(120.0, backoff * 2)
