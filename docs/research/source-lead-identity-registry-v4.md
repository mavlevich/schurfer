# Source-lead identity registry v4 (HYP-012 v4, PR C)

Status: rule and tooling registered 2026-09-25, before any v4 route was decided.
Code: `apps/analytics/schurfer_analytics/source_lead_identity_v4.py`
(CLI `source-lead-identity-v4`).

## Why v4

The v3 registry holds 14 hand-approved assets, and that is the bottleneck of the
HYP-012 forward cohort: 15 of the 100 qualified episodes it needs after three weeks,
with 903 leads rejected only because their asset was not in the registry.

v4 changes three things:

1. **The execution venue the owner can trade becomes the primary target.** Binance
   futures are not available to the owner (Poland). Bybit becomes the primary venue and
   Binance a descriptive comparison. The estimand change itself is registered in PR D.
2. **One written rule replaces per-row human approval.** The rule is fixed before any
   route is decided and applies identically to every candidate.
3. **The candidate set is a fixed, hashed snapshot**, not "whatever appeared by the PR
   date".

## Candidate window

Every Gate base with at least one source-lead capture in
`[2026-09-03T00:00:00Z, 2026-09-25T20:00:00Z)`. The query is `candidates-sql`. The
snapshot is `registry/source_lead_identity_v4_candidates.json`, and its
`candidates_sha256` covers the window, the query and the sorted list. Assets whose
first capture comes later are not covered by v4; readiness reports count them as
uncovered.

The 14 registry v3 assets are always added as candidates (`carried_over_from_v3`), so v4
never drops an asset only because it had no lead inside the window. They face the
same rule.

## The rule (`source_lead_identity_rule_v4`)

A route is one (base, target venue) pair. It is approved only if every check passes;
the first failing check is recorded as the reason.

1. Gate has a trading, non-delisting `{BASE}_USDT` perpetual.
2. The target has exactly one trading USDT-settled linear perpetual whose reported base
   equals BASE. `1000`-prefixed and renamed contracts never match.
3. Gate's own currency data reports one contract per chain, on at least one supported
   chain: Ethereum, BSC, Base or Arbitrum.
4. The target's own asset data names the coin exactly once and reports a contract on a
   supported chain. For Binance this is the Alpha catalog; for Bybit it is authenticated
   `Get Coin Info`, where an empty `contractAddress` means no confirmation.
5. No supported chain carries different addresses at Gate and the target.
6. At least one supported chain carries the same address at both venues.
7. Exactly one CoinGecko project lists that (chain, address), and its symbol equals
   BASE.
8. On-chain `decimals()` is read at a pinned block, and any decimals the target catalog
   reports agree with it. Bybit's `minAccuracy` is deposit/withdrawal precision and is
   never used as decimals.

Rules are decided per route. If Bybit passes and Binance fails, the asset stays, with
only the Bybit route. Contract migrations, symbol collisions and multichain-only links
are rejected by checks 3 to 7; v4 has no manual exception path. Cost, canary results and
returns play no part.

**Out of scope for v4:** Solana and other non-EVM chains. The decimals evidence is an EVM
`eth_call` pinned to a block. These routes are rejected as `gate_no_supported_contract`
or `target_no_supported_contract` and counted, so a later version can decide whether to
add them.

**Residual risk, accepted:** each venue's asset data and its perpetual are linked only
by the ticker (Gate currency to Gate futures, Alpha or Bybit coin-info to the target
perpetual). This is the same bridge v3 already documented.

## Human confirmation and review

This replaces the v3 checklist item "a second person independently confirmed the link"
(decision register, 2026-09-25):

- The owner confirms once: the rule, and the full list of approved and rejected routes
  with reasons. The confirmation is recorded in
  `registry/source_lead_identity_v4_approval.json`, which names the exact
  `decisions_sha256`.
- The reviewer independently re-derives every approved route from the stored raw
  evidence and recomputes the hashes, without relying on the tool's labels.
- `build-registry` refuses to run unless the approval names this decisions file and
  lists both the confirmer and the technical reviewer.

## Procedure

1. Commit the code with a clean tree.
2. Run the candidate query read-only on prod, then `source-lead-identity-v4 candidates`,
   and commit the snapshot.
3. The owner runs `decide` with a read-only Bybit key in the environment. It stores:
   - the exact response bytes of every source the rule reads, gzipped under
     `evidence/source_lead/v4/sources/`: Gate currencies (ccxt output) and perpetuals,
     the Binance Alpha catalog and exchangeInfo, every Bybit instruments page, Bybit
     coin-info, and the CoinGecko coin list with platforms;
   - one bundle per approved route;
   - `decisions.json` and `manifest.json`.

   Before publishing, it re-derives every decision from those stored files (the same
   check as `recompute`). Everything is then published together in one atomic swap. A
   fetch that still fails after its retries (CoinGecko 429, an RPC node returning null)
   aborts the run. Only a failed check on already captured evidence is recorded as a
   rejection (`capture_rejected`). The run records the code revision and whether the tree
   was clean.

4. Commit the evidence. The owner confirms. The reviewer runs `recompute`, which re-derives
   all routes from the stored sources and checks each bundle's catalog entry against
   them, and re-checks the approved bundles.
5. Commit the approval, then run `build-registry --evidence-commit <evidence commit>`. It
   writes registry v4, verifies it against the evidence, and prints the fingerprint.
6. Activation is a separate change (PR D): new qualification and estimand versions and a
   prospective cohort start. Nothing in this PR changes qualification, capture or the v3
   registry.
