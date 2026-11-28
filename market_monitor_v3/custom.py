"""V4 自定义提醒：用户自己写条件（到价、N 分钟涨跌幅、当日涨跌幅、N 分钟成交额、技术指标），随 1 分钟 K 线收盘计算。

和智能盯盘的异动分开：不进事件状态机、不进联动合并、不计入告警数/天，走自己的卡片。

- 确认方式：默认收盘确认（用收盘价判断，和防插针一致）；touch = 触价即推（到价类用这根 K 线的最高/最低价）。
- 频率：once 触发一次后停用；daily 每个交易日最多一次；always 每次重新满足条件都推，间隔至少 cooldown_min 分钟。
- 条件持续满足时不重复推：触发后要先回到条件之外（重新“上膛”）才会再推。
- 当日涨跌幅的参考价：美股 = 上一个交易日盘中（REG）最后收盘价；加密 = 上一个 UTC 日最后一根 K 线的收盘价。
- 指标提醒（V4 P3）：EMA 金叉 / 死叉、RSI 越过某个值、MACD 金叉 / 死叉、收盘突破布林带，周期 1m / 5m / 15m / 1h。
  在该周期的 K 线收盘时计算（由 1 分钟 K 线按 UTC 整点切分合成，美股包含盘前盘后）。启动时从库里补够历史。
"""
from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import Bar, OutMsg, display_name, market_of
from .sessions import ET, commodity_instance
from .windows import SymbolHistory

KINDS = ("price_above", "price_below", "move", "day_move", "turnover", "indicator")
INDICATORS = ("ema", "rsi", "macd", "boll")
IND_TFS = (1, 5, 15, 60)
SERIES_LEN = 400  # 每个周期留多少根收盘价
FREQS = ("once", "daily", "always")
FREQ_LABELS = {"once": "一次", "daily": "每天一次", "always": "持续"}
WINDOWS_ALLOWED = (1, 5, 15, 30, 60)


@dataclass
class CustomRule:
    id: int
    symbol: str
    kind: str
    value: float  # 价格；或涨跌幅（小数，带符号；both 时取绝对值）；或成交额
    window: int = 1  # move / turnover 的分钟数
    both: bool = False  # move / day_move：涨跌都算
    freq: str = "once"
    touch: bool = False
    cooldown_min: int = 30
    enabled: bool = True
    created_by: str = ""
    created_at: float = 0.0
    last_fired_at: float | None = None
    last_fired_day: str | None = None
    armed: bool = True
    fired: int = 0
    # 指标提醒：ind = ema / rsi / macd / boll；tf 周期分钟；p1/p2 参数（EMA 快慢线、RSI 周期）；
    # side = +1 只要向上（金叉、上穿、突破上轨），−1 只要向下，0 都要；RSI 的界线放在 value
    ind: str = ""
    tf: int = 0
    p1: float = 0.0
    p2: float = 0.0
    side: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "CustomRule":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def describe(self) -> str:
        """规则的简写，和 /alert 的写法一致。"""
        name = display_name(self.symbol)
        if self.kind == "price_above":
            cond = f"{name} > {_num(self.value)}"
        elif self.kind == "price_below":
            cond = f"{name} < {_num(self.value)}"
        elif self.kind == "move":
            cond = f"{name} {self.window}m {_signed_pct(self.value, self.both)}"
        elif self.kind == "day_move":
            cond = f"{name} 当日 {_signed_pct(self.value, self.both)}"
        elif self.kind == "indicator":
            cond = f"{name} {_tf(self.tf)} {ind_desc(self)}"
        else:
            cond = f"{name} {self.window}m 成交额 ≥ ${_compact(self.value)}"
        extra = FREQ_LABELS.get(self.freq, self.freq)
        if self.touch:
            extra += "，触价即推"
        return f"{cond}（{extra}）"


def _tf(m: int) -> str:
    return f"{m // 60}h" if m >= 60 else f"{m}m"


def ind_desc(r: "CustomRule") -> str:
    up_dn = {1: "金叉", -1: "死叉", 0: "金叉或死叉"}
    if r.ind == "ema":
        return f"EMA{int(r.p1)}/EMA{int(r.p2)} {up_dn[r.side]}"
    if r.ind == "macd":
        return f"MACD {up_dn[r.side]}"
    if r.ind == "rsi":
        word = {1: "上穿", -1: "下穿", 0: "穿过"}[r.side]
        return f"RSI{int(r.p1)} {word} {r.value:g}"
    word = {1: "突破上轨", -1: "跌破下轨", 0: "突破上轨或跌破下轨"}[r.side]
    return f"布林带(20, 2) {word}"


def _num(x: float) -> str:
    return f"{x:g}"


def _signed_pct(v: float, both: bool) -> str:
    p = abs(v) * 100
    if both:
        return f"±{p:g}%"
    return f"{'+' if v > 0 else '-'}{p:g}%"


def _compact(x: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{x / div:g}{suf}"
    return f"{x:g}"


# ---- 解析 /alert 的写法 ----
_PCT = re.compile(r"^([+\-±]|\+-)?(\d+(?:\.\d+)?)%$")
_WIN = re.compile(r"^(\d+)\s*(m|min|分钟|h)$", re.I)
_MONEY = re.compile(r"^\$?(\d+(?:\.\d+)?)([kmb])?$", re.I)
_PRICE = re.compile(r"^\$?(\d+(?:\.\d+)?)$")
_OPS = {">": "price_above", ">=": "price_above", "above": "price_above", "上穿": "price_above", "高于": "price_above",
        "<": "price_below", "<=": "price_below", "below": "price_below", "下穿": "price_below", "低于": "price_below"}
_DAY = {"day", "today", "当日", "今日", "日内", "今天"}
_FREQ_WORDS = {"once": "once", "一次": "once", "daily": "daily", "每天": "daily", "always": "always", "持续": "always",
               "repeat": "always"}
_TOUCH = {"touch", "触价", "即时"}

_IND_WORDS = {"ema": "ema", "均线": "ema", "rsi": "rsi", "macd": "macd", "boll": "boll", "bb": "boll", "布林": "boll", "布林带": "boll"}
_UP = {"up", "金叉", "上穿", "向上", "突破", "上"}
_DOWN = {"down", "死叉", "下穿", "向下", "跌破", "下"}

USAGE = (
    "用法：\n"
    "/alert NVDA > 140　到价（上穿）；&lt; 为下穿\n"
    "/alert BTC 5m -2%　5 分钟跌 2%；写 +2% 为涨，±2% 为涨跌都算\n"
    "/alert NVDA 当日 5%　当日涨跌幅（相对昨收）\n"
    "/alert MU 5m $20M　5 分钟成交额\n"
    "/alert BTC 1h ema 20 50 金叉　EMA 金叉 / 死叉（不写方向 = 都提醒）\n"
    "/alert NVDA 15m rsi &gt; 70　RSI(14) 上穿 70；&lt; 30 为下穿\n"
    "/alert BTC 1h macd 死叉　/alert ETH 15m boll 上　布林带上轨 / 下轨\n"
    "指标周期：1m、5m、15m、1h，按该周期 K 线收盘计算\n"
    "末尾可加 once / daily / always（一次 / 每天一次 / 持续），以及 touch（触价即推，默认收盘确认）"
)


def parse_rule(args: list[str], resolve: Callable[[str], str | None]) -> CustomRule | str:
    """把 /alert 后面的参数解析成规则；出错时返回中文说明。id 由存储分配，这里填 0。"""
    toks: list[str] = []
    for a in args:
        # “NVDA>140” 这种连写拆开
        toks += [t for t in re.split(r"(>=|<=|>|<)", a) if t.strip()]
    toks = [t.strip() for t in toks if t.strip()]
    if len(toks) < 2:
        return USAGE
    sym = resolve(toks[0])
    if sym is None:
        return f"没找到标的 {toks[0]}。"
    freq: str | None = None
    touch = False
    rest = []
    for t in toks[1:]:
        low = t.lower()
        if low in _FREQ_WORDS:
            freq = _FREQ_WORDS[low]
        elif low in _TOUCH:
            touch = True
        else:
            rest.append(t)
    rule: CustomRule | None = None
    if len(rest) >= 2 and _WIN.match(rest[0]) and rest[1].lower() in _IND_WORDS:
        got = _parse_indicator(sym, rest)
        if isinstance(got, str):
            return got
        got.freq = freq or "always"
        got.cooldown_min = got.tf
        got.touch = False
        return got
    if len(rest) == 2 and rest[0].lower() in _OPS:
        m = _PRICE.match(rest[1])
        if not m:
            return f"价格写得不对：{rest[1]}"
        rule = CustomRule(0, sym, _OPS[rest[0].lower()], float(m.group(1)))
    elif len(rest) == 2 and rest[0].lower() in _DAY:
        pct = _parse_pct(rest[1])
        if pct is None:
            return f"涨跌幅写得不对：{rest[1]}（例如 5%、-3%、±4%）"
        rule = CustomRule(0, sym, "day_move", pct[0], both=pct[1])
    elif len(rest) in (2, 3) and _WIN.match(rest[0]):
        wm = _WIN.match(rest[0])
        assert wm is not None
        window = int(wm.group(1)) * (60 if wm.group(2).lower() == "h" else 1)
        if window not in WINDOWS_ALLOWED:
            return f"窗口只支持 {'、'.join(f'{w}m' for w in WINDOWS_ALLOWED)}"
        val = rest[-1]
        pct = _parse_pct(val)
        if pct is not None and len(rest) == 2:
            rule = CustomRule(0, sym, "move", pct[0], window=window, both=pct[1])
        else:
            mm = _MONEY.match(val)
            if not mm or (len(rest) == 3 and rest[1] not in ("成交额", "额", "turnover", "vol")):
                return USAGE
            mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((mm.group(2) or "").lower(), 1.0)
            rule = CustomRule(0, sym, "turnover", float(mm.group(1)) * mult, window=window)
    if rule is None:
        return USAGE
    if rule.value == 0:
        return "数值不能是 0。"
    rule.freq = freq or ("once" if rule.kind in ("price_above", "price_below") else "daily")
    rule.touch = touch
    return rule


def _parse_indicator(sym: str, rest: list[str]) -> CustomRule | str:
    wm = _WIN.match(rest[0])
    assert wm is not None
    tf = int(wm.group(1)) * (60 if wm.group(2).lower() == "h" else 1)
    if tf not in IND_TFS:
        return "指标周期只支持 1m、5m、15m、1h"
    ind = _IND_WORDS[rest[1].lower()]
    args = rest[2:]
    side = 0
    nums: list[float] = []
    op: str | None = None
    for a in args:
        low = a.lower()
        if low in _UP:
            side = 1
        elif low in _DOWN:
            side = -1
        elif low in _OPS:
            op = _OPS[low]
        else:
            try:
                nums.append(float(a))
            except ValueError:
                return f"看不懂“{a}”。" + USAGE
    r = CustomRule(0, sym, "indicator", 0.0, ind=ind, tf=tf, side=side)
    if ind == "ema":
        fast, slow = (nums + [20, 50][len(nums):])[:2] if len(nums) < 2 else nums[:2]
        if not (2 <= fast < slow <= 200):
            return "EMA 写快线和慢线周期，例如 ema 20 50（快线要小于慢线，最大 200）"
        r.p1, r.p2 = float(int(fast)), float(int(slow))
    elif ind == "rsi":
        if not nums or op is None:
            return "RSI 写成 rsi > 70 或 rsi < 30"
        level = nums[-1]
        if not 0 < level < 100:
            return "RSI 的值要在 0 到 100 之间"
        r.value, r.p1 = level, 14.0
        r.side = 1 if op == "price_above" else -1
    elif ind == "macd":
        r.p1, r.p2 = 12.0, 26.0
    return r


def _parse_pct(s: str) -> tuple[float, bool] | None:
    m = _PCT.match(s.replace("＋", "+").replace("－", "-"))
    if not m:
        return None
    sign = m.group(1) or ""
    v = float(m.group(2)) / 100
    if sign in ("±", "+-"):
        return v, True
    if sign == "-":
        return -v, False
    if sign == "+":
        return v, False
    return v, True  # 不写正负号 = 涨跌都算


# ---- 计算 ----
@dataclass
class _Day:
    key: str = ""
    last: float | None = None  # 当天最后的参考收盘（美股只记 REG）
    ref: float | None = None  # 上一个交易日的参考价


def day_key(bar: Bar) -> str:
    if market_of(bar.symbol) == "US":
        return datetime.fromtimestamp(bar.ts, ET).strftime("%Y-%m-%d")
    if market_of(bar.symbol) == "CMDTY":
        # 大宗商品按 CME 交易日：美东 18:00 以后算下一天
        return commodity_instance(bar.ts).split(":", 1)[1]
    return datetime.fromtimestamp(bar.ts, UTC).strftime("%Y-%m-%d")


# ---- 指标 ----
def ema_series(xs: list[float], n: int) -> list[float]:
    if not xs:
        return []
    k = 2 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def rsi_last2(xs: list[float], n: int = 14) -> tuple[float, float] | None:
    if len(xs) < n + 2:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = xs[i] - xs[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / n, losses / n
    vals = []
    for i in range(n + 1, len(xs)):
        ch = xs[i] - xs[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
        vals.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return (vals[-2], vals[-1]) if len(vals) >= 2 else None


def boll_last(xs: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    if len(xs) < n:
        return None
    w = xs[-n:]
    mid = sum(w) / n
    sd = math.sqrt(sum((x - mid) ** 2 for x in w) / n)
    return mid - k * sd, mid, mid + k * sd


def min_bars(r: "CustomRule") -> int:
    if r.ind == "ema":
        return int(r.p2) * 3
    if r.ind == "macd":
        return 26 * 3 + 9
    if r.ind == "rsi":
        return 14 * 4
    return 22


@dataclass
class _Series:
    """某个标的某个周期的收盘价序列（由 1 分钟 K 线合成）。"""

    tf: int
    key: int = -1  # 当前这根的开始时间
    close: float = 0.0
    closes: deque[float] = field(default_factory=lambda: deque(maxlen=SERIES_LEN))
    closed_at: int = -1  # 最近一根收盘的开始时间

    def feed(self, bar: Bar) -> bool:
        """喂一根 1 分钟 K 线；这根周期 K 线刚好收盘时返回 True。"""
        span = self.tf * 60
        k = bar.ts - bar.ts % span
        if self.key != k:
            if self.key >= 0 and self.closed_at != self.key:
                self.closes.append(self.close)  # 上一根没等到最后一分钟（缺数据）：也算收盘
                self.closed_at = self.key
            self.key = k
        self.close = bar.c
        if bar.ts + 60 == k + span:
            self.closes.append(bar.c)
            self.closed_at = k
            return True
        return False


def ind_check(r: "CustomRule", xs: list[float]) -> tuple[bool | None, dict[str, Any]]:
    if len(xs) < min_bars(r):
        return None, {}
    if r.ind in ("ema", "macd"):
        if r.ind == "ema":
            a, b2 = ema_series(xs, int(r.p1)), ema_series(xs, int(r.p2))
            diff = [x - y for x, y in zip(a, b2, strict=True)]
            info = {"fast": a[-1], "slow": b2[-1]}
        else:
            a, b2 = ema_series(xs, 12), ema_series(xs, 26)
            macd = [x - y for x, y in zip(a, b2, strict=True)]
            sig = ema_series(macd, 9)
            diff = [x - y for x, y in zip(macd, sig, strict=True)]
            info = {"macd": macd[-1], "signal": sig[-1]}
        prev, cur = diff[-2], diff[-1]
        up = prev <= 0 < cur
        down = prev >= 0 > cur
        d = 1 if up else -1 if down else 0
        ok = d != 0 and (r.side == 0 or r.side == d)
        return ok, {"dir": d or (1 if cur > 0 else -1), "px": xs[-1], **info}
    if r.ind == "rsi":
        got = rsi_last2(xs, int(r.p1 or 14))
        if got is None:
            return None, {}
        prev, cur = got
        if r.side > 0:
            ok = prev < r.value <= cur
        else:
            ok = prev > r.value >= cur
        return ok, {"dir": 1 if r.side > 0 else -1, "px": xs[-1], "rsi": cur}
    band = boll_last(xs)
    prev_band = boll_last(xs[:-1])
    if band is None or prev_band is None:
        return None, {}
    lo, mid, hi = band
    px, prev_px = xs[-1], xs[-2]
    up = px > hi and prev_px <= prev_band[2]
    down = px < lo and prev_px >= prev_band[0]
    d = 1 if up else -1 if down else 0
    ok = d != 0 and (r.side == 0 or r.side == d)
    return ok, {"dir": d or 1, "px": px, "upper": hi, "lower": lo, "mid": mid}


@dataclass
class CustomAlerts:
    """引擎持有一个实例。rules 由运行时从数据库载入；触发后通过 sink.custom 写回状态。"""

    rules: dict[int, CustomRule] = field(default_factory=dict)
    days: dict[str, _Day] = field(default_factory=dict)
    series: dict[tuple[str, int], _Series] = field(default_factory=dict)

    def set_rules(self, rules: list[CustomRule]) -> None:
        self.rules = {r.id: r for r in rules}

    def needs_series(self) -> dict[str, int]:
        """有指标提醒的标的 → 需要补多少分钟的 1 分钟历史。"""
        out: dict[str, int] = {}
        for r in self.rules.values():
            if r.kind == "indicator" and r.enabled:
                mins = r.tf * (min_bars(r) + 5)
                out[r.symbol] = max(out.get(r.symbol, 0), mins)
        return out

    def seed_series(self, symbol: str, bars: Iterable[Bar]) -> None:
        """用历史 1 分钟 K 线重建这个标的所有指标周期的序列。"""
        tfs = {r.tf for r in self.rules.values() if r.kind == "indicator" and r.symbol == symbol}
        for tf in tfs:
            self.series[(symbol, tf)] = _Series(tf)
        for b in sorted(bars, key=lambda x: x.ts):
            if b.synthetic or b.c <= 0:
                continue
            for tf in tfs:
                self.series[(symbol, tf)].feed(b)

    def seed_ref(self, symbol: str, price: float | None) -> None:
        """启动时用数据库里上一个交易日的收盘价补参考价。"""
        if price and price > 0:
            st = self.days.setdefault(symbol, _Day())
            if st.ref is None:
                st.ref = price

    def observe(self, bar: Bar) -> None:
        st = self.days.setdefault(bar.symbol, _Day())
        key = day_key(bar)
        if st.key != key:
            if st.key and st.last is not None:
                st.ref = st.last
            st.key, st.last = key, None
        if bar.synthetic or bar.c <= 0:
            return
        if market_of(bar.symbol) != "US" or bar.session == "REG":
            st.last = bar.c

    def day_ref(self, symbol: str) -> float | None:
        st = self.days.get(symbol)
        return st.ref if st else None

    def on_bar(self, bar: Bar, hist: SymbolHistory, now: float, emit: Callable[[CustomRule, OutMsg], None]) -> None:
        self.observe(bar)
        if bar.synthetic:
            return
        closed: set[int] = set()
        for (sym, tf), ser in self.series.items():
            if sym == bar.symbol and bar.ts >= ser.key and bar.c > 0 and ser.feed(bar):
                closed.add(tf)
        for rule in list(self.rules.values()):
            if not rule.enabled or rule.symbol != bar.symbol:
                continue
            if rule.kind == "indicator":
                ser = self.series.get((rule.symbol, rule.tf))
                if ser is None:
                    ser = self.series[(rule.symbol, rule.tf)] = _Series(rule.tf)
                    ser.feed(bar)
                if rule.tf not in closed:
                    continue
                hit, detail = ind_check(rule, list(ser.closes))
            else:
                hit, detail = self._check(rule, bar, hist)
            if hit is None:
                continue
            if not hit:
                rule.armed = True
                continue
            if not rule.armed:
                continue
            dk = day_key(bar)
            if rule.freq == "daily" and rule.last_fired_day == dk:
                continue
            if rule.freq == "always" and rule.last_fired_at and now - rule.last_fired_at < rule.cooldown_min * 60:
                continue
            rule.armed = False
            rule.last_fired_at = now
            rule.last_fired_day = dk
            rule.fired += 1
            if rule.freq == "once":
                rule.enabled = False
            emit(rule, OutMsg(
                key=f"c{rule.id}:{bar.close_ts}",
                kind="send",
                text=card_text(rule, bar, detail),
                created_at=now,
                expires_at=bar.close_ts + 600,
                level="L1",
                market=market_of(bar.symbol),
                direction=1 if detail.get("dir", 0) > 0 else -1,
            ))

    def _check(self, rule: CustomRule, bar: Bar, hist: SymbolHistory) -> tuple[bool | None, dict[str, Any]]:
        """返回（是否满足，用于卡片的数据）；数据不够时返回 None（不改变上膛状态）。"""
        if rule.kind in ("price_above", "price_below"):
            above = rule.kind == "price_above"
            px = (bar.h if above else bar.l) if rule.touch else bar.c
            ok = px >= rule.value if above else px <= rule.value
            return ok, {"dir": 1 if above else -1, "px": px}
        if rule.kind == "move":
            b0 = hist.get(bar.ts - 60 * rule.window)
            if b0 is None or b0.c <= 0:
                return None, {}
            return self._pct_hit(rule, bar.c / b0.c - 1, b0.c)
        if rule.kind == "day_move":
            ref = self.day_ref(bar.symbol)
            if not ref:
                return None, {}
            return self._pct_hit(rule, bar.c / ref - 1, ref)
        if rule.kind == "turnover":
            tv = hist.turnover(bar.ts, rule.window)
            b0 = hist.get(bar.ts - 60 * rule.window)
            d = (bar.c / b0.c - 1) if b0 is not None and b0.c > 0 else 0.0
            return tv >= rule.value, {"dir": 1 if d >= 0 else -1, "turnover": tv, "chg": d}
        return None, {}

    @staticmethod
    def _pct_hit(rule: CustomRule, chg: float, ref: float) -> tuple[bool, dict[str, Any]]:
        if rule.both:
            ok = abs(chg) >= abs(rule.value)
        elif rule.value > 0:
            ok = chg >= rule.value
        else:
            ok = chg <= rule.value
        return ok, {"dir": 1 if chg >= 0 else -1, "chg": chg, "ref": ref}


def card_text(rule: CustomRule, bar: Bar, detail: dict[str, Any]) -> str:
    from .config import SESSION_LABELS
    from .render import b, esc, money, times_line, usd

    name = b(esc(display_name(rule.symbol)))
    up = detail.get("dir", 1) > 0
    if rule.kind in ("price_above", "price_below"):
        head = f"🔔 {name} {'上穿' if rule.kind == 'price_above' else '下穿'} {b(usd(rule.value))}"
    elif rule.kind == "move":
        head = f"🔔 {name} {rule.window} 分钟{'涨' if up else '跌'} {b(_pct(detail['chg']))}"
    elif rule.kind == "day_move":
        head = f"🔔 {name} 当日{'涨' if up else '跌'} {b(_pct(detail['chg']))}"
    elif rule.kind == "indicator":
        head = f"🔔 {name} {_tf(rule.tf)} {esc(ind_event(rule, detail))}"
    else:
        head = f"🔔 {name} {rule.window} 分钟成交额 {b(money(detail['turnover']))}"
    sess = SESSION_LABELS.get(bar.session, bar.session) if market_of(bar.symbol) == "US" else ""
    line2 = f"现价 {b(usd(bar.c))}" + (f"（{sess}）" if sess else "")
    if rule.kind == "day_move":
        line2 += f"｜昨收 {b(usd(detail['ref']))}"
    elif rule.kind == "move":
        line2 += f"｜起点 {b(usd(detail['ref']))}"
    elif rule.kind == "indicator":
        extra = ind_values(rule, detail)
        if extra:
            line2 += "｜" + esc(extra)
    return "\n".join([
        head,
        line2,
        f"自定义提醒 #{rule.id}：{esc(rule.describe())}",
        times_line(bar.close_ts, market_of(bar.symbol)),
    ])


def ind_event(r: CustomRule, d: dict[str, Any]) -> str:
    up = d.get("dir", 1) > 0
    if r.ind == "ema":
        return f"EMA{int(r.p1)}/EMA{int(r.p2)} {'金叉' if up else '死叉'}"
    if r.ind == "macd":
        return f"MACD {'金叉' if up else '死叉'}"
    if r.ind == "rsi":
        return f"RSI{int(r.p1)} {'上穿' if up else '下穿'} {r.value:g}"
    return "收盘突破布林上轨" if up else "收盘跌破布林下轨"


def ind_values(r: CustomRule, d: dict[str, Any]) -> str:
    if r.ind == "ema" and "fast" in d:
        return f"EMA{int(r.p1)} {d['fast']:,.4g}，EMA{int(r.p2)} {d['slow']:,.4g}"
    if r.ind == "rsi" and "rsi" in d:
        return f"RSI {d['rsi']:.1f}"
    if r.ind == "macd" and "macd" in d:
        return f"MACD {d['macd']:.4g}，信号线 {d['signal']:.4g}"
    if r.ind == "boll" and "upper" in d:
        return f"上轨 {d['upper']:,.4g}，下轨 {d['lower']:,.4g}"
    return ""


def _pct(chg: float) -> str:
    a = abs(chg) * 100
    return f"{a:.{2 if a < 1 else 1}f}%"
