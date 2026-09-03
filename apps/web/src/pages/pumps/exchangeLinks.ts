// Deep links to each exchange's own USDT-perpetual trade page for a given
// market_id -- ROADMAP tech debt ("pump scanner deep link"). Pure UX
// convenience: lets a reader open the exact instrument on the source venue
// in one click instead of hand-building the URL themselves.
//
// Colleague review, 2026-09-02: this codebase deliberately started
// capturing the EXACT exchange-native market_id specifically so downstream
// consumers never have to guess a derivatives symbol back together from a
// bare base ticker (the same "identity, not a bare ticker" discipline
// apps/analytics' own momentum-universe/source-lead identity work already
// established the hard way). An earlier version of this helper stripped
// market_id down to (base, "USDT") and rebuilt each exchange's own format
// from those two parts -- exactly the class of guess this repository's
// own identity capture exists to avoid, and a real risk for an alias,
// a multiplier ticker whose own shape isn't a plain BASE+USDT concatenation,
// or a future exchange whose native id convention doesn't match the
// assumption baked into the rebuild.
//
// Fixed: the exact market_id string is passed straight through. The only
// transform applied is the minimal, purely cosmetic one each exchange's
// own URL actually requires (lowercase for OKX/XT; nothing for the rest --
// every real market_id sample captured so far is already uppercase, the
// same case every other exchange's URL wants). Before using it, market_id
// is validated against that exchange's own known native-id shape
// (PATTERNS below) -- a market_id that doesn't match is treated as
// unrecognized and falls back to plain (non-linked) text, never a
// guessed/malformed link. All 8 URL patterns were verified live
// (2026-09-02) against a real BTCUSDT/BTC_USDT/BTC-USDT-SWAP instrument on
// each exchange's own site before shipping this.

interface ExchangeLinkSpec {
  /** Matches this exchange's own known native market_id shape (case-
   * insensitive) -- e.g. Binance/Bybit/LBank "BTCUSDT", Gate/MEXC/XT
   * "BTC_USDT", BingX "BTC-USDT", OKX "BTC-USDT-SWAP". A market_id that
   * doesn't match (an alias, a multi-part base, an unfamiliar shape) is
   * never guessed at -- the caller falls back to plain text instead. */
  pattern: RegExp;
  /** Builds the trade-page URL from the exact market_id (only case is
   * ever changed, never the structure). */
  url: (marketId: string) => string;
}

const EXCHANGE_LINKS: Record<string, ExchangeLinkSpec> = {
  binance: {
    pattern: /^[A-Z0-9]+USDT$/i,
    url: (marketId) => `https://www.binance.com/en/futures/${marketId}`,
  },
  bybit: {
    pattern: /^[A-Z0-9]+USDT$/i,
    url: (marketId) => `https://www.bybit.com/trade/usdt/${marketId}`,
  },
  lbank: {
    pattern: /^[A-Z0-9]+USDT$/i,
    url: (marketId) => `https://www.lbank.com/en-US/futures/${marketId}`,
  },
  gate: {
    pattern: /^[A-Z0-9]+_USDT$/i,
    url: (marketId) => `https://www.gate.com/futures/USDT/${marketId}`,
  },
  mexc: {
    pattern: /^[A-Z0-9]+_USDT$/i,
    url: (marketId) => `https://www.mexc.com/futures/${marketId}`,
  },
  xt: {
    pattern: /^[A-Z0-9]+_USDT$/i,
    url: (marketId) => `https://www.xt.com/en/futures/trade/${marketId.toLowerCase()}`,
  },
  bingx: {
    pattern: /^[A-Z0-9]+-USDT$/i,
    url: (marketId) => `https://bingx.com/en/perpetual/${marketId}`,
  },
  okx: {
    pattern: /^[A-Z0-9]+-USDT-SWAP$/i,
    url: (marketId) => `https://www.okx.com/trade-swap/${marketId.toLowerCase()}`,
  },
};

/** Returns the exchange's own trade-page URL for this EXACT market_id, or
 * null when the exchange isn't supported or market_id doesn't match that
 * exchange's own known native-id shape -- never a guessed/reconstructed
 * link. `market_id` is used verbatim (only case may change), never parsed
 * apart and rebuilt. */
export function exchangeTradeUrl(exchange: string, marketId: string | undefined): string | null {
  if (!marketId) {
    return null;
  }
  const spec = EXCHANGE_LINKS[exchange.toLowerCase()];
  if (!spec || !spec.pattern.test(marketId)) {
    return null;
  }
  return spec.url(marketId);
}
