import { useQuery } from '@tanstack/react-query';
import type { PumpsResponse, HistoryEntry, MomentumWatchResponse } from '@/pages/pumps/types';

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export function usePumps() {
  return useQuery({
    queryKey: ['pumps'],
    queryFn: () => fetchJSON<PumpsResponse>('/api/pumps'),
    refetchInterval: 60_000,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });
}

export function usePumpsHistory() {
  return useQuery({
    queryKey: ['pumps', 'history'],
    queryFn: () => fetchJSON<HistoryEntry[]>('/api/pumps/history'),
    refetchInterval: 60_000,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });
}

// useMomentumWatch fetches every currently-active momentum_flow WATCH episode
// -- the prospective-long counterpart of usePumps(), sourced from a different
// signal (60m return / OI growth / flow imbalance, not 24h % change).
export function useMomentumWatch() {
  return useQuery({
    queryKey: ['pumps', 'momentum-watch'],
    queryFn: () => fetchJSON<MomentumWatchResponse>('/api/pumps/momentum-watch'),
    refetchInterval: 60_000,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });
}
