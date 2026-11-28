<script lang="ts">
  // 滚动行情条：像电视财经节目底部的跑马灯一样匀速向左滚动，首尾相接不断开。
  // 内容 = 最近的异动快讯 + 各标的现价和当日涨跌。鼠标放上去暂停，点一下打开该标的。
  import { onDestroy, onMount } from "svelte";
  import { displayName, fmtPct, fmtPrice, movePct, patternName } from "../lib/format";
  import type { Quote, Signal } from "../lib/types";

  let {
    quotes,
    news = [],
    speed = 50,
    message = "",
    onpick,
  }: {
    quotes: Quote[];
    news?: Signal[];
    speed?: number; // 像素 / 秒
    message?: string; // 有内容时不滚动，只显示这句话
    onpick?: (symbol: string) => void;
  } = $props();

  let viewW = $state(0);
  let copyW = $state(0);
  let now = $state(Date.now());
  let clock: ReturnType<typeof setInterval> | undefined;

  // 一份内容不够铺满时多复制几份，保证滚动时右边不会空出来
  const reps = $derived(copyW > 0 ? Math.max(1, Math.ceil(viewW / copyW)) + 1 : 2);
  const dur = $derived(copyW > 0 ? Math.max(8, copyW / Math.max(10, speed)) : 60);

  let prev: Record<string, number> = {};
  let dirs = $state<Record<string, number>>({});
  $effect(() => {
    const d: Record<string, number> = {};
    for (const q of quotes) {
      const p = prev[q.symbol];
      if (q.price !== null && p !== undefined && q.price !== p) d[q.symbol] = q.price > p ? 1 : -1;
      if (q.price !== null) prev[q.symbol] = q.price;
    }
    dirs = d;
  });

  const hhmm = $derived(new Date(now).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }));

  onMount(() => {
    clock = setInterval(() => (now = Date.now()), 1000);
  });
  onDestroy(() => clearInterval(clock));
</script>

{#snippet content()}
  {#each news as s (s.seq)}
    <button class="it news" class:up={s.direction > 0} class:down={s.direction < 0} onclick={() => onpick?.(s.symbol)}>
      <span class="flag">快讯</span>
      <b>{displayName(s.symbol)}</b>
      <span>{patternName(s)}</span>
      <span class="mono">{fmtPct(movePct(s))}</span>
      <span class="lv {s.level}">{s.level}</span>
    </button>
  {/each}
  {#each quotes as q (q.symbol)}
    {@const chg = q.day_chg ?? 0}
    <button class="it" onclick={() => onpick?.(q.symbol)}>
      <b>{displayName(q.symbol)}</b>
      {#key q.price}
        <span class="mono px" class:fu={dirs[q.symbol] === 1} class:fd={dirs[q.symbol] === -1}>{fmtPrice(q.price)}</span>
      {/key}
      <span class="mono chg" class:up={chg > 0} class:down={chg < 0}>{chg > 0 ? "▲" : chg < 0 ? "▼" : ""}{fmtPct(q.day_chg, 2, true)}</span>
    </button>
  {/each}
{/snippet}

<div class="ticker">
  <span class="live"><i></i>实时</span>
  <div class="crawl" bind:clientWidth={viewW}>
    {#if message}
      <span class="msg">{message}</span>
    {:else if quotes.length + news.length === 0}
      <span class="msg">等待行情…</span>
    {:else}
      <div class="track" style="--shift: {copyW}px; animation-duration: {dur}s">
        <div class="copy" bind:clientWidth={copyW}>{@render content()}</div>
        {#each Array(reps - 1) as _, i (i)}
          <div class="copy" aria-hidden="true">{@render content()}</div>
        {/each}
      </div>
    {/if}
  </div>
  <span class="clock mono">{hhmm}</span>
</div>

<style>
  .ticker {
    display: flex;
    align-items: center;
    height: 100%;
    min-width: 0;
    gap: 0;
  }
  .live {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 20px;
    padding: 0 8px 0 7px;
    margin: 0 6px 0 6px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    color: #fff;
    background: linear-gradient(180deg, #ff5146, #e0281e);
    box-shadow: 0 1px 4px rgba(224, 40, 30, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.3);
    letter-spacing: 0.04em;
  }
  .live i {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #fff;
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    50% {
      opacity: 0.25;
    }
  }
  .crawl {
    flex: 1;
    min-width: 0;
    height: 100%;
    overflow: hidden;
    display: flex;
    align-items: center;
    -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 18px, #000 calc(100% - 28px), transparent 100%);
    mask-image: linear-gradient(90deg, transparent 0, #000 18px, #000 calc(100% - 28px), transparent 100%);
  }
  .track {
    display: flex;
    width: max-content;
    animation: crawl linear infinite;
    will-change: transform;
  }
  .crawl:hover .track {
    animation-play-state: paused;
  }
  @keyframes crawl {
    to {
      transform: translateX(calc(-1 * var(--shift)));
    }
  }
  .copy {
    display: flex;
    align-items: center;
    flex: none;
  }
  .it {
    all: unset;
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    padding: 2px 14px 2px 12px;
    white-space: nowrap;
    font-size: 12.5px;
    border-radius: 999px;
    position: relative;
    cursor: default;
  }
  .it::after {
    content: "";
    position: absolute;
    right: 0;
    top: 50%;
    width: 3px;
    height: 3px;
    margin-top: -1.5px;
    border-radius: 50%;
    background: var(--muted);
    opacity: 0.6;
  }
  .it:hover {
    background: var(--fill);
  }
  .it b {
    font-weight: 600;
    color: var(--text);
  }
  .px {
    color: var(--text-2);
    border-radius: 4px;
    padding: 0 2px;
  }
  .chg {
    font-size: 12px;
  }
  .news .flag {
    font-size: 10.5px;
    font-weight: 600;
    padding: 0 5px;
    border-radius: 4px;
    color: #fff;
    background: var(--accent);
    align-self: center;
    line-height: 16px;
  }
  .news.up .flag {
    background: var(--up);
  }
  .news.down .flag {
    background: var(--down);
  }
  .lv {
    font-size: 10.5px;
    font-weight: 600;
    opacity: 0.85;
  }
  .lv.L3 {
    color: var(--l3);
  }
  .lv.L2 {
    color: var(--l2);
  }
  .lv.L1 {
    color: var(--l1);
  }
  .fu {
    animation: fu 1.2s ease-out;
  }
  .fd {
    animation: fd 1.2s ease-out;
  }
  @keyframes fu {
    from {
      background: color-mix(in srgb, var(--up) 40%, transparent);
      color: var(--up);
    }
  }
  @keyframes fd {
    from {
      background: color-mix(in srgb, var(--down) 40%, transparent);
      color: var(--down);
    }
  }
  .msg {
    color: var(--muted);
    font-size: 12px;
    padding-left: 10px;
  }
  .clock {
    flex: none;
    font-size: 11.5px;
    color: var(--muted);
    padding: 0 12px 0 6px;
  }
  @media (prefers-reduced-motion: reduce) {
    .track {
      animation-duration: 0s !important;
    }
  }
</style>
