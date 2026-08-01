# Source lead long screen v1

Status: post-hoc discovery only. This contract cannot change production entry,
exit, venue, score, size, leverage, or `DRY_RUN=true`.

## Question

When Schurfer first observes a pump on MEXC or Gate before Binance or Bybit confirms
the same base ticker, does a hypothetical long on the later execution venue capture
an economically useful part of the move before confirmation?

This is not a listing-arbitrage claim and it is not a proof of canonical asset
identity. Schurfer stores stable identity only inside each venue. The keys are
venue-local, and equal base tickers can still represent different contracts or
tokens. A positive result can justify building canonical address mapping and
prospective quote capture; it cannot justify trading from the historical label.

## Frozen scope

- Dataset begins at `2026-07-24T00:00:00Z`, after durable source attribution became
  available. Earlier events are left-censored.
- First source must be a unique earliest persisted scanner observation from MEXC or
  Gate. Exact timestamp ties are excluded rather than broken by sort order.
- The target is a later Binance or Bybit observation no more than 60 minutes after
  the source.
- Both venue instruments must independently have non-conflicting swap metadata,
  matching base ticker, USDT quote and settlement, and an exact CCXT unified symbol.
  If a target onboarding timestamp is known, it must be no later than the source
  timestamp.
- Later confirmations are used to construct this post-hoc discovery sample. They are
  not available at the source timestamp and therefore are not a live selection rule.
- Scanner polling order, endpoint latency, venue-specific rolling-change semantics,
  and temporary API failures can all change which venue appears first.
- Binance and Bybit routes remain separate. MEXC and Gate routes remain separate.
  There is no combined cross-route headline.

## Paired economics

The primary lane compares two hypothetical longs for the same event and execution
venue with one common exit endpoint:

1. Early entry at the first complete one-minute bar after the source observation.
2. Control entry at the first complete one-minute bar after target confirmation.
3. Exit at 30 minutes after the control entry.

If confirmation arrives before the early entry can safely occur, the early policy is
cash. Missing or discontinuous one-minute paths make the pair unresolved. The path
must be complete from the source entry through the 240-minute control horizon.

Secondary discovery rows use early-entry delays of 0, 1, and 5 minutes and control
horizons of 1, 5, 15, 30, 60, and 240 minutes. Each delay/horizon row is descriptive;
it cannot be selected retrospectively for production.

The fixed cost assumption is deliberately explicit:

- 20 bps round-trip market impact;
- 10 bps taker fee per side;
- 5 bps funding cost per eight hours, prorated by holding time.

Historical OHLCV does not show executable source-time spread, depth, queue position,
or fill. The 20 bps value is an assumed cap, not an observation. Even a positive
screen requires prospective live quotes and paper fills before a shadow strategy.

## Inference and sensitivities

The primary 0-minute / 30-minute paired delta is bootstrapped by whole base-asset
cluster. The four source-to-execution routes form one Holm-corrected family. The
report also publishes leave-one-cluster-out and busiest-week exclusion results.
Inference is withheld for a route if any candidate pair is unresolved.

A route is worth prospective measurement only when all of the following point in
the same direction:

- early-long absolute net expectancy is positive;
- paired early-minus-confirmation delta is positive;
- the cluster interval and concentration sensitivities do not reveal a single-asset
  or single-week story;
- sample and route timing are economically meaningful;
- identity ambiguity and source-time executable liquidity can be measured forward.

## Separate short lane

For context, the report also describes a taker short entered at target confirmation
over the same horizons. It is a separate book. Confirmation is not a reversal
trigger, and its short result must never be combined with the early-long headline.
