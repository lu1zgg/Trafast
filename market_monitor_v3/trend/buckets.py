"""K 线切分：1 分钟 → 15 分钟 → 1 小时 / 4 小时。

加密货币按 UTC 整点切（和币安一致）。
美股只用正常盘（09:30–16:00 美东）：15m、1h 从 09:30 开始切，最后一根 1h 只有 30 分钟；
“4h”改成每天两根：09:30–13:30、13:30–16:00（提前收盘日在 13:00 截止）。
"""
from __future__ import annotations

from datetime import datetime
from datetime import time as dtime
from typing import Protocol

M15 = 900
H1 = 3600
H4 = 4 * 3600


class Bucketer(Protocol):
    def key15(self, ts: int) -> tuple[int, int] | None: ...
    def hkey(self, tf: str, start15: int) -> tuple[int, int]: ...
    def day_of(self, ts: int) -> int: ...


class UtcBucketer:
    """24 小时连续交易的品种。"""

    def key15(self, ts: int) -> tuple[int, int] | None:
        s = ts - ts % M15
        return s, s + M15

    def hkey(self, tf: str, start15: int) -> tuple[int, int]:
        n = H1 if tf == "1h" else H4
        s = start15 - start15 % n
        return s, s + n

    def day_of(self, ts: int) -> int:
        return ts // 86400


class UsRegularBucketer:
    """美股正常盘。cal 是 sessions.UsCalendar（只用到 session_at 和 early_closes）。"""

    def __init__(self, cal) -> None:  # noqa: ANN001
        from ..sessions import ET

        self.cal = cal
        self.tz = ET
        self._day: dict[int, tuple[int, int]] = {}

    def _open_close(self, ts: int) -> tuple[int, int]:
        d = datetime.fromtimestamp(ts, self.tz).date()
        key = d.toordinal()
        hit = self._day.get(key)
        if hit is None:
            o = int(datetime.combine(d, dtime(9, 30), self.tz).timestamp())
            early = d in self.cal.early_closes
            c = int(datetime.combine(d, dtime(13, 0) if early else dtime(16, 0), self.tz).timestamp())
            hit = (o, c)
            if len(self._day) > 4000:
                self._day.clear()
            self._day[key] = hit
        return hit

    def key15(self, ts: int) -> tuple[int, int] | None:
        if self.cal.session_at(ts) != "REG":
            return None
        o, c = self._open_close(ts)
        if not (o <= ts < c):
            return None
        s = o + (ts - o) // M15 * M15
        return s, min(s + M15, c)

    def hkey(self, tf: str, start15: int) -> tuple[int, int]:
        o, c = self._open_close(start15)
        if tf == "1h":
            s = o + (start15 - o) // H1 * H1
            return s, min(s + H1, c)
        if start15 - o < H4:
            return o, min(o + H4, c)
        return o + H4, c

    def day_of(self, ts: int) -> int:
        return datetime.fromtimestamp(ts, self.tz).date().toordinal()
