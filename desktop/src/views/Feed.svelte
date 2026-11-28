<script lang="ts">
  import { app } from "../lib/state.svelte";
  import { KIND_LABELS, MARKET_LABELS, TAG_LABELS, displayName, fmtPct, fmtPrice, fmtTime, movePct, patternName } from "../lib/format";
  import type { Signal } from "../lib/types";
  import Icon from "./Icon.svelte";

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

<div class="toolbar-row">
  <div class="seg" role="group" aria-label="市场">
    {#each [["", "全部"], ["US", "美股"], ["CRYPTO", "加密"], ["CMDTY", "大宗"]] as [v, l]}
      <button class:sel={market === v} onclick={() => (market = v)}>{l}</button>
    {/each}
  </div>
  <div class="seg" role="group" aria-label="方向">
    {#each [[0, "涨跌"], [1, "上涨"], [-1, "下跌"]] as [v, l]}
      <button class:sel={dir === v} onclick={() => (dir = v as number)}>{l}</button>
    {/each}
  </div>
  <label class="row toggle"><input type="checkbox" class="switch" bind:checked={onlyOpen} /> 只看首发和升级</label>
  <div class="grow"></div>
  {#if err}<span class="err small">{err}</span>{/if}
  <span class="muted small">最近 36 小时 · {rows.length} 条</span>
  <label class="search"><Icon name="search" size={13} /><input placeholder="搜索代码" bind:value={q} style="width: 140px" /></label>
</div>

{#if !app.client}
  <div class="sheet empty">还没连接服务器。请先到「设置」里填服务器地址和 API 密钥。</div>
{:else if rows.length === 0}
  <div class="sheet empty">暂时没有信号。</div>
{:else}
  <div class="sheet">
    <table>
      <thead>
        <tr><th>时间</th><th>标的</th><th>类型</th><th>级别</th><th class="num">幅度</th><th class="num">价格</th><th>标签</th><th>市场</th>{#if app.canWrite}<th>反馈</th>{/if}</tr>
      </thead>
      <tbody>
        {#each rows as s (s.seq)}
          <tr class="click" class:hot={s.level === "L3"} onclick={() => app.open(s.symbol)}>
            <td class="mono muted">{fmtTime(s.ts)}</td>
            <td><b>{displayName(s.symbol)}</b></td>
            <td><span class="kind" class:up={s.direction > 0} class:down={s.direction < 0}><span class="arrow">{s.direction > 0 ? "▲" : "▼"}</span>{patternName(s)}</span> <span class="muted small">{KIND_LABELS[s.kind] ?? s.kind}</span></td>
            <td><span class="badge {s.level}">{s.level}</span></td>
            <td class="num mono strong" class:up={s.direction > 0} class:down={s.direction < 0}>{fmtPct(movePct(s))}</td>
            <td class="num mono">{fmtPrice(s.price)}</td>
            <td><span class="tags">{#each s.tags ?? [] as t}{#if TAG_LABELS[t]}<span class="badge tag-{t}">{TAG_LABELS[t]}</span>{/if}{/each}</span></td>
            <td class="muted small">{MARKET_LABELS[s.market] ?? s.market}{EXT[s.session] ? ` · ${EXT[s.session]}` : ""}</td>
            {#if app.canWrite}
              <td onclick={(e) => e.stopPropagation()}>
                {#if s.kind === "open"}
                  <span class="votes">
                    <button class="small plain" class:on={voted[s.episode_id] === 1} onclick={() => vote(s, 1)} title="有用" aria-label="有用"><Icon name="thumbup" size={13} /></button>
                    <button class="small plain" class:on={voted[s.episode_id] === 0} onclick={() => vote(s, 0)} title="没用" aria-label="没用"><Icon name="thumbdown" size={13} /></button>
                  </span>
                {/if}
              </td>
            {/if}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .toggle {
    gap: 7px;
    font-size: 12px;
    color: var(--text-2);
  }
  .kind {
    font-weight: 500;
  }
  .arrow {
    font-size: 9px;
    margin-right: 5px;
    vertical-align: 1px;
  }
  .strong {
    font-weight: 600;
  }
  .tags {
    display: inline-flex;
    gap: 4px;
  }
  tr.hot td:first-child {
    box-shadow: inset 3px 0 0 var(--l3);
  }
  .votes {
    display: inline-flex;
    gap: 2px;
  }
  .votes button {
    padding: 4px 7px;
    color: var(--muted);
  }
  .votes button.on {
    color: #fff;
    background: var(--accent);
  }
</style>
