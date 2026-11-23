"""后台登录：一个管理员账号，密码（argon2 哈希）+ TOTP 两步验证；服务端会话；登录限速；CSRF。

账号文件（默认 /etc/market-monitor/web-auth.json）只由 `sudo mm web-passwd` 生成，网页上没有注册入口。
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()


@dataclass
class Account:
    username: str
    password_hash: str
    totp_secret: str

    def check_password(self, password: str) -> bool:
        try:
            return _ph.verify(self.password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def check_totp(self, code: str, last_used: str | None = None) -> bool:
        code = code.strip().replace(" ", "")
        if not code.isdigit() or len(code) != 6 or code == last_used:
            return False
        return pyotp.TOTP(self.totp_secret).verify(code, valid_window=1)


def load_account(path: str | Path) -> Account | None:
    try:
        data = json.loads(Path(path).read_text())
        return Account(data["username"], data["password_hash"], data["totp_secret"])
    except (FileNotFoundError, KeyError, ValueError, PermissionError):
        return None


def save_account(path: str | Path, username: str, password: str, totp_secret: str) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    payload = {"username": username, "password_hash": _ph.hash(password), "totp_secret": totp_secret, "created_at": int(time.time())}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, p)


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str, issuer: str = "Trafast") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


@dataclass
class Session:
    token: str
    username: str
    csrf: str
    created_at: float
    expires_at: float
    ip: str


@dataclass
class LoginGuard:
    """连续失败 N 次锁 M 分钟：按来源 IP 计，同时全局也计（防分布式猜密码）。"""

    max_failures: int = 5
    lock_s: float = 900
    failures: dict[str, list[float]] = field(default_factory=dict)
    locked_until: dict[str, float] = field(default_factory=dict)

    def locked(self, ip: str, now: float) -> float:
        until = max(self.locked_until.get(ip, 0.0), self.locked_until.get("*", 0.0))
        return until - now if until > now else 0.0

    def fail(self, ip: str, now: float) -> bool:
        """记一次失败；返回这次是否触发了锁定。"""
        hit = False
        for key, limit in ((ip, self.max_failures), ("*", self.max_failures * 4)):
            xs = [t for t in self.failures.get(key, []) if now - t < self.lock_s] + [now]
            self.failures[key] = xs
            if len(xs) >= limit:
                self.locked_until[key] = now + self.lock_s
                self.failures[key] = []
                hit = True
        return hit

    def success(self, ip: str) -> None:
        self.failures.pop(ip, None)


class Sessions:
    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._by_token: dict[str, Session] = {}

    def create(self, username: str, ip: str, now: float | None = None) -> Session:
        now = time.time() if now is None else now
        s = Session(secrets.token_urlsafe(32), username, secrets.token_urlsafe(24), now, now + self.ttl_s, ip)
        self._by_token[s.token] = s
        self._gc(now)
        return s

    def get(self, token: str | None, now: float | None = None) -> Session | None:
        if not token:
            return None
        now = time.time() if now is None else now
        s = self._by_token.get(token)
        if s is None or s.expires_at < now:
            self._by_token.pop(token, None)
            return None
        return s

    def drop(self, token: str | None) -> None:
        if token:
            self._by_token.pop(token, None)

    def drop_all(self) -> None:
        self._by_token.clear()

    def _gc(self, now: float) -> None:
        for t in [t for t, s in self._by_token.items() if s.expires_at < now]:
            del self._by_token[t]


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))
