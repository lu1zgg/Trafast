import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, SignalStream } from "./api";
import { DEFAULT_SETTINGS, looksLikeKey, loadSettings, normalizeServer, wsUrl } from "./config";
import { displayName, fmtPct, movePct, notifyText, recentNews, shouldNotify } from "./format";
import type { Signal } from "./types";

describe("normalizeServer", () => {
  it("adds https and strips trailing slash and /api/v1", () => {
    expect(normalizeServer("mm.example.com/")).toBe("https://mm.example.com");
    expect(normalizeServer(" https://mm.example.com/api/v1/ ")).toBe("https://mm.example.com");
    expect(normalizeServer("https://example.com:8443/mm")).toBe("https://example.com:8443/mm");
  });
  it("allows http only for local addresses", () => {
    expect(normalizeServer("http://127.0.0.1:8787")).toBe("http://127.0.0.1:8787");
    expect(normalizeServer("http://localhost:8787")).toBe("http://localhost:8787");
    expect(() => normalizeServer("http://mm.example.com")).toThrow(/HTTPS/);
    expect(() => normalizeServer("ftp://x")).toThrow();
    expect(() => normalizeServer("")).toThrow();
    expect(() => normalizeServer("https://u:p@x.com")).toThrow();
  });
  it("builds the ws url", () => {
    expect(wsUrl("https://a.com")).toBe("wss://a.com/api/v1/stream");
    expect(wsUrl("http://127.0.0.1:8787")).toBe("ws://127.0.0.1:8787/api/v1/stream");
  });
  it("checks key shape and has no default server", () => {
    expect(looksLikeKey("tfk_" + "a".repeat(43))).toBe(true);
    expect(looksLikeKey("abc")).toBe(false);
    expect(DEFAULT_SETTINGS.server).toBe("");
    expect(loadSettings({ getItem: () => "{bad json" }).server).toBe("");
    expect(loadSettings({ getItem: () => JSON.stringify({ server: "https://x", notify: { minLevel: "L2" } }) }).notify).toMatchObject({
      minLevel: "L2",
      enabled: true,
    });
  });
});

const sig = (o: Partial<Signal> = {}): Signal => ({
  seq: 1, id: "e:1", ts: 1_700_000_000, kind: "open", symbol: "NVDA.US", market: "US", session: "REG", direction: 1,
  level: "L2", pattern: "breakout", tags: ["big_buy"], price: 140, anchor: 137, move: Math.log(140 / 137), k: 5,
  score: null, episode_id: "e", rule_version: "v4.2", ...o,
});

describe("format", () => {
  it("names and percentages", () => {
    expect(displayName("NVDA.US")).toBe("NVDA");
    expect(displayName("BTCUSDT")).toBe("BTC");
    expect(movePct(sig())).toBeCloseTo((140 / 137 - 1) * 100, 6);
    expect(movePct(sig({ direction: -1, price: 130, anchor: 137, move: Math.log(137 / 130) }))).toBeCloseTo((130 / 137 - 1) * 100, 6);
    expect(fmtPct(0.0123, 2, true)).toBe("+1.23%");
    const t = notifyText(sig());
    expect(t.title).toContain("NVDA 盘整突破");
    expect(t.body).toContain("大单买入");
  });
  it("notification filter", () => {
    const n = { enabled: true, minLevel: "L2", upgrades: false };
    expect(shouldNotify(sig(), n)).toBe(true);
    expect(shouldNotify(sig({ level: "L1" }), n)).toBe(false);
    expect(shouldNotify(sig({ kind: "upgrade" }), n)).toBe(false);
    expect(shouldNotify(sig({ kind: "upgrade" }), { ...n, upgrades: true })).toBe(true);
    expect(shouldNotify(sig({ kind: "extend" }), { ...n, upgrades: true })).toBe(false);
    expect(shouldNotify(sig(), { ...n, enabled: false })).toBe(false);
  });
});

describe("ApiClient", () => {
  it("sends bearer key and maps errors", async () => {
    const calls: [string, RequestInit][] = [];
    const f = vi.fn(async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      if (url.includes("/ping")) return new Response(JSON.stringify({ api: 1 }), { status: 200 });
      return new Response(JSON.stringify({ error: "API 密钥无效或已吊销" }), { status: 401 });
    });
    const c = new ApiClient("https://a.com", "tfk_x", f as unknown as typeof fetch);
    await c.ping();
    expect(calls[0][0]).toBe("https://a.com/api/v1/ping");
    expect((calls[0][1].headers as Record<string, string>).Authorization).toBe("Bearer tfk_x");
    await expect(c.signals({ after: 5, symbol: "NVDA" })).rejects.toMatchObject({ status: 401 });
    expect(calls[1][0]).toBe("https://a.com/api/v1/signals?after=5&symbol=NVDA");
  });
  it("reports network failures as status 0", async () => {
    const c = new ApiClient("https://a.com", "k", (async () => {
      throw new TypeError("failed");
    }) as unknown as typeof fetch);
    const e = await c.ping().catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(0);
  });
  it("pages through history", async () => {
    const pages = [
      { items: [{ seq: 1 }, { seq: 2 }], next: 2 },
      { items: [{ seq: 3 }], next: 3 },
    ];
    let i = 0;
    const f = async () => new Response(JSON.stringify(pages[i++]), { status: 200 });
    const c = new ApiClient("https://a.com", "k", f as unknown as typeof fetch);
    const r = await c.history(0, 10, 2);
    expect(r.items.map((x) => x.seq)).toEqual([1, 2, 3]);
    expect(r.next).toBe(3);
  });
});

class FakeWs {
  static all: FakeWs[] = [];
  readyState = 1;
  sent: string[] = [];
  onopen: ((ev: any) => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: ((ev: any) => void) | null = null;
  constructor(public url: string) {
    FakeWs.all.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {}
  emit(m: unknown) {
    this.onmessage?.({ data: JSON.stringify(m) });
  }
}

describe("SignalStream", () => {
  it("auths with the key, resumes after a drop and stops on revoked key", () => {
    FakeWs.all = [];
    const got: number[] = [];
    const states: string[] = [];
    let fatal = "";
    const timeouts: (() => void)[] = [];
    const timers = {
      setTimeout: ((fn: () => void) => (timeouts.push(fn), 1)) as unknown as typeof setTimeout,
      clearTimeout: (() => {}) as unknown as typeof clearTimeout,
      setInterval: (() => 1) as unknown as typeof setInterval,
      clearInterval: (() => {}) as unknown as typeof clearInterval,
    };
    const s = new SignalStream(
      "https://a.com",
      "tfk_k",
      { onSignal: (x) => got.push(x.seq), onState: (st) => states.push(st), onFatal: (m) => (fatal = m) },
      null,
      (u) => new FakeWs(u),
      timers,
    );
    s.start();
    const w1 = FakeWs.all[0];
    expect(w1.url).toBe("wss://a.com/api/v1/stream");
    w1.onopen?.({});
    expect(JSON.parse(w1.sent[0])).toEqual({ type: "auth", key: "tfk_k" });
    w1.emit({ type: "hello", after: 10 });
    w1.emit({ type: "signal", seq: 11, data: sig({ seq: 0 }) });
    w1.emit({ type: "signal", seq: 11, data: sig() }); // 重复的不算
    expect(got).toEqual([11]);
    w1.onclose?.({ code: 1006, reason: "" });
    expect(states[states.length - 1]).toBe("retrying");
    timeouts.shift()!();
    const w2 = FakeWs.all[1];
    w2.onopen?.({});
    expect(JSON.parse(w2.sent[0])).toEqual({ type: "auth", key: "tfk_k", after: 11 });
    w2.onclose?.({ code: 4403, reason: "key revoked" });
    expect(fatal).toContain("吊销");
    expect(timeouts.length).toBe(0);
    expect(states[states.length - 1]).toBe("stopped");
  });
});

describe("ticker", () => {
  it("picks recent opens and upgrades, newest first", () => {
    const now = 100_000;
    const mk = (seq: number, ts: number, kind: Signal["kind"]) => ({ seq, ts, kind });
    const r = recentNews([mk(1, now - 4000, "open"), mk(2, now - 100, "extend"), mk(3, now - 200, "open"), mk(4, now - 50, "upgrade")], now);
    expect(r.map((s) => s.seq)).toEqual([4, 3]);
    expect(recentNews([mk(1, now, "open"), mk(2, now, "open"), mk(3, now, "open")], now, 60, 2)).toHaveLength(2);
  });
  it("old settings get the new ticker defaults", () => {
    const s = loadSettings({ getItem: () => JSON.stringify({ server: "https://x", ticker: { symbols: ["NVDA.US"], onTop: false } }) });
    expect(s.ticker).toEqual({ symbols: ["NVDA.US"], onTop: false, speed: "normal", news: true });
  });
});
