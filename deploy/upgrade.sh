#!/usr/bin/env bash
# 升级或回滚。在仓库里以 root 运行：
#   sudo bash deploy/upgrade.sh            # 拉 main 最新代码
#   sudo bash deploy/upgrade.sh <提交或标签>  # 切到指定版本（回滚用）
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SRC"
if [ "$(id -u)" != 0 ]; then
    echo "请用 root 运行：sudo bash deploy/upgrade.sh" >&2
    exit 1
fi

OLD="$(git rev-parse --short HEAD)"
git fetch --quiet origin
if [ $# -ge 1 ]; then
    git checkout --quiet "$1"
else
    git checkout --quiet main
    git merge --quiet --ff-only origin/main
fi
NEW="$(git rev-parse --short HEAD)"
echo "==> 代码 $OLD → $NEW"

"$SRC/.venv/bin/pip" install -q "$SRC[longbridge,web]" || "$SRC/.venv/bin/pip" install -q "$SRC[web]"
sed "s#/opt/market-monitor#$SRC#g" "$SRC/deploy/market-monitor.service" >/etc/systemd/system/market-monitor.service
sed "s#/opt/market-monitor#$SRC#g" "$SRC/deploy/market-monitor-web.service" >/etc/systemd/system/market-monitor-web.service
sed "s#/opt/market-monitor#$SRC#g" "$SRC/deploy/mm" >/usr/local/bin/mm
chmod 0755 /usr/local/bin/mm
systemctl daemon-reload

echo "==> 校验配置"
mm check-config

echo "==> 重启服务"
systemctl restart market-monitor
if systemctl is-enabled --quiet market-monitor-web 2>/dev/null; then
    systemctl restart market-monitor-web
fi
sleep 5
systemctl --no-pager --lines=15 status market-monitor || true
echo "回滚：sudo bash deploy/upgrade.sh $OLD"
