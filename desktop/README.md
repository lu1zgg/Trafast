# Trafast 桌面版（Mac + Windows）

连**你自己部署的** Trafast 服务器，在电脑上看实时异动、弹系统通知。没有内置服务器地址：第一次打开时填自己的地址和 API 密钥。

## 功能

- **信号**：最近 36 小时的异动（首发、升级、延伸、结束），按市场、涨跌、代码筛选；新信号经 WebSocket 实时推过来，断线自动重连、按序号补齐。读写密钥可以点 👍 / 👎。
- **系统通知**：macOS 通知中心 / Windows 操作中心。可以选最低级别（L1 / L2 / L3）、升级要不要通知、声音。
- **浮动行情条**：一个细长的置顶小窗，显示选定标的的现价和当日涨跌，可以拖到屏幕任意位置。
- **标的详情**：1 分钟 / 15 分钟 K 线，画出支撑压力区（实线是强位）和信号箭头；15m / 1h / 4h 趋势状态。
- **统计**：今天和最近 30 分钟按市场、分组、标的、类型的涨跌异动数和多空占比（按级别加权）。
- **提醒**：自定义提醒（写法和 `/alert` 一样，含指标提醒）、异动类型开关。只读密钥只能看。
- **托盘**：关掉主窗口后留在菜单栏 / 系统托盘，照样收通知；托盘菜单里打开主窗口、开关行情条、退出。

## 安装

1. 服务器先升级到 V4（有 `/api/v1`），在网页后台「API 密钥」页生成一个**读写**密钥（或 `sudo mm api-key create --name 我的Mac`）。
2. 下载安装包：GitHub 仓库的 Actions → `desktop` → 最新一次成功的运行 → Artifacts：
   - `trafast-desktop-macos-universal`：`.dmg`（Intel 和 Apple 芯片通用，macOS 11 及以上）
   - `trafast-desktop-windows-x64`：`.exe`（安装程序）或 `.msi`
   推 `desktop-v0.4.0` 这样的标签时还会建一个草稿 Release，附上同样的安装包。
3. 安装包**没有签名**，第一次打开会被系统拦一下：
   - macOS：在「访达」里右键 Trafast → 打开 → 打开。还不行就在终端执行 `xattr -cr /Applications/Trafast.app`。
   - Windows：SmartScreen 提示时点「更多信息」→「仍要运行」。
4. 打开后在「设置」里填服务器地址（`https://你的域名`）和密钥，点「保存并连接」。

## 安全

- 只能用 HTTPS 连接（地址是 `127.0.0.1` / `localhost` 时除外）。
- 密钥存在系统钥匙串里（macOS 钥匙串 / Windows 凭据管理器，服务名 `Trafast`），不写进普通文件。
- REST 用 `Authorization: Bearer` 头；WebSocket 连上后第一条消息才发密钥，不放进 URL。
- 服务器上吊销密钥后，桌面版几秒内断开并提示换密钥。

## 开发

需要 Node 22、Rust（stable）和 Tauri 的系统依赖（Linux 上是 `libwebkit2gtk-4.1-dev` 等）。

```bash
cd desktop
npm ci
npm run tauri dev          # 开发模式（界面热更新）
npm run check              # 类型检查
npm test                   # 单元测试（地址校验、API 客户端、WebSocket 重连、通知过滤）
npm run tauri build        # 打当前系统的安装包
```

只调界面时也可以 `npm run dev` 后用浏览器打开 http://localhost:1420 ：没有 Tauri 时密钥存在 sessionStorage，通知用浏览器通知。

```
src/
  App.svelte          主窗口框架（导航、连接状态）；地址带 #ticker 时渲染行情条
  lib/config.ts       设置、服务器地址校验（只允许 HTTPS）
  lib/secrets.ts      密钥读写（Tauri 命令 → 系统钥匙串）
  lib/api.ts          /api/v1 客户端和 WebSocket 实时流（重连、续传、吊销处理）
  lib/state.svelte.ts 全局状态、新信号弹通知
  lib/format.ts       叫法和格式（和服务器推送卡片一致）
  views/              信号、标的、详情（K 线）、统计、提醒、设置、行情条
src-tauri/
  src/lib.rs          钥匙串命令、托盘、关窗口改为隐藏、单实例
  tauri.conf.json     窗口（主窗口 + 行情条）、CSP、打包
```

以后（D3）：本机自带引擎（sidecar，不用服务器也能跑）、签名和自动更新。
