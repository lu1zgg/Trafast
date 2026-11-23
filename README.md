# Trafast · 异动监控 V3

美股正股（含盘前、盘后、夜盘）和加密货币现货的价格异动监控，推送到 Telegram 群。

- 美股：长桥 OpenAPI 1 分钟 K 线，10 只：NVDA、GOOGL、MU、SNDK、META、STX、INTC、BE、TSLA、SPCX
- 加密：Binance 现货 1 分钟 K 线，BTC、ETH、SOL
- 规则：灵敏档。设计和取舍见 [V3 方案文档](https://claude.ai/artifact/6idFtZaapTC4MdtDQ3EKLG)
- 不做：周末合约模块（配置里开不了）、财报和新闻、OKX

## 当前状态

| 部分 | 状态 |
|---|---|
| 检测规则、确认、事件（延伸、升级、结束）、联动合并、限流 | 完成，有单元测试和金标回放测试 |
| Telegram 推送（发件箱、重试、编辑、回复）、命令、👍/👎 反馈 | 完成，用模拟 Telegram 测过 |
| Binance 数据源（断线重连、补数、缺口补齐） | 完成，用本地模拟服务器测过断线、重复、丢推送 |
| 健康检查、运维消息、日报、心跳、备份 | 完成 |
| 长桥数据源 | 代码写好，**还没接通真实账号**，接通步骤见下文 |
| 真实行情回放 | 还没做：开发环境连不上 Binance。部署后跑 `sudo mm replay` 看实际消息量 |

## 怎么判断异动（灵敏档）

每分钟收盘后，对 1、5、15 分钟三个窗口算位移 d = ln(现价 / k 分钟前价格)：

- |d| 超过 `max(地板 × 标的系数, Z × 该时段近 20 个同类时段的波动)`，或者超过「直推线」，就是候选。
- 候选要等下一根 K 线：位移还保留 60% 以上才推送。插针（拉上去马上回来）在这一步被挡掉，收回来的那一段也不会当成反方向异动。
- 超过直推线或得分 ≥ 2 的直接推 L3，不等确认。
- 夜盘和美东 7 点前的盘前，成交太少的跳价不推（窗口成交额、以及走出这段位移的那几根 K 线的成交额，都要不低于该时段常态的 25%）。
- 只放量、价格不动，不推。
- 同一事件后续再走一个 5 分钟地板会在原消息下回复「延伸」，最多 3 条；结束时静默编辑原消息补上「已结束」。结束后往回走但没越过起点的，算回吐，不另发。
- 同一市场、同一方向 3 分钟内的多个标的合并成一条联动消息。

| 时段 | 地板 1/5/15 分钟 | Z | 直推线 1/5/15 分钟 |
|---|---|---|---|
| 美股盘中 | 0.4% / 0.8% / 1.2% | 5 | 1.5% / 2.5% / 3% |
| 美股盘前、盘后 | 0.6% / 1.2% / 1.8% | 6 | 1.5% / 2.5% / 2.5% |
| 美股夜盘 | 0.8% / 1.5% / 2.2% | 6 | 1.5% / 2.5% / 2.5% |
| 加密工作日 | 0.5% / 1.0% / 1.5% | 5 | 1.5% / 2.5% / 3.5% |
| 加密周末 | 0.5% / 1.0% / 1.5% | 6 | 1.5% / 2.5% / 3.5% |

ETH 地板 × 1.2，SOL × 1.4。`sudo mm check-config` 会打印当前生效的完整表。

## 部署到德国 VPS

要求：Debian 12 或 Ubuntu 24.04，1 核 1 GB 足够，**不需要开任何入站端口**（机器人用长轮询收消息）。

### 1. 拉代码（私有仓库用只读 Deploy key）

```bash
sudo -i
ssh-keygen -t ed25519 -N "" -f /root/.ssh/trafast_deploy
cat /root/.ssh/trafast_deploy.pub
```

把输出的公钥加到 GitHub：仓库 Trafast → Settings → Deploy keys → Add deploy key，不要勾 Allow write access。然后：

```bash
GIT_SSH_COMMAND="ssh -i /root/.ssh/trafast_deploy -o IdentitiesOnly=yes" \
  git clone git@github.com:louisg729-lgtm/Trafast.git /opt/market-monitor
git -C /opt/market-monitor config core.sshCommand "ssh -i /root/.ssh/trafast_deploy -o IdentitiesOnly=yes"
```

### 2. 安装

```bash
cd /opt/market-monitor
sudo bash deploy/install.sh
```

它会：装 `python3-venv` 和 `chrony`（对时，检测按分钟算，时钟要准）、建 `monitor` 系统用户、建虚拟环境、生成 `/etc/market-monitor/config.toml` 和 `secrets.env`、装 systemd 服务和 `mm` 命令。可以重复运行，不会覆盖你改过的配置。

`mm` 是运维命令的包装：以 `monitor` 用户、带上 `secrets.env` 运行 `monitor` 的子命令，所以都要 `sudo`。

### 3. Telegram 群和机器人

1. 找 @BotFather 建机器人，拿到 token，填进 `/etc/market-monitor/secrets.env` 的 `TELEGRAM_BOT_TOKEN="…"`。
2. 把机器人拉进群。想在群里直接发 `/status` 就能用，在 BotFather 里 `/setprivacy` → Disable；不关隐私模式的话，命令要写成 `/status@你的机器人名`。
3. 群 ID：在群里随便发一句话，然后在 VPS 上执行
   `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":-[0-9]*'`，
   把那个负数填进 `config.toml` 的 `[telegram] chat_id`。
4. 想限制谁能 `/mute`，把 Telegram 用户 ID 填进 `admin_user_ids`；留空则群里任何人都能用。

> **如果 V2 还在用同一个机器人 token，先停掉 V2。** 两个程序同时收消息会互相冲突（Telegram 返回 409），V3 会在日志里报这个错并不断重试。

### 4. 先只跑加密货币

`config.toml` 里 `[stocks] enabled = false` 是默认值，先别改。

```bash
sudo mm check-config                 # 校验配置，打印门槛表、密钥是否已设置
sudo mm test-telegram                # 往群里发一条测试消息
sudo mm backfill-crypto --days 14    # 拉 14 天 K 线做基线（启动时也会自动补，这一步只是提前做）
sudo mm replay --days 7              # 用最近 7 天回放：看平均每天会发多少条
sudo mm replay --days 7 --show       # 把每条消息打印出来看
sudo systemctl start market-monitor
journalctl -u market-monitor -f
```

启动后群里会收到「V3 已启动」，之后每天北京时间 09:05 发日报。

### 5. 外部心跳（推荐）

服务死掉或 VPS 断网时，它自己发不出消息。在 healthchecks.io 之类的服务建一个检查（周期 1 分钟、宽限 5 分钟），把 ping 地址填进 `secrets.env` 的 `HEARTBEAT_URL="…"`，再 `sudo systemctl restart market-monitor`。Telegram 发不出去时它会 ping `<地址>/fail`。

## 接通长桥（美股）

1. 在长桥开放平台拿到 App Key、App Secret、Access Token，填进 `secrets.env`：
   ```
   LONGBRIDGE_APP_KEY="…"
   LONGBRIDGE_APP_SECRET="…"
   LONGBRIDGE_ACCESS_TOKEN="…"
   ```
   账号要有美股行情权限，夜盘需要单独的夜盘行情权限。
   Access Token 有到期日，把日期填进 `config.toml` 的 `[health] longbridge_token_expires = 2027-02-20`，到期前 7 天群里会提醒。
   （也可以用 OAuth：`auth = "oauth"`，填 `LONGBRIDGE_OAUTH_CLIENT_ID`，在 `[stocks.longbridge]` 里加 `oauth_callback_port = 8765`，本机执行 `ssh -L 8765:127.0.0.1:8765 root@你的VPS`，再在 VPS 上 `sudo mm lb-login`，把打印的链接在本机浏览器打开。）
2. 逐项核对，**最好在美股交易时段（含盘前、盘后或夜盘）跑**：
   ```bash
   sudo mm lb-check --symbol NVDA.US
   ```
   它会检查 5 件事，结果贴给我或者自己对照：
   - ① 能连上，行情等级是什么
   - ② 10 只股票是不是都在夜盘名单里（不在的夜盘不会有数据）
   - ③ 长桥返回的当天交易时段
   - ④ 最近 10 根 K 线的原始时间戳和时段：如果最后一根比现在早 1 分钟左右，时间戳是开盘时间，`ts_is_close = false`（默认）；早不到 1 分钟则改成 `true`
   - ⑤ 订阅推送 150 秒：盘前、盘后、夜盘有没有推送、收盘后几秒到。交易时段完全没推送的话，把 `bar_source` 改成 `"trades"`（用逐笔成交自己合成 K 线）
3. `config.toml` 里 `[stocks] enabled = true`，然后 `sudo systemctl restart market-monitor`。启动后会在后台补 35 天历史 K 线做基线，每只要几十秒，期间只用直推线判断。
4. 过一两天跑 `sudo mm replay --days 2 --show` 看美股消息量是否合适。

## 机器人命令

| 命令 | 作用 |
|---|---|
| `/status` | 运行状态、各数据源多久前有数据、进行中的异动、静音列表、待发消息 |
| `/mute MU 2h` | 静音一个标的（默认 1 小时，最长 7 天；`/mute all 30m` 静音全部） |
| `/unmute MU`、`/unmute all` | 取消静音 |
| `/mutes` | 当前静音列表 |
| `/params` | 当前门槛表 |
| `/report` | 立即生成最近 24 小时日报 |
| `/help` | 命令说明 |

告警下面的 👍/👎 记录这条有没有用，进日报，用来校准。命令也可以在 VPS 上用：`sudo mm status`、`sudo mm ctl mute MU 2h`、`sudo mm ctl unmute all`。

## 调参和回放

- 改 `config.toml` 后 `sudo mm check-config`，再 `sudo systemctl reload market-monitor` 热加载规则参数；标的、群组、市场开关的变化要 `restart`。
- 换档位：`[rules] preset = "balanced"` 或 `"conservative"`。改之前可以先比较：`sudo mm replay --days 7 --preset balanced`。
- 单个时段微调，例如盘中 Z 改成 5.5：
  ```toml
  [rules.overrides.REG]
  z = 5.5
  ```
- 单个标的放宽地板：`[stocks.multipliers]` 里写 `"BE.US" = 1.3`。
- 每条判定（包括没推送的）都记在数据库 `alerts` 表里，带位移、门槛、得分和结果，日报里有「挡下的候选」统计。
- 回放也可以用 CSV：`sudo mm replay --csv bars.csv --from 2026-04-18 --to 2026-04-23`，列为 `symbol,ts,o,h,l,c,volume,turnover[,session]`。

## 日常运维

```bash
systemctl status market-monitor
journalctl -u market-monitor -f            # 日志（token 等密钥会自动打码）
journalctl -u market-monitor --since "1 hour ago"
sudo mm status
sudo mm report                             # 在终端打印日报；加 --send 发到群里
```

- 数据在 `/var/lib/market-monitor/`：`monitor.db`（SQLite），`backups/` 每天北京时间 12:30 备份一次，保留 7 份。K 线保留 120 天。
- 数据源断流超过 3 分钟、发件积压、事件循环卡顿、时钟偏差超过 1 秒、长桥 token 快到期，都会在群里发运维消息，恢复时再发一条。
- systemd 看门狗：主循环 60 秒没有心跳会自动重启。同一时间只能跑一个实例（文件锁）。

## 升级和回滚

```bash
cd /opt/market-monitor
sudo bash deploy/upgrade.sh           # 拉 main 最新代码、重装、校验配置、重启
sudo bash deploy/upgrade.sh 1a2b3c4   # 切到指定提交或标签（回滚）
```

脚本最后会打印回滚用的命令。

## 开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test,longbridge]" ruff
pytest -q          # 单元、故障、金标回放，约 25 秒
ruff check .
```

- `tests/golden/`：合成 5 天行情，注入 7 类事件（周末插针、站稳的台阶、三币联动下跌、盘中大跌后回吐、只放量、夜盘薄成交跳价、盘前台阶），断言该推的推、该挡的挡，且随机噪声本身不触发任何消息；消息清单和 `expected.json` 逐条比对。规则或文案有意改动后，用 `UPDATE_GOLDEN=1 pytest tests/golden` 重新生成，并在提交说明里写清原因。
- `tests/fault/`：本地 WebSocket 服务器模拟 Binance 断线、重复推送、未收盘更新、中途丢推送。

### 代码结构

```
market_monitor_v3/
  config.py      配置模型、档位表、门槛计算
  sessions.py    美股交易日历（2026–2027 休市日，接通长桥后每天刷新）、时段划分
  detector.py    触发规则（纯函数）
  episodes.py    事件状态机：待确认 → 进行中 → 结束 → 冷却，插针和回吐保护
  merge.py       联动合并、限流
  engine.py      把以上串起来；补平 K 线、迟到 K 线处理
  render.py      消息文案
  store.py       SQLite（K 线、判定、事件、发件箱、静音、反馈）
  outbox.py      Telegram 发件箱：重试、编辑、回复、各种错误处理
  bot.py         命令和反馈按钮（长轮询）
  health.py      断流、积压、卡顿、对时、心跳、看门狗
  reporting.py   日报
  replay.py      回放
  feeds/         binance_spot.py、longbridge.py
  app.py         运行时：各个循环、热加载、优雅退出
  cli.py         monitor 命令
deploy/          systemd 服务、install.sh、upgrade.sh、mm、配置和密钥示例
```
