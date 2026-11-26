// 桌面版设置：服务器地址存在本地（不是秘密），API 密钥存在系统钥匙串（见 secrets.ts）。
// 没有默认服务器：每个用户连自己部署的后端。

export type Level = "L1" | "L2" | "L3";

export interface Settings {
  server: string; // 规范化后的地址，例如 https://mm.example.com
  notify: {
    enabled: boolean;
    minLevel: Level;
    upgrades: boolean; // 升级也弹通知
    sound: boolean;
  };
  ticker: {
    symbols: string[]; // 行情条显示哪些标的；空 = 全部
    onTop: boolean;
  };
}

export const DEFAULT_SETTINGS: Settings = {
  server: "",
  notify: { enabled: true, minLevel: "L1", upgrades: true, sound: true },
  ticker: { symbols: [], onTop: true },
};

const KEY = "trafast.settings.v1";
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

export class ConfigError extends Error {}

/** 把用户填的地址整理成 https://host[:port][/path]；只允许 HTTPS（本机地址除外）。 */
export function normalizeServer(raw: string): string {
  let s = raw.trim();
  if (!s) throw new ConfigError("请填写服务器地址");
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s = "https://" + s;
  let u: URL;
  try {
    u = new URL(s);
  } catch {
    throw new ConfigError("服务器地址格式不对");
  }
  if (u.username || u.password) throw new ConfigError("地址里不要带用户名或密码");
  const local = LOCAL_HOSTS.has(u.hostname);
  if (u.protocol === "http:" && !local) throw new ConfigError("只能用 HTTPS 连接（地址是 127.0.0.1 或 localhost 时除外）");
  if (u.protocol !== "https:" && u.protocol !== "http:") throw new ConfigError("地址要以 https:// 开头");
  let path = u.pathname.replace(/\/+$/, "");
  path = path.replace(/\/api\/v1$/, "");
  return `${u.protocol}//${u.host}${path}`;
}

export function wsUrl(server: string): string {
  return server.replace(/^http/, "ws") + "/api/v1/stream";
}

export function looksLikeKey(k: string): boolean {
  return /^tfk_[A-Za-z0-9_-]{20,}$/.test(k.trim());
}

function merge(base: Settings, raw: unknown): Settings {
  if (!raw || typeof raw !== "object") return structuredClone(base);
  const r = raw as Partial<Settings>;
  return {
    server: typeof r.server === "string" ? r.server : base.server,
    notify: { ...base.notify, ...(r.notify ?? {}) },
    ticker: { ...base.ticker, ...(r.ticker ?? {}) },
  };
}

export function loadSettings(storage: Pick<Storage, "getItem"> = localStorage): Settings {
  try {
    return merge(DEFAULT_SETTINGS, JSON.parse(storage.getItem(KEY) || "null"));
  } catch {
    return structuredClone(DEFAULT_SETTINGS);
  }
}

export function saveSettings(s: Settings, storage: Pick<Storage, "setItem"> = localStorage): void {
  storage.setItem(KEY, JSON.stringify(s));
}

export const SETTINGS_KEY = KEY;

/** 每台服务器记住看到的最后一个信号序号，重开时不重复弹旧通知。 */
export function lastSeqKey(server: string): string {
  return "trafast.lastseq." + server;
}
