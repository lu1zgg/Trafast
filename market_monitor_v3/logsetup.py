"""日志：写到 stderr（由 journald 收集），统一打码。

httpx 在 INFO 级别会把完整 URL 写进日志，而 Telegram 的 URL 里含 bot token，所以把 httpx/httpcore 调到 WARNING，
并且所有日志在输出前把已知密钥和形如 bot123:ABC 的 token 替换成 ***。
"""
from __future__ import annotations

import logging
import os
import re
import sys

from .config import SECRET_ENV_VARS

_TOKEN_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")


class RedactFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.secrets = [v for k in SECRET_ENV_VARS if (v := os.environ.get(k)) and len(v) >= 6]

    def redact(self, s: str) -> str:
        for v in self.secrets:
            s = s.replace(v, "***")
        return _TOKEN_RE.sub("bot***", s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        red = self.redact(msg)
        if record.exc_info:
            import traceback

            red += "\n" + self.redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
            record.exc_text = None
        record.msg, record.args = red, None
        return True


def setup_logging(level: str = "INFO", journald: bool | None = None) -> None:
    if journald is None:
        journald = bool(os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"))
    handler = logging.StreamHandler(sys.stderr)
    fmt = "%(levelname)s %(name)s: %(message)s" if journald else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
