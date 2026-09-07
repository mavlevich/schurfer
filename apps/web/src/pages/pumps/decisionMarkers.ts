import type { Decision } from '@/hooks/useDecisionsData';
import type { TokenEpisode } from './types';

// Decisions are far denser than candles: the scanner evaluates a live pump about
// once a minute, so a single token can carry thousands of them (CZ has 2210
// `pump_below_entry_floor` skips alone) against a handful of 5m candles. Drawing
// one marker per decision is unreadable and, since lightweight-charts rejects
// duplicate marker times, most of them would be dropped arbitrarily anyway.
//
// So decisions are folded into the candle they belong to, and each candle gets
// at most one marker that says what happened there: whether anything was opened,
// how many evaluations there were, and which reason dominated the skips. That
// last part is the point -- production evidence for ENG-031 came from reading
// exactly this, thirteen hours of `execution_instrument_unresolved` across a
// +480% move, and it was only visible by querying the database by hand.

export interface DecisionBucket {
  /** Candle time the decisions belong to, seconds since epoch. */
  time: number;
  /** True when at least one decision in this bucket actually opened something. */
  opened: boolean;
  /** How many decisions fell into this candle. */
  count: number;
  /** The reason that occurred most often, ties broken by first occurrence. */
  dominantReason: string;
  /** Distinct reasons seen here, for the tooltip. */
  reasons: string[];
}

/** An action is an entry when it opened anything, live or paper. */
export function isOpenedAction(action: string): boolean {
  return action.startsWith('opened');
}

function nearestCandleTime(candleTimes: readonly number[], timestamp: number): number | null {
  if (candleTimes.length === 0) return null;
  // Candles are ascending; a decision belongs to the last candle at or before it.
  // A decision older than the first candle has no bucket rather than being
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
 * Fold decisions into one bucket per candle.
 *
 * Returns ascending buckets. Decisions with an unparseable timestamp, and those
 * before the first candle, are skipped rather than guessed into a bucket.
 */
export function bucketDecisions(
  decisions: readonly Decision[],
  candleTimes: readonly number[],
): DecisionBucket[] {
  const byTime = new Map<
    number,
    { opened: boolean; count: number; reasons: Map<string, number> }
  >();

  for (const decision of decisions) {
    const parsed = Date.parse(decision.ts);
    if (Number.isNaN(parsed)) continue;
    const time = nearestCandleTime(candleTimes, Math.floor(parsed / 1000));
    if (time === null) continue;

    let bucket = byTime.get(time);
    if (!bucket) {
      bucket = { opened: false, count: 0, reasons: new Map() };
      byTime.set(time, bucket);
    }
    bucket.count += 1;
    if (isOpenedAction(decision.action)) bucket.opened = true;
    const reason = decision.reason || 'unknown';
    bucket.reasons.set(reason, (bucket.reasons.get(reason) ?? 0) + 1);
  }

  return [...byTime.entries()]
    .map(([time, bucket]) => {
      const reasons = [...bucket.reasons.keys()];
      let dominantReason = reasons[0] ?? 'unknown';
      let best = -1;
      for (const [reason, count] of bucket.reasons) {
        if (count > best) {
          best = count;
          dominantReason = reason;
        }
      }
      return { time, opened: bucket.opened, count: bucket.count, dominantReason, reasons };
    })
    .sort((a, b) => a.time - b.time);
}

/** One-line summary for a marker tooltip. */
export function describeBucket(bucket: DecisionBucket): string {
  const evaluations = `${bucket.count} evaluation${bucket.count === 1 ? '' : 's'}`;
  if (bucket.opened) return `Opened. ${evaluations}`;
  const extra = bucket.reasons.length > 1 ? ` (+${bucket.reasons.length - 1} other)` : '';
  return `Skipped: ${bucket.dominantReason}${extra}. ${evaluations}`;
}

/** Minimal marker shape, kept free of the charting library so this stays pure. */
export interface ChartMarker {
  time: number;
  position: 'aboveBar' | 'belowBar';
  color: string;
  shape: 'circle' | 'arrowUp';
  size: number;
  text?: string;
  title?: string;
}

function peakColor(pct: number): string {
  if (pct >= 100) return '#f87171';
  if (pct >= 50) return '#fb923c';
  return '#facc15';
}

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
  decisions,
  candleTimes,
}: {
  episodes: readonly TokenEpisode[] | undefined;
  decisions: readonly Decision[] | undefined;
  candleTimes: readonly number[];
}): ChartMarker[] {
  const episodeMarkers: ChartMarker[] = (episodes ?? [])
    .filter((episode) => episode.first_seen_at)
    .map((episode): ChartMarker | null => {
      // Same rule as decisions, deliberately: an episode that started before
      // the first visible candle is dropped rather than pinned to it. Drawing
      // it at the window's left edge would claim it happened at a time it did
      // not, and TokenEpisodes already lists every episode in full, so nothing
      // is lost by leaving it off the chart.
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

  const decisionMarkers: ChartMarker[] = bucketDecisions(decisions ?? [], candleTimes).map(
    (bucket) => ({
      time: bucket.time,
      position: 'belowBar' as const,
      color: bucket.opened ? '#34d399' : '#64748b',
      shape: bucket.opened ? ('arrowUp' as const) : ('circle' as const),
      size: 1,
      text: bucket.opened ? undefined : String(bucket.count),
      title: describeBucket(bucket),
    }),
  );

  // lightweight-charts rejects duplicate times. Episode markers are listed
  // first and therefore win a tie: an episode start is the rarer event, and the
  // decisions of that candle are still counted in neighbouring tooltips.
  const seen = new Set<number>();
  const merged: ChartMarker[] = [];
  for (const marker of [...episodeMarkers, ...decisionMarkers]) {
    if (seen.has(marker.time)) continue;
    seen.add(marker.time);
    merged.push(marker);
  }
  return merged.sort((a, b) => a.time - b.time);
}
