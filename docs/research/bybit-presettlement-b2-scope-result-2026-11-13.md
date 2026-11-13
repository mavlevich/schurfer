# B2 archive scope result, 2026-11-13

**Metadata inventory complete.** Every one of the 724 frozen symbol-days has a
verified-size book file and trade file. This is availability and sizing, not a
maker-fill or profitability pass. No archive body was downloaded by this step.

The [B2 contract](bybit-presettlement-maker-feasibility-v1.md) and ten original
source-pilot IDs were committed before HEAD on clean `e3e33c6`. The input remains
B1's accepted event list, SHA-256
`4f1554108d6da503630d24d5b123d8000532b7bb3decb33f033e57dab65711f7`.
No signal, offset, entry rule or data-quality threshold was chosen from a fill
result or trade return.

## Source established

The [official Bybit catalogue](https://www.bybit.com/en/derivative-activity/history-data)
returned a `quote-saver.bycsi.com` URL through its own observed public `list-files`
request. Its 2025-09-30 0GUSDT example's 91,864,958-byte size matched a direct HEAD.
The source proof records this as decoded browser-observed JSON, not a fabricated
raw HTTP snapshot. Direct page/API requests from the CLI returned 403; the normal
unauthenticated browser rendered the public catalogue without a security bypass.
No archive download was clicked. The provenance example does not select a B1 event.

## Exact full-scope sizes

| Item                                        |             Result |
| ------------------------------------------- | -----------------: |
| Native instruments / symbol-days            |          151 / 724 |
| Fixed HEAD URLs / actual attempts           |      2,172 / 2,172 |
| Verified files / absent depth alternatives  |        1,448 / 724 |
| Selected ob500 / ob200 files                |           656 / 68 |
| Symbol-days missing a verified book or tape |                  0 |
| Selected files with unknown size            |                  0 |
| Book bytes                                  |     49,985,850,895 |
| Trade bytes                                 |     25,808,106,324 |
| Combined bytes                              | **75,793,957,219** |

The combined size is **75.79 GB / 70.59 GiB**, not a sampled extrapolation.
Both depth variants were checked for every day; a verified ob500 is preferred,
otherwise ob200. All 724 unavailable alternatives returned 404. No failed request
was treated as a zero-sized file. The final per-day inventory has no unknowns.
Only response headers were consumed, at most 3 request starts per second, below
the fixed 4,344-attempt limit; no retry was needed.

[Retained metadata](evidence/b2-archive-scope-2026-11-13/results/) contains the exact
run binding, every attempt and selected-header response, the result and checksums.
It records the input/revision/source fingerprint before HTTP. It makes no claim
that a 200 response establishes archive schema, continuity or eligible trade flow.

## Small source pilot instead of a full download

The original ten IDs remain unchanged: five instruments delivered by the discovery
end, five continuing then, selected by their frozen hash rule before HEAD.
Their 20 book/tape files total **1,163,609,276 bytes**. Every selected book is ob500.

Because the full scope also contains ob200, two separately labelled format fixtures
were fixed **after metadata but before any body or fill read**, without replacing
the original pilot: PROMPTUSDT 2025-08-24 and ASRUSDT 2025-08-23. The rule is the
lowest SHA-256 of `b2-ob200-format-v1:symbol:day` among verified ob200 days,
distinct instruments. These add 392,239,614 bytes for checking the other schema;
they are not an extra trading candidate or a resampling of the ten-day pilot.

The resulting source-only plan is **24 files / 1,555,848,890 bytes** (1.56 GB,
1.45 GiB). Its hard wire-byte cap is **2 GiB**, including retries and partial
transfers, with at most two attempts per file / 48 globally and 3 requests/s.
This does not authorize downloading the complete 75.8 GB corpus. The plan and
all native URLs, HEAD sizes and ETags are retained in `pilot-body-plan.json`.
No body fetch has run yet. At planning time the local disk had 48.3 GiB free.

## What the pilot must resolve

Validate actual snapshots/deltas, native quantity units, delivery identity,
timestamp alignment and data gaps before calculating any fill. The ordinary
[order-book API reference](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)
distinguishes the system timestamp from matching-engine `cts`; the latter can
be aligned with trade `T` when actually present. Current documentation does not
prove that old archive files contain it. Nor does a sequence ID promise adjacent
integers, so the legacy reader's strict `previous + 1` condition is not reused
without validation.

The [trade API reference](https://bybit-exchange.github.io/docs/v5/websocket/public/trade)
defines taker side and block/RPI flags. That does not establish CSV coverage of
those flags. Missing eligibility stays unknown; do not silently count it as
ordinary API-executable volume. Report the excluded/included-volume bounds as
registered. Do not convert touches, cancelled levels or unclassified trades into
identified maker fills.

The quality pilot emits source/format/coverage diagnostics, without strategy PnL.
The next step is a bounded pilot transport and source validator for these fixed
24 files. Only after that can the frozen placement policies be replayed. B3
returns, the reserved test year and any live deployment remain separate stages.

## Verification

- Ten scope/HEAD tests pass, including the body-forbidden stream, identity/window
  drift, unknown size, HTML response refusal, interrupted-attempt accounting and
  restart without duplicate requests. Ruff and strict mypy pass.
- Full `make verify` passed on the disposable local PostgreSQL. Its first run
  found unreachable code in the body-forbidden test generator; that stub was
  changed to an explicit async iterator and the entire command passed on rerun.
  The running metadata probe's application source was unchanged by this test fix.
- The completed HEAD inventory used the committed original source/revision; all
  response bodies remain unread. No production service or order was touched.
