from datetime import date

from market_monitor_v3.sessions import UsCalendar, crypto_session
from tests.helpers import et_ts, utc_ts

cal = UsCalendar()


def test_regular_week_sessions_winter():
    # 2026-11-23 周一（冬令时，美东 = UTC-5）
    assert cal.session_at(et_ts(2026, 11, 23, 3, 59)) == "ON"  # 周日晚夜盘延续到周一 04:00
    assert cal.session_at(et_ts(2026, 11, 23, 4, 0)) == "PRE"
    assert cal.session_at(et_ts(2026, 11, 23, 9, 29)) == "PRE"
    assert cal.session_at(et_ts(2026, 11, 23, 9, 30)) == "REG"
    assert cal.session_at(et_ts(2026, 11, 23, 15, 59)) == "REG"
    assert cal.session_at(et_ts(2026, 11, 23, 16, 0)) == "POST"
    assert cal.session_at(et_ts(2026, 11, 23, 19, 59)) == "POST"
    assert cal.session_at(et_ts(2026, 11, 23, 20, 0)) == "ON"
    # 北京时间 22:30 = 美东 09:30
    assert cal.session_at(utc_ts(2026, 11, 23, 14, 30)) == "REG"


def test_summer_time():
    assert cal.session_at(utc_ts(2026, 7, 1, 13, 30)) == "REG"  # 美东 09:30 EDT
    assert cal.session_at(utc_ts(2026, 7, 1, 13, 29)) == "PRE"


def test_weekend_and_overnight_rule():
    assert cal.session_at(et_ts(2026, 11, 20, 20, 30)) == "CLOSED"  # 周五晚没有夜盘
    assert cal.session_at(et_ts(2026, 11, 21, 12, 0)) == "CLOSED"  # 周六
    assert cal.session_at(et_ts(2026, 11, 22, 19, 59)) == "CLOSED"  # 周日夜盘前
    assert cal.session_at(et_ts(2026, 11, 22, 20, 0)) == "ON"  # 周日 20:00 夜盘开始


def test_thanksgiving_and_early_close():
    assert cal.session_at(et_ts(2026, 11, 26, 10, 0)) == "CLOSED"  # 感恩节
    assert cal.session_at(et_ts(2026, 11, 25, 21, 0)) == "CLOSED"  # 次日休市，前一晚没有夜盘
    assert cal.session_at(et_ts(2026, 11, 26, 21, 0)) == "ON"  # 周五交易，周四晚有夜盘
    assert cal.session_at(et_ts(2026, 11, 27, 12, 59)) == "REG"
    assert cal.session_at(et_ts(2026, 11, 27, 13, 0)) == "POST"  # 提前收盘
    assert cal.session_at(et_ts(2026, 11, 27, 16, 59)) == "POST"
    assert cal.session_at(et_ts(2026, 11, 27, 17, 0)) == "CLOSED"


def test_instances_and_thin():
    assert cal.instance_of(et_ts(2026, 11, 23, 21, 0)) == "ON:2026-11-24"
    assert cal.instance_of(et_ts(2026, 11, 24, 2, 0)) == "ON:2026-11-24"
    assert cal.is_thin(et_ts(2026, 11, 24, 2, 0), "ON")
    assert cal.is_thin(et_ts(2026, 11, 24, 6, 59), "PRE")
    assert not cal.is_thin(et_ts(2026, 11, 24, 7, 0), "PRE")
    assert not cal.is_thin(et_ts(2026, 11, 24, 10, 0), "REG")


def test_overnight_disabled():
    c = UsCalendar(overnight=False)
    assert c.session_at(et_ts(2026, 11, 23, 21, 0)) == "CLOSED"
    assert c.session_at(et_ts(2026, 11, 23, 5, 0)) == "PRE"


def test_update_from_exchange():
    c = UsCalendar()
    begin, end = date(2026, 11, 23), date(2026, 11, 30)
    trading = {date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 30)}
    half = {date(2026, 11, 27)}
    assert c.update_from_exchange(begin, end, trading, half) == 0  # 内置表和交易所一致
    trading.discard(date(2026, 11, 24))
    assert c.update_from_exchange(begin, end, trading, half) == 1
    assert c.session_at(et_ts(2026, 11, 24, 10, 0)) == "CLOSED"


def test_crypto_session():
    assert crypto_session(utc_ts(2026, 11, 21, 0, 0)) == "CWE"  # 周六 UTC
    assert crypto_session(utc_ts(2026, 11, 22, 23, 59)) == "CWE"
    assert crypto_session(utc_ts(2026, 11, 23, 0, 0)) == "CWD"
