<script lang="ts">
  import { app } from "../lib/state.svelte";
  import { KIND_LABELS, MARKET_LABELS, TAG_LABELS, displayName, fmtPct, fmtPrice, fmtTime, movePct, patternName } from "../lib/format";
  import type { Signal } from "../lib/types";

  const EXT: Record<string, string> = { PRE: "盘前", POST: "盘后", ON: "夜盘" };
  let market = $state("");
  let dir = $state(0);
  let onlyOpen = $state(true);
  let q = $state("");
  let voted = $state<Record<string, 0 | 1>>({});
  let err = $state("");

  const rows = $derived(
    app.signals.filter(
      (s) =>
        (!market || s.market === market) &&
        (!dir || s.direction === dir) &&
        (!onlyOpen || s.kind === "open" || s.kind === "upgrade") &&
        (!q || displayName(s.symbol).includes(q.trim().toUpperCase())),
    ),
  );

  async function vote(s: Signal, v: 0 | 1) {
    if (!app.client) return;
    err = "";
    try {
      await app.client.feedback(s.episode_id, v);
      voted = { ...voted, [s.episode_id]: v };
    } catch (e) {
      err = (e as Error).message;
    }
  }
</script>

<div class="row bar">
  <select bind:value={market}>
    <option value="">全部市场</option>
    <option value="US">美股</option>
    <option value="CRYPTO">加密</option>
  </select>
  <select bind:value={dir}>
    <option value={0}>涨跌都看</option>
    <option value={1}>只看上涨</option>
    <option value={-1}>只看下跌</option>
  </select>
  <label class="row"><input type="checkbox" bind:checked={onlyOpen} /> 只看首发和升级</label>
  <input placeholder="搜索代码" bind:value={q} style="width: 120px" />
  <span class="muted small">最近 36 小时 · {rows.length} 条</span>
  {#if err}<span class="err small">{err}</span>{/if}
</div>

{#if !app.client}
  <p class="muted">还没连接服务器。请先到「设置」里填服务器地址和 API 密钥。</p>
{:else if rows.length === 0}
  <p class="muted">暂时没有信号。</p>
{:else}
  <table>
    <thead>
      <tr><th>时间</th><th>标的</th><th>类型</th><th>级别</th><th class="num">幅度</th><th class="num">价格</th><th>标签</th><th>市场</th>{#if app.canWrite}<th>反馈</th>{/if}</tr>
    </thead>
    <tbody>
      {#each rows as s (s.seq)}
        <tr class="click" onclick={() => app.open(s.symbol)}>
          <td class="mono muted">{fmtTime(s.ts)}</td>
          <td><b>{displayName(s.symbol)}</b></td>
          <td class:up={s.direction > 0} class:down={s.direction < 0}>{s.direction > 0 ? "▲" : "▼"} {patternName(s)} <span class="muted small">{KIND_LABELS[s.kind] ?? s.kind}</span></td>
          <td><span class="badge {s.level}">{s.level}</span></td>
          <td class="num mono" class:up={s.direction > 0} class:down={s.direction < 0}>{fmtPct(movePct(s))}</td>
          <td class="num mono">{fmtPrice(s.price)}</td>
          <td>{#each s.tags ?? [] as t}{#if TAG_LABELS[t]}<span class="badge">{TAG_LABELS[t]}</span> {/if}{/each}</td>
          <td class="muted small">{MARKET_LABELS[s.market] ?? s.market}{EXT[s.session] ? ` · ${EXT[s.session]}` : ""}</td>
          {#if app.canWrite}
            <td onclick={(e) => e.stopPropagation()}>
              {#if s.kind === "open"}
                <button class="small" class:on={voted[s.episode_id] === 1} onclick={() => vote(s, 1)} title="有用">👍</button>
                <button class="small" class:on={voted[s.episode_id] === 0} onclick={() => vote(s, 0)} title="没用">👎</button>
              {/if}
            </td>
          {/if}
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<style>
  .bar {
    margin-bottom: 10px;
  }
  button.on {
    border-color: var(--accent);
    background: var(--accent);
  }
</style>
