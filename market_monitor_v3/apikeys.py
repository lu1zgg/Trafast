"""V4 P3 对外 API 的密钥（桌面版和其他客户端用）。

- 密钥形如 tfk_ + 32 字节随机串（base64url），只在生成时显示一次。
- 文件里只存 SHA-256 哈希、前缀（tfk_ 后 8 位）、名称、权限、创建 / 最后使用 / 吊销时间。
- 校验：按前缀找到记录，再用常数时间比较哈希。
- 权限：read 只能看；write 还能改自定义提醒、类型开关、静音、反馈。
- 文件由网页后台服务（mmweb）读写；`mm api-key` 以 root 运行时写完把属主改回来。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PREFIX = "tfk_"
SCOPES = ("read", "write")


@dataclass
class ApiKey:
    id: int
    name: str
    prefix: str  # tfk_ 之后的前 8 位，用来查找和展示
    hash: str  # 完整密钥的 SHA-256（hex）
    scope: str = "write"
    created_at: float = 0.0
    last_used_at: float | None = None
    revoked_at: float | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("hash")
        d["active"] = self.active
        return d


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(32)


class ApiKeyStore:
    """JSON 文件存储。网页后台进程每次校验前看一下文件修改时间，命令行改过就重新读。"""

    def __init__(self, path: str | Path, touch_every_s: float = 60.0):
        self.path = Path(path)
        self.touch_every_s = touch_every_s
        self.keys: list[ApiKey] = []
        self._mtime: float | None = None

    # ---- 文件 ----
    def load(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self.keys, self._mtime = [], None
            return
        if self._mtime == st.st_mtime:
            return
        raw = json.loads(self.path.read_text() or "{}")
        self.keys = [ApiKey(**k) for k in raw.get("keys", [])]
        self._mtime = st.st_mtime

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"keys": [asdict(k) for k in self.keys]}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    # ---- 管理 ----
    def create(self, name: str, scope: str = "write", now: float | None = None) -> tuple[ApiKey, str]:
        if scope not in SCOPES:
            raise ValueError(f"权限只能是 {' / '.join(SCOPES)}")
        name = name.strip()[:60] or "未命名"
        self.load()
        key = new_key()
        rec = ApiKey(
            id=max((k.id for k in self.keys), default=0) + 1,
            name=name,
            prefix=key[len(PREFIX) : len(PREFIX) + 8],
            hash=hash_key(key),
            scope=scope,
            created_at=now if now is not None else time.time(),
        )
        self.keys.append(rec)
        self.save()
        return rec, key

    def revoke(self, key_id: int, now: float | None = None) -> bool:
        self.load()
        for k in self.keys:
            if k.id == key_id and k.active:
                k.revoked_at = now if now is not None else time.time()
                self.save()
                return True
        return False

    def list(self) -> list[ApiKey]:
        self.load()
        return list(self.keys)

    # ---- 校验 ----
    def verify(self, key: str | None, now: float | None = None) -> ApiKey | None:
        if not key or not key.startswith(PREFIX) or len(key) < len(PREFIX) + 8:
            return None
        self.load()
        prefix = key[len(PREFIX) : len(PREFIX) + 8]
        h = hash_key(key)
        found = None
        for k in self.keys:
            # 每条都比较，耗时不随匹配位置变化
            if hmac.compare_digest(k.hash, h) and k.prefix == prefix and k.active:
                found = k
        if found is None:
            return None
        now = now if now is not None else time.time()
        if found.last_used_at is None or now - found.last_used_at >= self.touch_every_s:
            found.last_used_at = now
            try:
                self.save()
            except OSError:
                pass
        return found

    def is_active(self, key_id: int) -> bool:
        self.load()
        return any(k.id == key_id and k.active for k in self.keys)


class FailGuard:
    """同一 IP 连续错 max_fail 次，封 lock_s 秒。"""

    def __init__(self, max_fail: int = 10, lock_s: float = 900):
        self.max_fail = max_fail
        self.lock_s = lock_s
        self.fails: dict[str, int] = {}
        self.locked_until: dict[str, float] = {}

    def locked(self, ip: str, now: float) -> bool:
        until = self.locked_until.get(ip)
        if until is None:
            return False
        if until <= now:
            del self.locked_until[ip]
            return False
        return True

    def fail(self, ip: str, now: float) -> None:
        n = self.fails.get(ip, 0) + 1
        if n >= self.max_fail:
            self.locked_until[ip] = now + self.lock_s
            n = 0
        self.fails[ip] = n
        if len(self.fails) > 10_000:
            self.fails.clear()

    def ok(self, ip: str) -> None:
        self.fails.pop(ip, None)
