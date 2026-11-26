// /api/v1 返回的数据（字段和后端 SignalEvent、api_backend.py 对应，只增不改）。

export interface Signal {
  seq: number;
  id: string;
  ts: number;
  kind: "open" | "extend" | "upgrade" | "end";
  symbol: string;
  market: string;
  session: string;
  direction: number;
  level: string;
  pattern: string;
  tags: string[];
  price: number;
  anchor: number;
  move: number;
  k: number;
  score: number | null;
  episode_id: string;
  rule_version: string;
  retained?: number | null;
  end_reason?: string | null;
  reason?: Record<string, number | null>;
}

export interface Quote {
  symbol: string;
  name: string;
  price: number | null;
  ts: number | null;
  session: string | null;
  day_ref: number | null;
  day_chg: number | null;
}

export interface SymbolInfo extends Quote {
  market: string;
  groups: string[];
  muted: boolean;
  session_label: string;
}

export interface Ping {
  api: number;
  version: string;
  server_time: number;
  key: { id: number; name: string; scope: "read" | "write" };
  monitor: boolean;
  rule_version: string | null;
}

export interface Bar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  s?: string;
}

export interface Zone {
  lo: number;
  hi: number;
  stars: string;
  src: string;
  strong: boolean;
  score?: number;
}

export interface TfBrief {
  s?: number;
  e?: number;
  a?: number;
  sc?: number;
  adx?: number;
  label?: string;
}

export interface Levels {
  symbol: string;
  enabled: boolean;
  trend?: { ready: boolean; price: number | null; s15: TfBrief; s1: TfBrief; s4: TfBrief };
  atr1h?: number | null;
  res: Zone[];
  sup: Zone[];
}

export interface Bucket {
  up: number;
  down: number;
  up_n: number;
  down_n: number;
  bull: number | null;
}

export interface Tally {
  total: Bucket;
  markets: Record<string, Bucket>;
  groups: Record<string, Bucket>;
  symbols: Record<string, Bucket>;
  patterns: Record<string, Bucket>;
}

export interface Stats {
  today: Tally;
  recent: Tally;
  recent_min: number;
  t: number;
}

export interface AlertRule {
  id: number;
  symbol: string;
  kind: string;
  text: string;
  [k: string]: unknown;
}

export interface TypesInfo {
  types_off: Record<string, string[]>;
  labels: Record<string, string>;
  groups: Record<string, string[]>;
}
