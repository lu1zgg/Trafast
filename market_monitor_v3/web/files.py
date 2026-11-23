"""后台读写配置文件：config.toml（tomlkit 保留注释和顺序）和 secrets.env（只写不读回明文）。

每次保存：先用监控同一套 AppConfig 校验，旧文件存一份历史版本，再写临时文件、原子替换。
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.toml_document import TOMLDocument

from ..config import AppConfig

HISTORY_KEEP = 100


class StaleFile(Exception):
    """表单打开后文件被别人（或 SSH 手工）改过。"""


@dataclass
class Saved:
    cfg: AppConfig
    changed: bool
    history_file: str | None
    diff: str


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    try:
        st = path.stat()
        mode = st.st_mode & 0o777 if mode is None else mode
    except FileNotFoundError:
        st = None
        mode = 0o660 if mode is None else mode
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if st is not None:
            try:
                os.chown(tmp, -1, st.st_gid)
            except PermissionError:
                pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class ConfigFile:
    def __init__(self, path: str | Path, history_dir: str | Path):
        self.path = Path(path)
        self.history_dir = Path(history_dir)

    def read(self) -> tuple[str, TOMLDocument, AppConfig]:
        text = self.path.read_text()
        return text, tomlkit.parse(text), AppConfig.model_validate(tomllib.loads(text))

    def save(self, mutate: Callable[[TOMLDocument], None], expect_rev: str | None, who: str) -> Saved:
        text, doc, _ = self.read()
        if expect_rev is not None and expect_rev != digest(text):
            raise StaleFile("配置文件在你打开页面之后被改过，请刷新后再改")
        mutate(doc)
        new_text = tomlkit.dumps(doc)
        cfg = AppConfig.model_validate(tomllib.loads(new_text))  # 校验不过直接抛 ValidationError，文件不动
        if new_text == text:
            return Saved(cfg, False, None, "")
        hist = self._archive(text, who)
        _atomic_write(self.path, new_text)
        return Saved(cfg, True, hist, unified_diff(text, new_text))

    def restore(self, name: str, who: str) -> Saved:
        src = self.history_path(name)
        old_text = src.read_text()
        cfg = AppConfig.model_validate(tomllib.loads(old_text))
        cur = self.path.read_text()
        hist = self._archive(cur, who)
        _atomic_write(self.path, old_text)
        return Saved(cfg, cur != old_text, hist, unified_diff(cur, old_text))

    def _archive(self, text: str, who: str) -> str:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_who = re.sub(r"[^A-Za-z0-9_.-]", "_", who)[:32] or "web"
        name = f"config-{stamp}-{safe_who}.toml"
        n = 1
        while (self.history_dir / name).exists():  # 同一秒里保存了多次
            n += 1
            name = f"config-{stamp}-{safe_who[:28]}.{n}.toml"
        _atomic_write(self.history_dir / name, text, 0o640)
        files = self.history()
        for old in files[HISTORY_KEEP:]:
            (self.history_dir / old).unlink(missing_ok=True)
        return name

    def history(self) -> list[str]:
        if not self.history_dir.is_dir():
            return []
        files = [(p.stat().st_mtime_ns, p.name) for p in self.history_dir.glob("config-*.toml")]
        return [name for _, name in sorted(files, reverse=True)]

    def history_path(self, name: str) -> Path:
        if not re.fullmatch(r"config-[0-9]{8}-[0-9]{6}-[A-Za-z0-9_.-]{1,32}\.toml", name):
            raise FileNotFoundError(name)
        p = self.history_dir / name
        if not p.is_file():
            raise FileNotFoundError(name)
        return p


def unified_diff(a: str, b: str) -> str:
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), "修改前", "修改后", n=1))


# ---- secrets.env ----
_LINE = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*)\s*$')


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


class SecretsFile:
    """读写 KEY="value" 形式的环境变量文件。网页上只显示“已设置、末 4 位”，不回显明文。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return out
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            m = _LINE.match(line)
            if m:
                out[m.group(1)] = _unquote(m.group(2))
        return out

    def get(self, key: str) -> str:
        return self.values().get(key, "")

    def masked(self, key: str) -> str:
        v = self.get(key)
        if not v:
            return ""
        return "已设置，末 4 位 " + v[-4:] if len(v) >= 12 else "已设置"

    def set(self, key: str, value: str | None) -> None:
        """value 为 None 或空字符串 = 清空。值里不允许换行和双引号。"""
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("变量名不合法")
        value = (value or "").strip()
        if any(c in value for c in '\n\r"\\`$'):
            raise ValueError("值里不能有换行、引号、反斜杠、$ 或 `")
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        new_line = f'{key}="{value}"'
        for i, line in enumerate(lines):
            m = _LINE.match(line)
            if m and m.group(1) == key and not line.lstrip().startswith("#"):
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
        _atomic_write(self.path, "\n".join(lines) + "\n")
