import { useQuery } from '@tanstack/react-query';
import type { TokenResponse, TokenEpisode, SignalsResponse, TokenStats } from '@/pages/pumps/types';

const MIN = 60_000;

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

async function fetchNullable<T>(url: string): Promise<T | null> {
  const res = await fetch(url);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export function useToken(base: string | undefined) {
  return useQuery({
    queryKey: ['token', base],
    queryFn: () => fetchNullable<TokenResponse>(`/api/pumps/${encodeURIComponent(base!)}`),
    enabled: !!base,
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry: false,
  });
}

export function useTokenEpisodes(base: string | undefined) {
  return useQuery({
    queryKey: ['episodes', base],
    queryFn: () => fetchJSON<TokenEpisode[]>(`/api/pumps/${encodeURIComponent(base!)}/history`),
    enabled: !!base,
    staleTime: 5 * MIN,
  });
}

export function useTokenSignals(base: string | undefined) {
  return useQuery({
    queryKey: ['signals', base],
    queryFn: () =>
      fetchNullable<SignalsResponse>(`/api/pumps/${encodeURIComponent(base!)}/signals`),
    enabled: !!base,
    staleTime: 2 * MIN,
    refetchInterval: 60_000,
    retry: false,
  });
}

export function useTokenStats(base: string | undefined) {
  return useQuery({
    queryKey: ['stats', base],
    queryFn: () => fetchNullable<TokenStats>(`/api/pumps/${encodeURIComponent(base!)}/stats`),
    enabled: !!base,
    staleTime: 10 * MIN,
    retry: false,
  });
}
