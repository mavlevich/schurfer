import { describe, expect, it } from 'vitest';
import {
  bucketDecisions,
  buildChartMarkers,
  describeBucket,
  isOpenedAction,
} from './decisionMarkers';
import type { Decision } from '@/hooks/useDecisionsData';

function decision(ts: string, action: string, reason: string): Decision {
  return {
    id: 1,
    ts,
    base: 'AMEMECOIN',
    exchange: 'bingx',
    action,
    reason,
    score: 3,
    pump_pct: 42,
    price: 0.1,
  };
}

// 5m candles.
const CANDLES = [
  Date.parse('2026-09-06T19:15:00Z') / 1000,
  Date.parse('2026-09-06T19:20:00Z') / 1000,
  Date.parse('2026-09-06T19:25:00Z') / 1000,
];

describe('bucketDecisions', () => {
  it('folds many decisions into the candle they belong to', () => {
    // The scanner evaluates about once a minute, so one candle routinely holds
    // several decisions. One marker per decision would be unreadable, and
    // lightweight-charts drops duplicate times anyway.
    const buckets = bucketDecisions(
      [
        decision('2026-09-06T19:16:00Z', 'skipped', 'execution_instrument_unresolved'),
        decision('2026-09-06T19:17:00Z', 'skipped', 'execution_instrument_unresolved'),
        decision('2026-09-06T19:18:00Z', 'skipped', 'pump_below_entry_floor'),
        decision('2026-09-06T19:21:00Z', 'skipped', 'execution_instrument_unresolved'),
      ],
      CANDLES,
    );

    expect(buckets).toHaveLength(2);
    expect(buckets[0].time).toBe(CANDLES[0]);
    expect(buckets[0].count).toBe(3);
    expect(buckets[0].dominantReason).toBe('execution_instrument_unresolved');
    expect(buckets[0].reasons).toContain('pump_below_entry_floor');
    expect(buckets[1].count).toBe(1);
  });

  it('marks a candle as opened when anything opened in it', () => {
    const buckets = bucketDecisions(
      [
        decision('2026-09-06T19:16:00Z', 'skipped', 'pump_below_entry_floor'),
        decision('2026-09-06T19:17:00Z', 'opened_dry_run', 'paper trade'),
      ],
      CANDLES,
    );

    expect(buckets[0].opened).toBe(true);
  });

  it('returns buckets in ascending time order', () => {
    const buckets = bucketDecisions(
      [
        decision('2026-09-06T19:26:00Z', 'skipped', 'a'),
        decision('2026-09-06T19:16:00Z', 'skipped', 'b'),
        decision('2026-09-06T19:21:00Z', 'skipped', 'c'),
      ],
      CANDLES,
    );

    expect(buckets.map((b) => b.time)).toEqual([CANDLES[0], CANDLES[1], CANDLES[2]]);
  });

  it('drops a decision older than the first candle rather than guessing', () => {
    const buckets = bucketDecisions(
      [decision('2026-09-06T18:00:00Z', 'skipped', 'too_old')],
      CANDLES,
    );

    expect(buckets).toEqual([]);
  });

  it('drops an unparseable timestamp instead of throwing', () => {
    const buckets = bucketDecisions([decision('not-a-date', 'skipped', 'x')], CANDLES);
    expect(buckets).toEqual([]);
  });

  it('is empty when there are no candles to attach to', () => {
    expect(bucketDecisions([decision('2026-09-06T19:16:00Z', 'skipped', 'x')], [])).toEqual([]);
  });

  it('labels a missing reason rather than rendering an empty string', () => {
    const buckets = bucketDecisions([decision('2026-09-06T19:16:00Z', 'skipped', '')], CANDLES);
    expect(buckets[0].dominantReason).toBe('unknown');
  });
});

describe('isOpenedAction', () => {
  it('treats every opened variant as an entry', () => {
    expect(isOpenedAction('opened')).toBe(true);
    expect(isOpenedAction('opened_dry_run')).toBe(true);
    expect(isOpenedAction('skipped')).toBe(false);
  });
});

describe('describeBucket', () => {
  it('says what dominated the skips and how many evaluations there were', () => {
    const [bucket] = bucketDecisions(
      [
        decision('2026-09-06T19:16:00Z', 'skipped', 'execution_instrument_unresolved'),
        decision('2026-09-06T19:17:00Z', 'skipped', 'execution_instrument_unresolved'),
        decision('2026-09-06T19:18:00Z', 'skipped', 'pump_below_entry_floor'),
      ],
      CANDLES,
    );

    expect(describeBucket(bucket)).toBe(
      'Skipped: execution_instrument_unresolved (+1 other). 3 evaluations',
    );
  });

  it('reports an opened candle as opened', () => {
    const [bucket] = bucketDecisions(
      [decision('2026-09-06T19:16:00Z', 'opened_dry_run', 'paper trade')],
      CANDLES,
    );

    expect(describeBucket(bucket)).toBe('Opened. 1 evaluation');
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
      decisions: [decision('2026-09-06T19:16:00Z', 'skipped', 'momentum_too_low')],
      candleTimes: CANDLES,
    });

    expect(markers).toHaveLength(1);
    expect(markers[0].position).toBe('belowBar');
    expect(markers[0].title).toContain('momentum_too_low');
  });

  it('shows decision markers when episodes are undefined, not just empty', () => {
    const markers = buildChartMarkers({
      episodes: undefined,
      decisions: [decision('2026-09-06T19:16:00Z', 'skipped', 'x')],
      candleTimes: CANDLES,
    });

    expect(markers).toHaveLength(1);
  });

  it('returns an empty list when there is nothing to show', () => {
    // The caller applies the result unconditionally, so an empty list is what
    // clears the previous token's markers when switching tokens.
    expect(buildChartMarkers({ episodes: [], decisions: [], candleTimes: CANDLES })).toEqual([]);
    expect(
      buildChartMarkers({ episodes: undefined, decisions: undefined, candleTimes: CANDLES }),
    ).toEqual([]);
  });

  it('shows episode markers above the bar and decisions below it', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:16:00Z', 120)],
      decisions: [decision('2026-09-06T19:21:00Z', 'skipped', 'x')],
      candleTimes: CANDLES,
    });

    expect(markers.map((m) => m.position)).toEqual(['aboveBar', 'belowBar']);
  });

  it('lets an episode marker win a candle it shares with decisions', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:16:00Z', 120)],
      decisions: [decision('2026-09-06T19:17:00Z', 'skipped', 'x')],
      candleTimes: CANDLES,
    });

    expect(markers).toHaveLength(1);
    expect(markers[0].position).toBe('aboveBar');
  });

  it('returns markers in ascending time order', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T19:26:00Z', 50)],
      decisions: [decision('2026-09-06T19:16:00Z', 'skipped', 'x')],
      candleTimes: CANDLES,
    });

    expect(markers.map((m) => m.time)).toEqual([CANDLES[0], CANDLES[2]]);
  });

  it('drops an episode that predates every candle instead of pinning it to the first', () => {
    const markers = buildChartMarkers({
      episodes: [episode('2026-09-06T18:00:00Z', 50)],
      decisions: [],
      candleTimes: CANDLES,
    });

    expect(markers).toEqual([]);
  });
});
