import type { DecisionBucket } from '@/hooks/useDecisionBuckets';
import type { TokenEpisode } from './types';

// Decisions are far denser than candles: the scanner evaluates a live pump about
// once a minute, so a single token can carry thousands of them (CZ has 2210
// `pump_below_entry_floor` skips alone) against a handful of 5m candles. They
// arrive already folded into one bucket per candle by /api/decisions/buckets,
// because folding them here required fetching them all, and the list endpoint
// orders by ts DESC and caps at 200.
//
// Even one marker per candle is too many to read. A grey dot on every candle of a
// thirteen-hour skip streak says nothing that one dot at the start of the streak
// does not, and it buries the candles themselves. So a decision marker is drawn
// only where something changed: the first candle of a streak, any candle where
// the dominant reason changed, and every candle where something was opened.
//
// The information that used to be discarded is now reachable. `describeBucket`
// fed a `title` field that lightweight-charts v5's SeriesMarker does not have, so
// every tooltip string was computed and thrown away behind an `as unknown as`
// cast. The chart renders its own tooltip from the buckets instead.

export type { DecisionBucket };

/**
 * The candle an event at `timestamp` belongs to, or null if no candle covers it.
 *
 * Both edges are checked. Without the right edge, a timestamp falling in a candle
 * the series does not have -- a gap in the data, a halted market -- was attached
 * to the last candle before the gap, drawing the event minutes or hours before it
 * happened. A missing candle is missing coverage, and the honest rendering of
 * missing coverage is nothing.
 *
 * `intervalSeconds` is passed rather than inferred from the gaps between
 * candles, because it cannot be inferred: a five-minute series with one candle
 * missing is indistinguishable from a ten-minute series, and guessing wrong
 * turns the gap check back into the bug it replaces. The caller knows the
 * interval; it asked for it.
 */
function coveringCandleTime(
  candleTimes: readonly number[],
  timestamp: number,
  intervalSeconds: number,
): number | null {
  if (candleTimes.length === 0 || intervalSeconds <= 0) return null;
  if (timestamp < candleTimes[0]) return null;
  let index = -1;
  for (let position = 0; position < candleTimes.length; position += 1) {
    if (candleTimes[position] <= timestamp) index = position;
    else break;
  }
  if (index < 0) return null;
  const chosen = candleTimes[index];
  return timestamp < chosen + intervalSeconds ? chosen : null;
}

export interface AlignedBuckets {
  /** Buckets whose time is a candle the chart is drawing. */
  aligned: DecisionBucket[];
  /** Buckets with no such candle. Reported, never folded into a neighbour. */
  unaligned: number;
}

/**
 * Match server buckets to the candles actually drawn.
 *
 * **Exact match on the bucket's start**, not nearest-before. The server groups by
 * the interval the chart asked for, so a bucket that does not land on a candle
 * means the two grids disagree or the candle is missing -- and in both cases the
 * honest answer is that the chart has no place to draw it. Folding it into the
 * previous candle would report evaluations at a time they did not happen, and
 * re-aggregating two buckets into one cannot be done correctly from what the
 * server sends: the mode of a union is not the mode of its larger half, and
 * distinct reasons do not combine by taking a maximum.
 */
export function alignBuckets(
  buckets: readonly DecisionBucket[],
  candleTimes: readonly number[],
): AlignedBuckets {
  const drawn = new Set(candleTimes);
  const aligned: DecisionBucket[] = [];
  let unaligned = 0;
  for (const bucket of buckets) {
    if (drawn.has(bucket.time)) aligned.push(bucket);
    else unaligned += 1;
  }
  aligned.sort((a, b) => a.time - b.time);
  return { aligned, unaligned };
}

/**
 * Which buckets are worth a marker.
 *
 * Every opened bucket, and every bucket whose dominant reason differs from the
 * previous drawn one. A streak of identical skips collapses to its first candle,
 * which is the candle that carries the information; the rest are still counted in
 * the tooltip of the candle the user hovers.
 */
export function selectNotableBuckets(buckets: readonly DecisionBucket[]): DecisionBucket[] {
  const notable: DecisionBucket[] = [];
  let previousReason: string | null = null;
  for (const bucket of buckets) {
    if (bucket.opened) {
      notable.push(bucket);
      // An opened candle does not establish a skip reason, so the next skip is
      // judged against the reason before it rather than against nothing.
      continue;
    }
    if (bucket.dominant_reason !== previousReason) {
      notable.push(bucket);
      previousReason = bucket.dominant_reason;
    }
  }
  return notable;
}

/** One-line summary for a marker tooltip. */
export function describeBucket(bucket: DecisionBucket): string {
  const evaluations = `${bucket.count} evaluation${bucket.count === 1 ? '' : 's'}`;
  if (bucket.opened) return `Opened. ${evaluations}`;
  const extra = bucket.distinct_reasons > 1 ? ` (+${bucket.distinct_reasons - 1} other)` : '';
  const reason = bucket.dominant_reason || 'unknown';
  return `Skipped: ${reason}${extra}. ${evaluations}`;
}

/** Minimal marker shape, kept free of the charting library so this stays pure. */
export interface ChartMarker {
  time: number;
  position: 'aboveBar' | 'belowBar';
  color: string;
  shape: 'circle' | 'arrowUp';
  size: number;
  text?: string;
}

function peakColor(pct: number): string {
  if (pct >= 100) return '#f87171';
  if (pct >= 50) return '#fb923c';
  return '#facc15';
}

/** Which marker groups the legend can turn off. */
export interface MarkerVisibility {
  episodes: boolean;
  opened: boolean;
  skipped: boolean;
}

export const ALL_MARKERS_VISIBLE: MarkerVisibility = {
  episodes: true,
  opened: true,
  skipped: true,
};

/**
 * Every marker the token chart shows, deduplicated and time-ordered.
 *
 * Pure on purpose. This used to live inside the chart effect, nested in an
 * `if (episodes.length > 0)`, which silently made decision markers depend on a
 * token having pump episodes -- the exact case the feature exists for is a
 * token that was evaluated and never opened anything, which often has no
 * episode at all (colleague review). Returning a complete list, empty included,
 * also lets the caller apply it unconditionally, so switching to a token with
 * nothing to show clears the previous token's markers instead of leaving them
 * floating over the new candles.
 */
export function buildChartMarkers({
  episodes,
  buckets,
  candleTimes,
  intervalSeconds,
  visibility = ALL_MARKERS_VISIBLE,
}: {
  episodes: readonly TokenEpisode[] | undefined;
  buckets: readonly DecisionBucket[] | undefined;
  candleTimes: readonly number[];
  /** The candle interval the chart is drawing, in seconds. */
  intervalSeconds: number;
  visibility?: MarkerVisibility;
}): ChartMarker[] {
  const episodeMarkers: ChartMarker[] = !visibility.episodes
    ? []
    : (episodes ?? [])
        .filter((episode) => episode.first_seen_at)
        .map((episode): ChartMarker | null => {
          // Same rule as decisions, deliberately: an episode that started before
          // the first visible candle is dropped rather than pinned to it. Drawing
          // it at the window's left edge would claim it happened at a time it did
          // not, and TokenEpisodes already lists every episode in full, so
          // nothing is lost by leaving it off the chart.
          const time = coveringCandleTime(candleTimes, episode.first_seen_at, intervalSeconds);
          if (time === null) return null;
          return {
            time,
            position: 'aboveBar',
            color: peakColor(episode.observed_peak_pct),
            shape: 'circle',
            size: 1,
          };
        })
        .filter((marker): marker is ChartMarker => marker !== null);

  const decisionMarkers: ChartMarker[] = selectNotableBuckets(
    alignBuckets(buckets ?? [], candleTimes).aligned,
  )
    .filter((bucket) => (bucket.opened ? visibility.opened : visibility.skipped))
    .map((bucket) => ({
      time: bucket.time,
      position: 'belowBar' as const,
      color: bucket.opened ? '#34d399' : '#64748b',
      shape: bucket.opened ? ('arrowUp' as const) : ('circle' as const),
      size: 1,
      text: bucket.opened ? undefined : String(bucket.count),
    }));

  // lightweight-charts rejects duplicate times. Episode markers are listed
  // first and therefore win a tie: an episode start is the rarer event, and the
  // decisions of that candle are still reachable through the tooltip.
  const seen = new Set<number>();
  const merged: ChartMarker[] = [];
  for (const marker of [...episodeMarkers, ...decisionMarkers]) {
    if (seen.has(marker.time)) continue;
    seen.add(marker.time);
    merged.push(marker);
  }
  return merged.sort((a, b) => a.time - b.time);
}
