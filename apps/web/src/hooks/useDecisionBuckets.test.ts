import { describe, expect, it } from 'vitest';
import {
  keepWithinIdentity,
  type DecisionBucketsKey,
  type DecisionBucketsResponse,
} from './useDecisionBuckets';

const PREVIOUS: DecisionBucketsResponse = {
  bucket_seconds: 900,
  truncated: false,
  buckets: [
    { time: 1757200000, count: 3, opened: true, dominant_reason: 'entered', distinct_reasons: 1 },
  ],
};

function query(base: string | undefined, bucketSeconds: number, since = 1, until = 2) {
  return {
    queryKey: ['decision-buckets', base, bucketSeconds, since, until] as DecisionBucketsKey,
  };
}

describe('keepWithinIdentity', () => {
  it('keeps the previous answer when only the window moved', () => {
    // The case keepPreviousData exists for: the same token at the same interval,
    // one minute later. Dropping it here would blink every marker off the chart
    // once a minute.
    const keep = keepWithinIdentity('CZ', 900);
    expect(keep(PREVIOUS, query('CZ', 900, 5, 6))).toBe(PREVIOUS);
  });

  it('refuses to carry another token’s buckets', () => {
    // The defect. With the new token's candles already loaded and its decisions
    // still in flight, the previous token's entries were drawn over the new
    // token's price.
    const keep = keepWithinIdentity('BEAT', 900);
    expect(keep(PREVIOUS, query('CZ', 900))).toBeUndefined();
  });

  it('refuses to carry buckets from another interval', () => {
    // Bucket starts from a 15m grid do not land on 5m candles, so every one of
    // them would be reported as outside the drawn candles.
    const keep = keepWithinIdentity('CZ', 300);
    expect(keep(PREVIOUS, query('CZ', 900))).toBeUndefined();
  });

  it('has nothing to keep on the first query', () => {
    expect(keepWithinIdentity('CZ', 900)(undefined, undefined)).toBeUndefined();
  });
});
