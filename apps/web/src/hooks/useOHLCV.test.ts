import { describe, expect, it } from 'vitest';
import { keepOHLCVWithinIdentity } from './useOHLCV';
import type { OHLCVResponse } from '@/pages/pumps/types';

const PREVIOUS: OHLCVResponse = {
  base: 'B2',
  interval: 15,
  source: { exchange: 'binance', market_id: 'B2USDT', market_type: 'linear', is_proxy: false },
  status: 'ok',
  candles: [{ time: 1757200000, open: 1, high: 1, low: 1, close: 1, volume: 1 }],
  sources: [{ exchange: 'binance', market_id: 'B2USDT', market_type: 'linear', is_proxy: false }],
};

function query(base: string | undefined, minutes: number, exchange: string) {
  return { queryKey: ['ohlcv', base, minutes, exchange] as const };
}

describe('keepOHLCVWithinIdentity', () => {
  it('keeps previous candles on a same-identity background refetch', () => {
    const keep = keepOHLCVWithinIdentity('B2', 15, '');
    expect(keep(PREVIOUS, query('B2', 15, ''))).toBe(PREVIOUS);
  });

  it('drops previous candles when the token changes (the B2 stale-chart defect)', () => {
    // Switching B2 -> another token must not leave B2's candles on screen while the
    // new token loads.
    const keep = keepOHLCVWithinIdentity('OTHER', 15, '');
    expect(keep(PREVIOUS, query('B2', 15, ''))).toBeUndefined();
  });

  it('drops previous candles when the interval changes', () => {
    const keep = keepOHLCVWithinIdentity('B2', 60, '');
    expect(keep(PREVIOUS, query('B2', 15, ''))).toBeUndefined();
  });

  it('drops previous candles when the source (exchange) changes', () => {
    // Picking a different source must not show the old route's candles for a frame.
    const keep = keepOHLCVWithinIdentity('B2', 15, 'lbank');
    expect(keep(PREVIOUS, query('B2', 15, ''))).toBeUndefined();
  });

  it('drops previous candles when there is no previous query', () => {
    const keep = keepOHLCVWithinIdentity('B2', 15, '');
    expect(keep(PREVIOUS, undefined)).toBeUndefined();
  });
});
