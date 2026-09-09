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
  it('keeps buckets whose start is a candle being drawn', () => {
    expect(alignBuckets([bucket(0), bucket(2)], CANDLES).aligned.map((b) => b.time)).toEqual([
      CANDLES[0],
      CANDLES[2],
    ]);
  });

  it('does not fold a bucket into the previous candle', () => {
    // The defect: nearest-before matching moved a bucket that fell in a candle
    // the series does not have onto the candle before it, drawing the event
    // minutes early. Server buckets are interval starts, so anything that is not
    // one is a grid disagreement, not a candle to snap to.
    const between = { ...bucket(0), time: CANDLES[0] + 120 };
    const result = alignBuckets([between], CANDLES);
    expect(result.aligned).toEqual([]);
    expect(result.unaligned).toBe(1);
  });

  it('reports an opening in a missing candle instead of moving it', () => {
    // Minute candles at t=60 and t=180, with an opening in the absent t=120.
    // Drawing it at t=60 claims the entry happened a minute before it did.
    const minuteCandles = [60, 180];
    const opening: DecisionBucket = {
      time: 120,
      count: 1,
      opened: true,
      dominant_reason: 'entered',
      distinct_reasons: 1,
    };
    const result = alignBuckets([opening], minuteCandles);
    expect(result.aligned).toEqual([]);
    expect(result.unaligned).toBe(1);
  });

  it('drops a bucket older than the first candle rather than pinning it there', () => {
    const before = { ...bucket(0), time: CANDLES[0] - 600 };
    expect(alignBuckets([before], CANDLES).aligned).toEqual([]);
  });

  it('returns buckets in ascending time order', () => {
    expect(
      alignBuckets([bucket(2), bucket(0), bucket(1)], CANDLES).aligned.map((b) => b.time),
    ).toEqual(CANDLES);
  });

  it('is empty when there are no candles to attach to', () => {
    expect(alignBuckets([bucket(0)], []).aligned).toEqual([]);
  });
});

describe('episode markers respect the candle they fall in', () => {
  it('drops an episode that starts inside a candle the series does not have', () => {
    // Five-minute candles with 19:20 missing. An episode at 19:21 belongs to a
    // candle that is not drawn, and attaching it to 19:15 would claim it started
    // five minutes earlier than it did.
    const gapped = [CANDLES[0], CANDLES[2]];
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:21:00Z', 120)],
      buckets: [],
      candleTimes: gapped,
      intervalSeconds: 300,
    });
    expect(markers).toEqual([]);
  });

  it('keeps an episode inside the last candle', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:27:00Z', 120)],
      buckets: [],
      candleTimes: CANDLES,
      intervalSeconds: 300,
    });
    expect(markers.map((m) => m.time)).toEqual([CANDLES[2]]);
  });

  it('drops an episode past the right edge of the last candle', () => {
    // 19:31 is beyond the 19:25 candle's five minutes. The series simply does
    // not cover it yet.
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:31:00Z', 120)],
      buckets: [],
      candleTimes: CANDLES,
      intervalSeconds: 300,
    });
    expect(markers).toEqual([]);
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
      intervalSeconds: 300,
    });

    expect(markers).toHaveLength(1);
    expect(markers[0].position).toBe('belowBar');
  });

  it('shows decision markers when episodes are undefined, not just empty', () => {
    const markers = buildChartMarkers({
      episodes: undefined,
      buckets: [bucket(0)],
      candleTimes: CANDLES,
      intervalSeconds: 300,
    });

    expect(markers).toHaveLength(1);
  });

  it('returns an empty list when there is nothing to show', () => {
    // The caller applies the result unconditionally, so an empty list is what
    // clears the previous token's markers when switching tokens.
    expect(
      buildChartMarkers({ episodes: [], buckets: [], candleTimes: CANDLES, intervalSeconds: 300 }),
    ).toEqual([]);
    expect(
      buildChartMarkers({
        episodes: undefined,
        buckets: undefined,
        candleTimes: CANDLES,
        intervalSeconds: 300,
      }),
    ).toEqual([]);
  });

  it('shows episode markers above the bar and decisions below it', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:16:00Z', 120)],
      buckets: [bucket(1)],
      candleTimes: CANDLES,
      intervalSeconds: 300,
    });

    expect(markers.map((m) => m.position)).toEqual(['aboveBar', 'belowBar']);
  });

  it('lets an episode marker win a candle it shares with decisions', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:16:00Z', 120)],
      buckets: [bucket(0)],
      candleTimes: CANDLES,
      intervalSeconds: 300,
    });

    expect(markers).toHaveLength(1);
    expect(markers[0].position).toBe('aboveBar');
  });

  it('returns markers in ascending time order', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:26:00Z', 50)],
      buckets: [bucket(0)],
      candleTimes: CANDLES,
      intervalSeconds: 300,
    });

    expect(markers.map((m) => m.time)).toEqual([CANDLES[0], CANDLES[2]]);
  });

  it('drops an episode that predates every candle instead of pinning it to the first', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T18:00:00Z', 50)],
      buckets: [],
      candleTimes: CANDLES,
      intervalSeconds: 300,
    });

    expect(markers).toEqual([]);
  });

  it('shows the evaluation count on a skip marker and nothing on an entry', () => {
    const markers = buildChartMarkers({
      episodes: [],
      buckets: [bucket(0, { count: 14 }), bucket(1, { opened: true })],
      candleTimes: CANDLES,
      intervalSeconds: 300,
    });

    expect(markers[0].text).toBe('14');
    expect(markers[1].text).toBeUndefined();
  });

  describe('legend visibility', () => {
    const everything = {
      episodes: [episode('2026-09-06T19:16:00Z', 120)],
      buckets: [bucket(1, { opened: true }), bucket(2, { reason: 'x' })],
      candleTimes: CANDLES,
      intervalSeconds: 300,
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
