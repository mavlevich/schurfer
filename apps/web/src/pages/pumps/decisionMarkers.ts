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

function nearestCandleTime(candleTimes: readonly number[], timestamp: number): number | null {
  if (candleTimes.length === 0) return null;
  // Candles are ascending; an event belongs to the last candle at or before it.
  // An event older than the first candle has no bucket rather than being
  // silently attached to it.
  if (timestamp < candleTimes[0]) return null;
  let chosen = candleTimes[0];
  for (const candleTime of candleTimes) {
    if (candleTime <= timestamp) chosen = candleTime;
    else break;
  }
  return chosen;
}

/**
 * Snap server buckets onto the candles actually drawn, and drop the ones that
 * fall outside them.
 *
 * The server buckets by the same interval the chart requested, so this is
 * normally an identity. It is not skipped for that reason: the candle series is
 * what the user sees, a bucket with no candle would be a marker floating over
 * nothing, and lightweight-charts rejects a marker whose time it does not have.
 */
export function alignBuckets(
  buckets: readonly DecisionBucket[],
  candleTimes: readonly number[],
): DecisionBucket[] {
  const byTime = new Map<number, DecisionBucket>();
  for (const bucket of buckets) {
    const time = nearestCandleTime(candleTimes, bucket.time);
    if (time === null) continue;
    const existing = byTime.get(time);
    if (!existing) {
      byTime.set(time, { ...bucket, time });
      continue;
    }
    // Two server buckets landing on one candle can only happen when the
    // requested interval and the drawn one disagree. Merge rather than drop:
    // losing half the evaluations silently is the failure mode this whole change
    // is about.
    byTime.set(time, {
      time,
      count: existing.count + bucket.count,
      opened: existing.opened || bucket.opened,
      dominant_reason:
        existing.count >= bucket.count ? existing.dominant_reason : bucket.dominant_reason,
      distinct_reasons: Math.max(existing.distinct_reasons, bucket.distinct_reasons),
    });
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time);
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
  visibility = ALL_MARKERS_VISIBLE,
}: {
  episodes: readonly TokenEpisode[] | undefined;
  buckets: readonly DecisionBucket[] | undefined;
  candleTimes: readonly number[];
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
          const time = nearestCandleTime(candleTimes, episode.first_seen_at);
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
    alignBuckets(buckets ?? [], candleTimes),
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
