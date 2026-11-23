#!/usr/bin/env bash
# 首次安装（Debian 12 / Ubuntu 24.04）。在克隆下来的仓库里以 root 运行：
#   sudo bash deploy/install.sh
# 可以重复运行：已有的 config.toml、secrets.env 不会被覆盖。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ETC=/etc/market-monitor
STATE=/var/lib/market-monitor

if [ "$(id -u)" != 0 ]; then
    echo "请用 root 运行：sudo bash deploy/install.sh" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "需要 Python 3.11 或更新（Debian 12、Ubuntu 24.04 自带的就可以）" >&2
    exit 1
fi

echo "==> 安装系统依赖（python3-venv、chrony 对时）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv chrony >/dev/null
systemctl enable --now chrony >/dev/null 2>&1 || true

echo "==> 创建 monitor 用户和目录"
id monitor >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin monitor
install -d -o monitor -g monitor -m 0750 "$STATE"
install -d -m 0755 "$ETC"

echo "==> 创建虚拟环境并安装（$SRC/.venv）"
python3 -m venv "$SRC/.venv"
"$SRC/.venv/bin/pip" install -q --upgrade pip
if ! "$SRC/.venv/bin/pip" install -q "$SRC[longbridge]"; then
    echo "   长桥 SDK 安装失败，先装不带长桥的版本（只跑加密货币不受影响）"
    "$SRC/.venv/bin/pip" install -q "$SRC"
fi

echo "==> 配置文件"
if [ ! -f "$ETC/config.toml" ]; then
    install -m 0644 "$SRC/deploy/config.example.toml" "$ETC/config.toml"
    echo "   已生成 $ETC/config.toml（需要改 chat_id）"
else
    echo "   $ETC/config.toml 已存在，保留"
fi
if [ ! -f "$ETC/secrets.env" ]; then
    install -m 0600 -o root -g root "$SRC/deploy/secrets.env.example" "$ETC/secrets.env"
    echo "   已生成 $ETC/secrets.env（需要填 TELEGRAM_BOT_TOKEN）"
else
    chmod 600 "$ETC/secrets.env"
    echo "   $ETC/secrets.env 已存在，保留"
fi

echo "==> systemd 服务和 mm 命令"
sed "s#/opt/market-monitor#$SRC#g" "$SRC/deploy/market-monitor.service" >/etc/systemd/system/market-monitor.service
sed "s#/opt/market-monitor#$SRC#g" "$SRC/deploy/mm" >/usr/local/bin/mm
chmod 0755 /usr/local/bin/mm
systemctl daemon-reload
systemctl enable market-monitor >/dev/null 2>&1

cat <<MSG

安装完成，服务已设为开机自启，但还没有启动。接下来：
  1. 编辑 $ETC/secrets.env，填 TELEGRAM_BOT_TOKEN
  2. 编辑 $ETC/config.toml，填 [telegram] chat_id
  3. sudo mm check-config            # 校验配置、看门槛表
  4. sudo mm test-telegram           # 往群里发一条测试消息
  5. sudo systemctl start market-monitor
  6. journalctl -u market-monitor -f # 看日志
MSG
