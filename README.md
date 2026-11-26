# Trafast · 异动监控 V3

美股正股（含盘前、盘后、夜盘）和加密货币现货的价格异动监控，推送到 Telegram 群、Discord 频道（可以同时开），也能转发 TradingView 告警。标的、规则、推送渠道都可以在网页管理后台里改。

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
| Discord 推送（嵌入卡片、按钮、斜杠命令） | 完成，用模拟 Discord 测过，还没连真实服务器 |
| 网页管理后台（密码 + 两步验证） | 完成，有测试 |
| TradingView 告警转发 | 完成，用模拟 webhook 测过 |
| 长桥数据源 | 代码写好，**还没接通真实账号**，接通步骤见下文 |
| 趋势监控（15m / 1h / 4h，支撑压力位） | 完成，有单元测试；规则用 8 个月真实 K 线回测过，见下文 |
| V4 逐笔层（大单流向标签、单笔冲涨跌影子记录） | 完成，有单元测试和模拟断线测试；Binance 逐笔还没在 VPS 上测 CPU，长桥逐笔等接通 |
| V4 对外 API（/api/v1 + WebSocket，API 密钥）、关键位突破、异动统计、指标提醒 | 完成，有单元测试 |
| V4 桌面版（Mac + Windows，Tauri） | 完成，界面连本地后端实测过；安装包由 GitHub Actions 打，还没在真机上装过 |
| 真实行情回放 | 还没做：开发环境连不上 Binance。部署后跑 `sudo mm replay` 看实际消息量 |

## 怎么判断异动（灵敏档）

每分钟收盘后，对 1、5、15 分钟三个窗口算位移 d = ln(现价 / k 分钟前价格)：

- |d| 超过 `max(地板 × 标的系数, Z × 该时段近 20 个同类时段的波动)`，或者超过「直推线」，就是候选。
- 候选要等下一根 K 线：位移还保留 60% 以上才推送。插针（拉上去马上回来）在这一步被挡掉，收回来的那一段也不会当成反方向异动。
- 超过直推线或得分 ≥ 2 的是 L3：盘中收盘即推；盘前、盘后、夜盘也要等下一根确认（多 1 分钟），挡住单根坏价。
- 盘外某根 K 线正好收在上一交易日的盘中收盘价上、而上一根还离得很远，按「前收参考价」回报的旧价处理，不推送，也不留在窗口里。
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

要求：Debian 12 或 Ubuntu 24.04，1 核 1 GB 足够。只跑监控**不需要开任何入站端口**（机器人用长轮询收消息）；开网页后台时要放行 80 和 443 给 Caddy。

### 1. 拉代码（私有仓库用只读 Deploy key）

```bash
sudo -i
ssh-keygen -t ed25519 -N "" -f /root/.ssh/trafast_deploy
cat /root/.ssh/trafast_deploy.pub
```

把输出的公钥加到 GitHub：仓库 Trafast → Settings → Deploy keys → Add deploy key，不要勾 Allow write access。然后：

```bash
GIT_SSH_COMMAND="ssh -i /root/.ssh/trafast_deploy -o IdentitiesOnly=yes" \
  git clone git@github.com:lu1zgg/Trafast.git /opt/market-monitor
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

## 推送样式

🟢 涨、🔴 跌，标在代码前面；价格带 `$`，价格、涨跌幅、成交额、倍数在 Telegram 里加粗显示。多窗口和成交额用分割线分成单独的块；成交额按大小写成 `$845.30K`、`$3.62M`、`$1.20B`。同一条联动消息里的每个标的都按同样的完整格式展开（太多放不下时，后面的改成紧凑格式并注明还有几个）。

```
🔴 NVDA -1.9%｜1 分钟｜L3 剧烈
现价 $134.03（盘前）｜起点 $136.66
快速通道：位移很大，收盘即推
────────────────
多窗口
1 分钟 -1.9%｜5 分钟 -2.4%｜15 分钟 -2.7%
────────────────
成交额
1 分钟 $3.62M｜该时段常态的 2.1 倍
────────────────
北京 11-23 19:58｜美东 06:58
━━━━━━━━━━━━━━━━
同时异动（1）
🔴 INTC -1.8%｜1 分钟｜L2 显著
……（同样的完整格式）
```

Discord 里是同样的内容，放在左边带颜色竖条的卡片里（绿涨红跌）。网页后台的「推送渠道」页可以预览，也可以一键往群里发一条示例。

## 网页管理后台

在浏览器里加减标的、调规则、配置 Telegram / Discord / TradingView、看运行状态和最近的告警。是一个单独的服务（`market-monitor-web`），只听本机 `127.0.0.1:8787`，由 Caddy 反向代理出 HTTPS。

安全设计：

- 只有一个管理员账号，**只能在服务器上用 `sudo mm web-passwd` 创建**，网页上没有注册入口。登录要密码 + 手机验证器（TOTP）上的 6 位数字。
- 同一来源连续输错 5 次锁 15 分钟，全站累计错太多也会整体锁定；锁定和每次登录都会发运维消息到群里。
- 会话 12 小时过期，Cookie 只走 HTTPS（`HttpOnly`、`SameSite=Strict`），所有修改都校验 CSRF。
- 密钥（token、长桥凭据）只能写入，页面上只显示「已设置，末 4 位」，从不回显原文。
- 每次保存前用和监控相同的规则完整校验，不通过不写；旧配置自动存档，可以在「历史与审计」里对比和一键恢复；所有操作记审计日志。
- 后台以单独的 `mmweb` 用户运行，监控进程对配置只读。两者只通过本机 unix socket 通信。

### 开启

1. 准备一个域名（或子域名），在 DNS 里加一条 A 记录指向 VPS 的 IP。
2. 在 VPS 上：
   ```bash
   cd /opt/market-monitor
   sudo bash deploy/install.sh                  # 老安装先重跑一次：装后台依赖、建 mmweb 用户、调整配置文件权限
   sudo bash deploy/setup-web.sh 你的域名        # 装 Caddy、自动申请 HTTPS 证书、启动后台
   ```
   `setup-web.sh` 第一次运行时会接着执行 `mm web-passwd`：设用户名和密码（至少 12 位），用手机验证器（Google Authenticator、Microsoft Authenticator、1Password 等）扫终端里的二维码，再输入一次 6 位数字确认。
3. 浏览器打开 `https://你的域名`。

不想对外开放的话：`sudo bash deploy/setup-web.sh --local-only`，然后从自己电脑 `ssh -L 8787:127.0.0.1:8787 用户名@VPS地址`，浏览器开 `http://localhost:8787`（如果浏览器登录后又跳回登录页，把 `config.toml` 里 `[web] cookie_secure` 改成 `false` 再 `sudo systemctl restart market-monitor-web`）。

忘记密码或换手机：再运行一次 `sudo mm web-passwd`，然后 `sudo systemctl restart market-monitor-web` 让所有旧登录失效。

### 改了什么时候生效

- 规则、渠道接收范围、TradingView 设置：保存后立即生效。
- 标的、市场开关、渠道 token 和开关、数据源密钥：监控进程会自己退出，由 systemd 5 秒后拉起，十几秒后生效（启动时会补齐中间的 K 线）。
- 也可以继续直接编辑 `/etc/market-monitor/config.toml`。网页保存时如果发现文件在你打开页面后被改过，会拒绝覆盖，提示刷新。

## Discord 推送

1. 打开 [Discord 开发者后台](https://discord.com/developers/applications) → New Application → 左边 Bot → Reset Token，复制 token。
2. 在 Discord 设置 → 高级里打开「开发者模式」，右键要推送的频道 →「复制频道 ID」。
3. 网页后台「推送渠道」→ Discord：填 token 和频道 ID，勾「启用」，保存；再点「检查连接」，页面会给出邀请链接，打开它把机器人拉进你的服务器。
   不用后台的话：token 填进 `secrets.env` 的 `DISCORD_BOT_TOKEN`，`config.toml` 里 `[discord] enabled = true`、`channel_id = "…"`，然后 `sudo mm test-discord`。
4. 告警下面有「有用 / 噪音」按钮，频道里可以用斜杠命令 `/status` `/mute` `/unmute` `/mutes` `/params` `/report`。不需要开任何特权 intent。

每个渠道可以单独选择接收哪些消息（异动告警、最低级别、美股或加密、TradingView、运维消息、日报），例如 Telegram 收全部、Discord 只收 L3。

## TradingView 告警转发

把 TradingView 上自己设的价格、指标、策略告警，用同样的卡片样式转发到 Telegram / Discord。

1. 网页后台「TradingView」页勾选启用并保存，页面会生成一个带密钥的专用地址 `https://你的域名/hooks/tradingview/…`。地址泄露了可以一键更换。
2. TradingView 建告警 →「通知」→ 勾选 Webhook URL，粘贴这个地址。需要 TradingView 付费套餐，并且账号开启了两步验证（TradingView 的要求）。
3. 「消息」一栏推荐写 JSON，TradingView 会替换 `{{…}}` 占位符：
   ```json
   {"ticker": "{{ticker}}", "exchange": "{{exchange}}", "price": {{close}}, "interval": "{{interval}}", "action": "buy", "message": "突破前高"}
   ```
   `action` 写 buy / long 显示 🟢，sell / short 显示 🔴。不是 JSON 的消息按原文转发。

有限流（默认每分钟最多 20 条）和去重（相同内容 60 秒内只发一次），防止告警写错时刷屏。

## 接通长桥（美股）

1. 准备：长桥账户（在长桥 App 开户），并且有美股行情权限。开放平台里显示为 US LV1（仅 OpenAPI），夜盘行情包含在内。
   在 [开放平台](https://open.longbridge.com/) 登录后看「行情权限」；没有的话在长桥 App「我的 → 我的行情 → 商店」开通（英文界面是 Me → My Quotes → Store）。

   凭据二选一。

   **方式 A：OAuth（长桥推荐，token 自动续期，适合长期跑在服务器上）**

   ① 注册一个 OAuth 客户端，拿到 `client_id`。只做一次，在 VPS 或任何能上网的电脑上执行：
   ```bash
   curl -s -X POST https://openapi.longbridge.com/oauth2/register \
     -H 'Content-Type: application/json' \
     -d '{"client_name":"trafast-monitor","redirect_uris":["http://localhost:60355/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}'
   ```
   返回结果里的 `client_id` 填进 `secrets.env`：`LONGBRIDGE_OAUTH_CLIENT_ID="…"`。
   `config.toml` 的 `[stocks.longbridge]` 改成 `auth = "oauth"`，`oauth_callback_port` 不用写（默认 60355，要和上面注册的回调地址一致）。

   ② 授权。在你自己的电脑上开一个带端口转发的 SSH 连接：
   ```bash
   ssh -L 60355:127.0.0.1:60355 root@你的VPS
   ```
   在这个 SSH 窗口里执行 `sudo mm lb-login`，它会打印一个 `https://openapi.longbridge.com/oauth2/authorize?...` 开头的链接。
   这个链接每次都是现生成的（带一次性校验参数），所以没有固定的授权地址。
   把链接复制到你电脑的浏览器打开，登录长桥并同意授权。浏览器会跳回 `localhost:60355`，经端口转发回到 VPS，终端显示「授权完成」。
   token 存在 `/var/lib/market-monitor/.longbridge/openapi/tokens/`，服务会自动续期。以后如果日志提示重新授权，重复第 ② 步。

   **方式 B：API Key（旧方式，最简单，但 90 天要换一次）**

   在 [开放平台](https://open.longbridge.com/) 登录后进「用户中心」，复制 App Key、App Secret、Access Token，填进 `secrets.env`：
   ```
   LONGBRIDGE_APP_KEY="…"
   LONGBRIDGE_APP_SECRET="…"
   LONGBRIDGE_ACCESS_TOKEN="…"
   ```
   Access Token 默认 90 天过期。把到期日填进 `config.toml` 的 `[health] longbridge_token_expires = 2027-02-20`，到期前 7 天群里会提醒。
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
| `/trend`、`/trend NVDA` | 全部标的的趋势状态；带代码看单个标的的三个周期打分和支撑压力位 |
| `/types`、`/types off 急拉急跌 [代码\|分组]`、`/types on ...` | 异动类型开关（V 反转、盘整突破、急拉急跌、持续推进），可按全部、标的或分组 |
| `/alert NVDA > 140`、`/alert BTC 5m -2%`、`/alert NVDA 当日 5%`、`/alert MU 5m $20M` | 自定义提醒；末尾可加 `once` / `daily` / `always` 和 `touch`（触价即推，默认收盘确认） |
| `/alert BTC 1h ema 20 50 金叉`、`/alert NVDA 15m rsi > 70`、`/alert BTC 1h macd 死叉`、`/alert ETH 15m boll 上` | 指标提醒（周期 1m / 5m / 15m / 1h，按该周期收盘计算；不写方向 = 两个方向都提醒） |
| `/alerts`、`/unalert 3`、`/unalert all` | 自定义提醒列表、删除 |
| `/stats` | 异动统计：最近 30 分钟和今天各市场、各分组的涨跌异动数和多空比（按级别加权） |
| `/help` | 命令说明 |

Discord 里用同名的斜杠命令（`/mute` 的标的和时长是两个参数）。告警下面的 👍/👎 记录这条有没有用，进日报，用来校准。命令也可以在 VPS 上用：`sudo mm status`、`sudo mm ctl mute MU 2h`、`sudo mm ctl unmute all`。

## 趋势监控

用 15 分钟、1 小时、4 小时 K 线综合判断标的是否形成上涨或下跌趋势，只在**形成**和**结束**两个时刻推送，形成消息附上最近的强支撑、强压力位。规则、回测和取舍见 [趋势监控方案文档](https://claude.ai/artifact/PwSkXNs6ptuce4cgeqtMC6)。

- **每个周期打分**（−3 到 +3）：波段结构（高点低点是否抬高）+ EMA20/50 排列 + ADX 方向强度。
- **形成**（以上涨为例）：1h 结构和均线都向上且总分 ≥ 2；4h 不反向；15m 总分 ≥ 2，并且连续 4 根 15m 都满足（站稳 1 小时）；失效位（1h 最近波段低点）离现价不超过 2.5 倍 4h ATR。
- **不推送但照样跟踪**：离均线太远、离失效位太远、在高潮量里。这样同一段行情不会在更差的位置再触发一次（回测里直接跳过反而更差）。
- **结束**：1h 收盘跌破失效位（失效位随新的 1h 波段低点上移），或 1h 转为空头且 4h 结构不再向上。结束消息回复在形成消息下面。
- **美股**只用正常盘（09:30–16:00 美东）K 线判定和推送，1h 从 09:30 起切，“4h”是每天上下午两根（提前收盘日 13:00 截止）。
- **支撑压力位**：4h、1h 波段高低点，前一天高低点，近 10 天成交密集区，按时间衰减加权、相近的合并成区间。★ 越多越强，推送里只列含 4h 拐点或三种来源重叠的强位。
- 回测（2026-03 到 11 月，真实 15m K 线，13 个标的）：248 条推送信号，按“趋势结束”离场平均 +0.59R（随机入场同样离场 +0.10R），先到 +1R 的比例 60.5%；加密 +1.04R，美股 +0.22R；前后两半段 +0.35R / +0.79R。平均每个标的每周约 0.5–1.3 条。
- 服务启动时自动补齐 130 天 15m 历史（加密从 Binance，美股从长桥），静默回放重建状态后才开始推送；重启期间已经结束的趋势会在原消息下补一句。
- 网页后台「趋势」页：每个标的三个周期的打分、当前趋势、最近强位，最近 14 天的事件，以及参数和开关。各渠道在「推送渠道」页单独勾选要不要收趋势消息。
- 回测：`sudo mm trend-backtest`（用库里的 15m 数据），`sudo mm trend-backtest BTCUSDT --fetch --days 240 --show`（先从 Binance 拉）。

## 对外 API 和桌面版（V4）

网页后台服务同时提供 `/api/v1`，给桌面版和其他客户端用。和后台一样经 Caddy 出 HTTPS，不用另开端口。

1. 生成密钥：网页后台「API 密钥」页，或 `sudo mm api-key create --name 我的Mac`。密钥形如 `tfk_…`，只显示一次；服务器只存哈希。
2. 桌面版里填服务器地址（`https://你的域名`）和密钥。桌面版的下载、功能和开发见 [desktop/README.md](desktop/README.md)。
3. 吊销：后台一键吊销，或 `sudo mm api-key revoke 编号`，已连着的客户端几秒内断开。

| 接口 | 说明 |
|---|---|
| `GET /api/v1/ping` | 检查地址、密钥、版本 |
| `GET /api/v1/status` | 和 `/status` 一样的运行状态 |
| `GET /api/v1/signals?after=序号&limit=100&symbol=NVDA` | 信号历史（首发、延伸、升级、结束），按序号翻页 |
| `GET /api/v1/symbols`、`/quotes` | 标的、分组、最新价、当日涨跌 |
| `GET /api/v1/bars?symbol=BTC&tf=15m&limit=300`（tf 为 1m 或 15m） | K 线 |
| `GET /api/v1/levels?symbol=NVDA`、`/trend` | 支撑压力区、趋势状态 |
| `GET /api/v1/stats` | 异动统计 |
| `GET/POST /api/v1/alerts`、`DELETE /api/v1/alerts/编号` | 自定义提醒（POST 的 `text` 和 /alert 写法一样） |
| `GET/POST /api/v1/types`、`POST /api/v1/mute`、`DELETE /api/v1/mute/代码`、`POST /api/v1/feedback` | 类型开关、静音、👍/👎 |
| `WS /api/v1/stream` | 连上后第一条发 `{"type":"auth","key":"tfk_…","after":序号}`；之后推 `signal`（实时信号）和每 5 秒一次 `quotes` |

- 请求头 `Authorization: Bearer tfk_…`。read 权限只能看；write 还能改提醒、类型开关、静音、反馈。
- 同一 IP 连续错 10 次封 15 分钟。允许跨域（只认 Bearer 密钥，不用 Cookie）。

### 关键位突破

15 分钟收盘越过强支撑 / 压力区（有 4h 拐点，或 ≥ 3 种来源）边缘 0.25 倍 ATR(1h)，下一根 15 分钟收盘仍在外面才推，同一区间 4 小时内不重复。走趋势消息的订阅。一条价格异动整个穿过强位时，只在异动卡片上加一行“突破强压力区 / 跌破强支撑区”，不再另发。关闭：`[trend] level_breaks = false`。

## 逐笔层（V4）

加密货币另开一条 Binance aggTrade 连接收逐笔成交，美股在长桥接通后复用同一个连接订逐笔。逐笔本身不落库，只在内存里聚合；库里只存每分钟摘要（`tick_1m`）、单笔成交额直方图（`tick_hist`）和单笔冲涨跌的影子记录（`tick_shadow`）。

- **大单**：单笔成交额 ≥ 该标的近 14 天（美股近 20 个同类时段）单笔成交额的 99 分位。直方图攒够 2000 笔才算门槛，所以刚升级时要等一会儿。
- **大单流向**：5 分钟大单净额（主动买大单 − 主动卖大单）≥ 常态 3 倍，并且和价格异动同向，卡片的成交额块里多一行 `5 分钟大单净买入 $1.20M｜常态的 3.4 倍`。常态要攒 6 小时的样本才就绪。它只是证据，不触发、不升级。
- **单笔冲涨跌（影子模式）**：一笔成交相对上一笔跳价 ≥ max(1%, 2 × 该时段 1 分钟地板)，这一笔成交额 ≥ 单笔中位数 5 倍；之后 20 秒内 ≥ 3 笔、成交均价保留 ≥ 60% 算市场接受，再用实时价看 1 分钟位移能不能过入门门。**现在只记录不推送**，先跑一周看报告：

  ```bash
  sudo mm tick-report --days 7          # 跳价几次、过入门门几次、比 K 线推送早多少秒、多少次后来没有推送
  sudo mm tick-report --days 1 --show   # 列出每一次
  ```

  日报里也有一行汇总。美股盘前、盘后、夜盘不做（这些时段的坏价正是单笔跳价）。
- `sudo mm status` 和 Telegram 的 `/status` 里有一行逐笔状态：每秒笔数、逐笔解析占用的 CPU、哪些标的的大单门槛已就绪。
- 不想要逐笔层：`config.toml` 里 `[ticks] enabled = false`，然后重启。

## 调参和回放

- 最方便的是在网页后台「规则」页改：保存即生效，旧版本自动存档。
- 也可以直接改 `config.toml`，然后 `sudo mm check-config`，再 `sudo systemctl reload market-monitor` 热加载规则参数；标的、推送渠道、市场开关的变化要 `restart`。
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
systemctl status market-monitor-web        # 网页后台
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
pip install -e ".[test,longbridge,web]" ruff
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
  store.py       SQLite（K 线、判定、事件、发件箱（每个渠道一行）、静音、反馈）
  outbox.py      发件箱：每个渠道一个发送循环，重试、编辑、回复、各种错误处理
  notifiers/     推送渠道插件：telegram.py、discord.py（REST + Gateway 收按钮和斜杠命令）
  bot.py         Telegram 命令和反馈按钮（长轮询）；命令逻辑 Discord 共用
  control.py     本机控制通道（unix socket），网页后台用它让监控重载、静音、发测试消息
  tradingview.py TradingView webhook 解析、限流、排版
  health.py      断流、积压、卡顿、对时、心跳、看门狗
  reporting.py   日报
  replay.py      回放
  patterns.py    V4 形态分类（V 反转、盘整突破、急拉急跌、持续推进）和放量标签
  custom.py      V4 自定义提醒
  ticks.py       V4 逐笔层：分钟摘要、大单门槛、大单流向、单笔冲涨跌（影子）
  tick_report.py 单笔冲涨跌影子报告
  stats.py       异动统计（按级别加权的多空比）
  apikeys.py     对外 API 密钥
  api_backend.py 监控进程这一侧的 /api/v1 数据（经控制通道给网页后台）
  trend/         趋势监控：series.py 单周期打分、machine.py 状态机、levels.py 支撑压力位、
                 buckets.py 15m/1h/4h 分桶（美股正常盘）、engine.py 接入运行时、render.py 文案、backtest.py
  feeds/         binance_spot.py（K 线）、binance_trades.py（逐笔）、longbridge.py
  app.py         运行时：各个循环、热加载、优雅退出
  cli.py         monitor 命令
  web/           网页管理后台（FastAPI + Jinja2 服务端渲染，无前端构建步骤）；api.py 是 /api/v1
desktop/         桌面版（Tauri 2 + Svelte，Mac 和 Windows），见 desktop/README.md
deploy/          systemd 服务、install.sh、upgrade.sh、setup-web.sh、Caddy 示例、mm、配置和密钥示例
```
