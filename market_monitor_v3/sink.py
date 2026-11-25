"""引擎的输出口。引擎本身是同步的，只把副作用放进缓冲区；
实盘由运行时把缓冲区写进 SQLite（一个事务），回放直接在内存里统计。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .models import Bar, Decision, Episode, OutMsg, RootMessage, SignalEvent


class Sink(Protocol):
    def bar(self, b: Bar) -> None: ...
    def decision(self, d: Decision) -> None: ...
    def episode(self, ep: Episode) -> None: ...
    def root(self, r: RootMessage) -> None: ...
    def out(self, m: OutMsg) -> None: ...
    def signal(self, ev: SignalEvent) -> None: ...


@dataclass
class Effects:
    bars: list[Bar] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    episodes: list[tuple[str, str, str, str, int, int | None]] = field(default_factory=list)  # 已序列化
    roots: list[tuple[str, str]] = field(default_factory=list)  # (key, json)
    outs: list[OutMsg] = field(default_factory=list)
    signals: list[SignalEvent] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.bars or self.decisions or self.episodes or self.roots or self.outs or self.signals)


class BufferSink:
    def __init__(self) -> None:
        self._bars: list[Bar] = []
        self._decisions: list[Decision] = []
        self._episodes: dict[str, Episode] = {}
        self._roots: dict[str, RootMessage] = {}
        self._outs: list[OutMsg] = []
        self._signals: list[SignalEvent] = []

    def bar(self, b: Bar) -> None:
        self._bars.append(b)

    def decision(self, d: Decision) -> None:
        self._decisions.append(d)

    def episode(self, ep: Episode) -> None:
        self._episodes[ep.id] = ep

    def root(self, r: RootMessage) -> None:
        self._roots[r.key] = r

    def out(self, m: OutMsg) -> None:
        self._outs.append(m)

    def signal(self, ev: SignalEvent) -> None:
        self._signals.append(ev)

    def drain(self) -> Effects:
        """在事件循环线程里序列化，避免写库线程读到正在变化的对象。"""
        eff = Effects(
            bars=self._bars,
            decisions=self._decisions,
            episodes=[
                (ep.id, ep.symbol, ep.market, ep.state, ep.open_close_ts, ep.ended_at)
                + (json.dumps(ep.to_json(), ensure_ascii=False),)  # type: ignore[misc]
                for ep in self._episodes.values()
            ],
            roots=[(r.key, json.dumps(r.to_json(), ensure_ascii=False)) for r in self._roots.values()],
            outs=self._outs,
            signals=self._signals,
        )
        self._bars, self._decisions, self._outs, self._signals = [], [], [], []
        self._episodes, self._roots = {}, {}
        return eff


def decision_row(d: Decision) -> dict:
    return asdict(d)
