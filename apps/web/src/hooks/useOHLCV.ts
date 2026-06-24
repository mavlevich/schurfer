import { keepPreviousData, useQuery } from '@tanstack/react-query';
import type { OHLCVResponse } from '@/pages/pumps/types';

export const INTERVALS = [
  { label: '5m', range: 'last 24h', minutes: 5, limit: 288 },
  { label: '15m', range: 'last 48h', minutes: 15, limit: 192 },
  { label: '1h', range: 'last 8d', minutes: 60, limit: 200 },
  { label: '4h', range: 'last 30d', minutes: 240, limit: 180 },
] as const;

export type IntervalMinutes = (typeof INTERVALS)[number]['minutes'];

export function getInterval(minutes: number) {
  return INTERVALS.find((i) => i.minutes === minutes) ?? INTERVALS[1];
}

export function useOHLCV(base: string | undefined, minutes: number) {
  const iv = getInterval(minutes);
  return useQuery({
    queryKey: ['ohlcv', base, minutes],
    queryFn: async () => {
      const res = await fetch(
        `/api/pumps/${encodeURIComponent(base!)}/ohlcv?interval=${iv.minutes}&limit=${iv.limit}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<OHLCVResponse>;
    },
    enabled: !!base,
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
    retry: false,
  });
}
