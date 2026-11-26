// 主窗口的全局状态：连接、信号列表、行情。行情条窗口自己轮询 /quotes，不用这里。
import { ApiClient, SignalStream, type StreamState } from "./api";
import { lastSeqKey, loadSettings, saveSettings, type Settings } from "./config";
import { notifyText, shouldNotify } from "./format";
import { notify } from "./notify";
import { getKey } from "./secrets";
import type { Ping, Quote, Signal, SymbolInfo } from "./types";

const MAX_SIGNALS = 1000;

class App {
  settings = $state<Settings>(loadSettings());
  client = $state<ApiClient | null>(null);
  ping = $state<Ping | null>(null);
  conn = $state<StreamState | "idle" | "error">("idle");
  connDetail = $state("");
  fatal = $state("");
  monitorRunning = $state(true);
  signals = $state<Signal[]>([]);
  quotes = $state<Record<string, Quote>>({});
  symbols = $state<SymbolInfo[]>([]);
  groups = $state<Record<string, string[]>>({});
  view = $state<"feed" | "symbols" | "detail" | "stats" | "alerts" | "settings">("feed");
  detailSymbol = $state<string | null>(null);
  private stream: SignalStream | null = null;

  get canWrite(): boolean {
    return this.ping?.key.scope === "write";
  }

  saveSettings(s: Settings): void {
    this.settings = s;
    saveSettings(s);
  }

  open(symbol: string): void {
    this.detailSymbol = symbol;
    this.view = "detail";
  }

  /** 读设置和钥匙串里的密钥，连上服务器。没配置时返回 false（显示设置页）。 */
  async connect(): Promise<boolean> {
    this.disconnect();
    const server = this.settings.server;
    const key = server ? await getKey(server) : null;
    if (!server || !key) {
      this.view = "settings";
      return false;
    }
    const client = new ApiClient(server, key);
    this.client = client;
    this.fatal = "";
    this.conn = "connecting";
    try {
      this.ping = await client.ping();
    } catch (e) {
      this.conn = "error";
      this.connDetail = (e as Error).message;
      return true;
    }
    await Promise.all([this.loadSymbols(), this.loadHistory()]);
    const saved = Number(localStorage.getItem(lastSeqKey(server)) || "0");
    const after = Math.max(saved, this.signals[0]?.seq ?? 0);
    this.stream = new SignalStream(server, key, {
      onSignal: (s) => this.onSignal(s, true),
      onQuotes: (items) => this.applyQuotes(items),
      onState: (st, d) => {
        this.conn = st;
        this.connDetail = d ?? "";
      },
      onMonitor: (r) => (this.monitorRunning = r),
      onFatal: (m) => (this.fatal = m),
    }, after || null);
    this.stream.start();
    return true;
  }

  disconnect(): void {
    this.stream?.stop();
    this.stream = null;
    this.client = null;
    this.ping = null;
    this.conn = "idle";
    this.signals = [];
    this.quotes = {};
    this.symbols = [];
  }

  async loadSymbols(): Promise<void> {
    if (!this.client) return;
    try {
      const r = await this.client.symbols();
      this.symbols = r.items;
      this.groups = r.groups;
      this.applyQuotes(r.items);
    } catch {
      /* 状态栏会显示连接问题 */
    }
  }

  private async loadHistory(): Promise<void> {
    if (!this.client) return;
    try {
      const since = Math.floor(Date.now() / 1000) - 36 * 3600;
      const r = await this.client.history(since);
      this.signals = r.items.slice(-MAX_SIGNALS).reverse();
    } catch {
      /* 同上 */
    }
  }

  private applyQuotes(items: Quote[]): void {
    const next = { ...this.quotes };
    for (const q of items) next[q.symbol] = q;
    this.quotes = next;
  }

  private onSignal(s: Signal, live: boolean): void {
    if (this.signals.some((x) => x.seq === s.seq)) return;
    this.signals = [s, ...this.signals].slice(0, MAX_SIGNALS);
    if (this.client) localStorage.setItem(lastSeqKey(this.client.server), String(s.seq));
    if (live && shouldNotify(s, this.settings.notify)) {
      const { title, body } = notifyText(s);
      void notify(title, body, this.settings.notify.sound);
    }
  }
}

export const app = new App();
