<script lang="ts">
  import { onMount } from "svelte";
  import { app } from "../lib/state.svelte";
  import { PATTERN_LABELS, displayName } from "../lib/format";
  import type { AlertRule, TypesInfo } from "../lib/types";

  let rules = $state<AlertRule[]>([]);
  let types = $state<TypesInfo | null>(null);
  let text = $state("");
  let msg = $state("");
  let ok = $state(false);
  let busy = $state(false);
  let target = $state("*");

  const examples = [
    "NVDA > 140",
    "BTC 5m -2%",
    "NVDA 当日 5%",
    "MU 5m $20M",
    "BTC 1h ema 20 50 金叉",
    "NVDA 15m rsi > 70",
    "BTC 1h macd 死叉",
    "ETH 15m boll 上",
  ];

  async function load() {
    if (!app.client) return;
    try {
      const [a, t] = await Promise.all([app.client.alerts(), app.client.types()]);
      rules = a.rules;
      types = t;
    } catch (e) {
      say((e as Error).message, false);
    }
  }

  function say(m: string, good: boolean) {
    msg = m;
    ok = good;
  }

  async function add() {
    if (!app.client || !text.trim()) return;
    busy = true;
    try {
      const r = await app.client.addAlert(text.trim());
      say(r.message, true);
      text = "";
      await load();
    } catch (e) {
      say((e as Error).message, false);
    } finally {
      busy = false;
    }
  }

  async function del(id: number) {
    if (!app.client) return;
    try {
      await app.client.delAlert(id);
      await load();
    } catch (e) {
      say((e as Error).message, false);
    }
  }

  function isOff(p: string): boolean {
    return !!types?.types_off[target]?.includes(p);
  }

  async function setType(p: string, off: boolean) {
    if (!app.client) return;
    try {
      const r = await app.client.setType(target, p, off);
      if (types) types = { ...types, types_off: r.types_off };
    } catch (e) {
      say((e as Error).message, false);
    }
  }

  onMount(load);
</script>

<div class="wrap">
  <div class="card">
    <h2>自定义提醒</h2>
    <p class="muted small">写法和 Telegram 的 /alert 一样，触发后推送到服务器配置的所有渠道，桌面版不单独弹。</p>
    {#if app.canWrite}
      <div class="row">
        <input style="flex: 1; min-width: 240px" placeholder="例如 NVDA > 140" bind:value={text} onkeydown={(e) => e.key === "Enter" && add()} />
        <button class="primary" onclick={add} disabled={busy || !text.trim()}>添加</button>
      </div>
      <div class="row ex">
        <span class="muted small">示例</span>
        {#each examples as ex}<button class="small chip mono" onclick={() => (text = ex)}>{ex}</button>{/each}
      </div>
    {:else}
      <p class="muted">当前密钥是只读的，只能查看。</p>
    {/if}
    {#if msg}<p class:ok class:err={!ok}>{msg}</p>{/if}

    <table>
      <thead><tr><th>#</th><th>规则</th><th>标的</th>{#if app.canWrite}<th></th>{/if}</tr></thead>
      <tbody>
        {#each rules as r (r.id)}
          <tr>
            <td class="mono muted">{r.id}</td>
            <td>{r.text}</td>
            <td>{displayName(r.symbol)}</td>
            {#if app.canWrite}<td><button class="small" onclick={() => del(r.id)}>删除</button></td>{/if}
          </tr>
        {:else}
          <tr><td colspan="4" class="muted empty">还没有自定义提醒。</td></tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>异动类型开关</h2>
    <p class="muted small">关掉的类型不再推送（服务器上全局生效，所有渠道和桌面版都一样）。</p>
    {#if types}
      <div class="row">
        <select bind:value={target}>
          <option value="*">全部标的</option>
          {#each Object.keys(types.groups) as g}<option value={`group:${g}`}>分组：{g}</option>{/each}
          {#each app.symbols as s}<option value={s.symbol}>{displayName(s.symbol)}</option>{/each}
        </select>
      </div>
      <div class="types">
        {#each Object.entries(types.labels ?? PATTERN_LABELS) as [p, label]}
          <label class="type">
            <span>{label}</span>
            <input type="checkbox" class="switch" checked={!isOff(p)} disabled={!app.canWrite} onchange={(e) => setType(p, !e.currentTarget.checked)} />
          </label>
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  .wrap {
    max-width: 860px;
  }
  .ex {
    margin: 10px 0 14px;
    gap: 6px;
  }
  .chip {
    font-size: 11.5px;
    font-weight: 400;
  }
  .types {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 8px;
    margin-top: 12px;
  }
  .type {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 9px 12px;
    border-radius: var(--radius);
    background: var(--fill);
  }
</style>
