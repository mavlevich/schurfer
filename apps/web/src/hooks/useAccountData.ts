import { useQuery } from '@tanstack/react-query';

export interface Balance {
  exchange: string;
  wallet: string;
  asset: string;
  tradeable: boolean;
  free: number;
  used: number;
  total: number;
  usd_value: number;
}

export interface BalanceData {
  balances: Balance[];
  total_usd: number;
  total_usd_all: number;
  failed_exchanges?: string[];
}

export interface Position {
  exchange: string;
  symbol: string;
  base: string;
  side: string;
  size_usd: number;
  entry_price: number;
  unrealized_pnl: number;
  leverage: number;
  liquidation_price: number | null;
}

export interface PositionsData {
  positions: Position[];
  count: number;
}

export interface RiskData {
  trading_enabled: boolean;
  open_positions: number;
  max_positions: number;
  slots_free: number;
  daily_pnl_usd: number;
  daily_loss_limit_usd: number;
}

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

const ACCOUNT_OPTIONS = {
  refetchInterval: 15_000,
  retry: false,
} as const;

export function useBalance() {
  return useQuery({
    queryKey: ['account', 'balance'],
    queryFn: () => fetchJSON<BalanceData>('/api/account/balance'),
    ...ACCOUNT_OPTIONS,
  });
}

export function usePositions() {
  return useQuery({
    queryKey: ['account', 'positions'],
    queryFn: () => fetchJSON<PositionsData>('/api/account/positions'),
    ...ACCOUNT_OPTIONS,
  });
}

export function useRisk() {
  return useQuery({
    queryKey: ['account', 'risk'],
    queryFn: () => fetchJSON<RiskData>('/api/account/risk'),
    ...ACCOUNT_OPTIONS,
  });
}
