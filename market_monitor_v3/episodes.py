"""事件状态机：空闲 → 待确认 → 进行中 → 冷却。

- 待确认：候选出现后等下一根 K 线，位移保留 ≥ 60% 才推送；否则记 L0（这一步挡掉插针）。
- 快速通道（L3）不等确认。
- V4：急拉急跌（L1 / L2）普通确认之后再等 rules.spike_extra_confirm 根 K 线，每根都保留 ≥ 60% 才推，
  任何一根没站稳记 L0（spike_rejected）。等待期间同方向的新候选并进来（confirming），只可能升级。
- V4：被关掉的类型（/types）在推送前记 L0（type_off），不推送。
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
from .models import LEVEL_RANK, Bar, Candidate, Decision, Episode, Evaluation, OutMsg, SignalEvent, WindowHit, display_name
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
    # V4 急拉急跌多确认：已经额外确认了几根、期间最低的保留比例
    pending_checks: int = 0
    pending_min_ret: float = 1.0


RETRACE_EXTRA_S = max(WINDOWS) * 60


class EpisodeManager:
    def __init__(
        self,
        params: Params,
        merger: Merger,
        sink: Sink,
        is_muted: Callable[[str, float], bool],
        silent_ext: bool = False,
        type_off: Callable[[str, str], bool] = lambda symbol, pattern: False,
    ):
        self.params = params
        self.merger = merger
        self.sink = sink
        self.is_muted = is_muted
        self.silent_ext = silent_ext
        self.type_off = type_off
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
            checks, tr.pending_checks = tr.pending_checks, 0
            if bar.ts - p.bar_ts > (ecfg.pending_max_wait_min + checks) * 60:
                self._decide(p, "expired_pending", "L0")
            else:
                retained = p.direction * math.log(bar.c / p.primary.p0) / abs(p.primary.d)
                if retained >= self.params.rules.confirm_ratio:
                    level = p.level
                    if cand is not None and cand.direction == p.direction and LEVEL_RANK[cand.level] > LEVEL_RANK[level]:
                        level = cand.level
                    min_ret = min(retained, tr.pending_min_ret) if checks else retained
                    if checks < self._extra_needed(p, level):
                        # 急拉急跌：再等一根
                        p.level = level
                        tr.pending, tr.pending_checks, tr.pending_min_ret = p, checks + 1, min_ret
                        if cand is not None and cand.direction == p.direction:
                            self._decide(cand, "confirming", "L0")
                            cand = None
                    elif self.is_muted(sym, now):
                        self._decide(p, "muted", "L0", retained)
                    else:
                        self._push(tr, p, level, min_ret, bar, now, confirm_bars=checks + 1)
                        if cand is not None and cand.direction == p.direction and tr.episode is not None:
                            self._decide(cand, "absorbed", "L0", episode_id=tr.episode.id)
                            cand = None
                else:
                    self._decide(p, "spike_rejected" if checks else "rejected", "L0", retained)
                    tr.guard_until = bar.close_ts + 15 * 60
                    tr.guard_dir = p.direction
                    tr.guard_anchor = p.primary.p0

        # 2. 进行中的事件：延伸、升级、结束
        if tr.episode is not None and tr.episode.open_close_ts < bar.close_ts:
            if self._update(tr, bar, cand, now):
                cand = None

        # 3. 新候选
        if cand is not None and tr.pending is not None and tr.pending_checks > 0:
            # 急拉急跌还在多确认：同方向的并进去（只升级），否则放弃等待、按新候选处理
            p = tr.pending
            fast_now = cand.fast and cand.session not in self.params.rules.fast_confirm_sessions
            if cand.direction == p.direction and not fast_now:
                if LEVEL_RANK[cand.level] > LEVEL_RANK[p.level]:
                    p.level = cand.level
                self._decide(cand, "confirming", "L0")
                cand = None
            else:
                tr.pending, tr.pending_checks = None, 0
                self._decide(p, "superseded", "L0")
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
            elif cand.fast and cand.session not in self.params.rules.fast_confirm_sessions:
                self._push(tr, cand, "L3", None, bar, now)
            else:
                # 盘外的快速通道也先确认：单根坏价下一分钟就回去了
                tr.pending = cand

    def reject(self, cand: Candidate, outcome: str) -> None:
        """引擎层面判定为数据问题的候选：只记录，不进状态机。"""
        self._decide(cand, outcome, "L0")

    def on_tick(self, now: float) -> None:
        ecfg = self.params.episode
        for tr in self.trackers.values():
            p = tr.pending
            if p is not None and now - p.close_ts > (ecfg.pending_max_wait_min + tr.pending_checks) * 60 + 60:
                tr.pending, tr.pending_checks = None, 0
                self._decide(p, "expired_pending", "L0")
            ep = tr.episode
            if ep is not None and now - ep.last_extreme_at >= ecfg.idle_end_min * 60 + 120:
                move = ep.direction * math.log(ep.last_price / ep.anchor)
                self._end(tr, "idle", move, ep.last_extreme_at, now)

    # ---- 内部 ----
    def _extra_needed(self, c: Candidate, level: str) -> int:
        """急拉急跌（L1 / L2）还要多确认几根；L3 保留快速通道。"""
        if c.pattern == "spike" and level != "L3":
            return self.params.rules.spike_extra_confirm
        return 0

    def _push(
        self, tr: Tracker, c: Candidate, level: str, retained: float | None, bar: Bar, now: float, confirm_bars: int = 1
    ) -> None:
        if c.pattern and self.type_off(c.symbol, c.pattern):
            self._decide(c, "type_off", "L0", retained)
            return
        self._open(tr, c, level, retained, bar, now, confirm_bars)

    def _open(
        self, tr: Tracker, c: Candidate, level: str, retained: float | None, bar: Bar, now: float, confirm_bars: int = 1
    ) -> None:
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
            pattern=c.pattern,
            tags=list(c.tags),
        )
        tr.episode = ep
        self._decide(c, "pushed", level, retained, ep.id)
        self.merger.add(OpenAlert(ep, c, level, retained, bar.c, bar.close_ts, confirm_bars), now)
        self.sink.episode(ep)
        self._signal(ep, "open", bar, move_now, score=c.score, retained=retained, reason=c.reason)

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
                        market=ep.market,
                        direction=ep.direction,
                    )
                )
                ep.last_report = max(move, ep.last_report)
                ep.last_msg_at = now
                if upgrade:
                    ep.level = upgrade
                self._signal(ep, "upgrade" if upgrade else "extend", bar, move, score=cand.score if consumed and cand else None)
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
        self.sink.signal(
            SignalEvent(
                id=f"{ep.id}:end",
                ts=int(end_ts),
                kind="end",
                symbol=ep.symbol,
                market=ep.market,
                session=ep.session,
                direction=ep.direction,
                level=ep.level,
                pattern=ep.pattern,
                tags=list(ep.tags),
                price=ep.last_price,
                anchor=ep.anchor,
                move=move,
                k=ep.k,
                score=None,
                episode_id=ep.id,
                rule_version=self.params.rule_version,
                end_reason=reason,
            )
        )

    def _signal(
        self,
        ep: Episode,
        kind: str,
        bar: Bar,
        move: float,
        score: float | None = None,
        retained: float | None = None,
        reason: dict[str, float | None] | None = None,
    ) -> None:
        seq = "open" if kind == "open" else f"r{ep.replies}"
        self.sink.signal(
            SignalEvent(
                id=f"{ep.id}:{seq}",
                ts=bar.close_ts,
                kind=kind,
                symbol=ep.symbol,
                market=ep.market,
                session=bar.session or ep.session,
                direction=ep.direction,
                level=ep.level,
                pattern=ep.pattern,
                tags=list(ep.tags),
                price=bar.c,
                anchor=ep.anchor,
                move=move,
                k=ep.k,
                score=score,
                episode_id=ep.id,
                rule_version=self.params.rule_version,
                retained=retained,
                reason=dict(reason or {}),
            )
        )

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
                pattern=c.pattern or None,
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
