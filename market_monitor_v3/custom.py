"""V4 自定义提醒：用户自己写条件（到价、N 分钟涨跌幅、当日涨跌幅、N 分钟成交额），随 1 分钟 K 线收盘计算。

和智能盯盘的异动分开：不进事件状态机、不进联动合并、不计入告警数/天，走自己的卡片。

- 确认方式：默认收盘确认（用收盘价判断，和防插针一致）；touch = 触价即推（到价类用这根 K 线的最高/最低价）。
- 频率：once 触发一次后停用；daily 每个交易日最多一次；always 每次重新满足条件都推，间隔至少 cooldown_min 分钟。
- 条件持续满足时不重复推：触发后要先回到条件之外（重新“上膛”）才会再推。
- 当日涨跌幅的参考价：美股 = 上一个交易日盘中（REG）最后收盘价；加密 = 上一个 UTC 日最后一根 K 线的收盘价。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import Bar, OutMsg, display_name, market_of
from .sessions import ET
from .windows import SymbolHistory

KINDS = ("price_above", "price_below", "move", "day_move", "turnover")
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
        else:
            cond = f"{name} {self.window}m 成交额 ≥ ${_compact(self.value)}"
        extra = FREQ_LABELS.get(self.freq, self.freq)
        if self.touch:
            extra += "，触价即推"
        return f"{cond}（{extra}）"


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

USAGE = (
    "用法：\n"
    "/alert NVDA > 140　到价（上穿）；&lt; 为下穿\n"
    "/alert BTC 5m -2%　5 分钟跌 2%；写 +2% 为涨，±2% 为涨跌都算\n"
    "/alert NVDA 当日 5%　当日涨跌幅（相对昨收）\n"
    "/alert MU 5m $20M　5 分钟成交额\n"
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
    return datetime.fromtimestamp(bar.ts, UTC).strftime("%Y-%m-%d")


@dataclass
class CustomAlerts:
    """引擎持有一个实例。rules 由运行时从数据库载入；触发后通过 sink.custom 写回状态。"""

    rules: dict[int, CustomRule] = field(default_factory=dict)
    days: dict[str, _Day] = field(default_factory=dict)

    def set_rules(self, rules: list[CustomRule]) -> None:
        self.rules = {r.id: r for r in rules}

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
        for rule in list(self.rules.values()):
            if not rule.enabled or rule.symbol != bar.symbol:
                continue
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
    else:
        head = f"🔔 {name} {rule.window} 分钟成交额 {b(money(detail['turnover']))}"
    sess = SESSION_LABELS.get(bar.session, bar.session) if market_of(bar.symbol) == "US" else ""
    line2 = f"现价 {b(usd(bar.c))}" + (f"（{sess}）" if sess else "")
    if rule.kind == "day_move":
        line2 += f"｜昨收 {b(usd(detail['ref']))}"
    elif rule.kind == "move":
        line2 += f"｜起点 {b(usd(detail['ref']))}"
    return "\n".join([
        head,
        line2,
        f"自定义提醒 #{rule.id}：{esc(rule.describe())}",
        times_line(bar.close_ts, market_of(bar.symbol)),
    ])


def _pct(chg: float) -> str:
    a = abs(chg) * 100
    return f"{a:.{2 if a < 1 else 1}f}%"
