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
  pnl_usd: number | null;
  pnl_pct: number | null;
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
