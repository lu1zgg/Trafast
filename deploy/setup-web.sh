#!/usr/bin/env bash
# 开启网页管理后台：装 Caddy、配置 HTTPS 反向代理、启动后台服务。
#   sudo bash deploy/setup-web.sh 你的域名        # 例如 sudo bash deploy/setup-web.sh panel.example.com
#   sudo bash deploy/setup-web.sh --local-only    # 不对外开放，只能通过 SSH 隧道访问
# 域名要先在 DNS 里加一条 A 记录指向这台 VPS 的 IP。可以重复运行。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(id -u)" != 0 ]; then
    echo "请用 root 运行：sudo bash deploy/setup-web.sh 你的域名" >&2
    exit 1
fi
DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    echo "用法：sudo bash deploy/setup-web.sh 你的域名（或 --local-only）" >&2
    exit 1
fi
if [ ! -f /etc/systemd/system/market-monitor-web.service ]; then
    echo "先运行一次 sudo bash deploy/install.sh（会装好后台需要的依赖和服务）" >&2
    exit 1
fi

if [ "$DOMAIN" != "--local-only" ]; then
    if ! [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]]; then
        echo "域名格式不对：$DOMAIN" >&2
        exit 1
    fi
    echo "==> 安装 Caddy"
    export DEBIAN_FRONTEND=noninteractive
    command -v caddy >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq caddy >/dev/null; }

    echo "==> 生成 /etc/caddy/trafast.caddy（$DOMAIN → 127.0.0.1:8787）"
    sed "/^[[:space:]]*#/! s#{\$TRAFAST_DOMAIN}#$DOMAIN#" "$SRC/deploy/Caddyfile.example" >/etc/caddy/trafast.caddy
    # Debian 自带的 Caddyfile 是一个示例站点，直接换掉；已经有自己站点的，追加一行 import
    if [ ! -s /etc/caddy/Caddyfile ] || grep -q "^# The Caddyfile is an easy way" /etc/caddy/Caddyfile; then
        echo "import /etc/caddy/trafast.caddy" >/etc/caddy/Caddyfile
    elif ! grep -q "trafast.caddy" /etc/caddy/Caddyfile; then
        printf '\nimport /etc/caddy/trafast.caddy\n' >>/etc/caddy/Caddyfile
    fi
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
    systemctl enable caddy >/dev/null 2>&1
    systemctl reload-or-restart caddy

    if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
        echo "==> 防火墙放行 80、443"
        ufw allow 80/tcp >/dev/null
        ufw allow 443/tcp >/dev/null
    fi
fi

echo "==> 启动后台服务"
systemctl enable --now market-monitor-web >/dev/null 2>&1
systemctl restart market-monitor-web

if [ ! -f /etc/market-monitor/web-auth.json ]; then
    echo
    echo "==> 创建管理员账号（用户名、密码、手机验证器）"
    mm web-passwd
fi

echo
if [ "$DOMAIN" = "--local-only" ]; then
    cat <<MSG
完成。后台只在本机 127.0.0.1:8787 上，从你的电脑这样访问：
  ssh -L 8787:127.0.0.1:8787 用户名@VPS地址
  然后浏览器打开 http://localhost:8787
MSG
else
    cat <<MSG
完成。浏览器打开 https://$DOMAIN
第一次访问 Caddy 要申请证书，可能要等十几秒。打不开的话：journalctl -u caddy -n 30
MSG
fi
