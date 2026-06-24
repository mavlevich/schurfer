import { useQuery } from '@tanstack/react-query';
import type { PumpsResponse, HistoryEntry } from '@/pages/pumps/types';

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
