import { keepPreviousData, useQuery } from '@tanstack/react-query';
import type { OHLCVResponse } from '@/pages/pumps/types';

export const INTERVALS = [
  { label: '5m', range: 'last 3d', minutes: 5, limit: 1000 },
  { label: '15m', range: 'last 10d', minutes: 15, limit: 1000 },
  { label: '1h', range: 'last 41d', minutes: 60, limit: 1000 },
  { label: '4h', range: 'last 166d', minutes: 240, limit: 1000 },
  { label: '1d', range: 'last 3y', minutes: 1440, limit: 1000 },
  { label: '1w', range: 'last 20y', minutes: 10080, limit: 1000 },
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
    retry: (_count, err) => !String(err).includes('HTTP 4'),
    retryDelay: 2000,
  });
}
