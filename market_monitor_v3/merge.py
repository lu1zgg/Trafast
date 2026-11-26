"""联动合并与限流。

- 同一市场、同一方向、3 分钟内确认的多个标的并成一条“联动”消息。
- V4 分组联动：同一自选分组的标的 5 分钟内都能并进来；同一分组同向 ≥ 3 只时，标题写“某某板块联动上涨 / 下跌”。
- 首条等一个很短的批次窗口（L3 约 2 秒，其余约 8 秒）再发，让同一分钟收盘的标的进同一条。
- 已经发出后才加入的标的：静默编辑原消息补进“同时”一行；如果它是 L3 而原消息级别更低，另外回复一条。
- 全局每分钟最多发 6 条新消息，超出的合并成一条摘要。
- 所有计时都由 tick(now) 驱动，回放时换成虚拟时钟。
"""
from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from . import render
from .config import Params
from .models import LEVEL_RANK, Candidate, Episode, OutMsg, RootMessage, display_name
from .sink import Sink


@dataclass
class OpenAlert:
    ep: Episode
    cand: Candidate
    level: str
    retained: float | None
    price: float
    close_ts: int
    confirm_bars: int = 1


@dataclass
class Group:
    key: str
    market: str
    direction: int
    first_close_ts: int
    deadline: float
    members: list[OpenAlert] = field(default_factory=list)
    flushed: bool = False
    held: bool = False
    lead: OpenAlert | None = None
    flushed_at: float = 0.0


def _card(m: OpenAlert) -> dict:
    return render.card_from(m.cand, m.level, m.retained, m.price, m.close_ts, ep_id=m.ep.id, confirm_bars=m.confirm_bars)


def _lead(members: list[OpenAlert]) -> OpenAlert:
    return max(members, key=lambda m: (LEVEL_RANK[m.level], m.cand.score))


class Merger:
    def __init__(self, params: Params, sink: Sink, buttons: bool = True):
        self.params = params
        self.sink = sink
        self.buttons = buttons
        self.groups: list[Group] = []
        self.roots: dict[str, RootMessage] = {}
        self.sent: deque[float] = deque()
        self.held: list[Group] = []
        # 标的 → 所在的自选分组；由引擎按配置设置
        self.groups_of: Callable[[str], list[str]] = lambda symbol: []

    # ---- 入口 ----
    def add(self, oa: OpenAlert, now: float) -> None:
        mc = self.params.merge
        for g in self.groups:
            dt = oa.close_ts - g.first_close_ts
            if (
                g.market == oa.cand.market
                and g.direction == oa.cand.direction
                and 0 <= dt
                and (dt <= mc.window_s or (dt <= mc.group_window_s and self._shares_group(g, oa)))
            ):
                if not g.flushed:
                    g.members.append(oa)
                    oa.ep.root_key = g.key
                    if oa.level == "L3" and not g.held:
                        g.deadline = min(g.deadline, now + mc.fast_batch_delay_s)
                else:
                    self._late_join(g, oa, now)
                return
        delay = mc.fast_batch_delay_s if oa.level == "L3" else mc.batch_delay_s
        key = f"g{oa.close_ts}{oa.cand.market[0]}{'u' if oa.cand.direction > 0 else 'd'}{display_name(oa.cand.symbol)}"
        g = Group(key=key, market=oa.cand.market, direction=oa.cand.direction, first_close_ts=oa.close_ts, deadline=now + delay)
        g.members.append(oa)
        oa.ep.root_key = key
        self.groups.append(g)

    def tick(self, now: float) -> None:
        for g in self.groups:
            if not g.flushed and not g.held and now >= g.deadline:
                if self._budget_left(now) > 0:
                    self._emit_group(g, now)
                else:
                    g.held = True
                    self.held.append(g)
        if self.held and self._budget_left(now) > 0:
            self._emit_summary(now)
        keep = max(self.params.merge.window_s, self.params.merge.group_window_s) + 120
        self.groups = [g for g in self.groups if not (g.flushed and now - g.flushed_at > keep)]

    def end_episode(self, ep: Episode, now: float) -> None:
        root = self.roots.get(ep.root_key or "")
        if root is None:
            return
        card = next((c for c in root.cards if c.get("ep") == ep.id), None)
        if card is not None:
            card["end"] = render.end_info(ep)
        else:
            root.end_lines.append(render.end_line(ep, with_name=len(root.symbols) > 1))
        self._emit_edit(root, now)

    def gc(self, active_root_keys: set[str]) -> None:
        open_keys = {g.key for g in self.groups}
        for key in list(self.roots):
            if key not in active_root_keys and key not in open_keys:
                del self.roots[key]

    def restore(self, roots: list[RootMessage]) -> None:
        for r in roots:
            self.roots[r.key] = r

    # ---- 内部 ----
    def _shares_group(self, g: Group, oa: OpenAlert) -> bool:
        mine = set(self.groups_of(oa.cand.symbol))
        return bool(mine) and any(mine & set(self.groups_of(m.cand.symbol)) for m in g.members)

    def _title(self, members: list[OpenAlert], direction: int) -> str:
        """同一分组同向的标的达到 group_min 只：标题写分组名（取人数最多的分组）。"""
        count: Counter[str] = Counter()
        for m in members:
            for grp in set(self.groups_of(m.cand.symbol)):
                count[grp] += 1
        if not count:
            return ""
        grp, n = count.most_common(1)[0]
        if n < self.params.merge.group_min:
            return ""
        return render.group_title(grp, direction, n)

    def _budget_left(self, now: float) -> int:
        while self.sent and now - self.sent[0] >= 60:
            self.sent.popleft()
        return self.params.merge.max_new_per_min - len(self.sent)

    def _expires(self, members: list[OpenAlert]) -> float:
        return min(m.close_ts for m in members) + self.params.merge.expiry_s

    def _emit_group(self, g: Group, now: float) -> None:
        lead = _lead(g.members)
        others = [m for m in g.members if m is not lead]
        root = RootMessage(
            key=g.key,
            market=g.market,
            direction=g.direction,
            lead_html="",
            symbols=[m.cand.symbol for m in g.members],
            buttons=render.feedback_buttons(g.key) if self.buttons else None,
            cards=[_card(m) for m in [lead, *others]],
            title=self._title(g.members, g.direction),
        )
        g.lead, g.flushed, g.flushed_at = lead, True, now
        self.roots[g.key] = root
        for m in g.members:
            m.ep.root_key = g.key
            self.sink.episode(m.ep)
        self.sink.root(root)
        self.sink.out(
            OutMsg(
                key=g.key,
                kind="send",
                text=root.text(),
                created_at=now,
                expires_at=self._expires(g.members),
                level=lead.level,
                buttons=root.buttons,
                market=g.market,
                direction=g.direction,
            )
        )
        self.sent.append(now)

    def _emit_summary(self, now: float) -> None:
        groups, self.held = self.held, []
        key = f"s{int(now)}"
        lines = [render.summary_header(len(groups))]
        all_members: list[OpenAlert] = []
        top = "L1"
        for g in groups:
            lead = _lead(g.members)
            c = lead.cand
            others = [render.member_item(m.cand) for m in g.members if m is not lead]
            lines.append(render.summary_line(c, lead.level, lead.price, others))
            all_members.extend(g.members)
            if LEVEL_RANK[lead.level] > LEVEL_RANK[top]:
                top = lead.level
        lines.append(render.times_line(max(m.close_ts for m in all_members), groups[0].market))
        root = RootMessage(
            key=key,
            market=groups[0].market,
            direction=0,
            lead_html="\n".join(lines),
            symbols=[m.cand.symbol for m in all_members],
            buttons=render.feedback_buttons(key) if self.buttons else None,
        )
        self.roots[key] = root
        for g in groups:
            g.key, g.flushed, g.held, g.flushed_at = key, True, False, now
            g.lead = _lead(g.members)
        for m in all_members:
            m.ep.root_key = key
            self.sink.episode(m.ep)
        self.sink.root(root)
        self.sink.out(
            OutMsg(
                key=key,
                kind="send",
                text=root.text(),
                created_at=now,
                expires_at=self._expires(all_members),
                level=top,
                buttons=root.buttons,
                market=root.market,
            )
        )
        self.sent.append(now)

    def _late_join(self, g: Group, oa: OpenAlert, now: float) -> None:
        oa.ep.root_key = g.key
        g.members.append(oa)
        root = self.roots.get(g.key)
        if root is None:
            return
        root.symbols.append(oa.cand.symbol)
        if root.cards:
            root.cards.append(_card(oa))
            root.title = self._title(g.members, g.direction)
        else:
            root.members.append(render.member_item(oa.cand))
        if g.lead is not None and oa.level == "L3" and LEVEL_RANK["L3"] > LEVEL_RANK[g.lead.level]:
            self.sink.out(
                OutMsg(
                    key=f"{oa.ep.id}:j",
                    kind="reply",
                    text=render.joiner_reply(oa.cand, oa.level),
                    parent_key=g.key,
                    created_at=now,
                    expires_at=oa.close_ts + self.params.merge.expiry_s,
                    level="L3",
                    market=oa.cand.market,
                    direction=oa.cand.direction,
                )
            )
        self._emit_edit(root, now)
        self.sink.episode(oa.ep)

    def _emit_edit(self, root: RootMessage, now: float) -> None:
        root.rev += 1
        self.sink.root(root)
        self.sink.out(
            OutMsg(
                key=f"{root.key}:e{root.rev}",
                kind="edit",
                text=root.text(),
                parent_key=root.key,
                created_at=now,
                expires_at=now + 1800,
                buttons=root.buttons,
                silent=True,
                market=root.market,
                direction=root.direction,
            )
        )
