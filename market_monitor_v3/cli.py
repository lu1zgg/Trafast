"""命令行入口：monitor <子命令>。"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import AppConfig, Params, Secrets, load_config
from .models import Bar, OutMsg, display_name

UTC = UTC
DEFAULT_CONFIG = "/etc/market-monitor/config.toml"


def _cfg(args: argparse.Namespace) -> AppConfig:
    return load_config(args.config)


def _db_missing(db: Path) -> bool:
    if Path(db).exists():
        return False
    print(f"数据库 {db} 还不存在：服务还没运行过。先 systemctl start market-monitor，或 monitor backfill-crypto。", file=sys.stderr)
    return True


def _parse_day(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


# ---- run / check-config ----
def cmd_run(args: argparse.Namespace) -> int:
    from .app import run_service

    return run_service(args.config)


def cmd_check_config(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    p = Params(cfg)
    print(f"配置有效。rule_version = {p.rule_version}")
    print(f"数据库：{cfg.db_path}")
    print(f"美股：{'启用' if cfg.stocks.enabled else '关闭'} {', '.join(cfg.stocks.symbols)}（夜盘 {'开' if cfg.stocks.overnight else '关'}）")
    print(f"加密：{'启用' if cfg.crypto.enabled else '关闭'} {', '.join(cfg.crypto.symbols)}")
    print("时段          地板 1/5/15 分钟          Z    直通 1/5/15 分钟")
    for a, b, c, d in p.table():
        print(f"{a:<6}  {b:<24}  {c:<3}  {d}")
    s = Secrets.from_env()
    print(f"TELEGRAM_BOT_TOKEN：{'已设置' if s.telegram_token else '未设置'}；HEARTBEAT_URL：{'已设置' if s.heartbeat_url else '未设置'}")
    if cfg.stocks.enabled:
        keys = ["LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"]
        if cfg.stocks.longbridge.auth == "oauth":
            keys = ["LONGBRIDGE_OAUTH_CLIENT_ID"]
        print("长桥凭据：" + "，".join(f"{k} {'已设置' if os.environ.get(k) else '未设置'}" for k in keys))
    return 0


# ---- 回放 ----
def _load_bars(db: Path, symbols: list[str], since: int, until: int) -> list[Bar]:
    from .store import bar_from_row, connect

    conn = connect(db, readonly=True)
    try:
        out: list[Bar] = []
        for sym in symbols:
            rows = conn.execute(
                "SELECT * FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts", (sym, since, until)
            ).fetchall()
            out.extend(bar_from_row(r) for r in rows)
        return out
    finally:
        conn.close()


def _read_csv(path: str) -> list[Bar]:
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            ts = int(float(r["ts"]))
            if ts > 10**11:
                ts //= 1000
            out.append(
                Bar(
                    symbol=r["symbol"].upper(),
                    ts=ts - ts % 60,
                    o=float(r.get("o") or r["c"]),
                    h=float(r.get("h") or r["c"]),
                    l=float(r.get("l") or r["c"]),
                    c=float(r["c"]),
                    volume=float(r.get("volume") or 0),
                    turnover=float(r.get("turnover") or 0),
                    session=r.get("session") or "",
                    source="csv",
                    synthetic=str(r.get("synthetic", "0")) in ("1", "true", "True"),
                )
            )
    return out


def cmd_replay(args: argparse.Namespace) -> int:
    from .app import make_calendar
    from .render import to_plain
    from .replay import run_replay

    cfg = _cfg(args)
    if args.preset:
        cfg = cfg.model_copy(update={"rules": cfg.rules.model_copy(update={"preset": args.preset})})
    params = Params(cfg)
    cal = make_calendar(cfg)
    now = int(time.time())
    end = _parse_day(args.to) if args.to else now - now % 60
    start = _parse_day(args.start) if args.start else end - args.days * 86400
    warm = max(cfg.rules.crypto_baseline_days, cfg.stocks.longbridge.warmup_days) * 86400
    if args.csv:
        bars = []
        for p in args.csv:
            bars.extend(_read_csv(p))
    else:
        db = Path(args.db) if args.db else cfg.db_path
        if _db_missing(db):
            return 1
        bars = _load_bars(db, cfg.all_symbols(), start - warm, end)
    if not bars:
        print("没有 K 线数据。先用 monitor backfill-crypto 拉历史，或指定 --csv。", file=sys.stderr)
        return 1
    t0 = time.time()
    res = run_replay(cfg, params, cal, bars, start, end, progress=(lambda d: print(f"  … {d}", file=sys.stderr)) if args.verbose else None)
    print(res.summary())
    print(f"（耗时 {time.time() - t0:.1f} 秒，{len(bars)} 根 K 线）")
    if args.show:
        for m in res.outs:
            if m.kind == "edit":
                continue
            tag = "首发" if m.kind == "send" else "回复"
            print(f"\n[{tag}] {to_plain(m.text)}")
    if args.json:
        Path(args.json).write_text(json.dumps(res.golden(), ensure_ascii=False, indent=1))
        print(f"已写出 {args.json}")
    return 0


# ---- 补数 ----
def cmd_backfill_crypto(args: argparse.Namespace) -> int:
    from .feeds.binance_spot import BinanceSpotFeed
    from .store import Store

    cfg = _cfg(args)

    async def main() -> int:
        store = Store(cfg.db_path)
        await store.open()
        feed = BinanceSpotFeed(cfg.crypto, lambda b: None, lambda n, t: None)
        try:
            n = await feed.warmup(
                lambda bars: store.call(Store.insert_bars, bars),
                lambda sym, since: store.call(Store.first_bar_ts, sym, since),
                lambda sym: store.call(Store.last_bar_ts, sym),
                args.days,
            )
        finally:
            await feed.http.aclose()
            await store.close()
        print(f"写入 {n} 根 K 线到 {cfg.db_path}")
        return 0

    return asyncio.run(main())


def cmd_import_csv(args: argparse.Namespace) -> int:
    from .store import Store

    cfg = _cfg(args)
    bars = []
    for p in args.files:
        bars.extend(_read_csv(p))
    st = Store(cfg.db_path)
    st.open_sync()
    st.insert_bars(bars)
    st.close_sync()
    print(f"导入 {len(bars)} 根 K 线到 {cfg.db_path}")
    return 0


# ---- 运维 ----
def cmd_ctl(args: argparse.Namespace) -> int:
    from .bot import parse_duration
    from .store import Store, connect

    cfg = _cfg(args)
    now = time.time()
    if args.action == "status":
        if _db_missing(cfg.db_path):
            return 1
        conn = connect(cfg.db_path, readonly=True)
        print(f"数据库 {cfg.db_path}")
        for sym in cfg.all_symbols():
            r = conn.execute("SELECT MAX(ts) AS t FROM bars_1m WHERE symbol=? AND synthetic=0", (sym,)).fetchone()
            age = f"{(now - r['t'] - 60) / 60:.1f} 分钟前" if r["t"] else "无"
            print(f"  {display_name(sym):<6} 最新真实 K 线：{age}")
        eps = conn.execute("SELECT id, data FROM episodes WHERE state='active'").fetchall()
        print(f"进行中的事件：{len(eps)}" + ("（" + "、".join(e["id"] for e in eps) + "）" if eps else ""))
        r = conn.execute("SELECT COUNT(*) AS n FROM outbox WHERE status='pending'").fetchone()
        print(f"待发消息：{r['n']}")
        for r in conn.execute("SELECT target, until FROM mutes WHERE until>?", (now,)):
            print(f"静音：{r['target']} 至 {datetime.fromtimestamp(r['until']).strftime('%m-%d %H:%M')}")
        print("最近事件：")
        for r in conn.execute("SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT 8"):
            print(f"  {datetime.fromtimestamp(r['ts']).strftime('%m-%d %H:%M:%S')} {r['kind']} {r['detail'] or ''}")
        conn.close()
        return 0
    st = Store(cfg.db_path)
    st.open_sync()
    try:
        target = args.target.upper()
        if target in ("ALL", "*"):
            target = "*"
        else:
            syms = cfg.all_symbols()
            target = next((c for c in (target, target + ".US", target + "USDT") if c in syms), "")
            if not target:
                print(f"没找到标的 {args.target}", file=sys.stderr)
                return 1
        if args.action == "mute":
            dur = parse_duration(args.duration)
            if dur is None:
                print("时长格式不对，例如 30m、2h、1d", file=sys.stderr)
                return 1
            st.mute_set(target, now + dur, "cli")
            print(f"已静音 {target}，{dur // 60} 分钟（运行中的进程 10 秒内生效）")
        else:
            n = st.mute_clear(None if target == "*" else target)
            print("已取消静音" if n else "没有对应的静音")
    finally:
        st.close_sync()
    return 0


def cmd_test_telegram(args: argparse.Namespace) -> int:
    from .telegram import TelegramClient

    cfg = _cfg(args)
    s = Secrets.from_env()
    if not s.telegram_token:
        print("缺少 TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 1

    async def main() -> int:
        tg = TelegramClient(s.telegram_token, cfg.telegram.api_base)
        try:
            me = await tg.call("getMe")
            print("getMe：", "OK @" + me.result.get("username", "") if me.ok else f"{me.kind} {me.description}")
            chat = await tg.call("getChat", {"chat_id": cfg.telegram.chat_id})
            print("getChat：", (chat.result.get("title") or "OK") if chat.ok else f"{chat.kind} {chat.description}")
            r = await tg.call(
                "sendMessage",
                {"chat_id": cfg.telegram.chat_id, "text": "✅ V3 测试消息：Telegram 配置正常。", "disable_notification": True},
            )
            print("sendMessage：", "OK" if r.ok else f"{r.kind} {r.description}")
            return 0 if r.ok else 1
        finally:
            await tg.aclose()

    return asyncio.run(main())


def cmd_report(args: argparse.Namespace) -> int:
    from .app import make_calendar
    from .render import to_plain
    from .reporting import build_daily_report
    from .store import Store

    cfg = _cfg(args)
    st = Store(cfg.db_path)
    st.open_sync()
    try:
        now = time.time()
        text = build_daily_report(st, cfg, Params(cfg), make_calendar(cfg), now - 86400, now)
        print(to_plain(text))
        if args.send:
            st.enqueue(OutMsg(key=f"report:manual:{int(now)}", kind="ops", text=text, created_at=now, expires_at=now + 3600))
            print("已放进发件箱，运行中的服务会发出。")
    finally:
        st.close_sync()
    return 0


# ---- 长桥 ----
def cmd_lb_check(args: argparse.Namespace) -> int:
    """接通长桥时逐项核对：凭据、夜盘名单、K 线时间戳与时段、推送是否覆盖盘前/盘后/夜盘。"""
    from .app import make_calendar
    from .feeds.longbridge import LongbridgeFeed, load_sdk, map_session

    cfg = _cfg(args)
    cal = make_calendar(cfg)
    sdk = load_sdk()
    sym = (args.symbol or cfg.stocks.symbols[0]).upper()

    async def main() -> int:
        pushes: list[tuple[float, Bar | None, object]] = []
        feed = LongbridgeFeed(cfg.stocks, cal, lambda b: None, lambda n, t: None, stale_s=lambda now: 300)
        config = await feed._build_config()
        loop = asyncio.get_running_loop()
        ctx = sdk.AsyncQuoteContext.create(config, loop)
        print("① 连接与权限")
        try:
            print("   行情等级：", await ctx.quote_level())
        except Exception as e:  # noqa: BLE001
            print("   读取行情等级失败：", type(e).__name__, e)
        print("② 夜盘名单")
        try:
            secs = await ctx.security_list(sdk.Market.US, sdk.SecurityListCategory.Overnight)
            names = {str(s.symbol).upper().removesuffix(".US") for s in secs}
            for s in cfg.stocks.symbols:
                print(f"   {s:<10} {'✅ 在名单里' if s.removesuffix('.US') in names else '❌ 不在名单里'}")
        except Exception as e:  # noqa: BLE001
            print("   查询失败：", type(e).__name__, e)
        print("③ 今天的交易时段（长桥返回）")
        try:
            for m in await ctx.trading_session():
                if "US" in str(m.market):
                    for t in m.trade_sessions:
                        print(f"   {t.trade_session}  {t.begin_time}–{t.end_time}")
        except Exception as e:  # noqa: BLE001
            print("   查询失败：", type(e).__name__, e)
        print(f"④ {sym} 最近 10 根 1 分钟 K 线（全部时段）")
        rows = await ctx.candlesticks(sym, sdk.Period.Min_1, 10, sdk.AdjustType.NoAdjust, sdk.TradeSessions.All)
        for r in rows:
            b = feed.to_bar(sym, r)
            ts_raw = r.timestamp
            print(
                f"   原始时间 {ts_raw!r}  时段 {r.trade_session} → {map_session(sdk, r.trade_session)}  "
                f"按本地日历 {cal.session_at(b.ts) if b else '-'}  收 {r.close}"
            )
        print(f"   本机现在 UTC {datetime.now(UTC).strftime('%H:%M:%S')}。如果最后一根的时间比现在早 1 分钟左右，"
              "时间戳是开盘时间（ts_is_close = false）；早不到 1 分钟则可能是收盘时间。")
        print(f"⑤ 订阅推送 {args.listen} 秒（confirmed 模式，观察到达延迟和时段）")

        def on_candle(symbol: str, ev: object) -> None:
            loop.call_soon_threadsafe(lambda: pushes.append((time.time(), feed.to_bar(symbol, ev.candlestick), ev)))  # type: ignore[attr-defined]

        ctx.set_on_candlestick(on_candle)
        for s in cfg.stocks.symbols:
            await ctx.subscribe_candlesticks(s, sdk.Period.Min_1, sdk.TradeSessions.All)
        await asyncio.sleep(args.listen)
        for t, b, ev in pushes:
            if b is None:
                continue
            delay = t - b.close_ts
            print(f"   {b.symbol:<9} {datetime.fromtimestamp(b.ts, UTC).strftime('%H:%M')} UTC 时段 {b.session} "
                  f"confirmed={getattr(ev, 'is_confirmed', '?')} 收盘后 {delay:+.1f} 秒到达")
        sess_now = cal.session_at(time.time())
        print(f"   共收到 {len(pushes)} 条推送；当前时段（本地日历）：{sess_now}")
        if not pushes and sess_now != "CLOSED":
            print("   ⚠️ 交易时段内没收到 K 线推送：可以试 config 里 bar_source = \"trades\"（用逐笔成交合成 K 线）。")
        return 0

    return asyncio.run(main())


def cmd_lb_login(args: argparse.Namespace) -> int:
    from .feeds.longbridge import load_sdk

    sdk = load_sdk()
    cfg = _cfg(args)
    client_id = os.environ.get("LONGBRIDGE_OAUTH_CLIENT_ID")
    if not client_id:
        print("缺少 LONGBRIDGE_OAUTH_CLIENT_ID", file=sys.stderr)
        return 1

    async def main() -> int:
        await sdk.OAuthBuilder(client_id, cfg.stocks.longbridge.oauth_callback_port).build_async(
            lambda url: print("在浏览器打开（需要 SSH 端口转发，见 README）：\n" + url)
        )
        print("授权完成，token 已缓存在 ~/.longbridge/openapi/tokens/")
        return 0

    return asyncio.run(main())


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="monitor", description="异动监控 V3")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:  # noqa: ANN001
        p = sub.add_parser(name, help=help_)
        p.add_argument("--config", default=os.environ.get("MONITOR_CONFIG", DEFAULT_CONFIG))
        p.set_defaults(fn=fn)
        return p

    add("run", cmd_run, "运行服务")
    add("check-config", cmd_check_config, "校验配置并打印门槛表")
    p = add("replay", cmd_replay, "用历史 K 线回放，统计会发多少条消息")
    p.add_argument("--db")
    p.add_argument("--csv", nargs="*")
    p.add_argument("--from", dest="start")
    p.add_argument("--to")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--preset", choices=["sensitive", "balanced", "conservative"])
    p.add_argument("--show", action="store_true", help="打印每条消息")
    p.add_argument("--json", help="把消息清单写成 JSON（golden 测试用）")
    p.add_argument("-v", "--verbose", action="store_true")
    p = add("backfill-crypto", cmd_backfill_crypto, "从 Binance REST 拉加密货币历史 K 线")
    p.add_argument("--days", type=int, default=14)
    p = add("import-csv", cmd_import_csv, "导入 CSV K 线（symbol,ts,o,h,l,c,volume,turnover[,session]）")
    p.add_argument("files", nargs="+")
    p = add("ctl", cmd_ctl, "status / mute / unmute")
    p.add_argument("action", choices=["status", "mute", "unmute"])
    p.add_argument("target", nargs="?", default="")
    p.add_argument("duration", nargs="?", default=None)
    add("test-telegram", cmd_test_telegram, "发一条测试消息")
    p = add("report", cmd_report, "打印最近 24 小时日报")
    p.add_argument("--send", action="store_true")
    p = add("lb-check", cmd_lb_check, "接通长桥时的逐项检查")
    p.add_argument("--symbol")
    p.add_argument("--listen", type=int, default=150)
    add("lb-login", cmd_lb_login, "长桥 OAuth 授权（只在 auth = oauth 时需要）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "ctl" and args.action in ("mute", "unmute") and not args.target:
        print("用法：monitor ctl mute 代码 [时长] / monitor ctl unmute 代码|all", file=sys.stderr)
        return 1
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())



