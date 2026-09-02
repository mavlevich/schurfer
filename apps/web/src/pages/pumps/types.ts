export interface ExchangeEntry {
  exchange: string;
  symbol: string;
  // Native per-exchange market id (e.g. Binance "BTCUSDT", Gate "BTC_USDT",
  // OKX "BTC-USDT-SWAP") -- already returned by the API (apps/api-gateway's
  // own exchangeEntry.MarketID, used server-side for OHLCV fetches) but
  // unused on the frontend until exchangeLinks.ts's own deep link. May be
  // empty for a source that predates market_id capture.
  market_id?: string;
  price: string;
  change_pct: number;
  high_24h: string;
  volume_24h_usd: number | null;
  volume_24h_source?: 'quote_volume' | 'unavailable';
  ticker_timestamp_ms?: number | null;
  observed_at_ms?: number | null;
}

export interface PumpEntry {
  base: string;
  pump_event_id: number;
  max_change_pct: number;
  exchanges: ExchangeEntry[];
  // True only when the token is actually present in the live pumps:latest
  // snapshot right now. False means this is a DB fallback (the token has
  // history but is not currently pumping) -- max_change_pct is then the
  // episode's historical peak, and each ExchangeEntry's price/change_pct/
  // volume_24h_usd are last-observed values, not current ones. Consumers
  // must not present either as live when this is false (colleague review,
  // 2026-08-28).
  is_live: boolean;
}

// TokenNoPumpEpisode is what GET /api/pumps/<base> returns (still 200, not
// 404) for a base with zero app.pump_events history but real activity in a
// non-pump strategy -- e.g. an early_momentum_v4 paper trade
// (fix/token-activity-non-pump-assets-v1). has_pump_episode is always
// `false` here and never appears on PumpEntry, so it is a safe discriminant
// for TokenResponse without touching PumpEntry's own shape.
export interface TokenNoPumpEpisode {
  base: string;
  has_pump_episode: false;
  other_strategy_key: string;
}

export type TokenResponse = PumpEntry | TokenNoPumpEpisode;

// PumpEntry never carries has_pump_episode; TokenNoPumpEpisode always does
// (literal `false`). "in" narrows correctly either way even though
// PumpEntry never declares the property.
export function isPumpEntry(t: TokenResponse): t is PumpEntry {
  return !('has_pump_episode' in t);
}

export interface PumpsResponse {
  ts: number;
  published_at_ms?: number;
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
  exchange_24h_high_pct: number;
  observed_peak_pct: number;
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
  exchange_24h_high_pct: number;
  observed_peak_pct: number;
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

// MomentumWatchEntry is a currently-active momentum_flow WATCH episode: the
// prospective-long counterpart of PumpEntry, but built from a completely
// different signal (60m price return / OI growth / order-flow imbalance, not
// 24h % change). Kept as its own shape rather than merged into PumpEntry's
// columns -- the two surfaces stay two separate API calls feeding two
// separate tables on the Scanner page.
export interface MomentumWatchEntry {
  exchange: string;
  market_type: string;
  symbol: string;
  episode_id: string;
  first_watch_at: number;
  last_watch_at: number;
  clear_streak: number;
  decision_at: number;
  price_return_60m_pct: number | null;
  price_return_15m_pct: number | null;
  oi_growth_60m_pct: number | null;
  buy_imbalance_15m: number | null;
  flow_notional_15m_usd: number | null;
  flow_acceleration_15m_vs_prior_45m: number | null;
}

export interface MomentumWatchResponse {
  count: number;
  watch: MomentumWatchEntry[];
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
