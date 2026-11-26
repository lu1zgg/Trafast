<script lang="ts">
  import { app } from "../lib/state.svelte";
  import { ApiClient } from "../lib/api";
  import { ConfigError, looksLikeKey, normalizeServer, type Level } from "../lib/config";
  import { deleteKey, getKey, setKey } from "../lib/secrets";
  import { ensurePermission, notify } from "../lib/notify";
  import type { Ping } from "../lib/types";
  import { displayName } from "../lib/format";

  let server = $state(app.settings.server);
  let key = $state("");
  let hasKey = $state(false);
  let busy = $state(false);
  let msg = $state("");
  let msgOk = $state(false);
  let tested = $state<Ping | null>(null);

  $effect(() => {
    const s = app.settings.server;
    if (s) void getKey(s).then((k) => (hasKey = !!k));
  });

  function fail(e: unknown) {
    msgOk = false;
    msg = e instanceof Error ? e.message : String(e);
  }

  /** 先 ping 一下：地址、密钥、版本都对了才保存。 */
  async function test(): Promise<{ url: string; k: string } | null> {
    msg = "";
    tested = null;
    let url: string;
    try {
      url = normalizeServer(server);
    } catch (e) {
      fail(e);
      return null;
    }
    const k = key.trim() || (url === app.settings.server ? ((await getKey(url)) ?? "") : "");
    if (!k) {
      fail(new ConfigError("请填写 API 密钥（在服务器网页后台「API 密钥」页生成）"));
      return null;
    }
    if (!looksLikeKey(k)) {
      fail(new ConfigError("密钥格式不对，应以 tfk_ 开头"));
      return null;
    }
    busy = true;
    try {
      tested = await new ApiClient(url, k).ping();
      msgOk = true;
      msg = `连接成功：服务器 v${tested.version}，密钥「${tested.key.name}」${tested.key.scope === "write" ? "读写" : "只读"}` + (tested.monitor ? "" : "（监控进程没在运行）");
      return { url, k };
    } catch (e) {
      fail(e);
      return null;
    } finally {
      busy = false;
    }
  }

  async function save() {
    const r = await test();
    if (!r) return;
    const old = app.settings.server;
    await setKey(r.url, r.k);
    if (old && old !== r.url) await deleteKey(old);
    server = r.url;
    key = "";
    hasKey = true;
    app.saveSettings({ ...app.settings, server: r.url });
    await app.connect();
    app.view = "feed";
  }

  async function forget() {
    await deleteKey(app.settings.server);
    app.disconnect();
    app.saveSettings({ ...app.settings, server: "" });
    server = "";
    hasKey = false;
    msg = "已删除这台电脑上保存的密钥";
    msgOk = true;
  }

  function setNotify<K extends keyof typeof app.settings.notify>(k: K, v: (typeof app.settings.notify)[K]) {
    app.saveSettings({ ...app.settings, notify: { ...app.settings.notify, [k]: v } });
  }

  function toggleTickerSym(sym: string, on: boolean) {
    const cur = new Set(app.settings.ticker.symbols);
    if (on) cur.add(sym);
    else cur.delete(sym);
    app.saveSettings({ ...app.settings, ticker: { ...app.settings.ticker, symbols: [...cur] } });
  }

  async function testNotify() {
    if (!(await ensurePermission())) {
      fail(new Error("系统没有允许通知：请到系统设置里给 Trafast 打开通知"));
      return;
    }
    await notify("🟢 测试通知", "Trafast 桌面版通知正常", app.settings.notify.sound);
  }
</script>

<div class="wrap">
  <div class="card">
    <h2>服务器</h2>
    <p class="muted small">
      填你自己部署的 Trafast 服务器地址，以及在它的网页后台「API 密钥」页（或命令行 <code>sudo mm api-key create --name 我的电脑</code>）生成的密钥。
      只能用 HTTPS 连接（127.0.0.1 / localhost 除外）。密钥保存在系统钥匙串里。
    </p>
    <div class="field">
      <label for="srv">服务器地址</label>
      <input id="srv" placeholder="https://你的域名" bind:value={server} autocomplete="off" spellcheck="false" />
    </div>
    <div class="field">
      <label for="key">API 密钥 {#if hasKey}<span class="ok">（已保存，留空则不改）</span>{/if}</label>
      <input id="key" type="password" placeholder="tfk_…" bind:value={key} autocomplete="off" spellcheck="false" />
    </div>
    <div class="row">
      <button onclick={test} disabled={busy}>测试连接</button>
      <button class="primary" onclick={save} disabled={busy}>保存并连接</button>
      {#if app.settings.server}<button onclick={forget} disabled={busy}>断开并删除密钥</button>{/if}
    </div>
    {#if msg}<p class:ok={msgOk} class:err={!msgOk}>{msg}</p>{/if}
  </div>

  <div class="card">
    <h2>系统通知</h2>
    <label class="row"><input type="checkbox" checked={app.settings.notify.enabled} onchange={(e) => setNotify("enabled", e.currentTarget.checked)} /> 新异动弹系统通知</label>
    <label class="row"><input type="checkbox" checked={app.settings.notify.upgrades} onchange={(e) => setNotify("upgrades", e.currentTarget.checked)} /> 升级（L1 → L2 → L3）也通知</label>
    <label class="row"><input type="checkbox" checked={app.settings.notify.sound} onchange={(e) => setNotify("sound", e.currentTarget.checked)} /> 声音</label>
    <div class="row" style="margin-top: 8px">
      <span>最低级别</span>
      <select value={app.settings.notify.minLevel} onchange={(e) => setNotify("minLevel", e.currentTarget.value as Level)}>
        <option value="L1">L1 及以上（全部）</option>
        <option value="L2">L2 及以上</option>
        <option value="L3">只要 L3</option>
      </select>
      <button onclick={testNotify}>发一条测试通知</button>
    </div>
    <p class="muted small">关掉主窗口后程序留在菜单栏 / 系统托盘里，照样弹通知；要彻底退出请用托盘菜单的「退出」。</p>
  </div>

  <div class="card">
    <h2>浮动行情条</h2>
    <p class="muted small">勾选要在行情条上显示的标的；都不勾就显示全部。行情条可以拖动，托盘菜单里可以打开或关掉。</p>
    <div class="syms">
      {#each app.symbols as s (s.symbol)}
        <label><input type="checkbox" checked={app.settings.ticker.symbols.includes(s.symbol)} onchange={(e) => toggleTickerSym(s.symbol, e.currentTarget.checked)} /> {displayName(s.symbol)}</label>
      {:else}
        <span class="muted">连上服务器后这里会列出标的。</span>
      {/each}
    </div>
    <label class="row" style="margin-top: 8px"><input type="checkbox" checked={app.settings.ticker.onTop} onchange={(e) => app.saveSettings({ ...app.settings, ticker: { ...app.settings.ticker, onTop: e.currentTarget.checked } })} /> 总在最前面</label>
  </div>
</div>

<style>
  .wrap {
    max-width: 720px;
  }
  .syms {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
  }
  label.row {
    margin: 4px 0;
  }
  code {
    font-size: 12px;
  }
</style>
