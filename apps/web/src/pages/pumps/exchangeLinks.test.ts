import { describe, expect, it } from 'vitest';
import { exchangeTradeUrl } from './exchangeLinks';

// Table-driven per colleague review, 2026-09-02: covers all 8 supported
// exchanges plus the edge cases that mattered for the "don't parse and
// rebuild market_id" fix -- a multiplier ticker, an unfamiliar/malformed
// shape, an unsupported exchange, and a missing market_id.
describe('exchangeTradeUrl', () => {
  const supported: Array<[string, string, string]> = [
    ['binance', 'BTCUSDT', 'https://www.binance.com/en/futures/BTCUSDT'],
    ['bybit', 'BTCUSDT', 'https://www.bybit.com/trade/usdt/BTCUSDT'],
    ['lbank', 'BTCUSDT', 'https://www.lbank.com/en-US/futures/BTCUSDT'],
    ['gate', 'BTC_USDT', 'https://www.gate.com/futures/USDT/BTC_USDT'],
    ['mexc', 'BTC_USDT', 'https://www.mexc.com/futures/BTC_USDT'],
    ['xt', 'BTC_USDT', 'https://www.xt.com/en/futures/trade/btc_usdt'],
    ['bingx', 'BTC-USDT', 'https://bingx.com/en/perpetual/BTC-USDT'],
    ['okx', 'BTC-USDT-SWAP', 'https://www.okx.com/trade-swap/btc-usdt-swap'],
  ];

  it.each(supported)('builds the live-verified %s URL for %s', (exchange, marketId, expected) => {
    expect(exchangeTradeUrl(exchange, marketId)).toBe(expected);
  });

  it.each(supported)('is case-insensitive on the exchange name (%s)', (exchange, marketId) => {
    expect(exchangeTradeUrl(exchange.toUpperCase(), marketId)).not.toBeNull();
  });

  it('preserves a multiplier ticker market_id exactly, never destructively reconstructed', () => {
    // 1000BONK is a real captured base (verified against prod: binance
    // market_id "1000BONKUSDT") -- the exact string must round-trip
    // unchanged into the URL, not be parsed apart and rebuilt.
    expect(exchangeTradeUrl('binance', '1000BONKUSDT')).toBe(
      'https://www.binance.com/en/futures/1000BONKUSDT',
    );
  });

  it('passes an unusual base straight through for gate (no reconstruction)', () => {
    expect(exchangeTradeUrl('gate', 'TRUMPSOL_USDT')).toBe(
      'https://www.gate.com/futures/USDT/TRUMPSOL_USDT',
    );
  });

  it('returns null for an unsupported exchange', () => {
    expect(exchangeTradeUrl('some_unlisted_exchange', 'BTCUSDT')).toBeNull();
  });

  it('returns null for a missing market_id', () => {
    expect(exchangeTradeUrl('binance', undefined)).toBeNull();
    expect(exchangeTradeUrl('binance', '')).toBeNull();
  });

  it('returns null for a malformed market_id rather than guessing', () => {
    expect(exchangeTradeUrl('binance', 'GARBAGE')).toBeNull();
    expect(exchangeTradeUrl('binance', 'BTC-USDT')).toBeNull(); // wrong separator for binance
  });

  it("returns null when a market_id's own separator style doesn't match this exchange", () => {
    // A Gate-shaped id handed to an exchange expecting no separator at all
    // must fail closed, not silently drop the underscore.
    expect(exchangeTradeUrl('binance', 'BTC_USDT')).toBeNull();
    // An OKX-shaped id (with its own -SWAP suffix) handed to BingX (no
    // -SWAP suffix in its own convention) must also fail closed.
    expect(exchangeTradeUrl('bingx', 'BTC-USDT-SWAP')).toBeNull();
  });
});
