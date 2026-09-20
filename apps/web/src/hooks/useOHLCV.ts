import { useQuery } from '@tanstack/react-query';
import type { OHLCVResponse } from '@/pages/pumps/types';

type OHLCVKey = readonly ['ohlcv', string | undefined, number, string];

// Keep the previous candles only while they answer the SAME question. A plain
// keepPreviousData carried the last token's (or source's) candles across a switch
// and the chart drew them until the new response arrived, so B2's Binance candles
// could briefly appear under another token, or the old route's candles under a
// freshly picked source. Same token + interval + source is a refresh and keeps its
// candles; changing any of them clears the chart until the real answer arrives.
// (Mirrors keepWithinIdentity in useDecisionBuckets, which closed the same defect
// for decision markers.)
export function keepOHLCVWithinIdentity(
  base: string | undefined,
  minutes: number,
  exchange: string,
) {
  return (
    previous: OHLCVResponse | undefined,
    previousQuery?: { queryKey: readonly unknown[] },
  ): OHLCVResponse | undefined => {
    const key = previousQuery?.queryKey as OHLCVKey | undefined;
    if (!key || key[1] !== base || key[2] !== minutes || key[3] !== exchange) return undefined;
    return previous;
  };
}

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

// useOHLCV fetches candles for base at the given interval. Pass exchange to pin a
// specific source (the user picked it from the response's `sources`); omit it to
// let the server resolve the deterministic default (real futures route first).
export function useOHLCV(base: string | undefined, minutes: number, exchange?: string) {
  const iv = getInterval(minutes);
  const exchangeKey = exchange ?? '';
  return useQuery({
    queryKey: ['ohlcv', base, minutes, exchangeKey],
    queryFn: async () => {
      const params = new URLSearchParams({
        interval: String(iv.minutes),
        limit: String(iv.limit),
      });
      if (exchange) params.set('exchange', exchange);
      const res = await fetch(`/api/pumps/${encodeURIComponent(base!)}/ohlcv?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<OHLCVResponse>;
    },
    enabled: !!base,
    staleTime: 5 * 60_000,
    // Previous candles are kept only while token, interval and source are unchanged
    // (see keepOHLCVWithinIdentity); any change clears the chart until the new data.
    placeholderData: keepOHLCVWithinIdentity(base, minutes, exchangeKey),
    retry: (_count, err) => !String(err).includes('HTTP 4'),
    retryDelay: 2000,
  });
}
