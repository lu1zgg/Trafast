"""SQLite 存储：WAL 模式，synchronous=NORMAL，所有读写都在一个专用线程里执行。

协程里用 `await store.call(Store.method, args...)`，方法第一个参数是 Store 自己（在写线程里运行）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeVar

from .models import Bar, Episode, OutMsg, RootMessage
from .sink import Effects

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol TEXT NOT NULL, ts INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, volume REAL, turnover REAL,
    session TEXT, source TEXT, synthetic INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS baseline (
    symbol TEXT NOT NULL, session TEXT NOT NULL, k INTEGER NOT NULL,
    sigma REAL, turnover_median REAL, n_instances INTEGER, n_returns INTEGER, ready INTEGER,
    computed_at INTEGER NOT NULL,
    PRIMARY KEY (symbol, session, k, computed_at)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, symbol TEXT NOT NULL, session TEXT,
    level TEXT, outcome TEXT, k INTEGER, d REAL, threshold REAL, sigma REAL, floor REAL, bypass REAL,
    score REAL, retained REAL, turnover_ratio REAL, episode_id TEXT, rule_version TEXT
);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, symbol TEXT, market TEXT, state TEXT,
    open_ts INTEGER, ended_ts INTEGER, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_state ON episodes (state);

CREATE TABLE IF NOT EXISTS roots (
    key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    parent_key TEXT,
    reply_to_tg INTEGER,
    text TEXT NOT NULL,
    level TEXT,
    silent INTEGER NOT NULL DEFAULT 0,
    buttons TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL,
    tg_message_id INTEGER,
    error TEXT,
    sent_at REAL,
    plain INTEGER NOT NULL DEFAULT 0,
    maybe_dup INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (status, next_at);

CREATE TABLE IF NOT EXISTS mutes (
    target TEXT PRIMARY KEY, until REAL NOT NULL, by TEXT, created_at REAL
);

CREATE TABLE IF NOT EXISTS feedback (
    root_key TEXT NOT NULL, user_id INTEGER NOT NULL, value INTEGER NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (root_key, user_id)
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS control (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT, command TEXT, result TEXT
);
"""

ALERT_COLS = (
    "ts",
    "symbol",
    "session",
    "level",
    "outcome",
    "k",
    "d",
    "threshold",
    "sigma",
    "floor",
    "bypass",
    "score",
    "retained",
    "turnover_ratio",
    "episode_id",
    "rule_version",
)


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def bar_from_row(r: sqlite3.Row) -> Bar:
    return Bar(
        symbol=r["symbol"],
        ts=r["ts"],
        o=r["o"],
        h=r["h"],
        l=r["l"],
        c=r["c"],
        volume=r["volume"] or 0.0,
        turnover=r["turnover"] or 0.0,
        session=r["session"] or "",
        source=r["source"] or "",
        synthetic=bool(r["synthetic"]),
    )


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn: sqlite3.Connection | None = None
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite-writer")

    # ---- 生命周期 ----
    def open_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    async def open(self) -> None:
        await self.call(Store.open_sync)

    async def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._exec, lambda: fn(self, *args, **kwargs))

    def close_sync(self) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    async def close(self) -> None:
        await self.call(Store.close_sync)
        self._exec.shutdown(wait=True)

    @property
    def c(self) -> sqlite3.Connection:
        assert self.conn is not None, "store not opened"
        return self.conn

    # ---- 写入 ----
    def apply_effects(self, eff: Effects) -> None:
        c = self.c
        with c:
            if eff.bars:
                self._insert_bars(eff.bars)
            if eff.decisions:
                c.executemany(
                    f"INSERT INTO alerts ({','.join(ALERT_COLS)}) VALUES ({','.join('?' * len(ALERT_COLS))})",
                    [tuple(asdict(d)[k] for k in ALERT_COLS) for d in eff.decisions],
                )
            for ep_id, sym, market, state, open_ts, ended_ts, data in eff.episodes:  # type: ignore[misc]
                c.execute(
                    "INSERT INTO episodes (id, symbol, market, state, open_ts, ended_ts, data) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET state=excluded.state, ended_ts=excluded.ended_ts, data=excluded.data",
                    (ep_id, sym, market, state, open_ts, ended_ts, data),
                )
            for key, data in eff.roots:
                c.execute(
                    "INSERT INTO roots (key, data, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                    (key, data, time.time()),
                )
            for m in eff.outs:
                self._insert_out(m)

    def _insert_bars(self, bars: Iterable[Bar]) -> None:
        self.c.executemany(
            "INSERT OR REPLACE INTO bars_1m (symbol, ts, o, h, l, c, volume, turnover, session, source, synthetic) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (b.symbol, b.ts, b.o, b.h, b.l, b.c, b.volume, b.turnover, b.session, b.source, int(b.synthetic))
                for b in bars
            ],
        )

    def insert_bars(self, bars: list[Bar]) -> int:
        with self.c:
            self._insert_bars(bars)
        return len(bars)

    def _insert_out(self, m: OutMsg) -> None:
        self.c.execute(
            "INSERT OR IGNORE INTO outbox (key, kind, parent_key, reply_to_tg, text, level, silent, buttons, "
            "created_at, expires_at, status, next_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (
                m.key,
                m.kind,
                m.parent_key,
                m.reply_to_tg,
                m.text,
                m.level,
                int(m.silent),
                json.dumps(m.buttons, ensure_ascii=False) if m.buttons else None,
                m.created_at,
                m.expires_at,
                m.created_at,
            ),
        )

    def enqueue(self, m: OutMsg) -> None:
        with self.c:
            self._insert_out(m)

    def log_event(self, kind: str, detail: str = "", ts: float | None = None) -> None:
        with self.c:
            self.c.execute("INSERT INTO events (ts, kind, detail) VALUES (?,?,?)", (ts or time.time(), kind, detail))

    def kv_get(self, key: str) -> str | None:
        r = self.c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return None if r is None else r["value"]

    def kv_set(self, key: str, value: str) -> None:
        with self.c:
            self.c.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---- 发件箱 ----
    def outbox_next(self, now: float) -> dict[str, Any] | None:
        """取下一条可以发送的消息。回复和编辑要等原消息先发出。"""
        c = self.c
        rows = c.execute(
            "SELECT * FROM outbox WHERE status='pending' AND next_at<=? ORDER BY id LIMIT 50", (now,)
        ).fetchall()
        for r in rows:
            row = dict(r)
            if row["expires_at"] < now:
                self.outbox_mark(row["id"], "expired", error="过期未发出")
                continue
            if row["kind"] == "edit":
                newer = c.execute(
                    "SELECT 1 FROM outbox WHERE kind='edit' AND parent_key=? AND status='pending' AND id>? LIMIT 1",
                    (row["parent_key"], row["id"]),
                ).fetchone()
                if newer:
                    self.outbox_mark(row["id"], "superseded")
                    continue
            row["target_tg"] = None
            if row["kind"] in ("reply", "edit") and row["parent_key"]:
                p = c.execute(
                    "SELECT status, tg_message_id FROM outbox WHERE key=?", (row["parent_key"],)
                ).fetchone()
                if p is not None and p["status"] == "pending":
                    continue  # 原消息还没发出，先等
                if p is not None and p["status"] == "sent" and p["tg_message_id"]:
                    row["target_tg"] = p["tg_message_id"]
                elif row["kind"] == "edit":
                    self.outbox_mark(row["id"], "dropped", error="原消息未发出，放弃编辑")
                    continue
            elif row["kind"] == "edit":
                self.outbox_mark(row["id"], "dropped", error="编辑缺少目标")
                continue
            return row
        return None

    def outbox_mark(
        self,
        row_id: int,
        status: str,
        *,
        error: str | None = None,
        tg_message_id: int | None = None,
        next_at: float | None = None,
        attempts_inc: int = 0,
        text: str | None = None,
        plain: bool | None = None,
        maybe_dup: bool | None = None,
    ) -> None:
        sets = ["status=?", "attempts=attempts+?"]
        vals: list[Any] = [status, attempts_inc]
        if error is not None:
            sets.append("error=?")
            vals.append(error[:500])
        if tg_message_id is not None:
            sets.append("tg_message_id=?")
            vals.append(tg_message_id)
        if status == "sent":
            sets.append("sent_at=?")
            vals.append(time.time())
        if next_at is not None:
            sets.append("next_at=?")
            vals.append(next_at)
        if text is not None:
            sets.append("text=?")
            vals.append(text)
        if plain is not None:
            sets.append("plain=?")
            vals.append(int(plain))
        if maybe_dup is not None:
            sets.append("maybe_dup=?")
            vals.append(int(maybe_dup))
        vals.append(row_id)
        with self.c:
            self.c.execute(f"UPDATE outbox SET {', '.join(sets)} WHERE id=?", vals)

    def outbox_backlog(self, now: float) -> tuple[int, float]:
        r = self.c.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM outbox WHERE status='pending'"
        ).fetchone()
        n = r["n"] or 0
        age = now - r["oldest"] if r["oldest"] else 0.0
        return n, age

    # ---- 状态恢复 ----
    def load_recent_bars(self, symbol: str, since_ts: int) -> list[Bar]:
        rows = self.c.execute(
            "SELECT * FROM bars_1m WHERE symbol=? AND ts>=? ORDER BY ts", (symbol, since_ts)
        ).fetchall()
        return [bar_from_row(r) for r in rows]

    def last_bar_ts(self, symbol: str, real_only: bool = True) -> int | None:
        q = "SELECT MAX(ts) AS t FROM bars_1m WHERE symbol=?" + (" AND synthetic=0" if real_only else "")
        r = self.c.execute(q, (symbol,)).fetchone()
        return r["t"]

    def first_bar_ts(self, symbol: str, since_ts: int) -> int | None:
        r = self.c.execute(
            "SELECT MIN(ts) AS t FROM bars_1m WHERE symbol=? AND ts>=? AND synthetic=0", (symbol, since_ts)
        ).fetchone()
        return r["t"]

    def active_episodes(self) -> list[Episode]:
        rows = self.c.execute("SELECT data FROM episodes WHERE state='active'").fetchall()
        return [Episode.from_json(json.loads(r["data"])) for r in rows]

    def load_roots(self, keys: Iterable[str]) -> list[RootMessage]:
        out = []
        for k in set(keys):
            r = self.c.execute("SELECT data FROM roots WHERE key=?", (k,)).fetchone()
            if r is not None:
                out.append(RootMessage.from_json(json.loads(r["data"])))
        return out

    # ---- 静音、反馈 ----
    def mutes_load(self, now: float) -> dict[str, float]:
        rows = self.c.execute("SELECT target, until FROM mutes WHERE until>?", (now,)).fetchall()
        return {r["target"]: r["until"] for r in rows}

    def mute_set(self, target: str, until: float, by: str) -> None:
        with self.c:
            self.c.execute(
                "INSERT INTO mutes (target, until, by, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(target) DO UPDATE SET until=excluded.until, by=excluded.by, created_at=excluded.created_at",
                (target, until, by, time.time()),
            )

    def mute_clear(self, target: str | None) -> int:
        with self.c:
            if target is None:
                cur = self.c.execute("DELETE FROM mutes")
            else:
                cur = self.c.execute("DELETE FROM mutes WHERE target=?", (target,))
        return cur.rowcount

    def feedback_set(self, root_key: str, user_id: int, value: int) -> None:
        with self.c:
            self.c.execute(
                "INSERT INTO feedback (root_key, user_id, value, ts) VALUES (?,?,?,?) "
                "ON CONFLICT(root_key, user_id) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (root_key, user_id, value, time.time()),
            )

    # ---- 基线 ----
    def save_baseline(self, symbol: str, stats: dict, when: int) -> None:
        with self.c:
            self.c.executemany(
                "INSERT OR REPLACE INTO baseline (symbol, session, k, sigma, turnover_median, n_instances, n_returns, ready, computed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (symbol, s, k, st.sigma, st.turnover_median, st.n_instances, st.n_returns, int(st.ready), when)
                    for (s, k), st in stats.items()
                ],
            )

    # ---- 维护 ----
    def maintenance(self, now: float, bars_days: int, l0_days: int, outbox_days: int, backup_dir: Path, backup_keep: int) -> str:
        c = self.c
        with c:
            n_bars = c.execute("DELETE FROM bars_1m WHERE ts<?", (int(now - bars_days * 86400),)).rowcount
            n_l0 = c.execute("DELETE FROM alerts WHERE level='L0' AND ts<?", (int(now - l0_days * 86400),)).rowcount
            n_out = c.execute(
                "DELETE FROM outbox WHERE created_at<? AND status!='pending'", (now - outbox_days * 86400,)
            ).rowcount
            c.execute("DELETE FROM control WHERE ts<?", (now - 30 * 86400,))
            c.execute("DELETE FROM events WHERE ts<?", (now - 365 * 86400,))
            c.execute(
                "DELETE FROM roots WHERE updated_at<? AND key NOT IN (SELECT DISTINCT json_extract(data,'$.root_key') "
                "FROM episodes WHERE state='active' AND json_extract(data,'$.root_key') IS NOT NULL)",
                (now - 7 * 86400,),
            )
            c.execute("DELETE FROM baseline WHERE computed_at<?", (int(now - 180 * 86400),))
        c.execute("PRAGMA optimize")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d", time.gmtime(now))
        target = backup_dir / f"monitor-{stamp}.db"
        if target.exists():
            target.unlink()
        c.execute("VACUUM INTO ?", (str(target),))
        backups = sorted(backup_dir.glob("monitor-*.db"))
        for old in backups[:-backup_keep]:
            old.unlink()
        return f"删除 K 线 {n_bars}、L0 记录 {n_l0}、旧消息 {n_out}；备份 {target.name}"
