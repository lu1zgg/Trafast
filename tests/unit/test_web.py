"""网页后台：登录（密码 + TOTP + 锁定）、CSRF、改配置（保留注释、历史、校验、并发保护）、密钥只写不读、TradingView webhook。"""
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pyotp = pytest.importorskip("pyotp")

from fastapi.testclient import TestClient  # noqa: E402

from market_monitor_v3.config import load_config  # noqa: E402
from market_monitor_v3.web import app as webapp  # noqa: E402
from market_monitor_v3.web.auth import LoginGuard, save_account  # noqa: E402
from market_monitor_v3.web.files import ConfigFile, SecretsFile, digest  # noqa: E402

USER = "admin"
PASSWORD = "correct horse battery staple"
TG_TOKEN = "123456789:AAAAbbbbCCCCddddEEEEffffGGGGhhhhIII"

CONFIG = """\
# 测试配置：这行注释必须在保存后保留
[general]
log_level = "INFO"

[telegram]
chat_id = -1001234567890  # 群组 ID

[stocks]
symbols = ["NVDA.US", "AAPL.US"]  # 美股

[crypto]
symbols = ["BTCUSDT"]
multipliers = {}
rest_url = "http://127.0.0.1:9"  # 测试里连不上，走“没能核对”的分支
"""


def _status() -> dict:
    now = time.time()
    return {
        "version": "3.0.0",
        "rule_version": "v3.0-灵敏-abcd1234",
        "preset": "灵敏",
        "uptime_s": 7200,
        "us_session": "盘前",
        "feeds": {"longbridge": 12.0, "binance": None},
        "channels": {
            "telegram": {"label": "Telegram", "paused": False, "reason": None, "last_ok_at": now - 30},
            "discord": {"label": "Discord", "paused": True, "reason": "token 无效", "last_ok_at": None},
        },
        "discord_gateway": False,
        "backlog": 2,
        "backlog_age_s": 30,
        "episodes": [{"symbol": "NVDA", "direction": -1, "move": -2.3, "minutes": 4, "level": "L3"}],
        "mutes": {"TSLA.US": now + 3600},
        "baseline_not_ready": ["PLTR"],
        "recent": [{"ts": now - 600, "symbol": "NVDA", "session": "盘前", "level": "L3", "k": 1, "move": -1.9}],
    }


class FakeMonitor:
    """代替本机控制通道。online=False 时模拟监控进程没在跑。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.online = True

    async def __call__(self, path, cmd, timeout=15, **args):
        if not self.online:
            raise webapp.ControlUnavailable("down")
        self.calls.append((cmd, args))
        if cmd == "status":
            return _status()
        if cmd == "reload":
            return {"ok": True, "rule_version": "v3.0-灵敏-new", "restart": False, "restart_reasons": []}
        if cmd == "mute":
            return {"ok": True, "target": args["target"], "until": time.time() + args["seconds"]}
        return {"ok": True}

    def cmds(self, name: str) -> list[dict]:
        return [a for c, a in self.calls if c == name]


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG)
    (tmp_path / "secrets.env").write_text('# 密钥\nHEARTBEAT_URL=""\n')
    secret = pyotp.random_base32()
    save_account(tmp_path / "auth.json", USER, PASSWORD, secret)
    mon = FakeMonitor()
    monkeypatch.setattr(webapp, "control_call", mon)
    paths = webapp.WebPaths(
        config=cfg,
        secrets=tmp_path / "secrets.env",
        auth=tmp_path / "auth.json",
        history=tmp_path / "history",
        audit=tmp_path / "audit.log",
    )
    app = webapp.create_app(paths, control_socket=str(tmp_path / "ctl.sock"), fail_delay_s=0)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    return {"client": client, "paths": paths, "secret": secret, "mon": mon, "tmp": tmp_path}


def _code(secret: str, offset: int = 0) -> str:
    return pyotp.TOTP(secret).at(time.time() + offset)


def login(env, offset: int = 0) -> str:
    r = env["client"].post("/login", data={"username": USER, "password": PASSWORD, "code": _code(env["secret"], offset)})
    assert r.status_code == 303 and r.headers["location"] == "/", r.text
    page = env["client"].get("/symbols")
    assert page.status_code == 200
    return _field(page.text, "csrf")


def _field(html: str, name: str) -> str:
    marker = f'name="{name}" value="'
    i = html.index(marker) + len(marker)
    return html[i : html.index('"', i)]


def _flash(env, path: str) -> str:
    return env["client"].get(path).text


# ---- 登录 ----
def test_login_page_without_account_tells_how_to_create(env):
    env["paths"].auth.unlink()
    r = env["client"].get("/login")
    assert "还没有管理员账号" in r.text and "mm web-passwd" in r.text


def test_pages_require_login(env):
    for path in ["/", "/symbols", "/rules", "/channels", "/tradingview", "/system", "/history"]:
        r = env["client"].get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_login_success_sets_strict_cookie_and_renders_every_page(env):
    login(env)
    r = env["client"].post("/login", data={"username": USER, "password": PASSWORD, "code": "000000"})
    for path in ["/", "/symbols", "/rules", "/channels", "/tradingview", "/system", "/history"]:
        r = env["client"].get(path)
        assert r.status_code == 200, (path, r.text[:500])
        assert "退出（admin）" in r.text
        assert "Content-Security-Policy" in r.headers and r.headers["X-Frame-Options"] == "DENY"
    overview = env["client"].get("/").text
    assert "NVDA" in overview and "token 无效" in overview and "PLTR" in overview
    assert any(c == "ops" and "登录了后台" in a["text"] for c, a in env["mon"].calls)


def test_cookie_flags(env):
    r = env["client"].post("/login", data={"username": USER, "password": PASSWORD, "code": _code(env["secret"])})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_overview_when_monitor_offline(env):
    login(env)
    env["mon"].online = False
    r = env["client"].get("/")
    assert r.status_code == 200 and "连不上监控进程" in r.text


def test_wrong_password_or_code_rejected(env):
    c = env["client"]
    r = c.post("/login", data={"username": USER, "password": "nope", "code": _code(env["secret"])})
    assert r.status_code == 200 and "不对" in r.text
    r = c.post("/login", data={"username": USER, "password": PASSWORD, "code": "123456"})
    assert "不对" in r.text
    r = c.post("/login", data={"username": "other", "password": PASSWORD, "code": _code(env["secret"])})
    assert "不对" in r.text
    assert c.get("/").status_code == 303


def test_totp_code_cannot_be_replayed(env):
    code = _code(env["secret"])
    c = env["client"]
    assert c.post("/login", data={"username": USER, "password": PASSWORD, "code": code}).status_code == 303
    c.cookies.clear()
    r = c.post("/login", data={"username": USER, "password": PASSWORD, "code": code})
    assert r.status_code == 200 and "不对" in r.text


def test_lockout_after_repeated_failures_even_with_right_password(env):
    c = env["client"]
    for _ in range(5):
        c.post("/login", data={"username": USER, "password": "bad", "code": "000000"})
    r = c.post("/login", data={"username": USER, "password": PASSWORD, "code": _code(env["secret"])})
    assert "尝试次数过多" in r.text
    assert any(c == "ops" and "锁定" in a["text"] for c, a in env["mon"].calls)


def test_login_guard_global_limit():
    g = LoginGuard(max_failures=2, lock_s=60)
    now = 1000.0
    for i in range(8):  # 8 个不同 IP 各错一次：全局上限 2 × 4 = 8
        g.fail(f"10.0.0.{i}", now)
    assert g.locked("10.9.9.9", now) > 0
    assert g.locked("10.9.9.9", now + 61) == 0


def test_logout_invalidates_session(env):
    csrf = login(env)
    assert env["client"].post("/logout", data={"csrf": csrf}).status_code == 303
    assert env["client"].get("/").status_code == 303


# ---- CSRF ----
def test_post_without_csrf_or_from_other_origin_is_refused(env):
    csrf = login(env)
    c = env["client"]
    before = env["paths"].config.read_text()
    assert c.post("/symbols/add", data={"market": "US", "symbol": "TSLA"}).status_code == 403
    r = c.post("/symbols/add", data={"market": "US", "symbol": "TSLA", "csrf": csrf}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert env["paths"].config.read_text() == before


# ---- 标的 ----
def test_add_and_remove_symbol_keeps_comments_and_archives(env):
    csrf = login(env)
    c = env["client"]
    r = c.post("/symbols/add", data={"market": "US", "symbol": " tsla ", "csrf": csrf})
    assert r.status_code == 303
    text = env["paths"].config.read_text()
    assert "测试配置：这行注释必须在保存后保留" in text and "# 群组 ID" in text
    assert load_config(env["paths"].config).stocks.symbols == ["NVDA.US", "AAPL.US", "TSLA.US"]
    assert env["mon"].cmds("reload") == [{"auto_restart": True}]
    hist = ConfigFile(env["paths"].config, env["paths"].history).history()
    assert len(hist) == 1 and hist[0].endswith("-admin.toml")
    assert "已保存" in _flash(env, "/symbols")

    r = c.post("/symbols/add", data={"market": "US", "symbol": "TSLA.US", "csrf": csrf})
    assert "已经在列表里" in _flash(env, "/symbols")

    c.post("/symbols/remove", data={"symbol": "AAPL.US", "csrf": csrf})
    assert load_config(env["paths"].config).stocks.symbols == ["NVDA.US", "TSLA.US"]
    assert "删除标的 AAPL.US" in env["paths"].audit.read_text()


def test_add_crypto_symbol_when_binance_unreachable_still_saves_with_warning(env):
    csrf = login(env)
    env["client"].post("/symbols/add", data={"market": "CRYPTO", "symbol": "eth", "csrf": csrf})
    assert load_config(env["paths"].config).crypto.symbols == ["BTCUSDT", "ETHUSDT"]
    assert "没能核对" in _flash(env, "/symbols")


def test_bad_symbol_rejected(env):
    csrf = login(env)
    env["client"].post("/symbols/add", data={"market": "US", "symbol": "NV<DA", "csrf": csrf})
    assert "只能包含" in _flash(env, "/symbols")
    assert webapp.normalize_symbol("CRYPTO", "sol") == "SOLUSDT"
    assert webapp.normalize_symbol("CRYPTO", "BTCFDUSD") == "BTCFDUSD"
    assert webapp.normalize_symbol("US", "brk.b") == "BRK.B.US"


def test_symbol_settings_multipliers(env):
    csrf = login(env)
    form = {"csrf": csrf, "stocks_enabled": "on", "crypto_enabled": "on", "overnight": "on", "mult_NVDA.US": "1.5", "mult_AAPL.US": "1", "mult_BTCUSDT": "0.8"}
    env["client"].post("/symbols/settings", data=form)
    cfg = load_config(env["paths"].config)
    assert cfg.stocks.multipliers == {"NVDA.US": 1.5} and cfg.crypto.multipliers == {"BTCUSDT": 0.8}
    form["mult_NVDA.US"] = "9"
    env["client"].post("/symbols/settings", data=form)
    assert "0.2 到 5" in _flash(env, "/symbols")
    assert load_config(env["paths"].config).stocks.multipliers == {"NVDA.US": 1.5}


def test_stale_form_does_not_overwrite_newer_file(env):
    csrf = login(env)
    old_rev = digest(env["paths"].config.read_text())
    env["paths"].config.write_text(CONFIG + "\n# 有人在 SSH 里改过\n")
    env["client"].post("/symbols/add", data={"market": "US", "symbol": "TSLA", "csrf": csrf, "rev": old_rev})
    assert "被改过" in _flash(env, "/symbols")
    assert "TSLA.US" not in env["paths"].config.read_text()


# ---- 规则 ----
def _rules_form(csrf: str, **over) -> dict:
    form = {
        "csrf": csrf, "preset": "balanced", "confirm_ratio": "0.6", "fast_score": "2", "l2_score": "1.5",
        "l2_turnover_mult": "3", "thin_turnover_frac": "0.25", "thin_pre_until": "07:00",
        "extend_floors": "1", "end_retrace": "0.5", "idle_end_min": "30", "cooldown_min": "10",
        "max_replies": "3", "reply_gap_min": "5", "window_s": "180", "max_new_per_min": "6", "expiry_s": "600",
        "PRE_floor": "0.8, 1.5, 2.2", "PRE_z": "7",
    }
    form.update(over)
    return form


def test_rules_save_with_session_override(env):
    csrf = login(env)
    env["client"].post("/rules", data=_rules_form(csrf))
    cfg = load_config(env["paths"].config)
    assert cfg.rules.preset == "balanced"
    assert cfg.rules.overrides["PRE"].floor_pct == (0.8, 1.5, 2.2) and cfg.rules.overrides["PRE"].z == 7
    assert "REG" not in cfg.rules.overrides
    page = env["client"].get("/rules").text
    assert "已保存并生效" in page and 'value="0.8, 1.5, 2.2"' in page
    # 去掉覆盖
    env["client"].post("/rules", data=_rules_form(csrf, PRE_floor="", PRE_z=""))
    assert load_config(env["paths"].config).rules.overrides == {}


def test_rules_invalid_value_is_not_written(env):
    csrf = login(env)
    before = env["paths"].config.read_text()
    env["client"].post("/rules", data=_rules_form(csrf, confirm_ratio="5"))
    assert "没有保存" in _flash(env, "/rules")
    env["client"].post("/rules", data=_rules_form(csrf, PRE_floor="1, 2"))
    assert "需要 3 个数" in _flash(env, "/rules")
    assert env["paths"].config.read_text() == before


# ---- 推送渠道与密钥 ----
def _tg_form(csrf: str, **over) -> dict:
    form = {"csrf": csrf, "enabled": "on", "chat_id": "-1001234567890", "admin_user_ids": "11, 22", "commands": "on",
            "tg_alerts": "on", "tg_m_US": "on", "tg_min_level": "L2", "tg_ops": "on", "token": ""}
    form.update(over)
    return form


def test_telegram_token_write_only(env):
    csrf = login(env)
    c = env["client"]
    c.post("/channels/telegram", data=_tg_form(csrf, token=TG_TOKEN))
    assert SecretsFile(env["paths"].secrets).get("TELEGRAM_BOT_TOKEN") == TG_TOKEN
    assert "# 密钥" in env["paths"].secrets.read_text()
    cfg = load_config(env["paths"].config)
    assert cfg.telegram.admin_user_ids == [11, 22]
    assert cfg.telegram.route.min_level == "L2" and cfg.telegram.route.markets == ["US"] and not cfg.telegram.route.reports
    assert env["mon"].cmds("restart")
    page = c.get("/channels").text
    assert TG_TOKEN not in page and "末 4 位 hIII" in page
    assert TG_TOKEN not in env["paths"].audit.read_text()
    # 留空 = 不改
    c.post("/channels/telegram", data=_tg_form(csrf))
    assert SecretsFile(env["paths"].secrets).get("TELEGRAM_BOT_TOKEN") == TG_TOKEN
    # 格式不对拒收
    c.post("/channels/telegram", data=_tg_form(csrf, token="not-a-token"))
    assert "格式不对" in _flash(env, "/channels")
    # 清空
    c.post("/channels/telegram", data=_tg_form(csrf, token_clear="on"))
    assert SecretsFile(env["paths"].secrets).get("TELEGRAM_BOT_TOKEN") == ""


def test_discord_enable_without_channel_id_fails_validation(env):
    csrf = login(env)
    env["client"].post("/channels/discord", data={"csrf": csrf, "enabled": "on", "channel_id": "", "dc_alerts": "on"})
    page = _flash(env, "/channels")
    assert "没有保存" in page
    assert not load_config(env["paths"].config).discord.enabled


def test_secrets_file_rejects_injection(tmp_path):
    sf = SecretsFile(tmp_path / "s.env")
    for bad in ['a"b', "a\nB=1", "$(id)", "a`b", "a\\b"]:
        with pytest.raises(ValueError):
            sf.set("HEARTBEAT_URL", bad)
    with pytest.raises(ValueError):
        sf.set("bad-key", "x")
    sf.set("HEARTBEAT_URL", "https://hc-ping.com/abc?x=1&y=2")
    assert sf.get("HEARTBEAT_URL") == "https://hc-ping.com/abc?x=1&y=2"
    assert sf.masked("HEARTBEAT_URL").startswith("已设置")


def test_channel_test_message(env):
    csrf = login(env)
    env["client"].post("/channels/test", data={"csrf": csrf, "channel": "telegram", "kind": "sample"})
    sent = env["mon"].cmds("test")
    assert sent[0]["channel"] == "telegram" and "🔴" in sent[0]["text"] and "NVDA" in sent[0]["text"]


def test_mute_and_unmute(env):
    csrf = login(env)
    env["client"].post("/mute", data={"csrf": csrf, "target": "TSLA", "seconds": "3600"})
    env["client"].post("/unmute", data={"csrf": csrf, "target": "TSLA.US"})
    assert env["mon"].cmds("mute") == [{"target": "TSLA", "seconds": 3600}]
    assert env["mon"].cmds("unmute") == [{"target": "TSLA.US"}]


# ---- TradingView ----
def test_tradingview_enable_generates_secret_and_hook_checks_it(env):
    csrf = login(env)
    c = env["client"]
    c.post("/tradingview", data={"csrf": csrf, "enabled": "on", "max_per_min": "20", "dedup_s": "60"})
    secret = SecretsFile(env["paths"].secrets).get("TRADINGVIEW_WEBHOOK_SECRET")
    assert len(secret) >= 24
    page = c.get("/tradingview").text
    assert f"/hooks/tradingview/{secret}" in page and "{{ticker}}" in page

    anon = TestClient(c.app, base_url="https://testserver")
    assert anon.post("/hooks/tradingview/wrong", content=b"{}").status_code == 404
    r = anon.post(f"/hooks/tradingview/{secret}", content=b'{"ticker": "BTCUSDT", "price": 97000, "action": "buy"}')
    assert r.status_code == 200 and r.text == "ok"
    alert = env["mon"].cmds("tv_alert")[0]["alert"]
    assert alert["ticker"] == "BTCUSDT" and alert["price"] == 97000 and alert["action"] == "buy"
    assert anon.post(f"/hooks/tradingview/{secret}", content=b"x" * 9000).status_code == 413

    # 换地址后旧地址失效
    c.post("/tradingview/rotate", data={"csrf": csrf})
    assert anon.post(f"/hooks/tradingview/{secret}", content=b"{}").status_code == 404


def test_tradingview_hook_disabled_without_secret(env):
    anon = TestClient(env["client"].app, base_url="https://testserver")
    assert anon.post("/hooks/tradingview/anything", content=b"{}").status_code == 404


# ---- 系统、历史 ----
def test_system_settings_and_bad_timezone(env):
    csrf = login(env)
    form = {"csrf": csrf, "log_level": "DEBUG", "daily_report_time": "08:30", "report_tz": "Asia/Shanghai",
            "binance_stale_s": "30", "lb_stale_reg_s": "60", "lb_stale_other_s": "300", "stale_alert_after_s": "180",
            "longbridge_token_expires": "2027-03-01", "bars_days": "120", "outbox_days": "30", "backup_keep": "7", "lb_auth": "apikey"}
    env["client"].post("/system", data=form)
    cfg = load_config(env["paths"].config)
    assert cfg.general.log_level == "DEBUG" and cfg.health.daily_report_time == "08:30"
    assert str(cfg.health.longbridge_token_expires) == "2027-03-01"
    env["client"].post("/system", data={**form, "report_tz": "Mars/Base"})
    assert "没有保存" in _flash(env, "/system")


def test_system_secrets_and_restart(env):
    csrf = login(env)
    env["client"].post("/system/secrets", data={"csrf": csrf, "LONGBRIDGE_APP_KEY": "abcdefghijklmnop"})
    assert SecretsFile(env["paths"].secrets).get("LONGBRIDGE_APP_KEY") == "abcdefghijklmnop"
    assert env["mon"].cmds("restart")
    page = env["client"].get("/system").text
    assert "abcdefghijklmnop" not in page and "末 4 位 mnop" in page


def test_history_view_and_restore(env):
    csrf = login(env)
    original = env["paths"].config.read_text()
    env["client"].post("/symbols/add", data={"market": "US", "symbol": "TSLA", "csrf": csrf})
    name = ConfigFile(env["paths"].config, env["paths"].history).history()[0]
    page = env["client"].get(f"/history/{name}").text
    assert "TSLA.US" in page
    assert env["client"].get("/history/..%2Fconfig.toml").status_code == 404
    env["client"].post(f"/history/{name}/restore", data={"csrf": csrf})
    assert env["paths"].config.read_text() == original
    assert len(ConfigFile(env["paths"].config, env["paths"].history).history()) == 2
    assert "恢复到" in env["client"].get("/history").text


def test_config_file_history_is_capped(tmp_path, monkeypatch):
    from market_monitor_v3.web import files

    monkeypatch.setattr(files, "HISTORY_KEEP", 3)
    p = tmp_path / "c.toml"
    p.write_text(CONFIG)
    cf = ConfigFile(p, tmp_path / "h")
    for i in range(5):
        (tmp_path / "h").mkdir(exist_ok=True)
        (tmp_path / "h" / f"config-20260101-00000{i}-web.toml").write_text(CONFIG)
    cf.save(lambda doc: doc["general"].__setitem__("log_level", "DEBUG"), None, "web")
    assert len(cf.history()) == 3


def test_web_static_has_no_inline_script_or_style():
    root = Path(webapp.__file__).parent / "templates"
    for f in root.glob("*.html"):
        text = f.read_text()
        assert "<script>" not in text and " style=" not in text and "onclick" not in text and "onsubmit" not in text, f.name


def test_every_form_on_the_shipped_example_config(env):
    """拿仓库里的 config.example.toml 把每个表单都存一遍：每次都能通过校验，注释还在。"""
    example = Path(__file__).resolve().parents[2] / "deploy" / "config.example.toml"
    env["paths"].config.write_text(example.read_text())
    csrf = login(env)
    c = env["client"]
    c.post("/symbols/add", data={"market": "US", "symbol": "AMD", "csrf": csrf})
    c.post("/symbols/settings", data={"csrf": csrf, "crypto_enabled": "on", "overnight": "on", "mult_SOLUSDT": "1.4", "mult_AMD.US": "1.3"})
    c.post("/rules", data=_rules_form(csrf))
    c.post("/channels/telegram", data=_tg_form(csrf))
    c.post("/channels/discord", data={"csrf": csrf, "channel_id": "123456789012345678", "dc_alerts": "on", "dc_m_CRYPTO": "on", "dc_min_level": "L3"})
    c.post("/tradingview", data={"csrf": csrf, "enabled": "on", "max_per_min": "10", "dedup_s": "30"})
    form = {"csrf": csrf, "log_level": "INFO", "daily_report_time": "09:05", "report_tz": "Asia/Shanghai",
            "binance_stale_s": "30", "lb_stale_reg_s": "60", "lb_stale_other_s": "300", "stale_alert_after_s": "180",
            "longbridge_token_expires": "", "bars_days": "120", "outbox_days": "30", "backup_keep": "7", "lb_auth": "apikey"}
    c.post("/system", data=form)
    page = c.get("/history").text
    assert "没有保存" not in page
    cfg = load_config(env["paths"].config)
    assert "AMD.US" in cfg.stocks.symbols and cfg.stocks.multipliers == {"AMD.US": 1.3}
    assert cfg.crypto.multipliers == {"SOLUSDT": 1.4}
    assert cfg.rules.overrides["PRE"].z == 7 and cfg.rules.preset == "balanced"
    assert cfg.discord.route.min_level == "L3" and cfg.discord.route.markets == ["CRYPTO"] and not cfg.discord.enabled
    assert cfg.tradingview.enabled and cfg.tradingview.max_per_min == 10
    text = env["paths"].config.read_text()
    for comment in ["# 同市场同方向 3 分钟内合并成一条联动消息", "# 长桥接通前先保持 false", "# 只听本机，由 Caddy 反向代理出 HTTPS"]:
        assert comment in text
    assert len(ConfigFile(env["paths"].config, env["paths"].history).history()) == 7


def test_same_origin_post_and_null_origin_accepted(env):
    csrf = login(env)
    c = env["client"]
    r = c.post("/mute", data={"csrf": csrf, "target": "NVDA", "seconds": "600"}, headers={"Origin": "https://testserver"})
    assert r.status_code == 303
    r = c.post("/mute", data={"csrf": csrf, "target": "NVDA", "seconds": "600"}, headers={"Origin": "null"})
    assert r.status_code == 303
    assert len(env["mon"].cmds("mute")) == 2
    assert c.get("/").headers["Referrer-Policy"] == "same-origin"


# ---- 趋势 ----
def _trend_form(csrf: str, **over) -> dict:
    form = {
        "csrf": csrf, "enabled": "on", "sym_NVDA.US": "on", "sym_AAPL.US": "on", "sym_BTCUSDT": "on",
        **{f"sym_{s}": "on" for s in ("XAUUSDT", "XAGUSDT", "BZUSDT", "CLUSDT")},
        "accept_bars": "4", "min_4h_score": "1", "cooldown_h": "4", "hard_risk_atr4h": "2.5",
        "max_risk_atr4h": "1.8", "max_ext_atr1h": "2.5", "climax_mult": "2.5",
        "sr_tol_atr": "0.4", "sr_half_life_d": "15", "sr_vp_days": "10", "sr_show": "2",
        "zigzag_atr": "1.5", "adx_min": "20", "history_days": "130",
    }
    form.update(over)
    return {k: v for k, v in form.items() if v is not None}


def test_trend_page_shows_status_and_saves(env, monkeypatch):
    now = time.time()
    brief = {"s": 1, "e": 1, "a": 1, "sc": 3, "adx": 31.0, "label": "上涨结构（低点抬高）"}
    item = {
        "symbol": "NVDA.US", "name": "NVDA", "seeded": True, "ready": True, "price": 172.4, "t": now,
        "s15": brief, "s1": brief, "s4": {},
        "active": {"d": 1, "entry_t": now - 3600, "entry_price": 170.0, "stop": 167.8, "quiet": ""},
        "bars": {"15m": 2000, "1h": 800, "4h": 400},
        "res": [{"lo": 177.8, "hi": 182.0, "stars": "★★", "src": "4h+1h", "strong": True}], "sup": [],
    }
    event = {"ts": now - 3600, "symbol": "NVDA.US", "kind": "formed", "d": 1, "price": 170.0,
             "data": {"grade": "B", "stop": 167.8, "quiet": ""}}
    orig = webapp.control_call

    async def mon(path, cmd, timeout=15, **args):
        if cmd == "trend":
            return {"ok": True, "enabled": True, "items": [item], "events": [event]}
        return await orig(path, cmd, timeout, **args)

    monkeypatch.setattr(webapp, "control_call", mon)
    csrf = login(env)
    page = env["client"].get("/trend")
    assert page.status_code == 200
    assert "上涨趋势" in page.text and "$167.80" in page.text and "★★" in page.text and "趋势形成" in page.text
    env["client"].post("/trend", data=_trend_form(csrf, **{"sym_BTCUSDT": None, "accept_bars": "6"}))
    cfg = load_config(env["paths"].config)
    assert cfg.trend.accept_bars == 6 and cfg.trend.disabled_symbols == ["BTCUSDT"]
    assert "# 测试配置" in env["paths"].config.read_text()


def test_channel_route_trend_checkbox(env):
    login(env)
    page = env["client"].get("/channels").text
    assert "趋势形成 / 结束" in page and '_trend"' in page


def test_web_api_keys_page(env):
    c = env["client"]
    csrf = login(env)
    r = c.post("/apikeys/create", data={"csrf": csrf, "name": "我的Mac", "scope": "write"})
    assert r.status_code == 303
    page = c.get("/apikeys").text
    key = page.split('select-all">')[1].split("<")[0].strip()
    assert key.startswith("tfk_") and "我的Mac" in page
    assert "tfk_" + key[4:12] in c.get("/apikeys").text and key not in c.get("/apikeys").text  # 只显示一次
    r = c.get("/api/v1/ping", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.json()["key"]["name"] == "我的Mac"
    c.post("/apikeys/revoke", data={"csrf": csrf, "id": "1"})
    assert c.get("/api/v1/ping", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert (env["tmp"] / "api-keys.json").exists()
