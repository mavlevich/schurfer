export interface ExchangeEntry {
  exchange: string;
  symbol: string;
  price: string;
  change_pct: number;
  high_24h: string;
  volume_24h_usd: number;
}

export interface PumpEntry {
  base: string;
  max_change_pct: number;
  exchanges: ExchangeEntry[];
}

export interface PumpsResponse {
  ts: number;
  count: number;
  min_change_pct: number | null;
  pumps: PumpEntry[];
  errors?: Record<string, string>;
  scanned?: string[];
}

export interface HistoryEntry {
  base: string;
  first_seen_at: number;
  last_seen_at: number;
  peak_pct: number;
  last_pct: number;
  is_live: boolean;
  exchanges: ExchangeEntry[];
}
