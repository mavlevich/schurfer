import { useQuery } from '@tanstack/react-query';

export interface Trade {
  id: number;
  symbol: string;
  exchange: string;
  market_type: string;
  side: string;
  size_usd: number;
  leverage: number;
  entry_price: number;
  entry_at: string;
  exit_price: number | null;
  exit_at: string | null;
  entry_slippage_bps: number | null;
  exit_slippage_bps: number | null;
  fees_usd: number;
  funding_usd: number;
  slippage_usd: number | null;
  gross_pnl_usd: number | null;
  gross_pnl_pct: number | null;
  net_pnl_usd: number | null;
  net_pnl_pct: number | null;
  pnl_usd: number | null;
  pnl_pct: number | null;
  accounting_version: string;
  accounting_status: string;
  accounting_error: string | null;
  status: string;
  outcome_label: string | null;
  setup_context: Record<string, unknown>;
  notes: string | null;
  created_at: string;
}

export interface TradesResponse {
  total: number;
  limit: number;
  offset: number;
  trades: Trade[];
}

export interface TradeStats {
  count: number;
  win_rate: number;
  expectancy: number;
  avg_win: number;
  avg_loss: number;
  profit_factor: number | null;
  gross_usd: number;
  net_count: number;
  net_win_rate: number | null;
  net_expectancy: number | null;
  net_avg_win: number | null;
  net_avg_loss: number | null;
  net_profit_factor: number | null;
  net_usd: number | null;
  legacy_count: number;
  incomplete_count: number;
}

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export function useTrades(
  params: { status?: string; exchange?: string; limit?: number; offset?: number } = {},
) {
  const q = new URLSearchParams();
  if (params.status) q.set('status', params.status);
  if (params.exchange) q.set('exchange', params.exchange);
  if (params.limit) q.set('limit', String(params.limit));
  if (params.offset) q.set('offset', String(params.offset));
  const url = `/api/trades${q.size ? '?' + q.toString() : ''}`;

  return useQuery({
    queryKey: ['trades', params],
    queryFn: () => fetchJSON<TradesResponse>(url),
    refetchInterval: 30_000,
    staleTime: 15_000,
    placeholderData: (prev) => prev,
  });
}

// Aggregate stats over the whole closed-trade set (optionally by exchange), computed
// server-side so they cover every trade, not just the current page of the list.
export function useTradeStats(params: { exchange?: string } = {}) {
  const q = new URLSearchParams();
  if (params.exchange) q.set('exchange', params.exchange);
  const url = `/api/trades/stats${q.size ? '?' + q.toString() : ''}`;

  return useQuery({
    queryKey: ['trades-stats', params],
    queryFn: () => fetchJSON<TradeStats>(url),
    refetchInterval: 30_000,
    staleTime: 15_000,
    placeholderData: (prev) => prev,
  });
}
