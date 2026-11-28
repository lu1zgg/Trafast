<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { app } from "../lib/state.svelte";
  import { MARKET_LABELS, PATTERN_LABELS, displayName, fmtClock } from "../lib/format";
  import type { Bucket, Stats, Tally } from "../lib/types";

  let st = $state<Stats | null>(null);
  let err = $state("");
  let win = $state<"recent" | "today">("today");
  let timer: ReturnType<typeof setInterval> | undefined;

  async function load() {
    if (!app.client) return;
    try {
      st = await app.client.stats();
      err = "";
    } catch (e) {
      err = (e as Error).message;
    }
  }

  onMount(() => {
    void load();
    timer = setInterval(load, 60_000);
  });
  onDestroy(() => clearInterval(timer));

  function top(rec: Record<string, Bucket>, n: number): [string, Bucket][] {
    return Object.entries(rec)
      .sort((a, b) => b[1].up + b[1].down - (a[1].up + a[1].down))
      .slice(0, n);
  }

  const t = $derived<Tally | null>(st ? st[win] : null);
</script>

{#snippet line(name: string, b: Bucket)}
  <tr>
    <td>{name}</td>
    <td class="num mono up">{b.up_n}（{b.up}）</td>
    <td class="num mono down">{b.down_n}（{b.down}）</td>
    <td class="barcell">
      {#if b.bull !== null}
        <div class="bar"><div class="bull" style="width: {b.bull * 100}%"></div></div>
      {/if}
    </td>
    <td class="num mono">{b.bull === null ? "—" : `${Math.round(b.bull * 100)}%`}</td>
  </tr>
{/snippet}

<div class="toolbar-row">
  <div class="seg" role="group" aria-label="时间范围">
    <button class:sel={win === "recent"} onclick={() => (win = "recent")}>最近 {st?.recent_min ?? 30} 分钟</button>
    <button class:sel={win === "today"} onclick={() => (win = "today")}>今天</button>
  </div>
  {#if st}<span class="muted small">更新于 {fmtClock(st.t)} · 括号里按级别加权（L1 = 1，L2 = 2，L3 = 3）· 每段行情只算一次</span>{/if}
  <div class="grow"></div>
  {#if err}<span class="err small">{err}</span>{/if}
  <button class="small" onclick={load}>刷新</button>
</div>

{#if t}
  {#if t.total.up_n + t.total.down_n === 0}
    <div class="sheet empty">这段时间没有异动。</div>
  {:else}
    <div class="cols">
      <div class="card">
        <h2>市场</h2>
        <table>
          <thead><tr><th></th><th class="num">涨</th><th class="num">跌</th><th>多空</th><th class="num">多头占比</th></tr></thead>
          <tbody>
            {@render line("合计", t.total)}
            {#each Object.entries(t.markets) as [m, b]}{@render line(MARKET_LABELS[m] ?? m, b)}{/each}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>分组</h2>
        <table>
          <thead><tr><th></th><th class="num">涨</th><th class="num">跌</th><th>多空</th><th class="num">多头占比</th></tr></thead>
          <tbody>{#each top(t.groups, 12) as [g, b]}{@render line(g, b)}{/each}</tbody>
        </table>
      </div>
      <div class="card">
        <h2>异动最多的标的</h2>
        <table>
          <thead><tr><th></th><th class="num">涨</th><th class="num">跌</th><th>多空</th><th class="num">多头占比</th></tr></thead>
          <tbody>{#each top(t.symbols, 12) as [s, b]}{@render line(displayName(s), b)}{/each}</tbody>
        </table>
      </div>
      <div class="card">
        <h2>类型</h2>
        <table>
          <thead><tr><th></th><th class="num">涨</th><th class="num">跌</th><th>多空</th><th class="num">多头占比</th></tr></thead>
          <tbody>{#each top(t.patterns, 8) as [p, b]}{@render line(PATTERN_LABELS[p] ?? p, b)}{/each}</tbody>
        </table>
      </div>
    </div>
  {/if}
{:else if !err}
  <div class="sheet empty">加载中…</div>
{/if}

<style>
  .cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
  @media (max-width: 1000px) {
    .cols {
      grid-template-columns: 1fr;
    }
  }
  .barcell {
    width: 35%;
  }
  .cols .card {
    margin-bottom: 0;
  }
  .bar {
    height: 6px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--down) 75%, transparent);
    overflow: hidden;
    display: flex;
  }
  .bull {
    height: 100%;
    background: var(--up);
    border-right: 2px solid var(--glass-solid);
  }
</style>
