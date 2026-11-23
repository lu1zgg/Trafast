<p align="center"><img src="assets/logo/trafast-logo-256.png" width="128" alt="Trafast"></p>

<h1 align="center">Trafast</h1>

<p align="center">美股正股（含盘前、盘后、夜盘）与加密货币现货的价格异动监控，实时推送到 Telegram。</p>

<p align="center"><em>A self-hosted price-anomaly monitor for US stocks (incl. pre-market, after-hours and overnight) and crypto spot, with Telegram alerts. Docs are in Chinese.</em></p>

---

## 它做什么

Trafast 按分钟盯着一组标的，价格在短时间内出现**真实、站得住**的位移时，往 Telegram 群发一条告警；插针、只放量不动价、薄成交时段的跳价则尽量不报。它是一个常驻的 Python 服务，适合跑在一台小 VPS 上。

- **美股**：通过 [长桥 OpenAPI](https://open.longbridge.com/) 获取 1 分钟 K 线，覆盖盘中、盘前、盘后和夜盘
- **加密货币**：通过 Binance 现货公开行情获取 1 分钟 K 线，不需要 Binance 账号
- **推送**：Telegram 机器人，支持命令、静音、👍/👎 反馈、日报和运维消息

> 本项目只做行情监控和提醒，**不构成任何投资建议**，也不会下单。详见文末免责声明。

## 功能

- **多窗口检测**：每分钟对 1、5、15 分钟三个窗口计算位移，门槛同时参考固定地板和该时段的历史波动（Z 值）
- **防插针**：候选告警要等下一根 K 线确认，位移保留 60% 以上才推送；拉上去马上回来的不报，回来的那段也不会当成反向异动
- **薄成交过滤**：夜盘和早盘前，成交额明显低于常态的跳价不报
- **不报单纯放量**：价格没动的放量不推送，成交量只用来提高告警级别
- **事件跟踪**：同一波行情的后续延伸以回复形式挂在原消息下，结束时静默编辑原消息标注「已结束」
- **联动合并**：同一市场、同方向、3 分钟内的多个标的合并成一条消息，并有限流
- **三档预设**：灵敏 / 均衡 / 克制，可以按时段、按标的微调
- **可靠投递**：发件箱持久化在 SQLite，失败自动重试；每条判定（包括没推送的）都有记录
- **运维**：断流、积压、卡顿、时钟偏差、凭据快到期时在群里提醒；systemd 看门狗；每日备份；可选外部心跳
- **回放**：用历史 K 线或 CSV 回放，先看清楚每天大概会发多少条再上线

## 工作方式

```
Binance 现货 WS ──┐
                  ├─> 补齐/去重 ─> 检测（detector）─> 事件状态机（episodes）─> 合并限流（merge）─> 发件箱 ─> Telegram
长桥 OpenAPI ─────┘                    │                                                                ▲
                                       └──────────── SQLite：K 线、判定、事件、静音、反馈 ─────────────────┘
                                                                                  机器人命令（长轮询）──┘
```

### 判定规则（灵敏档）

对每个窗口 k ∈ {1, 5, 15} 分钟，计算 d = ln(现价 / k 分钟前价格)：

- |d| 超过 `max(地板 × 标的系数, Z × 该时段近 20 个同类时段的波动)`，或超过「直推线」，即为候选
- 超过直推线或得分 ≥ 2 的直接推送（L3），不等确认
- 同一事件后续再走一个 5 分钟地板，作为「延伸」回复在原消息下，最多 3 条

| 时段 | 地板 1/5/15 分钟 | Z | 直推线 1/5/15 分钟 |
|---|---|---|---|
| 美股盘中 | 0.4% / 0.8% / 1.2% | 5 | 1.5% / 2.5% / 3% |
| 美股盘前、盘后 | 0.6% / 1.2% / 1.8% | 6 | 1.5% / 2.5% / 2.5% |
| 美股夜盘 | 0.8% / 1.5% / 2.2% | 6 | 1.5% / 2.5% / 2.5% |
| 加密工作日 | 0.5% / 1.0% / 1.5% | 5 | 1.5% / 2.5% / 3.5% |
| 加密周末 | 0.5% / 1.0% / 1.5% | 6 | 1.5% / 2.5% / 3.5% |

默认配置里 ETH 地板 × 1.2、SOL × 1.4。`monitor check-config` 会打印当前生效的完整门槛表。

## 快速开始

### 环境要求

- Linux 服务器：Debian 12 或 Ubuntu 24.04（安装脚本基于 apt 和 systemd），1 核 1 GB 内存即可
- Python 3.11+
- 能访问 Binance 和 Telegram 的网络（部分地区的服务器访问 Binance 会受限，请先确认）
- **不需要开放任何入站端口**：机器人用长轮询收消息
- 只监控加密货币时，只需要一个 Telegram 机器人；监控美股还需要长桥账户和美股行情权限

### 1. 安装

```bash
sudo git clone https://github.com/lu1zgg/Trafast.git /opt/market-monitor
cd /opt/market-monitor
sudo bash deploy/install.sh
```

安装脚本会：

- 安装 `python3-venv` 和 `chrony`（检测按分钟计算，系统时钟要准）
- 创建 `monitor` 系统用户和虚拟环境
- 生成 `/etc/market-monitor/config.toml` 和 `/etc/market-monitor/secrets.env`（权限 600）
- 安装 systemd 服务 `market-monitor` 和运维命令 `mm`

脚本可以重复运行，不会覆盖已经改过的配置。`mm` 以 `monitor` 用户、带上 `secrets.env` 运行 `monitor` 的子命令，因此需要 `sudo`。

仓库可以放在任意目录，安装脚本会按实际路径生成服务文件。

### 2. 创建 Telegram 机器人和群

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather)，发 `/newbot` 建机器人，拿到 token，写进 `/etc/market-monitor/secrets.env`：
   ```
   TELEGRAM_BOT_TOKEN="123456:ABC..."
   ```
2. 建一个群，把机器人拉进去。想在群里直接发 `/status` 这类命令，在 BotFather 里 `/setprivacy` → Disable；不关隐私模式的话，命令要写成 `/status@你的机器人名`。
3. 获取群 ID：在群里随便发一句话，然后执行
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":-[0-9]*'
   ```
   把得到的负数填进 `config.toml` 的 `[telegram] chat_id`。
4. 可选：把允许使用 `/mute` 等管理命令的 Telegram 用户 ID 填进 `admin_user_ids`。留空表示群里任何人都能用。
5. 可选：用 BotFather 的 `/setuserpic` 把 `assets/logo/trafast-logo-640.png` 设为机器人头像。

> 同一个机器人 token 同时只能被一个程序轮询。如果有别的程序在用这个 token，先停掉它，否则 Telegram 会返回 409，日志里会持续报错。

### 3. 先只跑加密货币

`config.toml` 里 `[stocks] enabled = false` 是默认值，美股要等接通长桥后再开。

```bash
sudo mm check-config                 # 校验配置，打印门槛表和密钥是否已设置
sudo mm test-telegram                # 往群里发一条测试消息
sudo mm backfill-crypto --days 14    # 提前拉 14 天 K 线做基线（服务启动时也会自动补）
sudo mm replay --days 7              # 用最近 7 天回放，看平均每天会发多少条
sudo mm replay --days 7 --show       # 把每条消息打印出来
sudo systemctl start market-monitor
journalctl -u market-monitor -f
```

启动后群里会收到一条启动消息，之后每天按 `[health] daily_report_time` 发日报。

### 4. 外部心跳（推荐）

服务挂掉或服务器断网时，它自己没法报警。可以在 [healthchecks.io](https://healthchecks.io/) 之类的服务建一个检查（周期 1 分钟、宽限 5 分钟），把 ping 地址写进 `secrets.env`：

```
HEARTBEAT_URL="https://hc-ping.com/xxxxxxxx"
```

然后 `sudo systemctl restart market-monitor`。Telegram 发不出去时，服务会访问 `<地址>/fail`。

## 接通长桥（美股）

### 准备

需要一个长桥证券账户，并开通美股行情权限。开放平台里显示为 US LV1（仅 OpenAPI），夜盘行情包含在内。登录 [长桥开放平台](https://open.longbridge.com/) 可以查看「行情权限」；没有的话，在长桥 App「我的 → 我的行情 → 商店」开通。

凭据有两种方式，二选一。

### 方式 A：OAuth（推荐，token 自动续期）

1. 注册一个 OAuth 客户端，拿到 `client_id`（只做一次，任何能上网的机器都可以）：
   ```bash
   curl -s -X POST https://openapi.longbridge.com/oauth2/register \
     -H 'Content-Type: application/json' \
     -d '{"client_name":"trafast-monitor","redirect_uris":["http://localhost:60355/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}'
   ```
   把返回的 `client_id` 写进 `secrets.env`：`LONGBRIDGE_OAUTH_CLIENT_ID="…"`，并在 `config.toml` 的 `[stocks.longbridge]` 里设置 `auth = "oauth"`。回调端口默认 60355，要和上面注册的地址一致。
2. 授权。在本地电脑上开一个带端口转发的 SSH 连接：
   ```bash
   ssh -L 60355:127.0.0.1:60355 user@your-server
   ```
   在这个 SSH 会话里执行 `sudo mm lb-login`，它会打印一个 `https://openapi.longbridge.com/oauth2/authorize?...` 链接（每次都是新生成的）。在本地浏览器打开，登录长桥并同意授权；浏览器跳回 `localhost:60355` 后经端口转发回到服务器，终端显示「授权完成」。
3. token 保存在 `/var/lib/market-monitor/.longbridge/openapi/tokens/`，服务会自动续期。日志提示需要重新授权时，重复第 2 步。

### 方式 B：API Key（简单，但约 90 天要更换）

在开放平台「用户中心」复制 App Key、App Secret、Access Token，写进 `secrets.env`：

```
LONGBRIDGE_APP_KEY="…"
LONGBRIDGE_APP_SECRET="…"
LONGBRIDGE_ACCESS_TOKEN="…"
```

Access Token 默认 90 天过期。把到期日写进 `config.toml` 的 `[health] longbridge_token_expires = 2027-02-20`，到期前 7 天群里会提醒。

### 核对并启用

1. 最好在美股交易时段（含盘前、盘后或夜盘）运行：
   ```bash
   sudo mm lb-check --symbol NVDA.US
   ```
   它会逐项检查：
   - 能否连上，行情等级是什么
   - 配置的股票是否都在长桥夜盘名单里（不在的夜盘没有数据）
   - 长桥返回的当天交易时段
   - 最近 10 根 K 线的原始时间戳：如果最后一根比当前时间早 1 分钟左右，说明时间戳是开盘时间，保持 `ts_is_close = false`（默认）；早不到 1 分钟则改成 `true`
   - 订阅推送 150 秒，看各时段有没有推送、收盘后几秒到达。交易时段完全收不到推送的话，把 `bar_source` 改成 `"trades"`（用逐笔成交合成 K 线）
2. `config.toml` 里设置 `[stocks] enabled = true`，然后 `sudo systemctl restart market-monitor`。启动后会在后台补 35 天历史 K 线做基线，期间只按直推线判断。
3. 运行一两天后，用 `sudo mm replay --days 2 --show` 检查美股的消息量是否合适。

## 配置

所有配置在 `/etc/market-monitor/config.toml`（模板见 [`deploy/config.example.toml`](deploy/config.example.toml)），密钥只放在 `/etc/market-monitor/secrets.env`（模板见 [`deploy/secrets.env.example`](deploy/secrets.env.example)），不要写进配置文件或提交到 git。

改完配置后先 `sudo mm check-config` 校验：

- 规则参数、静音等：`sudo systemctl reload market-monitor` 热加载
- 标的、群组、市场开关：需要 `sudo systemctl restart market-monitor`

### 增删监控标的

目前通过配置文件管理：

```toml
[crypto]
symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]     # Binance 现货交易对

[stocks]
symbols = ["NVDA.US", "TSLA.US", "AAPL.US"]     # 长桥代码，必须带 .US
```

在列表里加上或删掉代码，`sudo mm check-config` 校验后 `sudo systemctl restart market-monitor`。新标的启动时会自动补历史 K 线（加密 14 天、美股 35 天），基线够数之前只按直推线判断。新加的美股建议跑一次 `sudo mm lb-check` 确认夜盘数据可用。删掉的标的历史数据保留在数据库里，不影响运行。

### 调参

- 换档位：`[rules] preset = "sensitive"`（灵敏）、`"balanced"`（均衡）或 `"conservative"`（克制）。改之前可以先对比：`sudo mm replay --days 7 --preset balanced`
- 单个时段微调，例如美股盘中 Z 改成 5.5：
  ```toml
  [rules.overrides.REG]
  z = 5.5
  ```
  时段代码：`REG` 盘中、`PRE` 盘前、`POST` 盘后、`ON` 夜盘
- 单个标的放宽地板：
  ```toml
  [stocks.multipliers]
  "BE.US" = 1.3

  [crypto.multipliers]
  SOLUSDT = 1.4
  ```
- 日报时间、时区、各种健康检查阈值在 `[health]`；数据保留天数和备份在 `[storage]`

每条判定（包括没推送的）都记在数据库 `alerts` 表里，带位移、门槛、得分和结果；日报里有「挡下的候选」统计，方便校准。

## Telegram 命令

| 命令 | 作用 |
|---|---|
| `/status` | 运行状态、各数据源最近一次数据、进行中的异动、静音列表、待发消息 |
| `/mute MU 2h` | 静音一个标的（默认 1 小时，最长 7 天）；`/mute all 30m` 静音全部 |
| `/unmute MU`、`/unmute all` | 取消静音 |
| `/mutes` | 当前静音列表 |
| `/params` | 当前门槛表 |
| `/report` | 立即生成最近 24 小时日报 |
| `/help` | 命令说明 |

告警下方的 👍/👎 按钮记录这条告警是否有用，结果进入日报。

## 命令行

服务器上通过 `sudo mm <子命令>` 运行（开发环境里直接用 `monitor <子命令>`）：

| 子命令 | 作用 |
|---|---|
| `run` | 运行服务（由 systemd 调用） |
| `check-config` | 校验配置，打印门槛表和密钥设置情况 |
| `test-telegram` | 往群里发一条测试消息 |
| `backfill-crypto --days N` | 从 Binance 拉加密货币历史 K 线 |
| `import-csv FILE...` | 导入 CSV K 线，列为 `symbol,ts,o,h,l,c,volume,turnover[,session]` |
| `replay [--days N] [--preset P] [--show]` | 用数据库里的历史 K 线回放，统计会发多少条；也可以 `--csv bars.csv --from 2026-04-18 --to 2026-04-23` |
| `report [--send]` | 打印最近 24 小时日报，`--send` 发到群里 |
| `ctl status`、`ctl mute MU 2h`、`ctl unmute all` | 和机器人命令相同 |
| `lb-check [--symbol X]` | 长桥接入逐项检查 |
| `lb-login` | 长桥 OAuth 授权 |

## 运维

```bash
systemctl status market-monitor
journalctl -u market-monitor -f            # 日志（token 等密钥会自动打码）
sudo mm status
```

- 数据目录 `/var/lib/market-monitor/`：`monitor.db`（SQLite）；`backups/` 每天备份一次，默认保留 7 份；K 线默认保留 120 天
- 数据源断流超过 3 分钟、发件积压、事件循环卡顿、时钟偏差超过 1 秒、长桥 token 快到期，都会在群里发运维消息，恢复时再发一条
- systemd 看门狗：主循环 60 秒没有心跳会自动重启；文件锁保证同一时间只跑一个实例

### 升级和回滚

```bash
cd /opt/market-monitor
sudo bash deploy/upgrade.sh           # 拉取 main 最新代码、重装、校验配置、重启
sudo bash deploy/upgrade.sh 1a2b3c4   # 切到指定提交或标签（回滚）
```

脚本最后会打印回滚用的命令。

## 开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test,longbridge]" ruff
pytest -q          # 单元测试、故障注入测试、金标回放，约 25 秒
ruff check .
```

- `tests/golden/`：合成 5 天行情并注入 7 类事件（周末插针、站稳的台阶、三币联动下跌、盘中大跌后回吐、只放量、夜盘薄成交跳价、盘前台阶），断言该推的推、该挡的挡，随机噪声本身不触发任何消息；消息清单和 `expected.json` 逐条比对。规则或文案有意改动后，用 `UPDATE_GOLDEN=1 pytest tests/golden` 重新生成，并在提交说明里写清原因。
- `tests/fault/`：本地 WebSocket 服务器模拟 Binance 断线、重复推送、未收盘更新、中途丢推送。

### 代码结构

```
market_monitor_v3/
  config.py      配置模型、档位表、门槛计算
  sessions.py    美股交易日历（内置 2026–2027 休市日，接通长桥后每天刷新）、时段划分
  detector.py    触发规则（纯函数）
  episodes.py    事件状态机：待确认 → 进行中 → 结束 → 冷却，插针和回吐保护
  merge.py       联动合并、限流
  engine.py      串起以上模块；补平 K 线、迟到 K 线处理
  render.py      消息文案
  store.py       SQLite（K 线、判定、事件、发件箱、静音、反馈）
  outbox.py      Telegram 发件箱：重试、编辑、回复、错误处理
  bot.py         命令和反馈按钮（长轮询）
  health.py      断流、积压、卡顿、对时、心跳、看门狗
  reporting.py   日报
  replay.py      回放
  feeds/         binance_spot.py、longbridge.py
  app.py         运行时：各个循环、热加载、优雅退出
  cli.py         monitor 命令
deploy/          systemd 服务、install.sh、upgrade.sh、mm、配置和密钥模板
assets/logo/     Logo（SVG 源文件和多尺寸 PNG）
```

## 已知限制

- 长桥数据源已按 SDK `longbridge` 5.5 实现并有模拟测试，但真实账号下的行为请先用 `mm lb-check` 核对
- 内置美股休市日历覆盖 2026–2027 年，之后依赖长桥每天刷新，或在 `[calendar] extra_holidays` 手动补充
- 不包含财报、新闻等事件数据；周末合约模块（`[weekend_perp]`）未实现
- 消息文案和日志目前只有中文

## 参与贡献

欢迎提 Issue 和 Pull Request。提交前请确保：

- `pytest -q` 和 `ruff check .` 通过
- 改动了判定规则或消息文案的，更新金标测试并在 PR 说明里写清原因
- 不要在代码、测试或提交里放真实的 token、群 ID 或账户信息

## 许可证

本项目以 [MIT License](LICENSE) 发布。

## 免责声明

本项目仅用于技术研究和个人行情监控。告警由程序按固定规则自动产生，可能延迟、遗漏或误报，**不构成任何投资建议**。行情数据来自第三方（Binance、长桥），其可用性和准确性由数据提供方负责，使用时请遵守各平台的服务条款。据此做出的任何交易决定及其后果由使用者自行承担。
