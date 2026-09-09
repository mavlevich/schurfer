import { describe, expect, it } from 'vitest';
import {
  alignBuckets,
  buildChartMarkers,
  describeBucket,
  selectNotableBuckets,
} from './decisionMarkers';
import type { DecisionBucket } from '@/hooks/useDecisionBuckets';

// 5m candles.
const CANDLES = [
  Date.parse('2026-09-06T19:15:00Z') / 1000,
  Date.parse('2026-09-06T19:20:00Z') / 1000,
  Date.parse('2026-09-06T19:25:00Z') / 1000,
];

function bucket(
  candleIndex: number,
  {
    count = 1,
    opened = false,
    reason = 'momentum_too_low',
    distinct = 1,
  }: { count?: number; opened?: boolean; reason?: string; distinct?: number } = {},
): DecisionBucket {
  return {
    time: CANDLES[candleIndex],
    count,
    opened,
    dominant_reason: reason,
    distinct_reasons: distinct,
  };
}

describe('alignBuckets', () => {
  it('keeps buckets that land on a candle', () => {
    expect(alignBuckets([bucket(0), bucket(2)], CANDLES).map((b) => b.time)).toEqual([
      CANDLES[0],
      CANDLES[2],
    ]);
  });

  it('snaps a bucket inside a candle to that candle', () => {
    const inside = { ...bucket(0), time: CANDLES[0] + 120 };
    expect(alignBuckets([inside], CANDLES)[0].time).toBe(CANDLES[0]);
  });

  it('drops a bucket older than the first candle rather than pinning it there', () => {
    // Drawing it at the window's left edge would claim it happened at a time it
    // did not.
    const before = { ...bucket(0), time: CANDLES[0] - 600 };
    expect(alignBuckets([before], CANDLES)).toEqual([]);
  });

  it('merges rather than drops when two buckets land on one candle', () => {
    // Only possible when the requested interval and the drawn one disagree.
    // Losing half the evaluations silently is the failure this whole change is
    // about.
    const first = { ...bucket(0), count: 3, dominant_reason: 'a', distinct_reasons: 1 };
    const second = { ...bucket(0), time: CANDLES[0] + 60, count: 9, dominant_reason: 'b' };
    const merged = alignBuckets([first, second], CANDLES);

    expect(merged).toHaveLength(1);
    expect(merged[0].count).toBe(12);
    // The larger side's reason wins, because it is the one that dominated.
    expect(merged[0].dominant_reason).toBe('b');
  });

  it('marks a merged candle as opened when either side opened', () => {
    const skip = { ...bucket(0), opened: false };
    const open = { ...bucket(0), time: CANDLES[0] + 60, opened: true };
    expect(alignBuckets([skip, open], CANDLES)[0].opened).toBe(true);
  });

  it('returns buckets in ascending time order', () => {
    expect(alignBuckets([bucket(2), bucket(0), bucket(1)], CANDLES).map((b) => b.time)).toEqual(
      CANDLES,
    );
  });

  it('is empty when there are no candles to attach to', () => {
    expect(alignBuckets([bucket(0)], [])).toEqual([]);
  });
});

describe('selectNotableBuckets', () => {
  it('collapses a streak of identical skips to its first candle', () => {
    // A grey dot on every candle of a thirteen-hour skip streak says nothing the
    // first dot does not, and it buries the candles themselves.
    const streak = [
      bucket(0, { reason: 'execution_instrument_unresolved' }),
      bucket(1, { reason: 'execution_instrument_unresolved' }),
      bucket(2, { reason: 'execution_instrument_unresolved' }),
    ];
    expect(selectNotableBuckets(streak).map((b) => b.time)).toEqual([CANDLES[0]]);
  });

  it('marks the candle where the dominant reason changed', () => {
    // This is the whole point: production evidence for ENG-031 was thirteen
    // hours of execution_instrument_unresolved across a +480% move, and the
    // moment it started is the readable fact.
    const buckets = [
      bucket(0, { reason: 'pump_below_entry_floor' }),
      bucket(1, { reason: 'execution_instrument_unresolved' }),
      bucket(2, { reason: 'execution_instrument_unresolved' }),
    ];
    expect(selectNotableBuckets(buckets).map((b) => b.time)).toEqual([CANDLES[0], CANDLES[1]]);
  });

  it('never hides a candle where something opened', () => {
    const buckets = [
      bucket(0, { reason: 'x' }),
      bucket(1, { reason: 'x', opened: true }),
      bucket(2, { reason: 'x' }),
    ];
    expect(selectNotableBuckets(buckets).map((b) => b.opened)).toEqual([false, true]);
  });

  it('judges a skip after an opened candle against the reason before it', () => {
    // An opened candle establishes no skip reason, so a streak interrupted by an
    // entry does not get a second dot for the same reason.
    const buckets = [
      bucket(0, { reason: 'x' }),
      bucket(1, { reason: 'ignored', opened: true }),
      bucket(2, { reason: 'x' }),
    ];
    expect(selectNotableBuckets(buckets).map((b) => b.time)).toEqual([CANDLES[0], CANDLES[1]]);
  });
});

describe('describeBucket', () => {
  it('says what dominated the skips and how many evaluations there were', () => {
    expect(describeBucket(bucket(0, { count: 14, reason: 'momentum_too_low', distinct: 3 }))).toBe(
      'Skipped: momentum_too_low (+2 other). 14 evaluations',
    );
  });

  it('reports an opened candle as opened', () => {
    expect(describeBucket(bucket(0, { opened: true }))).toBe('Opened. 1 evaluation');
  });

  it('labels a missing reason rather than rendering an empty string', () => {
    expect(describeBucket(bucket(0, { reason: '' }))).toContain('unknown');
  });
});

function episode(firstSeenAt: string, peakPct: number) {
  return {
    id: 1,
    base: 'AMEMECOIN',
    first_seen_at: Date.parse(firstSeenAt) / 1000,
    last_seen_at: Date.parse(firstSeenAt) / 1000,
    observed_peak_pct: peakPct,
  } as never;
}

describe('buildChartMarkers', () => {
  it('shows decision markers for a token with no episodes at all', () => {
    // The case the feature exists for: the token was evaluated repeatedly and
    // nothing ever opened, so it has no pump episode. Nesting this logic under
    // `if (episodes.length > 0)` silently hid exactly these tokens
    // (colleague review).
    const markers = buildChartMarkers({
      episodes: [],
      buckets: [bucket(0, { reason: 'momentum_too_low' })],
      candleTimes: CANDLES,
    });

    expect(markers).toHaveLength(1);
    expect(markers[0].position).toBe('belowBar');
  });

  it('shows decision markers when episodes are undefined, not just empty', () => {
    const markers = buildChartMarkers({
      episodes: undefined,
      buckets: [bucket(0)],
      candleTimes: CANDLES,
    });

    expect(markers).toHaveLength(1);
  });

  it('returns an empty list when there is nothing to show', () => {
    // The caller applies the result unconditionally, so an empty list is what
    // clears the previous token's markers when switching tokens.
    expect(buildChartMarkers({ episodes: [], buckets: [], candleTimes: CANDLES })).toEqual([]);
    expect(
      buildChartMarkers({ episodes: undefined, buckets: undefined, candleTimes: CANDLES }),
    ).toEqual([]);
  });

  it('shows episode markers above the bar and decisions below it', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:16:00Z', 120)],
      buckets: [bucket(1)],
      candleTimes: CANDLES,
    });

    expect(markers.map((m) => m.position)).toEqual(['aboveBar', 'belowBar']);
  });

  it('lets an episode marker win a candle it shares with decisions', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:16:00Z', 120)],
      buckets: [bucket(0)],
      candleTimes: CANDLES,
    });

    expect(markers).toHaveLength(1);
    expect(markers[0].position).toBe('aboveBar');
  });

  it('returns markers in ascending time order', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:26:00Z', 50)],
      buckets: [bucket(0)],
      candleTimes: CANDLES,
    });

    expect(markers.map((m) => m.time)).toEqual([CANDLES[0], CANDLES[2]]);
  });

  it('drops an episode that predates every candle instead of pinning it to the first', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T18:00:00Z', 50)],
      buckets: [],
      candleTimes: CANDLES,
    });

    expect(markers).toEqual([]);
  });

  it('shows the evaluation count on a skip marker and nothing on an entry', () => {
    const markers = buildChartMarkers({
      episodes: [],
      buckets: [bucket(0, { count: 14 }), bucket(1, { opened: true })],
      candleTimes: CANDLES,
    });

    expect(markers[0].text).toBe('14');
    expect(markers[1].text).toBeUndefined();
  });

  describe('legend visibility', () => {
    const everything = {
      episodes: [episode('2026-09-06T19:16:00Z', 120)],
      buckets: [bucket(1, { opened: true }), bucket(2, { reason: 'x' })],
      candleTimes: CANDLES,
    };

    it('hides pump starts when unchecked', () => {
      const markers = buildChartMarkers({
        ...everything,
        visibility: { episodes: false, opened: true, skipped: true },
      });
      expect(markers.every((m) => m.position === 'belowBar')).toBe(true);
    });

    it('hides entries when unchecked', () => {
      const markers = buildChartMarkers({
        ...everything,
        visibility: { episodes: true, opened: false, skipped: true },
      });
      expect(markers.some((m) => m.shape === 'arrowUp')).toBe(false);
    });

    it('hides skips when unchecked but keeps entries', () => {
      const markers = buildChartMarkers({
        ...everything,
        visibility: { episodes: true, opened: true, skipped: false },
      });
      expect(markers.filter((m) => m.position === 'belowBar')).toHaveLength(1);
      expect(markers.find((m) => m.position === 'belowBar')?.shape).toBe('arrowUp');
    });

    it('shows everything by default', () => {
      expect(buildChartMarkers(everything)).toHaveLength(3);
    });
  });
});
