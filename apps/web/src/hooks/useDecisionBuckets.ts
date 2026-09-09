import { keepPreviousData, useQuery } from '@tanstack/react-query';

// One row per candle, aggregated in SQL. The chart used to fetch raw decisions
// and fold them client-side, which could not work: /api/decisions orders by
// ts DESC and caps at 200, so a token with 2210 skips got its most recent 200 and
// the older half of the visible candles had no markers at all -- with nothing on
// screen saying so.
export interface DecisionBucket {
  /** Candle opening time, seconds since epoch. */
  time: number;
  /** How many decisions fell into this candle. */
  count: number;
  /** True when at least one of them opened something, live or paper. */
  opened: boolean;
  /** The most frequent reason in this candle. */
  dominant_reason: string;
  /** How many distinct reasons appeared here. */
  distinct_reasons: number;
}

export interface DecisionBucketsResponse {
  bucket_seconds: number;
  /** True when the server had more candles than it would return. */
  truncated: boolean;
  buckets: DecisionBucket[];
}

interface UseDecisionBucketsParams {
  base: string | undefined;
  /** Candle interval in minutes, so buckets line up with the candles drawn. */
  intervalMinutes: number;
  /** Inclusive lower bound, seconds since epoch. Usually the first candle. */
  sinceSeconds: number | undefined;
  /** Exclusive upper bound, seconds since epoch. */
  untilSeconds: number | undefined;
}

export function useDecisionBuckets({
  base,
  intervalMinutes,
  sinceSeconds,
  untilSeconds,
}: UseDecisionBucketsParams) {
  const bucketSeconds = intervalMinutes * 60;
  return useQuery<DecisionBucketsResponse>({
    queryKey: ['decision-buckets', base, bucketSeconds, sinceSeconds, untilSeconds],
    queryFn: async () => {
      const params = new URLSearchParams({
        base: base!,
        bucket_seconds: String(bucketSeconds),
      });
      if (sinceSeconds !== undefined) {
        params.set('since', new Date(sinceSeconds * 1000).toISOString());
      }
      if (untilSeconds !== undefined) {
        params.set('until', new Date(untilSeconds * 1000).toISOString());
      }
      const res = await fetch(`/api/decisions/buckets?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<DecisionBucketsResponse>;
    },
    // Waiting for the candles is deliberate: without the window this would ask
    // for every decision the token ever had, which is the request this endpoint
    // exists to avoid.
    enabled: !!base && sinceSeconds !== undefined && untilSeconds !== undefined,
    staleTime: 30_000,
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
  });
}
