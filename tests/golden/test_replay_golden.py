"""金标回放：合成行情 + 7 类注入事件，跑完整引擎（检测、确认、事件、合并、渲染）。

- 语义断言：每类事件该推的推、该挡的挡，且没有任何一条消息来自纯随机噪声。
- 金标比对：消息清单（类型、键、父消息、首行）和 expected.json 完全一致。
  规则或文案有意改动后，用 UPDATE_GOLDEN=1 重新生成，并在提交里说明原因。
- 命令行：monitor replay --csv 的输出和进程内回放一致；check-config 能校验示例配置。
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from market_monitor_v3.cli import main as cli_main
from market_monitor_v3.config import Params
from market_monitor_v3.render import to_plain
from market_monitor_v3.replay import ReplayResult, run_replay
from market_monitor_v3.sessions import UsCalendar
from tests.golden import synth
from tests.helpers import make_cfg

HERE = Path(__file__).parent
EXPECTED = HERE / "expected.json"
ROOT = HERE.parent.parent


def _cfg():
    return make_cfg(stocks={"enabled": True, "symbols": list(synth.STOCKS)}, crypto={"symbols": list(synth.CRYPTO)})


@pytest.fixture(scope="module")
def bars():
    return synth.generate()


@pytest.fixture(scope="module")
def res(bars) -> ReplayResult:
    cfg = _cfg()
    return run_replay(cfg, Params(cfg), UsCalendar(), bars, synth.REPLAY_START, synth.REPLAY_END)


def key_close(key: str) -> int:
    digits = ""
    for ch in key[1:]:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits)


def sends(res: ReplayResult, market: str = "", direction: str = "", name: str = "") -> list:
    out = []
    for m in res.outs:
        if m.kind != "send" or not m.key.startswith("g"):
            continue
        tail = m.key[1:].lstrip("0123456789")
        if market and tail[0] != market:
            continue
        if direction and tail[1] != direction:
            continue
        if name and tail[2:] != name:
            continue
        out.append(m)
    return out


def near(msgs: list, t0: int, minutes: int) -> list:
    return [m for m in msgs if t0 < key_close(m.key) <= t0 + minutes * 60]


def outcomes(res: ReplayResult, symbol: str, t0: int, minutes: int) -> set[str]:
    return {d.outcome for d in res.decisions if d.symbol == symbol and t0 < d.ts <= t0 + minutes * 60}


# ---- 语义断言 ----
def test_weekend_wick_not_pushed(res):
    ev = synth.event("wick")
    assert near(sends(res, "C"), ev.t0, 30) == []
    got = outcomes(res, "BTCUSDT", ev.t0, 30)
    assert "rejected" in got  # 插针本身没站稳
    assert "wick_return" in got  # 收回来的那一段不当成下跌异动


def test_held_step_pushed_once(res):
    ev = synth.event("held_step")
    assert len(near(sends(res, "C", "u", "ETH"), ev.t0, 10)) == 1


def test_correlated_drop_merged(res):
    ev = synth.event("corr_btc")
    msgs = near(sends(res, "C"), ev.t0, 5)
    assert len(msgs) == 1, "三个币同一分钟下跌应合并成一条"
    text = to_plain(msgs[0].text)
    for name in ("BTC", "ETH", "SOL"):
        assert name in text
    assert "同时" in text
    ends = [m for m in res.outs if m.kind == "edit" and m.parent_key == msgs[0].key]
    final = to_plain(ends[-1].text) if ends else ""
    assert final.count("已结束") == 3 and all(n in final for n in ("BTC", "ETH", "SOL"))


def test_big_move_extends_upgrades_and_retrace_not_pushed(res):
    ev = synth.event("big_move")
    first = near(sends(res, "U", "d", "NVDA"), ev.t0, 5)
    assert len(first) == 1
    root = first[0].key
    replies = [m for m in res.outs if m.kind == "reply" and m.parent_key == root]
    assert 1 <= len(replies) <= 3
    assert any(m.level == "L3" for m in replies), "跌破 5 分钟直推线后应升级 L3"
    edits = [m for m in res.outs if m.kind == "edit" and m.parent_key == root]
    assert edits and "已结束" in to_plain(edits[-1].text)
    # 之后的反弹没有越过下跌起点：只是回吐，不另发上涨消息
    assert near(sends(res, "U", "u", "NVDA"), ev.t0, 90) == []
    assert "retrace" in outcomes(res, "NVDA.US", ev.t0, 90)


def test_volume_only_not_pushed(res):
    ev = synth.event("volume_only")
    assert near(sends(res, "U", name="MU"), ev.t0 - 60, 30) == []


def test_overnight_thin_print_not_pushed(res):
    ev = synth.event("on_thin")
    assert near(sends(res, "U", name="MU"), ev.t0 - 60, 60) == []
    assert "thin" in outcomes(res, "MU.US", ev.t0 - 60, 30)


def test_premarket_step_pushed(res):
    ev = synth.event("pre_step")
    msgs = near(sends(res, "U", "u", "NVDA"), ev.t0, 5)
    assert len(msgs) == 1
    assert "盘前" in to_plain(msgs[0].text)


def test_every_message_comes_from_an_injected_event(res):
    """随机游走本身（含周末、夜盘）一条消息都不该触发。"""
    starts = {}
    for e in synth.EVENTS:
        starts.setdefault(e.symbol, []).append(e.t0)
    names = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL", "NVDA.US": "NVDA", "MU.US": "MU"}
    for m in sends(res):
        tail = m.key[1:].lstrip("0123456789")
        sym = next(s for s, n in names.items() if n == tail[2:])
        close = key_close(m.key)
        assert any(t0 < close <= t0 + 10 * 60 for t0 in starts.get(sym, [])), f"噪声触发了消息：{m.key}"


# ---- 金标 ----
def _snapshot(res: ReplayResult) -> dict:
    return {"rule_version": res.rule_version, "messages": res.golden()}


def test_golden_matches(res):
    snap = _snapshot(res)
    if os.environ.get("UPDATE_GOLDEN") == "1" or not EXPECTED.exists():
        EXPECTED.write_text(json.dumps(snap, ensure_ascii=False, indent=1) + "\n")
        if os.environ.get("UPDATE_GOLDEN") != "1":
            pytest.fail("expected.json 不存在，已生成；检查后重新运行")
        return
    expected = json.loads(EXPECTED.read_text())
    assert snap == expected


# ---- 命令行 ----
CONFIG_TOML = """
[telegram]
chat_id = -100123

[stocks]
enabled = true
symbols = ["NVDA.US", "MU.US"]

[crypto]
symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
"""


def test_cli_replay_matches_in_process(tmp_path, bars, res, capsys):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_TOML)
    csv_path = tmp_path / "bars.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "ts", "o", "h", "l", "c", "volume", "turnover", "session"])
        for b in bars:
            w.writerow([b.symbol, b.ts, repr(b.o), repr(b.h), repr(b.l), repr(b.c), repr(b.volume), repr(b.turnover), b.session])
    out = tmp_path / "golden.json"
    rc = cli_main(
        ["replay", "--config", str(cfg_path), "--csv", str(csv_path), "--from", "2026-04-18", "--to", "2026-04-23", "--json", str(out)]
    )
    assert rc == 0
    printed = capsys.readouterr().out
    assert "回放 2026-04-18 00:00" in printed and res.rule_version in printed
    assert json.loads(out.read_text()) == res.golden()


def test_cli_check_config_example(capsys, monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "HEARTBEAT_URL"):
        monkeypatch.delenv(k, raising=False)
    rc = cli_main(["check-config", "--config", str(ROOT / "deploy" / "config.example.toml")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "配置有效" in out and "灵敏" in out
    assert "TELEGRAM_BOT_TOKEN：未设置" in out
