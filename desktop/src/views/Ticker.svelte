<script lang="ts">
  // 屏幕顶部的行情条：单独的小窗，固定在当前屏幕顶部正中（位置由 Rust 端在显示时摆好），
  // 内容和主窗口工具栏里的行情条一样匀速滚动。每 5 秒拉一次 /quotes，每 30 秒拉一次最近信号做快讯。
  import { onDestroy, onMount } from "svelte";
  import { ApiClient } from "../lib/api";
  import { SETTINGS_KEY, TICKER_SPEED, loadSettings, type Settings } from "../lib/config";
  import { recentNews } from "../lib/format";
  import { getKey, inTauri } from "../lib/secrets";
  import type { Quote, Signal } from "../lib/types";
  import TickerCrawl from "./TickerCrawl.svelte";

  let settings = $state<Settings>(loadSettings());
  let quotes = $state<Quote[]>([]);
  let signals = $state<Signal[]>([]);
  let err = $state("");
  let client: ApiClient | null = null;
  let timer: ReturnType<typeof setInterval> | undefined;
  let n = 0;

  const shown = $derived(
    (settings.ticker.symbols.length ? quotes.filter((q) => settings.ticker.symbols.includes(q.symbol)) : quotes).filter((q) => q.price !== null),
  );
  const news = $derived(settings.ticker.news ? recentNews(signals, Math.floor(Date.now() / 1000)) : []);

  async function setup() {
    client = null;
    err = "";
    const key = settings.server ? await getKey(settings.server) : null;
    if (!settings.server || !key) {
      err = "先在主窗口「设置」里连接服务器";
      return;
    }
    client = new ApiClient(settings.server, key);
    n = 0;
    await tick();
  }

  async function tick() {
    if (!client) return;
    try {
      const wantNews = settings.ticker.news && n++ % 6 === 0;
      const [r, s] = await Promise.all([
        client.quotes(),
        wantNews ? client.signals({ since: Math.floor(Date.now() / 1000) - 3600, limit: 200 }) : Promise.resolve(null),
      ]);
      quotes = r.items;
      if (s) signals = s.items;
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

  async function openMain(symbol: string) {
    if (!inTauri()) return;
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("show_main_symbol", { symbol });
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

<div class="bar">
  <TickerCrawl quotes={shown} {news} speed={TICKER_SPEED[settings.ticker.speed] ?? 52} message={err} onpick={openMain} />
  <button class="x" onclick={hide} title="隐藏行情条" aria-label="隐藏行情条">
    <svg width="10" height="10" viewBox="0 0 10 10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 2l6 6M8 2 2 8" /></svg>
  </button>
</div>

<style>
  :global(html.ticker-mode),
  :global(html.ticker-mode body) {
    background: transparent;
    overflow: hidden;
  }
  /* Mac 上窗口本身是系统玻璃（圆角由 windowEffects.radius 给）；其他系统用半透明底色 */
  .bar {
    display: flex;
    align-items: center;
    height: 100vh;
    border-radius: 999px;
    background: var(--glass-solid);
    border: 0.5px solid var(--glass-rim);
    box-shadow: inset 0 1px 0 var(--glass-edge);
    overflow: hidden;
    user-select: none;
  }
  :global(html.vibrant) .bar {
    background: transparent;
    border: none;
    box-shadow: none;
  }
  .bar > :global(.ticker) {
    flex: 1;
    min-width: 0;
  }
  .x {
    flex: none;
    width: 20px;
    height: 20px;
    padding: 0;
    margin-right: 7px;
    display: grid;
    place-items: center;
    background: var(--fill);
    color: var(--muted);
    opacity: 0;
    transition: opacity 0.15s;
  }
  .bar:hover .x {
    opacity: 1;
  }
</style>
