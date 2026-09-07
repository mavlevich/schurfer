import { describe, expect, it } from 'vitest';
import { assetClassBadge } from './assetClass';

describe('assetClassBadge', () => {
  it('badges a positively non-crypto class', () => {
    const badge = assetClassBadge('commodity', 'venue.bybit.symbolType');
    expect(badge?.label).toBe('Commodity');
    expect(badge?.curated).toBe(false);
    expect(badge?.description).toContain('not a crypto asset');
  });

  it('puts the curated caveat in the visible label, not only in the tooltip', () => {
    // A native `title` never appears on touch devices, so the weaker claim has
    // to be readable without hovering (colleague review).
    const badge = assetClassBadge('tokenized_equity', 'curated');
    expect(badge?.label).toBe('Stock · curated');
    expect(badge?.curated).toBe(true);
    expect(badge?.description).toContain('not venue-declared');
  });

  it('leaves a venue-declared class unqualified', () => {
    const badge = assetClassBadge('commodity', 'venue.bybit.symbolType');
    expect(badge?.label).not.toContain('curated');
    expect(badge?.description).not.toContain('curated');
  });

  it('shows nothing for crypto, which is the expectation', () => {
    expect(assetClassBadge('crypto', 'venue.bybit.symbolType')).toBeNull();
  });

  it('shows nothing for unknown, which covers most venues and says nothing actionable', () => {
    expect(assetClassBadge('unknown', 'venue_field_absent')).toBeNull();
  });

  it('shows nothing for an entry captured before the classifier existed', () => {
    expect(assetClassBadge(undefined)).toBeNull();
    expect(assetClassBadge('')).toBeNull();
  });

  it('shows nothing for a class this build does not know', () => {
    expect(assetClassBadge('something_new', 'venue.xt.tags')).toBeNull();
  });
});
