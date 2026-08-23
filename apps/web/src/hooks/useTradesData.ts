import { useQuery } from '@tanstack/react-query';

// origin distinguishes the pump-short strategy's own live/dry-run execution
// ledger (already-promoted, app.trades) from momentum_flow's own WATCH->paper
// discovery instrumentation (app.momentum_flow_paper_probes) -- the two never
// share a table (see docs/research/momentum-flow-paper-v1.md), only this API's
// own read-time UNION presents them together. Always show origin visibly next
// to a trade; a momentum_flow_paper row is a research probe, not promotion
// evidence, and must never look like an already-vetted live trade.

export interface Trade {
  id: string;
  origin: string;
  strategy_key: string;
  strategy_name: string;
  strategy_version: string;
  mode: string;
  exit_reason: string;
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
  params: {
    status?: string;
    exchange?: string;
    origin?: string;
    limit?: number;
    strategy?: string;
    mode?: string;
    side?: string;
    offset?: number;
  } = {},
) {
  const q = new URLSearchParams();
  if (params.status) q.set('status', params.status);
  if (params.exchange) q.set('exchange', params.exchange);
  if (params.origin) q.set('origin', params.origin);
  if (params.strategy) q.set('strategy', params.strategy);
  if (params.mode) q.set('mode', params.mode);
  if (params.side) q.set('side', params.side);
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
export function useTradeStats(
  params: {
    exchange?: string;
    origin?: string;
    strategy?: string;
    mode?: string;
    side?: string;
  } = {},
) {
  const q = new URLSearchParams();
  if (params.exchange) q.set('exchange', params.exchange);
  if (params.origin) q.set('origin', params.origin);
  if (params.strategy) q.set('strategy', params.strategy);
  if (params.mode) q.set('mode', params.mode);
  if (params.side) q.set('side', params.side);
  const url = `/api/trades/stats${q.size ? '?' + q.toString() : ''}`;

  return useQuery({
    queryKey: ['trades-stats', params],
    queryFn: () => fetchJSON<TradeStats>(url),
    refetchInterval: 30_000,
    staleTime: 15_000,
    placeholderData: (prev) => prev,
  });
}
