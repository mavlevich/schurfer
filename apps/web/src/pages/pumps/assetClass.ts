// ENG-018: the scanner records what each instrument actually tracks, not just
// that it is a perpetual. LBank 24H stock futures such as DJT entered pump
// cohorts as extreme crypto pumps when their fresh 24-hour baseline
// initialized, and nothing in the UI said they were not coins.
//
// Only a positively non-crypto class is surfaced. `crypto` is the expectation
// and needs no badge; `unknown` covers most venues today (bingx, bitmart,
// lbank and others expose no class field at all), so badging it would put a
// marker on nearly every row and say nothing actionable. Empty means the entry
// predates the classifier.
const LABELS: Record<string, string> = {
  tokenized_equity: 'Stock',
  commodity: 'Commodity',
  index: 'Index',
  forex: 'Forex',
  leveraged_product: 'Leveraged',
};

const DESCRIPTIONS: Record<string, string> = {
  tokenized_equity: 'Tokenized equity, not a crypto asset',
  commodity: 'Commodity contract, not a crypto asset',
  index: 'Index contract, not a crypto asset',
  forex: 'Forex contract, not a crypto asset',
  leveraged_product: 'Leveraged product, not a plain crypto perpetual',
};

export interface AssetClassBadge {
  /** Rendered text. Carries the curated caveat itself rather than hiding it in
   * a tooltip: a native `title` never appears on touch devices, so anything
   * essential has to be visible (colleague review). */
  label: string;
  /** Longer wording for `title` and `aria-label` -- helpful, never the only
   * place a caveat appears. */
  description: string;
  /** True when the class came from the curated table rather than a venue
   * field, so the caller can style it as the weaker claim it is. */
  curated: boolean;
}

/** The badge to show for a venue entry, or null when there is nothing worth saying. */
export function assetClassBadge(
  assetClass: string | undefined,
  assetClassSource?: string,
): AssetClassBadge | null {
  if (!assetClass) return null;
  const label = LABELS[assetClass];
  if (!label) return null;
  // A curated classification is an interim source with no venue field behind
  // it, so the badge says so rather than presenting it as the venue's word.
  const curated = assetClassSource === 'curated';
  return {
    label: curated ? `${label} · curated` : label,
    description: curated
      ? `${DESCRIPTIONS[assetClass]} (curated, not venue-declared)`
      : DESCRIPTIONS[assetClass],
    curated,
  };
}
