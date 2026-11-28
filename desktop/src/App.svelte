<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { app } from "./lib/state.svelte";
  import { TICKER_SPEED } from "./lib/config";
  import { recentNews } from "./lib/format";
  import { inTauri } from "./lib/secrets";
  import Ticker from "./views/Ticker.svelte";
  import TickerCrawl from "./views/TickerCrawl.svelte";
  import Icon from "./views/Icon.svelte";
  import logo from "./assets/logo.svg";
  import Feed from "./views/Feed.svelte";
  import Symbols from "./views/Symbols.svelte";
  import Detail from "./views/Detail.svelte";
  import Stats from "./views/Stats.svelte";
  import Alerts from "./views/Alerts.svelte";
  import SettingsView from "./views/Settings.svelte";

  const isTicker = location.hash === "#ticker";

  const tabs = [
    ["feed", "信号", "feed"],
    ["symbols", "标的", "symbols"],
    ["stats", "统计", "stats"],
    ["alerts", "提醒", "alerts"],
    ["settings", "设置", "settings"],
  ] as const;

  const titles: Record<string, string> = { feed: "信号", symbols: "标的", detail: "标的", stats: "统计", alerts: "提醒", settings: "设置" };

  const connText: Record<string, string> = {
    idle: "未连接",
    connecting: "连接中",
    live: "实时",
    retrying: "重连中",
    stopped: "已断开",
    error: "连接失败",
  };

  let nowSec = $state(Math.floor(Date.now() / 1000));
  let clock: ReturnType<typeof setInterval> | undefined;

  // 行情条：设置里勾了标的就只显示这些，否则显示全部（按服务器的标的顺序）
  const tickerQuotes = $derived.by(() => {
    const pick = app.settings.ticker.symbols;
    const order = app.symbols.length ? app.symbols.map((s) => s.symbol) : Object.keys(app.quotes);
    return order
      .filter((s) => !pick.length || pick.includes(s))
      .map((s) => app.quotes[s])
      .filter((q) => q && q.price !== null);
  });
  const news = $derived(app.settings.ticker.news ? recentNews(app.signals, nowSec) : []);
  const tickerMsg = $derived(!app.client ? "未连接服务器" : "");

  const host = $derived(app.client ? app.client.server.replace(/^https?:\/\//, "") : "");

  let unlisten: (() => void) | undefined;

  onMount(() => {
    if (isTicker) return;
    clock = setInterval(() => (nowSec = Math.floor(Date.now() / 1000)), 15_000);
    void app.connect();
    // 屏幕顶部行情条上点了某个标的
    if (inTauri()) {
      void import("@tauri-apps/api/event").then(async ({ listen }) => {
        unlisten = await listen<string>("open-symbol", (e) => app.open(e.payload));
      });
    }
  });
  onDestroy(() => {
    clearInterval(clock);
    unlisten?.();
  });
</script>

{#if isTicker}
  <Ticker />
{:else}
  <div class="shell">
    <aside class="sidebar glass">
      <div class="lights" data-tauri-drag-region></div>
      <div class="brand" data-tauri-drag-region>
        <img class="logo" src={logo} alt="" />
        <span>Trafast</span>
      </div>
      <nav>
        {#each tabs as [id, label, icon]}
          <button class:active={app.view === id || (id === "symbols" && app.view === "detail")} onclick={() => (app.view = id)}>
            <Icon name={icon} />
            <span>{label}</span>
            {#if id === "feed" && app.signals.length}<span class="count">{app.signals.filter((s) => s.kind === "open" || s.kind === "upgrade").length}</span>{/if}
          </button>
        {/each}
      </nav>
      <div class="grow" data-tauri-drag-region></div>
      <div class="conn" title={app.ping ? `密钥 ${app.ping.key.name} · 服务器 v${app.ping.version} · 规则 ${app.ping.rule_version ?? "—"}` : ""}>
        <div class="conn-line">
          <span class="dot {app.conn}"></span>
          <b>{connText[app.conn] ?? app.conn}</b>
          {#if app.connDetail && app.conn === "retrying"}<span class="muted small">{app.connDetail}</span>{/if}
        </div>
        {#if host}<div class="muted small ellipsis">{host}</div>{/if}
        {#if app.ping}
          <div class="muted small ellipsis">{app.ping.key.scope === "write" ? "读写" : "只读"}密钥 · v{app.ping.version} · 规则 {app.ping.rule_version ?? "—"}</div>
        {/if}
      </div>
    </aside>

    <section class="content">
      <header class="toolbar" data-tauri-drag-region>
        <h1 data-tauri-drag-region>{titles[app.view]}</h1>
        <div class="ticker-wrap glass">
          <TickerCrawl quotes={tickerQuotes} {news} speed={TICKER_SPEED[app.settings.ticker.speed] ?? 52} message={tickerMsg} onpick={(s) => app.open(s)} />
        </div>
        <div class="side" data-tauri-drag-region></div>
      </header>

      {#if app.fatal}
        <div class="banner err">{app.fatal}。请到「设置」里换一个密钥。</div>
      {:else if app.conn === "error"}
        <div class="banner err">{app.connDetail} <button class="small" onclick={() => app.connect()}>重试</button></div>
      {:else if app.client && !app.monitorRunning}
        <div class="banner warn">服务器上的监控进程没有运行，暂时收不到新信号。</div>
      {/if}

      <main>
        {#if app.view === "feed"}<Feed />
        {:else if app.view === "symbols"}<Symbols />
        {:else if app.view === "detail"}<Detail />
        {:else if app.view === "stats"}<Stats />
        {:else if app.view === "alerts"}<Alerts />
        {:else}<SettingsView />
        {/if}
      </main>
    </section>
  </div>
{/if}

<style>
  .shell {
    display: flex;
    height: 100%;
    padding: 8px;
    gap: 8px;
  }
  .sidebar {
    width: 184px;
    flex: none;
    display: flex;
    flex-direction: column;
    border-radius: var(--radius-lg);
    padding: 0 10px 10px;
    user-select: none;
  }
  /* Mac 上红绿灯按钮放在侧栏顶部；其他系统用自带标题栏，这里只留一点空 */
  .lights {
    height: 14px;
    flex: none;
  }
  :global(html[data-platform="mac"]) .lights {
    height: 42px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 700;
    font-size: 14px;
    letter-spacing: -0.01em;
    padding: 0 6px 12px;
  }
  .logo {
    width: 24px;
    height: 24px;
    border-radius: 7px;
    box-shadow: 0 2px 6px rgba(11, 24, 52, 0.35), 0 0 0 0.5px rgba(255, 255, 255, 0.15) inset;
  }
  nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  nav button {
    display: flex;
    align-items: center;
    gap: 9px;
    width: 100%;
    background: transparent;
    border-radius: 9px;
    padding: 6px 9px;
    font-weight: 500;
    color: var(--text-2);
    text-align: left;
  }
  nav button :global(svg) {
    color: var(--accent);
    flex: none;
  }
  nav button:hover:not(.active) {
    background: var(--hover);
  }
  nav button.active {
    background: color-mix(in srgb, var(--accent) 16%, transparent);
    color: var(--text);
    font-weight: 600;
  }
  nav button:active {
    transform: none;
  }
  .count {
    margin-left: auto;
    font-size: 11px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    font-weight: 500;
  }
  .conn {
    padding: 10px;
    border-radius: var(--radius);
    background: var(--fill);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .conn-line {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .ellipsis {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--muted);
    flex: none;
  }
  .dot.live {
    background: var(--up);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--up) 22%, transparent);
  }
  .dot.retrying,
  .dot.connecting {
    background: var(--warn);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 22%, transparent);
  }
  .dot.error,
  .dot.stopped {
    background: var(--down);
  }

  .content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .toolbar {
    height: 46px;
    flex: none;
    display: grid;
    grid-template-columns: minmax(70px, 1fr) minmax(0, 760px) minmax(0, 1fr);
    align-items: center;
    gap: 12px;
    padding: 0 6px 0 8px;
    user-select: none;
  }
  h1 {
    margin: 0;
    font-size: 17px;
    font-weight: 700;
    letter-spacing: -0.015em;
    white-space: nowrap;
  }
  .ticker-wrap {
    height: 32px;
    border-radius: 999px;
    overflow: hidden;
  }
  .banner {
    margin: 4px 8px 6px;
    padding: 8px 14px;
    border-radius: var(--radius);
    background: color-mix(in srgb, currentColor 10%, transparent);
  }
  .banner.warn {
    color: var(--warn);
  }
  main {
    flex: 1;
    overflow: auto;
    padding: 6px 8px 8px;
    min-height: 0;
  }
</style>
