export interface ExchangeEntry {
  exchange: string;
  symbol: string;
  price: string;
  change_pct: number;
  high_24h: string;
  volume_24h_usd: number | null;
  volume_24h_source?: 'quote_volume' | 'unavailable';
  ticker_timestamp_ms?: number | null;
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

export interface TokenEpisode {
  base: string;
  episode: number;
  first_seen_at: number;
  last_seen_at: number;
  closed_at: number | null;
  peak_pct: number;
  last_pct: number;
  retrace_pct: number | null;
  is_live: boolean;
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface OHLCVResponse {
  base: string;
  exchange: string;
  interval: number;
  candles: Candle[];
}

export interface SignalComponent {
  value: number;
  points: number;
  max: number;
  note: string;
}

export interface SignalsResponse {
  base: string;
  verdict: string;
  score: number;
  max_score: number;
  episode: {
    id: number;
    first_seen_at: number;
    age_hours: number;
    peak_pct: number;
    last_pct: number;
    is_open: boolean;
  };
  components: {
    pump_age: SignalComponent;
    price_extent: SignalComponent;
    oi_trend: SignalComponent;
    funding_rate: SignalComponent;
    retrace_from_peak: SignalComponent;
  };
  data_quality: {
    oi: boolean;
    funding: boolean;
  };
}

export interface TokenStats {
  base: string;
  episode_count: number;
  retrace_count: number;
  confidence: 'low' | 'medium' | 'high';
  avg_peak_pct: number;
  median_peak_pct: number;
  avg_retrace_pct: number | null;
  median_retrace_pct: number | null;
  min_retrace_pct: number | null;
  max_retrace_pct: number | null;
  avg_duration_hours: number;
  median_duration_hours: number;
}
