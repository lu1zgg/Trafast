"""事件状态机：空闲 → 待确认 → 进行中 → 冷却。

- 待确认：候选出现后等下一根 K 线，位移保留 ≥ 60% 才推送；否则记 L0（这一步挡掉插针）。
- 快速通道（L3）不等确认。
- 进行中：同方向再走 ≥ 1 个 5 分钟地板算“延伸”，回复在原消息下；级别升高也回复。
  每个事件最多 3 条回复，间隔 ≥ 5 分钟（升到 L3 不受间隔限制）。
- 结束：从最大位移回撤超过一半，或 30 分钟没有新高（新低），或出现反向的 L3。
  结束时静默编辑原消息，然后冷却 10 分钟（冷却期只挡同方向的非 L3 候选）。
- 回吐：事件结束后的冷却期再加 15 分钟（最长窗口）内，反方向的候选必须越过事件起点才算新异动，
  否则 15 分钟窗口会在冷却刚结束时把同一段回吐再报一次。
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from . import render
from .config import WINDOWS, Params
from .merge import Merger, OpenAlert
from .models import LEVEL_RANK, Bar, Candidate, Decision, Episode, Evaluation, OutMsg, WindowHit, display_name
from .sink import Sink


@dataclass
class Tracker:
    pending: Candidate | None = None
    episode: Episode | None = None
    cooldown_until: float = 0.0
    cooldown_dir: int = 0
    cooldown_anchor: float = 0.0
    # 被拒绝的插针：15 分钟内，反方向的“回落”必须越过插针起点才算新异动
    guard_until: int = 0
    guard_dir: int = 0
    guard_anchor: float = 0.0


RETRACE_EXTRA_S = max(WINDOWS) * 60


class EpisodeManager:
    def __init__(
        self,
        params: Params,
        merger: Merger,
        sink: Sink,
        is_muted: Callable[[str, float], bool],
        silent_ext: bool = False,
    ):
        self.params = params
        self.merger = merger
        self.sink = sink
        self.is_muted = is_muted
        self.silent_ext = silent_ext
        self.trackers: dict[str, Tracker] = {}

    def tracker(self, sym: str) -> Tracker:
        tr = self.trackers.get(sym)
        if tr is None:
            tr = self.trackers[sym] = Tracker()
        return tr

    def idle(self, sym: str) -> bool:
        tr = self.trackers.get(sym)
        return tr is None or (tr.pending is None and tr.episode is None)

    def active(self) -> list[Episode]:
        return [t.episode for t in self.trackers.values() if t.episode is not None]

    def restore(self, eps: list[Episode]) -> None:
        for ep in eps:
            if ep.state == "active":
                self.tracker(ep.symbol).episode = ep

    # ---- 主流程 ----
    def on_bar(self, bar: Bar, ev: Evaluation, now: float) -> None:
        sym = bar.symbol
        tr = self.tracker(sym)
        cand = ev.candidate
        ecfg = self.params.episode
        for h in ev.thin_blocked:
            self._decide_hit(bar, h, "thin")

        # 1. 待确认的候选：看这一根 K 线是否站稳
        if tr.pending is not None and bar.ts > tr.pending.bar_ts:
            p, tr.pending = tr.pending, None
            if bar.ts - p.bar_ts > ecfg.pending_max_wait_min * 60:
                self._decide(p, "expired_pending", "L0")
            else:
                retained = p.direction * math.log(bar.c / p.primary.p0) / abs(p.primary.d)
                if retained >= self.params.rules.confirm_ratio:
                    level = p.level
                    if cand is not None and cand.direction == p.direction and LEVEL_RANK[cand.level] > LEVEL_RANK[level]:
                        level = cand.level
                    if self.is_muted(sym, now):
                        self._decide(p, "muted", "L0", retained)
                    else:
                        self._open(tr, p, level, retained, bar, now)
                        if cand is not None and cand.direction == p.direction and tr.episode is not None:
                            self._decide(cand, "absorbed", "L0", episode_id=tr.episode.id)
                            cand = None
                else:
                    self._decide(p, "rejected", "L0", retained)
                    tr.guard_until = bar.close_ts + 15 * 60
                    tr.guard_dir = p.direction
                    tr.guard_anchor = p.primary.p0

        # 2. 进行中的事件：延伸、升级、结束
        if tr.episode is not None and tr.episode.open_close_ts < bar.close_ts:
            if self._update(tr, bar, cand, now):
                cand = None

        # 3. 新候选
        if cand is not None:
            if tr.episode is not None:
                self._decide(cand, "absorbed", "L0", episode_id=tr.episode.id)
            elif self.is_muted(sym, now):
                self._decide(cand, "muted", "L0")
            elif now < tr.cooldown_until and cand.direction == tr.cooldown_dir and not cand.fast:
                self._decide(cand, "cooldown", "L0")
            elif (
                now < tr.cooldown_until + RETRACE_EXTRA_S
                and cand.direction != tr.cooldown_dir
                and not self._beyond(bar, tr.cooldown_dir, tr.cooldown_anchor)
            ):
                # 刚结束的事件往回走，只是回吐，不算新的异动
                self._decide(cand, "retrace", "L0")
            elif (
                bar.close_ts < tr.guard_until
                and cand.direction != tr.guard_dir
                and not self._beyond(bar, tr.guard_dir, tr.guard_anchor)
            ):
                # 插针收回来的那一段，不算反方向异动
                self._decide(cand, "wick_return", "L0")
            elif cand.fast:
                self._open(tr, cand, "L3", None, bar, now)
            else:
                tr.pending = cand

    def on_tick(self, now: float) -> None:
        ecfg = self.params.episode
        for tr in self.trackers.values():
            p = tr.pending
            if p is not None and now - p.close_ts > ecfg.pending_max_wait_min * 60 + 60:
                tr.pending = None
                self._decide(p, "expired_pending", "L0")
            ep = tr.episode
            if ep is not None and now - ep.last_extreme_at >= ecfg.idle_end_min * 60 + 120:
                move = ep.direction * math.log(ep.last_price / ep.anchor)
                self._end(tr, "idle", move, ep.last_extreme_at, now)

    # ---- 内部 ----
    def _open(self, tr: Tracker, c: Candidate, level: str, retained: float | None, bar: Bar, now: float) -> None:
        p = c.primary
        move_now = c.direction * math.log(bar.c / p.p0)
        extreme = max(abs(p.d), move_now)
        ep = Episode(
            id=f"{display_name(c.symbol)}-{c.close_ts}",
            symbol=c.symbol,
            market=c.market,
            direction=c.direction,
            session=c.session,
            k=p.k,
            anchor=p.p0,
            anchor_close_ts=p.anchor_close_ts,
            open_close_ts=bar.close_ts,
            first_d=p.d,
            level=level,
            score=c.score,
            retained=retained,
            extreme=extreme,
            last_report=extreme,
            last_msg_at=now,
            last_extreme_at=bar.close_ts,
            last_price=bar.c,
        )
        tr.episode = ep
        self._decide(c, "pushed", level, retained, ep.id)
        self.merger.add(OpenAlert(ep, c, level, retained, bar.c, bar.close_ts), now)
        self.sink.episode(ep)

    def _update(self, tr: Tracker, bar: Bar, cand: Candidate | None, now: float) -> bool:
        ep = tr.episode
        assert ep is not None
        ecfg = self.params.episode
        move = ep.direction * math.log(bar.c / ep.anchor)
        ep.last_price = bar.c
        if move > ep.extreme:
            ep.extreme = move
            ep.last_extreme_at = bar.close_ts

        if cand is not None and cand.direction != ep.direction and cand.fast:
            beyond = -move >= self.params.floor(ep.symbol, self._sess(bar, ep), 1)
            self._end(tr, "reversal" if beyond else "retrace", move, bar.close_ts, now)
            return False
        if ep.extreme > 0 and ep.extreme - move > ecfg.end_retrace * ep.extreme:
            self._end(tr, "retrace", move, bar.close_ts, now)
            return False
        if bar.close_ts - ep.last_extreme_at >= ecfg.idle_end_min * 60:
            self._end(tr, "idle", move, ep.last_extreme_at, now)
            return False

        consumed = cand is not None and cand.direction == ep.direction
        upgrade = cand.level if consumed and cand is not None and LEVEL_RANK[cand.level] > LEVEL_RANK[ep.level] else None
        step = ecfg.extend_floors * self.params.floor(ep.symbol, self._sess(bar, ep), 5)
        extended = move >= ep.last_report + step
        if (extended or upgrade) and ep.replies < ecfg.max_replies and not self.is_muted(ep.symbol, now):
            if now - ep.last_msg_at >= ecfg.reply_gap_min * 60 or upgrade == "L3":
                minutes = max(1, round((bar.close_ts - ep.anchor_close_ts) / 60))
                ep.replies += 1
                self.sink.out(
                    OutMsg(
                        key=f"{ep.id}:r{ep.replies}",
                        kind="reply",
                        text=render.extension_reply(ep, move, minutes, upgrade),
                        parent_key=ep.root_key,
                        created_at=now,
                        expires_at=bar.close_ts + self.params.merge.expiry_s,
                        level=upgrade or ep.level,
                        silent=self.silent_ext and upgrade != "L3",
                    )
                )
                ep.last_report = max(move, ep.last_report)
                ep.last_msg_at = now
                if upgrade:
                    ep.level = upgrade
        if consumed and cand is not None:
            self._decide(cand, "absorbed", "L0", episode_id=ep.id)
        self.sink.episode(ep)
        return consumed

    def _end(self, tr: Tracker, reason: str, move: float, end_ts: int, now: float) -> None:
        ep = tr.episode
        assert ep is not None
        ep.state = "ended"
        ep.end_reason = reason
        ep.ended_at = int(end_ts)
        ep.end_move = move
        tr.episode = None
        tr.cooldown_until = now + self.params.episode.cooldown_min * 60
        tr.cooldown_dir = ep.direction
        tr.cooldown_anchor = ep.anchor
        self.merger.end_episode(ep, now)
        self.sink.episode(ep)

    def _sess(self, bar: Bar, ep: Episode) -> str:
        return bar.session if bar.session in self.params.sessions else ep.session

    def _beyond(self, bar: Bar, direction: int, anchor: float) -> bool:
        """价格是否已经越过参考起点、并且反向走出至少一个 1 分钟地板。"""
        if anchor <= 0:
            return True
        back = -direction * math.log(bar.c / anchor)
        sess = bar.session if bar.session in self.params.sessions else "REG"
        return back >= self.params.floor(bar.symbol, sess, 1)

    def _decide(
        self,
        c: Candidate,
        outcome: str,
        level: str,
        retained: float | None = None,
        episode_id: str | None = None,
    ) -> None:
        p = c.primary
        self.sink.decision(
            Decision(
                ts=c.close_ts,
                symbol=c.symbol,
                session=c.session,
                level=level,
                outcome=outcome,
                k=p.k,
                d=p.d,
                threshold=p.threshold,
                sigma=p.sigma,
                floor=p.floor,
                bypass=p.bypass,
                score=c.score,
                retained=retained,
                turnover_ratio=p.turnover_ratio,
                episode_id=episode_id,
                rule_version=self.params.rule_version,
            )
        )

    def _decide_hit(self, bar: Bar, h: WindowHit, outcome: str) -> None:
        self.sink.decision(
            Decision(
                ts=bar.close_ts,
                symbol=bar.symbol,
                session=bar.session,
                level="L0",
                outcome=outcome,
                k=h.k,
                d=h.d,
                threshold=h.threshold,
                sigma=h.sigma,
                floor=h.floor,
                bypass=h.bypass,
                score=h.score,
                turnover_ratio=h.turnover_ratio,
                rule_version=self.params.rule_version,
            )
        )
