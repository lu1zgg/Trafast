"""交易时段日历。

美股（美东时间）：
  盘前 PRE  04:00–09:30
  盘中 REG  09:30–16:00（提前收盘日 09:30–13:00）
  盘后 POST 16:00–20:00（提前收盘日 13:00–17:00）
  夜盘 ON   20:00–次日 04:00，只有“次日”是交易日时才开（周日晚到周四晚）
加密货币按 UTC 分工作日 CWD 和周末 CWE。

休市日先用内置表，接通长桥后每天用 trading_days 接口刷新（以交易所为准）。
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = UTC

# NYSE 休市日。2027 年按规则推算，接通长桥后会被交易所数据覆盖。
NYSE_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    # 2027（待核对）
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),
    date(2027, 7, 5),
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),
}
NYSE_EARLY_CLOSES: set[date] = {
    date(2026, 11, 27),
    date(2026, 12, 24),
    date(2027, 11, 26),
}

PRE_START = 4 * 60
REG_START = 9 * 60 + 30
REG_END = 16 * 60
REG_END_EARLY = 13 * 60
POST_END = 20 * 60
POST_END_EARLY = 17 * 60


def crypto_session(ts: float) -> str:
    return "CWE" if datetime.fromtimestamp(ts, UTC).weekday() >= 5 else "CWD"


def crypto_instance(ts: float) -> str:
    d = datetime.fromtimestamp(ts, UTC).date()
    return f"{'CWE' if d.weekday() >= 5 else 'CWD'}:{d.isoformat()}"


class UsCalendar:
    def __init__(
        self,
        extra_holidays: list[date] | tuple[date, ...] = (),
        extra_early_closes: list[date] | tuple[date, ...] = (),
        overnight: bool = True,
        thin_pre_until_min: int = 7 * 60,
    ):
        self.holidays: set[date] = set(NYSE_HOLIDAYS) | set(extra_holidays)
        self.early_closes: set[date] = set(NYSE_EARLY_CLOSES) | set(extra_early_closes)
        self.overnight = overnight
        self.thin_pre_until_min = thin_pre_until_min
        self._cache: dict[int, tuple[str, date]] = {}

    # ---- 交易日 ----
    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def update_from_exchange(self, begin: date, end: date, trading_days: set[date], half_days: set[date]) -> int:
        """用交易所给出的交易日覆盖 [begin, end] 区间内的日历，返回改动的天数。"""
        changed = 0
        d = begin
        while d <= end:
            if d.weekday() < 5:
                is_trading = d in trading_days or d in half_days
                if is_trading and d in self.holidays:
                    self.holidays.discard(d)
                    changed += 1
                elif not is_trading and d not in self.holidays:
                    self.holidays.add(d)
                    changed += 1
                if d in half_days and d not in self.early_closes:
                    self.early_closes.add(d)
                    changed += 1
                elif is_trading and d not in half_days and d in self.early_closes:
                    self.early_closes.discard(d)
                    changed += 1
            d += timedelta(days=1)
        if changed:
            self._cache.clear()
        return changed

    # ---- 时段 ----
    def session_at(self, ts: float) -> str:
        return self._classify(int(ts) // 60 * 60)[0]

    def instance_of(self, ts: float) -> str:
        """时段实例：时段类型 + 所属交易日。基线按最近 N 个同类实例统计。"""
        s, d = self._classify(int(ts) // 60 * 60)
        return f"{s}:{d.isoformat()}"

    def is_thin(self, ts: float, session: str) -> bool:
        if session == "ON":
            return True
        if session == "PRE":
            dt = datetime.fromtimestamp(ts, ET)
            return dt.hour * 60 + dt.minute < self.thin_pre_until_min
        return False

    def is_open(self, ts: float) -> bool:
        return self.session_at(ts) != "CLOSED"

    def _classify(self, ts: int) -> tuple[str, date]:
        hit = self._cache.get(ts)
        if hit is not None:
            return hit
        dt = datetime.fromtimestamp(ts, ET)
        d = dt.date()
        m = dt.hour * 60 + dt.minute
        res: tuple[str, date]
        if m < PRE_START:
            res = ("ON", d) if self.overnight and self.is_trading_day(d) else ("CLOSED", d)
        elif m >= POST_END:
            nd = d + timedelta(days=1)
            res = ("ON", nd) if self.overnight and self.is_trading_day(nd) else ("CLOSED", d)
        elif not self.is_trading_day(d):
            res = ("CLOSED", d)
        else:
            early = d in self.early_closes
            reg_end = REG_END_EARLY if early else REG_END
            post_end = POST_END_EARLY if early else POST_END
            if m < REG_START:
                res = ("PRE", d)
            elif m < reg_end:
                res = ("REG", d)
            elif m < post_end:
                res = ("POST", d)
            else:
                res = ("CLOSED", d)
        if len(self._cache) > 200_000:
            self._cache.clear()
        self._cache[ts] = res
        return res


@lru_cache(maxsize=4096)
def et_minute(ts: int) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")
