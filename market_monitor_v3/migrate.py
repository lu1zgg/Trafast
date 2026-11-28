"""升级时对旧 config.toml 做的一次性补丁（只改文本，保留注释）。由 deploy/upgrade.sh 以 root 调用。

v4.3 大宗商品：
- 旧配置没有 [commodities]：在文件末尾补上这一节（默认 XAU、XAG、BZ、CL）。
- 推送渠道的 markets 写死了 ["US", "CRYPTO"]：补上 "CMDTY"，否则大宗商品的告警会被渠道过滤掉。
  只在第一次补 [commodities] 时做；之后用户在网页后台取消勾选就不会再被加回来。
"""
from __future__ import annotations

import re
from pathlib import Path

COMMODITIES_BLOCK = '''
[commodities]                   # 大宗商品：Binance USDT-M TradFi 永续合约（v4.3 升级时自动加入）
enabled = true
symbols = ["XAUUSDT", "XAGUSDT", "BZUSDT", "CLUSDT"]   # 黄金、白银、布伦特原油、WTI 原油
# 合约 7×24 交易，但只在标的市场开市时检测：美东周日 18:00 – 周五 17:00，每天 17:00–18:00 休市不检测
ws_url = "wss://fstream.binance.com/market"
rest_url = "https://fapi.binance.com"
baseline_days = 20

[commodities.multipliers]       # 地板以黄金为基准（0.3% / 0.6% / 1.0%），其他品种按波动放大
XAGUSDT = 2.0
BZUSDT = 1.8
CLUSDT = 1.8
'''

_HAS_SECTION = re.compile(r"^\s*\[commodities\]", re.M)
_MARKETS = re.compile(r'^(\s*markets\s*=\s*\[)([^\]\n]*)\]', re.M)


def migrate_text(text: str) -> tuple[str, list[str]]:
    """返回 (新文本, 改动说明)。已经迁移过的配置原样返回。"""
    if _HAS_SECTION.search(text):
        return text, []
    notes: list[str] = []

    def add_cm(m: re.Match[str]) -> str:
        inner = m.group(2)
        if "CMDTY" in inner or "CRYPTO" not in inner:
            return m.group(0)
        notes.append("推送渠道 markets 加上 CMDTY")
        sep = ", " if inner.strip() else ""
        return f'{m.group(1)}{inner.rstrip()}{sep}"CMDTY"]'

    text = _MARKETS.sub(add_cm, text)
    text = text.rstrip("\n") + "\n" + COMMODITIES_BLOCK
    notes.append("加入 [commodities]（XAU、XAG、BZ、CL）")
    return text, notes


def migrate_file(path: str | Path) -> list[str]:
    p = Path(path)
    old = p.read_text(encoding="utf-8")
    new, notes = migrate_text(old)
    if new != old:
        # 原地写，保留文件属主和权限（config.toml 是 mmweb:monitor 0640）
        with open(p, "r+", encoding="utf-8") as f:
            f.seek(0)
            f.write(new)
            f.truncate()
    return notes
