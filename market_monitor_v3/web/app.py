"""网页管理后台（FastAPI + Jinja2，服务端渲染，没有前端构建步骤）。

单独的 systemd 服务 market-monitor-web，只听 127.0.0.1，由 Caddy 反向代理出 HTTPS。
改配置 = 改 config.toml（保留注释）→ 通过本机控制通道让监控进程重载；需要重启的变化由监控自己退出、systemd 拉起。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import tomlkit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from tomlkit.items import Table

from .. import __version__, render
from ..config import CRYPTO_SESSIONS, PRESETS, SESSION_LABELS, STOCK_SESSIONS, AppConfig, Params
from ..control import ControlUnavailable, control_call
from ..models import display_name
from ..notifiers.discord import DiscordClient, DiscordNotifier
from ..telegram import TelegramClient
from ..tradingview import MAX_BODY, parse_alert
from ..tradingview import card_text as tv_card_text
from .auth import LoginGuard, Sessions, load_account, same
from .files import ConfigFile, SecretsFile, StaleFile, digest

HERE = Path(__file__).parent
BJ = ZoneInfo("Asia/Shanghai")
COOKIE = "mm_session"
DISCORD_PERMS = 1024 + 2048 + 16384 + 65536  # 查看频道、发消息、嵌入链接、读消息历史

NAV = [
    ("/", "概览"),
    ("/symbols", "标的"),
    ("/rules", "规则"),
    ("/trend", "趋势"),
    ("/channels", "推送渠道"),
    ("/tradingview", "TradingView"),
    ("/system", "系统"),
    ("/history", "历史与审计"),
]
SESSION_ORDER = [*STOCK_SESSIONS, *CRYPTO_SESSIONS]
AUDIT_LABELS = {
    "login": "登录",
    "logout": "退出",
    "config": "改配置",
    "secret": "改密钥",
    "restart": "重启监控",
    "rollback": "恢复配置",
    "mute": "静音",
    "unmute": "取消静音",
    "test": "测试推送",
}


@dataclass
class WebPaths:
    config: Path
    secrets: Path
    auth: Path
    history: Path
    audit: Path


class Audit:
    def __init__(self, path: Path):
        self.path = path

    def log(self, who: str, ip: str, action: str, detail: str = "") -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({"ts": time.time(), "who": who, "ip": ip, "action": action, "detail": detail[:500]}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def tail(self, n: int = 200) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text().splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, BJ).strftime("%m-%d %H:%M:%S")


def _dur(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 90:
        return f"{s:.0f} 秒"
    if s < 5400:
        return f"{s / 60:.0f} 分钟"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} 小时"
    return f"{s / 86400:.1f} 天"


def _ago(s: float | None) -> str:
    if s is None:
        return "尚无数据"
    if s < 90:
        return f"{s:.0f} 秒前"
    if s < 5400:
        return f"{s / 60:.0f} 分钟前"
    return f"{s / 3600:.1f} 小时前"


def _nums(xs: Any) -> str:
    """(0.6, 1.2, 1.8) -> "0.6, 1.2, 1.8"（页面上显示覆盖值）。"""
    return ", ".join(f"{x:g}" for x in xs) if xs else ""


def _pcts(xs: Any) -> str:
    """小数门槛 -> 百分数文字：(0.004, 0.008) -> "0.4 / 0.8"。"""
    return " / ".join(f"{x * 100:.4g}" for x in xs)


def _tbl(doc: Any, *path: str) -> Table:
    t = doc
    for key in path:
        if key not in t:
            t[key] = tomlkit.table()
        t = t[key]
    return t


def _floats(raw: str, n: int) -> list[float] | None:
    parts = [p for p in re.split(r"[\s,，/]+", raw.strip()) if p]
    if not parts:
        return None
    if len(parts) != n:
        raise ValueError(f"需要 {n} 个数，用逗号或斜杠分开")
    return [float(p) for p in parts]


def _ints(raw: str) -> list[int]:
    return [int(p) for p in re.split(r"[\s,，]+", raw.strip()) if p]


def _bool(form: Any, name: str) -> bool:
    return form.get(name) in ("on", "true", "1", "yes")


def _num(form: Any, name: str, cast: type = float) -> Any:
    v = str(form.get(name, "")).strip()
    if v == "":
        raise ValueError(f"{name} 不能为空")
    return cast(v)


def normalize_symbol(market: str, raw: str) -> str:
    s = re.sub(r"\s+", "", raw).upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,20}", s):
        raise ValueError("代码只能包含字母、数字、点和横线")
    if market == "US":
        return s if s.endswith(".US") else s + ".US"
    if s.endswith(".US"):
        raise ValueError("加密货币代码不能以 .US 结尾")
    return s if s.endswith(("USDT", "USDC", "FDUSD")) else s + "USDT"


def sample_cards() -> str:
    """示例告警（测试推送和页面预览用）。"""
    now = int(time.time()) // 60 * 60
    base = {
        "ep": "demo",
        "market": "US",
        "direction": -1,
        "level": "L3",
        "session": "PRE",
        "k": 1,
        "retained": None,
        "close_ts": now,
        "end": None,
    }
    lead = {**base, "symbol": "NVDA.US", "d": math.log(134.03 / 136.66), "price": 134.03, "p0": 136.66,
            "hits": [[1, math.log(0.981)], [5, math.log(0.976)], [15, math.log(0.973)]],
            "turnover": 3_620_000, "turnover_ratio": 2.1}
    other = {**base, "symbol": "INTC.US", "level": "L2", "d": math.log(0.982), "price": 21.37, "p0": 21.76,
             "hits": [[1, math.log(0.982)], [5, math.log(0.979)]], "turnover": 845_300, "turnover_ratio": 1.6,
             "retained": 0.74}
    return "🧪 示例告警（不是真实行情）\n" + render.cards_text([lead, other])


def create_app(paths: WebPaths, control_socket: str | None = None, fail_delay_s: float = 1.0) -> FastAPI:
    cfg_file = ConfigFile(paths.config, paths.history)
    sec = SecretsFile(paths.secrets)
    audit = Audit(paths.audit)
    try:
        _, _, boot_cfg = cfg_file.read()
    except Exception:  # noqa: BLE001
        boot_cfg = None
    webcfg = boot_cfg.web if boot_cfg else AppConfig.model_fields["web"].default_factory()  # type: ignore[misc]
    sock = control_socket or (boot_cfg.general.control_socket if boot_cfg else "/run/market-monitor/control.sock")
    sessions = Sessions(webcfg.session_hours * 3600)
    guard = LoginGuard(webcfg.login_max_failures, webcfg.login_lock_min * 60)
    totp_used: dict[str, str] = {}
    tv_hits: dict[str, list[float]] = {}

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="Trafast")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(
        nav=NAV, version=__version__, fmt_ts=_fmt_ts, ago=_ago, dur=_dur, labels=SESSION_LABELS, name=display_name, nums=_nums, pcts=_pcts,
    )
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ---- 通用 ----
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        # same-origin 而不是 no-referrer：后者会让浏览器在同源表单 POST 里也发 Origin: null
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def current(request: Request) -> Any:
        return sessions.get(request.cookies.get(COOKIE))

    def need_login(request: Request) -> Any:
        s = current(request)
        if s is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return s

    async def form_checked(request: Request, s: Any) -> Any:
        form = await request.form()
        origin = request.headers.get("origin")
        # 主要防线是 CSRF 令牌 + SameSite=Strict；Origin 只是多一层（"null" 交给令牌判断）
        if origin and origin != "null" and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "来源不符")
        if not same(str(form.get("csrf") or ""), s.csrf):
            raise HTTPException(403, "表单已失效，请刷新页面后重试")
        return form

    def flash(s: Any, text: str, kind: str = "ok") -> None:
        if not hasattr(s, "flash"):
            s.flash = []
        s.flash.append((kind, text))

    def page(request: Request, s: Any, tpl: str, **ctx: Any) -> HTMLResponse:
        msgs = getattr(s, "flash", []) if s else []
        if s is not None:
            s.flash = []
        return templates.TemplateResponse(
            request, tpl, {"s": s, "msgs": msgs, "path": request.url.path, **ctx}
        )

    async def monitor(cmd: str, **args: Any) -> dict[str, Any] | None:
        try:
            return await control_call(sock, cmd, **args)
        except ControlUnavailable:
            return None

    async def apply(s: Any, request: Request, form: Any, mutate: Any, what: str) -> None:
        """保存 config.toml 并让监控重载。"""
        try:
            saved = cfg_file.save(mutate, str(form.get("rev") or "") or None, s.username)
        except StaleFile as e:
            flash(s, str(e), "err")
            return
        except ValidationError as e:
            errs = "；".join(f"{'.'.join(str(x) for x in er['loc'])}：{er['msg']}" for er in e.errors()[:5])
            flash(s, f"没有保存：{errs}", "err")
            return
        except (ValueError, TypeError) as e:
            flash(s, f"没有保存：{e}", "err")
            return
        if not saved.changed:
            flash(s, "没有变化")
            return
        audit.log(s.username, client_ip(request), "config", f"{what}；旧版本 {saved.history_file}")
        res = await monitor("reload", auto_restart=True)
        if res is None:
            flash(s, f"{what}：已保存。监控进程没有运行，下次启动时生效。", "warn")
        elif not res.get("ok"):
            flash(s, f"{what}：已保存，但监控重载失败：{res.get('error')}", "err")
        elif res.get("restart"):
            flash(s, f"{what}：已保存。{'、'.join(res.get('restart_reasons') or [])}有变化，监控正在自动重启（约 15 秒）。")
        else:
            flash(s, f"{what}：已保存并生效（规则版本 {res.get('rule_version')}）。")

    async def restart_monitor(s: Any, request: Request, why: str) -> None:
        res = await monitor("restart")
        audit.log(s.username, client_ip(request), "restart", why)
        if res is None:
            flash(s, f"{why}。监控进程没有运行，下次启动时生效。", "warn")
        else:
            flash(s, f"{why}。监控正在重启（约 15 秒）。")

    def load() -> tuple[str, Any, AppConfig]:
        return cfg_file.read()

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 303 and exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    # ---- 登录 ----
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if current(request):
            return RedirectResponse("/", status_code=303)
        return page(request, None, "login.html", ready=load_account(paths.auth) is not None, error=None)

    @app.post("/login")
    async def login(request: Request) -> Response:
        ip = client_ip(request)
        now = time.time()
        acct = load_account(paths.auth)
        form = await request.form()
        wait = guard.locked(ip, now)
        if wait > 0:
            return page(request, None, "login.html", ready=acct is not None, error=f"尝试次数过多，{math.ceil(wait / 60)} 分钟后再试")
        user, pw, code = str(form.get("username") or ""), str(form.get("password") or ""), str(form.get("code") or "")
        ok = (
            acct is not None
            and same(user, acct.username)
            and acct.check_password(pw)
            and acct.check_totp(code, totp_used.get(acct.username))
        )
        if not ok:
            locked = guard.fail(ip, now)
            audit.log(user[:40] or "?", ip, "login-fail", "锁定" if locked else "")
            if locked:
                await monitor("ops", text=f"后台登录连续失败，已锁定 {webcfg.login_lock_min} 分钟（来源 {ip}）")
            await asyncio.sleep(fail_delay_s)
            return page(request, None, "login.html", ready=acct is not None, error="用户名、密码或验证码不对")
        assert acct is not None
        guard.success(ip)
        totp_used[acct.username] = code.strip()
        s = sessions.create(acct.username, ip, now)
        audit.log(acct.username, ip, "login")
        await monitor("ops", text=f"{acct.username} 登录了后台（来源 {ip}）")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, s.token, httponly=True, secure=webcfg.cookie_secure, samesite="strict", max_age=int(sessions.ttl_s), path="/")
        return resp

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        s = need_login(request)
        await form_checked(request, s)
        sessions.drop(s.token)
        audit.log(s.username, client_ip(request), "logout")
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---- 概览 ----
    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> Response:
        s = need_login(request)
        st = await monitor("status")
        _, _, cfg = load()
        return page(request, s, "overview.html", st=st, cfg=cfg, now=time.time())

    @app.post("/mute")
    async def mute(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        target = str(form.get("target") or "").strip()
        seconds = int(form.get("seconds") or 3600)
        res = await monitor("mute", target=target, seconds=seconds)
        if res and res.get("ok"):
            audit.log(s.username, client_ip(request), "mute", f"{target} {seconds}s")
            flash(s, f"已静音 {res.get('target')}，{seconds // 60} 分钟")
        else:
            flash(s, f"静音失败：{(res or {}).get('error', '监控进程没有运行')}", "err")
        return RedirectResponse("/", status_code=303)

    @app.post("/unmute")
    async def unmute(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        target = str(form.get("target") or "*")
        res = await monitor("unmute", target=target)
        if res and res.get("ok"):
            audit.log(s.username, client_ip(request), "unmute", target)
            flash(s, "已取消静音" if res.get("cleared") else "没有对应的静音")
        else:
            flash(s, "监控进程没有运行", "err")
        return RedirectResponse("/", status_code=303)

    # ---- 标的 ----
    @app.get("/symbols", response_class=HTMLResponse)
    async def symbols_page(request: Request) -> Response:
        s = need_login(request)
        text, _, cfg = load()
        return page(request, s, "symbols.html", cfg=cfg, rev=digest(text))

    async def binance_symbol_exists(cfg: AppConfig, symbol: str) -> bool | None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{cfg.crypto.rest_url.rstrip('/')}/api/v3/exchangeInfo", params={"symbol": symbol})
        except httpx.HTTPError:
            return None
        if r.status_code == 200:
            return True
        if r.status_code == 400:
            return False
        return None

    @app.post("/symbols/add")
    async def symbols_add(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        market = "US" if form.get("market") == "US" else "CRYPTO"
        try:
            sym = normalize_symbol(market, str(form.get("symbol") or ""))
        except ValueError as e:
            flash(s, str(e), "err")
            return RedirectResponse("/symbols", status_code=303)
        _, _, cfg = load()
        if market == "CRYPTO":
            exists = await binance_symbol_exists(cfg, sym)
            if exists is False:
                flash(s, f"Binance 现货没有 {sym} 这个交易对", "err")
                return RedirectResponse("/symbols", status_code=303)
            if exists is None:
                flash(s, f"暂时连不上 Binance，没能核对 {sym} 是否存在，先按你填的保存", "warn")

        def mutate(doc: Any) -> None:
            t = _tbl(doc, "stocks" if market == "US" else "crypto")
            cur = [str(x) for x in t.get("symbols", [])]
            if sym in cur:
                raise ValueError(f"{sym} 已经在列表里")
            arr = tomlkit.array()
            arr.extend([*cur, sym])
            t["symbols"] = arr

        await apply(s, request, form, mutate, f"添加标的 {sym}")
        return RedirectResponse("/symbols", status_code=303)

    @app.post("/symbols/remove")
    async def symbols_remove(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        sym = str(form.get("symbol") or "")
        market = "stocks" if sym.endswith(".US") else "crypto"

        def mutate(doc: Any) -> None:
            t = _tbl(doc, market)
            cur = [str(x) for x in t.get("symbols", [])]
            if sym not in cur:
                raise ValueError(f"{sym} 不在列表里")
            arr = tomlkit.array()
            arr.extend([x for x in cur if x != sym])
            t["symbols"] = arr
            mult = t.get("multipliers")
            if mult is not None and sym in mult:
                del mult[sym]

        await apply(s, request, form, mutate, f"删除标的 {sym}")
        return RedirectResponse("/symbols", status_code=303)

    @app.post("/symbols/settings")
    async def symbols_settings(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        _, _, cfg = load()

        def mutate(doc: Any) -> None:
            for market, key in (("stocks", cfg.stocks), ("crypto", cfg.crypto)):
                t = _tbl(doc, market)
                t["enabled"] = _bool(form, f"{market}_enabled")
                wanted: dict[str, float] = {}
                for sym in key.symbols:
                    raw = str(form.get(f"mult_{sym}") or "").strip()
                    if raw and float(raw) != 1.0:
                        v = float(raw)
                        if not 0.2 <= v <= 5:
                            raise ValueError(f"{sym} 的系数要在 0.2 到 5 之间")
                        wanted[sym] = v
                # 就地改，保留表上的注释
                mult = t.get("multipliers")
                if mult is None:
                    mult = t["multipliers"] = tomlkit.table()
                for k in [k for k in mult if k not in wanted]:
                    del mult[k]
                for k, v in wanted.items():
                    if mult.get(k) != v:
                        mult[k] = v
            _tbl(doc, "stocks")["overnight"] = _bool(form, "overnight")

        await apply(s, request, form, mutate, "标的设置")
        return RedirectResponse("/symbols", status_code=303)

    # ---- 规则 ----
    @app.get("/rules", response_class=HTMLResponse)
    async def rules_page(request: Request) -> Response:
        s = need_login(request)
        text, _, cfg = load()
        params = Params(cfg)
        # 不带覆盖项的门槛，作为输入框的占位提示
        base = Params(cfg.model_copy(update={"rules": cfg.rules.model_copy(update={"overrides": {}})}))
        return page(
            request, s, "rules.html", cfg=cfg, rev=digest(text), params=params, base=base, presets=PRESETS,
            sessions=SESSION_ORDER, stock_sessions=STOCK_SESSIONS,
        )

    @app.post("/rules")
    async def rules_save(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)

        def mutate(doc: Any) -> None:
            r = _tbl(doc, "rules")
            preset = str(form.get("preset"))
            if preset not in PRESETS:
                raise ValueError("未知档位")
            r["preset"] = preset
            for name in ("confirm_ratio", "fast_score", "l2_score", "l2_turnover_mult", "thin_turnover_frac"):
                r[name] = _num(form, name)
            r["thin_pre_until"] = str(form.get("thin_pre_until") or "07:00").strip()
            overrides = tomlkit.table(is_super_table=True)
            for sess in SESSION_ORDER:
                ov = tomlkit.table()
                floor = _floats(str(form.get(f"{sess}_floor") or ""), 3)
                bypass = _floats(str(form.get(f"{sess}_bypass") or ""), 3)
                z = str(form.get(f"{sess}_z") or "").strip()
                if floor:
                    ov["floor_pct"] = floor
                if z:
                    ov["z"] = float(z)
                if bypass:
                    ov["bypass_pct"] = bypass
                if len(ov):
                    overrides[sess] = ov
            if "overrides" in r:
                del r["overrides"]
            if len(overrides):
                r["overrides"] = overrides
            e = _tbl(doc, "episode")
            e["extend_floors"] = _num(form, "extend_floors")
            e["end_retrace"] = _num(form, "end_retrace")
            for name in ("idle_end_min", "cooldown_min", "max_replies", "reply_gap_min"):
                e[name] = _num(form, name, int)
            m = _tbl(doc, "merge")
            for name in ("window_s", "max_new_per_min", "expiry_s"):
                m[name] = _num(form, name, int)

        await apply(s, request, form, mutate, "规则")
        return RedirectResponse("/rules", status_code=303)

    # ---- 趋势 ----
    TREND_NUMS = (
        ("accept_bars", int), ("min_4h_score", int), ("cooldown_h", float), ("hard_risk_atr4h", float),
        ("max_risk_atr4h", float), ("max_ext_atr1h", float), ("climax_mult", float),
        ("sr_tol_atr", float), ("sr_half_life_d", float), ("sr_vp_days", float), ("sr_show", int),
        ("zigzag_atr", float), ("adx_min", float), ("history_days", int),
    )

    @app.get("/trend", response_class=HTMLResponse)
    async def trend_page(request: Request) -> Response:
        s = need_login(request)
        text, _, cfg = load()
        st = await monitor("trend")
        live = {it["symbol"]: it for it in (st or {}).get("items") or []}
        groups = [("美股", "正常盘 09:30–16:00 美东", cfg.stocks.symbols), ("加密货币", "全天 24 小时", cfg.crypto.symbols)]
        return page(request, s, "trend.html", cfg=cfg, rev=digest(text), st=st, groups=groups, live=live)

    @app.post("/trend")
    async def trend_save(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        _, _, cfg = load()

        def mutate(doc: Any) -> None:
            t = _tbl(doc, "trend")
            t["enabled"] = _bool(form, "enabled")
            t["disabled_symbols"] = [x for x in cfg.all_symbols() if not _bool(form, f"sym_{x}")]
            for name, cast in TREND_NUMS:
                t[name] = _num(form, name, cast)

        await apply(s, request, form, mutate, "趋势监控")
        return RedirectResponse("/trend", status_code=303)

    # ---- 推送渠道 ----
    def _route_form(t: Table, form: Any, prefix: str) -> None:
        """就地改 [xxx.route]，保留原有注释。"""
        t["alerts"] = _bool(form, f"{prefix}_alerts")
        lvl = str(form.get(f"{prefix}_min_level") or "L1")
        t["min_level"] = lvl if lvl in ("L1", "L2", "L3") else "L1"
        markets = [m for m in ("US", "CRYPTO") if _bool(form, f"{prefix}_m_{m}")]
        t["markets"] = markets
        t["tradingview"] = _bool(form, f"{prefix}_tradingview")
        t["trend"] = _bool(form, f"{prefix}_trend")
        t["ops"] = _bool(form, f"{prefix}_ops")
        t["reports"] = _bool(form, f"{prefix}_reports")

    @app.get("/channels", response_class=HTMLResponse)
    async def channels_page(request: Request) -> Response:
        s = need_login(request)
        text, _, cfg = load()
        st = await monitor("status")
        return page(
            request, s, "channels.html", cfg=cfg, rev=digest(text), st=st,
            tg_token=sec.masked("TELEGRAM_BOT_TOKEN"), dc_token=sec.masked("DISCORD_BOT_TOKEN"),
            sample=sample_cards(),
        )

    def _secret_from_form(form: Any, field: str, key: str) -> str | None:
        """返回要写入的新值：None = 不动，"" = 清空。"""
        if _bool(form, f"{field}_clear"):
            return ""
        v = str(form.get(field) or "").strip()
        return v or None

    @app.post("/channels/telegram")
    async def channels_telegram(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        new_token = _secret_from_form(form, "token", "TELEGRAM_BOT_TOKEN")
        if new_token and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", new_token):
            flash(s, "Telegram token 格式不对，应该像 123456789:AA…（找 @BotFather 要）", "err")
            return RedirectResponse("/channels", status_code=303)

        def mutate(doc: Any) -> None:
            t = _tbl(doc, "telegram")
            t["enabled"] = _bool(form, "enabled")
            chat = str(form.get("chat_id") or "").strip()
            t["chat_id"] = int(chat) if re.fullmatch(r"-?\d+", chat) else chat
            t["admin_user_ids"] = _ints(str(form.get("admin_user_ids") or ""))
            t["commands"] = _bool(form, "commands")
            t["feedback_buttons"] = _bool(form, "feedback_buttons")
            t["silent_extension_replies"] = _bool(form, "silent_extension_replies")
            _route_form(_tbl(doc, "telegram", "route"), form, "tg")

        if new_token is not None:
            sec.set("TELEGRAM_BOT_TOKEN", new_token)
            audit.log(s.username, client_ip(request), "secret", "TELEGRAM_BOT_TOKEN " + ("清空" if not new_token else "更新"))
        await apply(s, request, form, mutate, "Telegram 设置")
        if new_token is not None:
            await restart_monitor(s, request, "Telegram token 已更新")
        return RedirectResponse("/channels", status_code=303)

    @app.post("/channels/discord")
    async def channels_discord(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        new_token = _secret_from_form(form, "token", "DISCORD_BOT_TOKEN")
        if new_token and not re.fullmatch(r"[A-Za-z0-9_.\-]{50,100}", new_token):
            flash(s, "Discord token 格式不对（在 Discord 开发者后台的 Bot 页面 Reset Token 获取）", "err")
            return RedirectResponse("/channels", status_code=303)

        def mutate(doc: Any) -> None:
            t = _tbl(doc, "discord")
            t["enabled"] = _bool(form, "enabled")
            t["channel_id"] = str(form.get("channel_id") or "").strip()
            t["admin_user_ids"] = _ints(str(form.get("admin_user_ids") or ""))
            t["commands"] = _bool(form, "commands")
            t["feedback_buttons"] = _bool(form, "feedback_buttons")
            _route_form(_tbl(doc, "discord", "route"), form, "dc")

        if new_token is not None:
            sec.set("DISCORD_BOT_TOKEN", new_token)
            audit.log(s.username, client_ip(request), "secret", "DISCORD_BOT_TOKEN " + ("清空" if not new_token else "更新"))
        await apply(s, request, form, mutate, "Discord 设置")
        if new_token is not None:
            await restart_monitor(s, request, "Discord token 已更新")
        return RedirectResponse("/channels", status_code=303)

    @app.post("/channels/check")
    async def channels_check(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        _, _, cfg = load()
        which = form.get("channel")
        if which == "telegram":
            token = sec.get("TELEGRAM_BOT_TOKEN")
            if not token:
                flash(s, "还没有填 Telegram token", "err")
            else:
                tg = TelegramClient(token, cfg.telegram.api_base, 10)
                me = await tg.call("getMe")
                chat = await tg.call("getChat", {"chat_id": cfg.telegram.chat_id}) if me.ok else None
                await tg.aclose()
                if not me.ok:
                    flash(s, f"token 无效或网络不通：{me.description or me.kind}", "err")
                elif chat is None or not chat.ok:
                    flash(s, f"机器人 @{me.result.get('username')} 正常，但找不到群 {cfg.telegram.chat_id}：{chat.description if chat else ''}。先把机器人拉进群。", "err")
                else:
                    flash(s, f"连接正常：机器人 @{me.result.get('username')}，群「{chat.result.get('title')}」")
        elif which == "discord":
            token = sec.get("DISCORD_BOT_TOKEN")
            if not token:
                flash(s, "还没有填 Discord token", "err")
            else:
                client = DiscordClient(token, cfg.discord.api_base, 10)
                ok, text = await DiscordNotifier(client, cfg.discord.channel_id or "0").check()
                app_res, app_data = await client.request("GET", "/oauth2/applications/@me", kind="probe")
                await client.aclose()
                flash(s, ("连接正常：" if ok else "") + text, "ok" if ok else "err")
                if app_res.ok and app_data:
                    s.discord_invite = (
                        f"https://discord.com/oauth2/authorize?client_id={app_data.get('id')}"
                        f"&scope=bot%20applications.commands&permissions={DISCORD_PERMS}"
                    )
        return RedirectResponse("/channels", status_code=303)

    @app.post("/channels/test")
    async def channels_test(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        ch = str(form.get("channel") or "")
        kind = form.get("kind")
        args: dict[str, Any] = {"channel": ch}
        if kind == "sample":
            args.update(text=sample_cards(), direction=-1)
        res = await monitor("test", **args)
        if res is None:
            flash(s, "监控进程没有运行，发不了测试消息", "err")
        elif not res.get("ok"):
            flash(s, f"没发出去：{res.get('error')}", "err")
        else:
            audit.log(s.username, client_ip(request), "test", ch)
            flash(s, "已放进发件箱，几秒内到达")
        return RedirectResponse("/channels", status_code=303)

    # ---- TradingView ----
    @app.get("/tradingview", response_class=HTMLResponse)
    async def tv_page(request: Request) -> Response:
        s = need_login(request)
        text, _, cfg = load()
        secret = sec.get("TRADINGVIEW_WEBHOOK_SECRET")
        host = request.headers.get("host", "你的域名")
        url = f"https://{host}/hooks/tradingview/{secret}" if secret else ""
        demo = {"ticker": "BTCUSDT", "exchange": "BINANCE", "price": 97250.5, "interval": "15", "action": "buy", "message": "突破前高"}
        return page(request, s, "tradingview.html", cfg=cfg, rev=digest(text), url=url, sample=tv_card_text(demo, time.time())[0])

    @app.post("/tradingview")
    async def tv_save(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)

        def mutate(doc: Any) -> None:
            t = _tbl(doc, "tradingview")
            t["enabled"] = _bool(form, "enabled")
            t["max_per_min"] = _num(form, "max_per_min", int)
            t["dedup_s"] = _num(form, "dedup_s", int)

        if _bool(form, "enabled") and not sec.get("TRADINGVIEW_WEBHOOK_SECRET"):
            sec.set("TRADINGVIEW_WEBHOOK_SECRET", secrets.token_urlsafe(24))
            audit.log(s.username, client_ip(request), "secret", "TRADINGVIEW_WEBHOOK_SECRET 生成")
        await apply(s, request, form, mutate, "TradingView 设置")
        return RedirectResponse("/tradingview", status_code=303)

    @app.post("/tradingview/rotate")
    async def tv_rotate(request: Request) -> Response:
        s = need_login(request)
        await form_checked(request, s)
        sec.set("TRADINGVIEW_WEBHOOK_SECRET", secrets.token_urlsafe(24))
        audit.log(s.username, client_ip(request), "secret", "TRADINGVIEW_WEBHOOK_SECRET 更换")
        flash(s, "已生成新的 webhook 地址，旧地址立即失效。记得到 TradingView 的告警里更新。")
        return RedirectResponse("/tradingview", status_code=303)

    @app.post("/hooks/tradingview/{token}")
    async def tv_hook(token: str, request: Request) -> Response:
        expected = sec.get("TRADINGVIEW_WEBHOOK_SECRET")
        ip = client_ip(request)
        now = time.time()
        if len(tv_hits) > 5000:  # 防止大量来源把字典撑大
            for k in [k for k, v in tv_hits.items() if not v or now - v[-1] >= 60]:
                del tv_hits[k]
        hits = [t for t in tv_hits.get(ip, []) if now - t < 60]
        tv_hits[ip] = hits + [now]
        if len(hits) >= 120:
            return PlainTextResponse("too many requests", status_code=429)
        if not expected or not same(token, expected):
            return PlainTextResponse("not found", status_code=404)
        body = await request.body()
        if len(body) > MAX_BODY:
            return PlainTextResponse("too large", status_code=413)
        res = await monitor("tv_alert", alert=parse_alert(body))
        if res is None:
            return PlainTextResponse("monitor offline", status_code=503)
        return PlainTextResponse("ok" if res.get("ok") else str(res.get("error") or "ignored"))

    # ---- 系统 ----
    @app.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request) -> Response:
        s = need_login(request)
        text, _, cfg = load()
        lb_keys = ["LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN", "LONGBRIDGE_OAUTH_CLIENT_ID", "HEARTBEAT_URL"]
        return page(request, s, "system.html", cfg=cfg, rev=digest(text), secrets_masked={k: sec.masked(k) for k in lb_keys})

    @app.post("/system")
    async def system_save(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)

        def mutate(doc: Any) -> None:
            g = _tbl(doc, "general")
            g["log_level"] = str(form.get("log_level") or "INFO")
            h = _tbl(doc, "health")
            h["daily_report_time"] = str(form.get("daily_report_time") or "09:05").strip()
            h["report_tz"] = str(form.get("report_tz") or "Asia/Shanghai").strip()
            try:
                ZoneInfo(str(h["report_tz"]))
            except (KeyError, ValueError) as e:
                raise ValueError(f"时区 {h['report_tz']} 不存在，例子：Asia/Shanghai") from e
            h["startup_message"] = _bool(form, "startup_message")
            for name in ("binance_stale_s", "lb_stale_reg_s", "lb_stale_other_s", "stale_alert_after_s"):
                h[name] = _num(form, name, int)
            exp = str(form.get("longbridge_token_expires") or "").strip()
            if exp:
                h["longbridge_token_expires"] = datetime.strptime(exp, "%Y-%m-%d").date()
            elif "longbridge_token_expires" in h:
                del h["longbridge_token_expires"]
            st = _tbl(doc, "storage")
            for name in ("bars_days", "outbox_days", "backup_keep"):
                st[name] = _num(form, name, int)
            lb = _tbl(doc, "stocks", "longbridge")
            auth = str(form.get("lb_auth") or "apikey")
            lb["auth"] = auth if auth in ("apikey", "oauth") else "apikey"

        await apply(s, request, form, mutate, "系统设置")
        return RedirectResponse("/system", status_code=303)

    @app.post("/system/secrets")
    async def system_secrets(request: Request) -> Response:
        s = need_login(request)
        form = await form_checked(request, s)
        changed = []
        for key in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN", "LONGBRIDGE_OAUTH_CLIENT_ID", "HEARTBEAT_URL"):
            v = _secret_from_form(form, key, key)
            if v is None:
                continue
            try:
                sec.set(key, v)
            except ValueError as e:
                flash(s, f"{key}：{e}", "err")
                return RedirectResponse("/system", status_code=303)
            changed.append(key)
        if changed:
            audit.log(s.username, client_ip(request), "secret", "、".join(changed))
            await restart_monitor(s, request, f"已更新 {'、'.join(changed)}")
        else:
            flash(s, "没有变化")
        return RedirectResponse("/system", status_code=303)

    @app.post("/system/restart")
    async def system_restart(request: Request) -> Response:
        s = need_login(request)
        await form_checked(request, s)
        await restart_monitor(s, request, "手动重启")
        return RedirectResponse("/system", status_code=303)

    # ---- 历史与审计 ----
    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request) -> Response:
        s = need_login(request)
        return page(request, s, "history.html", files=cfg_file.history()[:50], audit=audit.tail(200), actions=AUDIT_LABELS)

    @app.get("/history/{name}", response_class=HTMLResponse)
    async def history_view(name: str, request: Request) -> Response:
        s = need_login(request)
        try:
            old = cfg_file.history_path(name).read_text()
        except FileNotFoundError as e:
            raise HTTPException(404, "没有这个版本") from e
        from .files import unified_diff

        cur = paths.config.read_text()
        return page(request, s, "history_view.html", fname=name, diff=unified_diff(cur, old) or "和当前配置完全相同")

    @app.post("/history/{name}/restore")
    async def history_restore(name: str, request: Request) -> Response:
        s = need_login(request)
        await form_checked(request, s)
        try:
            saved = cfg_file.restore(name, s.username)
        except FileNotFoundError as e:
            raise HTTPException(404, "没有这个版本") from e
        except ValidationError as e:
            flash(s, f"这个版本已不符合当前程序的配置格式，不能直接恢复：{e.errors()[0]['msg']}", "err")
            return RedirectResponse("/history", status_code=303)
        audit.log(s.username, client_ip(request), "rollback", f"恢复到 {name}；当前版本存为 {saved.history_file}")
        res = await monitor("reload", auto_restart=True)
        flash(s, f"已恢复到 {name}。" + ("监控没有运行，下次启动时生效。" if res is None else "监控已重载。"))
        return RedirectResponse("/history", status_code=303)

    return app


def run_web(config_path: str, secrets_path: str, auth_path: str, history_dir: str, audit_path: str) -> int:
    import uvicorn

    from ..config import load_config
    from ..logsetup import setup_logging

    cfg = load_config(config_path)
    setup_logging(cfg.general.log_level)
    paths = WebPaths(Path(config_path), Path(secrets_path), Path(auth_path), Path(history_dir), Path(audit_path))
    app = create_app(paths)
    uvicorn.run(
        app,
        host=cfg.web.listen_host,
        port=cfg.web.listen_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        server_header=False,
        log_level="warning",
        access_log=False,
    )
    return 0
