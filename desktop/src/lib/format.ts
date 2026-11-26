// 显示用的格式化：和服务器推送卡片的叫法保持一致。
import type { Signal } from "./types";

export const PATTERN_NAMES: Record<string, [string, string]> = {
  v_reversal: ["低位反弹", "高位回落"],
  breakout: ["盘整突破", "平台跳水"],
  spike: ["急拉", "急跌"],
  trend: ["持续上涨", "持续下跌"],
};

export const PATTERN_LABELS: Record<string, string> = {
  v_reversal: "V 反转",
  breakout: "盘整突破",
  spike: "急拉急跌",
  trend: "持续推进",
};

export const KIND_LABELS: Record<string, string> = { open: "首发", extend: "延伸", upgrade: "升级", end: "结束" };

export const TAG_LABELS: Record<string, string> = {
  vol: "放量",
  big_buy: "大单买入",
  big_sell: "大单卖出",
  level: "关键位",
};

export const MARKET_LABELS: Record<string, string> = { US: "美股", CRYPTO: "加密" };

export const LEVEL_RANK: Record<string, number> = { L1: 1, L2: 2, L3: 3 };

const BJ = "Asia/Shanghai";

export function patternName(s: Pick<Signal, "pattern" | "direction">): string {
  const pair = PATTERN_NAMES[s.pattern];
  if (!pair) return s.direction > 0 ? "上涨" : "下跌";
  return s.direction > 0 ? pair[0] : pair[1];
}

/** 相对起点的涨跌幅（move 是与方向同号为正的对数位移）。 */
export function movePct(s: Pick<Signal, "move" | "direction">): number {
  return (Math.exp(s.move * (s.direction >= 0 ? 1 : -1)) - 1) * 100;
}

export function fmtPrice(p: number | null | undefined): string {
  if (p === null || p === undefined || !isFinite(p)) return "—";
  const a = Math.abs(p);
  const d = a >= 1 ? 2 : a >= 0.01 ? 4 : 6;
  return p.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function fmtPct(x: number | null | undefined, digits = 2, ratio = false): string {
  if (x === null || x === undefined || !isFinite(x)) return "—";
  const v = ratio ? x * 100 : x;
  return (v > 0 ? "+" : "") + v.toFixed(digits) + "%";
}

export function fmtMoney(x: number): string {
  const a = Math.abs(x);
  if (a >= 1e9) return `$${(x / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(x / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `$${(x / 1e3).toFixed(1)}K`;
  return `$${x.toFixed(0)}`;
}

/** 月-日 时:分，本机时区；beijing = true 时用北京时间（和服务器卡片一致）。 */
export function fmtTime(ts: number, beijing = false): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: beijing ? BJ : undefined,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(ts * 1000));
}

export function fmtClock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** 和后端 display_name 一样：NVDA.US → NVDA，BTCUSDT → BTC。 */
export function displayName(sym: string): string {
  const s = sym.toUpperCase();
  if (s.endsWith(".US")) return s.slice(0, -3);
  for (const q of ["USDT", "USDC", "FDUSD"]) {
    if (s.endsWith(q) && s.length > q.length) return s.slice(0, -q.length);
  }
  return s;
}

/** 通知标题和正文。 */
export function notifyText(s: Signal): { title: string; body: string } {
  const icon = s.direction > 0 ? "🟢" : "🔴";
  const kind = s.kind === "upgrade" ? `升级 ${s.level}` : s.level;
  const title = `${icon} ${displayName(s.symbol)} ${patternName(s)}｜${kind}`;
  const tags = (s.tags ?? []).map((t) => TAG_LABELS[t]).filter(Boolean);
  const body = `${fmtPct(movePct(s))}  现价 ${fmtPrice(s.price)}` + (tags.length ? `  ${tags.join(" · ")}` : "");
  return { title, body };
}

export function shouldNotify(s: Signal, n: { enabled: boolean; minLevel: string; upgrades: boolean }): boolean {
  if (!n.enabled) return false;
  if (s.kind !== "open" && !(s.kind === "upgrade" && n.upgrades)) return false;
  return (LEVEL_RANK[s.level] ?? 1) >= (LEVEL_RANK[n.minLevel] ?? 1);
}
