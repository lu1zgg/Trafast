<script lang="ts">
  import { onMount } from "svelte";
  import { app } from "./lib/state.svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { inTauri } from "./lib/secrets";
  import Ticker from "./views/Ticker.svelte";
  import Feed from "./views/Feed.svelte";
  import Symbols from "./views/Symbols.svelte";
  import Detail from "./views/Detail.svelte";
  import Stats from "./views/Stats.svelte";
  import Alerts from "./views/Alerts.svelte";
  import SettingsView from "./views/Settings.svelte";

  const isTicker = location.hash === "#ticker";

  const tabs = [
    ["feed", "信号"],
    ["symbols", "标的"],
    ["stats", "统计"],
    ["alerts", "提醒"],
    ["settings", "设置"],
  ] as const;

  const connText: Record<string, string> = {
    idle: "未连接",
    connecting: "连接中",
    live: "实时",
    retrying: "重连中",
    stopped: "已断开",
    error: "连接失败",
  };

  async function toggleTicker() {
    if (!inTauri()) return;
    await invoke("toggle_ticker");
  }

  onMount(() => {
    if (isTicker) return;
    void app.connect();
  });
</script>

{#if isTicker}
  <Ticker />
{:else}
  <div class="shell">
    <nav>
      <div class="brand">Trafast</div>
      {#each tabs as [id, label]}
        <button class:active={app.view === id || (id === "symbols" && app.view === "detail")} onclick={() => (app.view = id)}>{label}</button>
      {/each}
      <div class="grow"></div>
      {#if inTauri()}
        <button class="small" onclick={toggleTicker} title="显示或隐藏浮动行情条">行情条</button>
      {/if}
    </nav>

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

    <footer>
      <span class="dot {app.conn}"></span>
      <span>{connText[app.conn] ?? app.conn}</span>
      {#if app.connDetail && app.conn === "retrying"}<span class="muted">（{app.connDetail}）</span>{/if}
      {#if app.client}<span class="muted">· {app.client.server}</span>{/if}
      <div class="grow"></div>
      {#if app.ping}
        <span class="muted">密钥 {app.ping.key.name}（{app.ping.key.scope === "write" ? "读写" : "只读"}）· 服务器 v{app.ping.version} · 规则 {app.ping.rule_version ?? "—"}</span>
      {/if}
    </footer>
  </div>
{/if}

<style>
  .shell {
    display: flex;
    flex-direction: column;
    height: 100%;
  }
  nav {
    display: flex;
    gap: 4px;
    align-items: center;
    padding: 8px 12px;
    border-bottom: 1px solid var(--line);
    background: var(--panel);
  }
  nav button {
    border: none;
    background: transparent;
    color: var(--muted);
  }
  nav button.active {
    color: var(--text);
    background: var(--panel-2);
  }
  .brand {
    font-weight: 700;
    margin-right: 12px;
  }
  .grow {
    flex: 1;
  }
  main {
    flex: 1;
    overflow: auto;
    padding: 14px 16px;
    min-height: 0;
  }
  .banner {
    padding: 8px 16px;
    border-bottom: 1px solid var(--line);
  }
  .banner.warn {
    color: var(--warn);
  }
  footer {
    display: flex;
    gap: 6px;
    align-items: center;
    padding: 5px 12px;
    font-size: 12px;
    border-top: 1px solid var(--line);
    background: var(--panel);
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--muted);
  }
  .dot.live {
    background: var(--up);
  }
  .dot.retrying,
  .dot.connecting {
    background: var(--warn);
  }
  .dot.error,
  .dot.stopped {
    background: var(--down);
  }
</style>
