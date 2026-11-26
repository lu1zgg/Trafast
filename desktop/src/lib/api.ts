// /api/v1 客户端：REST 带 Bearer 密钥；WebSocket 连上后第一条消息发密钥（不放在 URL 里）。
import { wsUrl } from "./config";
import type { AlertRule, Bar, Levels, Ping, Quote, Signal, Stats, SymbolInfo, TypesInfo } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type Fetch = typeof fetch;

export function explain(status: number, msg?: string): string {
  if (msg) return msg;
  if (status === 0) return "连不上服务器，检查地址和网络";
  if (status === 401) return "API 密钥无效或已吊销";
  if (status === 403) return "这个密钥只有只读权限";
  if (status === 404) return "这个地址上没有 /api/v1，确认后端已升级到 V4";
  if (status === 429) return "错误次数太多，15 分钟后再试";
  if (status === 503) return "监控进程没有运行";
  return `请求失败（HTTP ${status}）`;
}

export class ApiClient {
  constructor(
    public server: string,
    private key: string,
    private fetchImpl: Fetch = (...a) => fetch(...a),
    private timeoutMs = 15000,
  ) {}

  private async req<T>(method: string, path: string, params?: Record<string, unknown>, body?: unknown): Promise<T> {
    const url = new URL(this.server + "/api/v1" + path);
    for (const [k, v] of Object.entries(params ?? {})) {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
    }
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), this.timeoutMs);
    let resp: Response;
    try {
      resp = await this.fetchImpl(url.toString(), {
        method,
        headers: { Authorization: `Bearer ${this.key}`, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: ctl.signal,
      });
    } catch {
      throw new ApiError(0, explain(0));
    } finally {
      clearTimeout(timer);
    }
    let data: any = null;
    try {
      data = await resp.json();
    } catch {
      data = null;
    }
    if (!resp.ok) throw new ApiError(resp.status, explain(resp.status, data?.error));
    return data as T;
  }

  ping = () => this.req<Ping>("GET", "/ping");
  status = () => this.req<Record<string, any>>("GET", "/status");
  symbols = () => this.req<{ items: SymbolInfo[]; groups: Record<string, string[]> }>("GET", "/symbols");
  quotes = () => this.req<{ items: Quote[] }>("GET", "/quotes");
  signals = (p: { after?: number; since?: number; limit?: number; symbol?: string }) =>
    this.req<{ items: Signal[]; next: number }>("GET", "/signals", p);
  bars = (symbol: string, tf: "1m" | "15m", limit = 300) =>
    this.req<{ symbol: string; tf: string; items: Bar[] }>("GET", "/bars", { symbol, tf, limit });
  levels = (symbol: string) => this.req<Levels>("GET", "/levels", { symbol });
  stats = () => this.req<Stats>("GET", "/stats");
  trend = () => this.req<{ enabled: boolean; items: any[]; events: any[] }>("GET", "/trend");
  alerts = () => this.req<{ rules: AlertRule[] }>("GET", "/alerts");
  addAlert = (text: string) => this.req<{ message: string }>("POST", "/alerts", undefined, { text });
  delAlert = (id: number | "all") => this.req<{ removed: number }>("DELETE", `/alerts/${id}`);
  types = () => this.req<TypesInfo>("GET", "/types");
  setType = (target: string, pattern: string, off: boolean) =>
    this.req<{ types_off: Record<string, string[]> }>("POST", "/types", undefined, { target, pattern, off });
  mute = (target: string, seconds: number) => this.req<{ until: number }>("POST", "/mute", undefined, { target, seconds });
  unmute = (target: string) => this.req<{ cleared: number }>("DELETE", `/mute/${encodeURIComponent(target)}`);
  feedback = (episode_id: string, value: 0 | 1) => this.req<{ root_key: string }>("POST", "/feedback", undefined, { episode_id, value });

  /** 取一段时间以来的全部信号（按序号翻页，最多 pages 页）。 */
  async history(since: number, pages = 10, limit = 500): Promise<{ items: Signal[]; next: number }> {
    const all: Signal[] = [];
    let after = 0;
    for (let i = 0; i < pages; i++) {
      const r = await this.signals({ after, since, limit });
      all.push(...r.items);
      after = r.next;
      if (r.items.length < limit) break;
    }
    return { items: all, next: after };
  }
}

// ---- WebSocket 实时流 ----

export type StreamState = "connecting" | "live" | "retrying" | "stopped";

export interface StreamHandlers {
  onSignal(s: Signal): void;
  onQuotes?(items: Quote[]): void;
  onState?(state: StreamState, detail?: string): void;
  onMonitor?(running: boolean): void;
  onFatal?(message: string): void; // 密钥无效或被吊销：不再重连
}

interface WsLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: any) => void) | null;
  onmessage: ((ev: { data: any }) => void) | null;
  onclose: ((ev: { code: number; reason: string }) => void) | null;
  onerror: ((ev: any) => void) | null;
}

export type WsFactory = (url: string) => WsLike;

export const BACKOFF_S = [1, 2, 5, 10, 20, 30];

export class SignalStream {
  private ws: WsLike | null = null;
  private stopped = false;
  private attempt = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  lastSeq: number | null;

  constructor(
    private server: string,
    private key: string,
    private h: StreamHandlers,
    after: number | null = null,
    private factory: WsFactory = (u) => new WebSocket(u) as unknown as WsLike,
    // 包一层：浏览器里 setTimeout 不能挂在别的对象上调用（Illegal invocation）
    private timers = {
      setTimeout: ((fn: () => void, ms?: number) => setTimeout(fn, ms)) as typeof setTimeout,
      clearTimeout: ((id?: ReturnType<typeof setTimeout>) => clearTimeout(id)) as typeof clearTimeout,
      setInterval: ((fn: () => void, ms?: number) => setInterval(fn, ms)) as typeof setInterval,
      clearInterval: ((id?: ReturnType<typeof setInterval>) => clearInterval(id)) as typeof clearInterval,
    },
  ) {
    this.lastSeq = after;
  }

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.clear();
    this.ws?.close(1000, "bye");
    this.ws = null;
    this.h.onState?.("stopped");
  }

  private clear(): void {
    if (this.retryTimer) this.timers.clearTimeout(this.retryTimer);
    if (this.pingTimer) this.timers.clearInterval(this.pingTimer);
    this.retryTimer = this.pingTimer = null;
  }

  private open(): void {
    this.h.onState?.("connecting");
    const ws = this.factory(wsUrl(this.server));
    this.ws = ws;
    ws.onopen = () => {
      const msg: Record<string, unknown> = { type: "auth", key: this.key };
      if (this.lastSeq !== null) msg.after = this.lastSeq;
      ws.send(JSON.stringify(msg));
    };
    ws.onmessage = (ev) => {
      let m: any;
      try {
        m = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      switch (m?.type) {
        case "hello":
          this.attempt = 0;
          if (this.lastSeq === null) this.lastSeq = m.after;
          this.h.onState?.("live");
          this.pingTimer = this.timers.setInterval(() => {
            if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" }));
          }, 25000);
          break;
        case "signal":
          if (this.lastSeq === null || m.seq > this.lastSeq) {
            this.lastSeq = m.seq;
            this.h.onSignal({ ...m.data, seq: m.seq });
          }
          break;
        case "quotes":
          this.h.onQuotes?.(m.items ?? []);
          break;
        case "monitor":
          this.h.onMonitor?.(!!m.running);
          break;
      }
    };
    ws.onerror = () => {};
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.clear();
      this.ws = null;
      if (this.stopped) return;
      if (ev.code === 4401 || ev.code === 4403) {
        this.stopped = true;
        this.h.onFatal?.(ev.code === 4403 ? "API 密钥已被吊销" : "API 密钥无效");
        this.h.onState?.("stopped", "auth");
        return;
      }
      const wait = ev.code === 4429 ? 15 * 60 : BACKOFF_S[Math.min(this.attempt, BACKOFF_S.length - 1)];
      this.attempt++;
      this.h.onState?.("retrying", `${wait} 秒后重连`);
      this.retryTimer = this.timers.setTimeout(() => this.open(), wait * 1000);
    };
  }
}
