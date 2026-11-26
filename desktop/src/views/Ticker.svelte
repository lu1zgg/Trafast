<script lang="ts">
  // 浮动行情条：单独的小窗口，没有边框，可以拖动；每 5 秒拉一次 /quotes。
  import { onDestroy, onMount } from "svelte";
  import { ApiClient } from "../lib/api";
  import { SETTINGS_KEY, loadSettings, type Settings } from "../lib/config";
  import { displayName, fmtPct, fmtPrice } from "../lib/format";
  import { getKey, inTauri } from "../lib/secrets";
  import type { Quote } from "../lib/types";

  let settings = $state<Settings>(loadSettings());
  let quotes = $state<Quote[]>([]);
  let err = $state("");
  let client: ApiClient | null = null;
  let timer: ReturnType<typeof setInterval> | undefined;
  let prev: Record<string, number> = {};
  let flash = $state<Record<string, number>>({});

  const shown = $derived(
    settings.ticker.symbols.length ? quotes.filter((q) => settings.ticker.symbols.includes(q.symbol)) : quotes,
  );

  async function setup() {
    client = null;
    err = "";
    const key = settings.server ? await getKey(settings.server) : null;
    if (!settings.server || !key) {
      err = "先在主窗口「设置」里连接服务器";
      return;
    }
    client = new ApiClient(settings.server, key);
    await tick();
  }

  async function tick() {
    if (!client) return;
    try {
      const r = await client.quotes();
      const f: Record<string, number> = {};
      for (const q of r.items) {
        const p = prev[q.symbol];
        if (q.price !== null && p !== undefined && q.price !== p) f[q.symbol] = q.price > p ? 1 : -1;
        if (q.price !== null) prev[q.symbol] = q.price;
      }
      flash = f;
      quotes = r.items;
      err = "";
    } catch (e) {
      err = (e as Error).message;
    }
  }

  async function applyOnTop() {
    if (!inTauri()) return;
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().setAlwaysOnTop(settings.ticker.onTop);
  }

  function onStorage(e: StorageEvent) {
    if (e.key !== SETTINGS_KEY) return;
    const before = settings.server;
    settings = loadSettings();
    void applyOnTop();
    if (settings.server !== before) void setup();
  }

  async function hide() {
    if (!inTauri()) return;
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().hide();
  }

  onMount(() => {
    document.documentElement.classList.add("ticker-mode");
    window.addEventListener("storage", onStorage);
    void setup();
    void applyOnTop();
    timer = setInterval(tick, 5000);
  });

  onDestroy(() => {
    clearInterval(timer);
    window.removeEventListener("storage", onStorage);
  });
</script>

<div class="ticker" data-tauri-drag-region>
  {#if err}
    <span class="muted" data-tauri-drag-region>{err}</span>
  {:else}
    {#each shown as q (q.symbol)}
      <span class="item" data-tauri-drag-region>
        <b data-tauri-drag-region>{displayName(q.symbol)}</b>
        <span class="mono" class:fu={flash[q.symbol] === 1} class:fd={flash[q.symbol] === -1} data-tauri-drag-region>{fmtPrice(q.price)}</span>
        <span class="mono" class:up={(q.day_chg ?? 0) > 0} class:down={(q.day_chg ?? 0) < 0} data-tauri-drag-region>{fmtPct(q.day_chg, 2, true)}</span>
      </span>
    {/each}
  {/if}
  <button class="x" onclick={hide} title="隐藏行情条">×</button>
</div>

<style>
  :global(html.ticker-mode),
  :global(html.ticker-mode body) {
    background: var(--panel);
    overflow: hidden;
  }
  .ticker {
    display: flex;
    align-items: center;
    gap: 18px;
    height: 100vh;
    padding: 0 30px 0 12px;
    white-space: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    user-select: none;
    cursor: move;
    font-size: 13px;
  }
  .item {
    display: inline-flex;
    gap: 6px;
  }
  .fu {
    animation: fu 1s;
  }
  .fd {
    animation: fd 1s;
  }
  @keyframes fu {
    from {
      background: color-mix(in srgb, var(--up) 45%, transparent);
    }
  }
  @keyframes fd {
    from {
      background: color-mix(in srgb, var(--down) 45%, transparent);
    }
  }
  .x {
    position: fixed;
    right: 4px;
    top: 50%;
    transform: translateY(-50%);
    border: none;
    background: transparent;
    color: var(--muted);
    padding: 0 6px;
    font-size: 16px;
  }
</style>
