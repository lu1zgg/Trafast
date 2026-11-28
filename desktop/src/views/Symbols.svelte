<script lang="ts">
  import { app } from "../lib/state.svelte";
  import { MARKET_LABELS, displayName, fmtPct, fmtPrice } from "../lib/format";

  let group = $state("");
  let market = $state("");
  let err = $state("");

  const rows = $derived(
    app.symbols
      .filter((s) => !group || s.groups.includes(group))
      .filter((s) => !market || s.market === market)
      .map((s) => ({ ...s, ...(app.quotes[s.symbol] ?? {}) })),
  );

  async function mute(sym: string, on: boolean) {
    if (!app.client) return;
    err = "";
    try {
      if (on) await app.client.mute(sym, 3600);
      else await app.client.unmute(sym);
      await app.loadSymbols();
    } catch (e) {
      err = (e as Error).message;
    }
  }

  // 整个市场静音 / 取消（服务器 v4.3 起支持 market:US / market:CRYPTO / market:CMDTY）
  const marketMuted = $derived(rows.length > 0 && rows.every((s) => s.muted));
  function muteMarket(on: boolean) {
    return mute(`market:${market}`, on);
  }
</script>

<div class="toolbar-row">
  <select bind:value={group}>
    <option value="">全部分组</option>
    {#each Object.keys(app.groups) as g}<option value={g}>{g}</option>{/each}
  </select>
  <div class="seg" role="group" aria-label="市场">
    <button class:sel={market === ""} onclick={() => (market = "")}>全部</button>
    {#each Object.entries(MARKET_LABELS) as [k, v]}<button class:sel={market === k} onclick={() => (market = k)}>{v}</button>{/each}
  </div>
  {#if market && app.canWrite}
    {#if marketMuted}<button class="small" onclick={() => muteMarket(false)}>取消{MARKET_LABELS[market]}静音</button>
    {:else}<button class="small" onclick={() => muteMarket(true)}>静音全部{MARKET_LABELS[market]} 1 小时</button>{/if}
  {/if}
  <div class="grow"></div>
  {#if err}<span class="err small">{err}</span>{/if}
  <span class="muted small">{rows.length} 个标的</span>
  <button class="small" onclick={() => app.loadSymbols()}>刷新</button>
</div>

<div class="sheet">
<table>
  <thead>
    <tr><th>标的</th><th>市场</th><th class="num">现价</th><th class="num">当日涨跌</th><th>时段</th><th>分组</th><th>状态</th>{#if app.canWrite}<th></th>{/if}</tr>
  </thead>
  <tbody>
    {#each rows as s (s.symbol)}
      <tr class="click" onclick={() => app.open(s.symbol)}>
        <td><b>{displayName(s.symbol)}</b></td>
        <td class="muted">{MARKET_LABELS[s.market] ?? s.market}</td>
        <td class="num mono strong">{fmtPrice(s.price)}</td>
        <td class="num mono" class:up={(s.day_chg ?? 0) > 0} class:down={(s.day_chg ?? 0) < 0}>{fmtPct(s.day_chg, 2, true)}</td>
        <td class="muted small">{s.session_label}</td>
        <td class="muted small">{s.groups.join("、")}</td>
        <td>{#if s.muted}<span class="badge">静音中</span>{/if}</td>
        {#if app.canWrite}
          <td onclick={(e) => e.stopPropagation()}>
            {#if s.muted}<button class="small" onclick={() => mute(s.symbol, false)}>取消静音</button>
            {:else}<button class="small" onclick={() => mute(s.symbol, true)}>静音 1 小时</button>{/if}
          </td>
        {/if}
      </tr>
    {:else}
      <tr><td colspan="8" class="empty">没有标的。</td></tr>
    {/each}
  </tbody>
</table>
</div>

<style>
  .strong {
    font-weight: 600;
  }
</style>
