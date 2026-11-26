import pytest
from pydantic import ValidationError

from market_monitor_v3.config import AppConfig, Params
from tests.helpers import make_cfg


def test_sensitive_preset_has_louis_decisions():
    p = Params(make_cfg())
    assert p.preset == "sensitive" and p.label == "灵敏"
    assert p.sessions["REG"].z == 5 and p.sessions["PRE"].z == 6 and p.sessions["ON"].z == 6
    assert p.sessions["REG"].bypass == pytest.approx((0.015, 0.025, 0.03))
    for s in ("PRE", "POST", "ON"):
        assert p.sessions[s].bypass[2] == pytest.approx(0.025)  # 盘前盘后夜盘 15 分钟直通 2.5%
    assert p.sessions["CWD"].z == 5 and p.sessions["CWE"].z == 6
    assert p.sessions["CWD"].bypass == pytest.approx((0.015, 0.025, 0.035))
    assert p.floor("ETHUSDT", "CWD", 1) == pytest.approx(0.006)
    assert p.floor("SOLUSDT", "CWD", 15) == pytest.approx(0.021)
    assert p.rule_version.startswith("v4.0-灵敏-")


def test_rule_version_changes_with_params():
    a = Params(make_cfg()).rule_version
    b = Params(make_cfg(rules={"confirm_ratio": 0.7})).rule_version
    c = Params(make_cfg(rules={"overrides": {"REG": {"z": 4.5}}})).rule_version
    assert len({a, b, c}) == 3
    assert Params(make_cfg()).rule_version == a


def test_validation_errors():
    with pytest.raises(ValidationError):
        make_cfg(stocks={"symbols": ["NVDA"]})  # 缺 .US
    with pytest.raises(ValidationError):
        make_cfg(weekend_perp={"enabled": True})
    with pytest.raises(ValidationError):
        make_cfg(rules={"overrides": {"XYZ": {"z": 3}}})
    with pytest.raises(ValidationError):
        make_cfg(health={"daily_report_time": "9:5"})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"telegram": {"chat_id": 1}, "unknown": 1})


def test_example_config_parses(tmp_path):
    from pathlib import Path

    from market_monitor_v3.config import load_config

    ex = Path(__file__).resolve().parents[2] / "deploy" / "config.example.toml"
    cfg = load_config(ex)
    assert cfg.rules.preset == "sensitive"
    assert len(cfg.stocks.symbols) == 10
