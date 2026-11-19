# I25 M2(ii) v3 result, 2026-11-19

## Result and scope

The approved fresh HTML scan on clean merged `49edd5d` completed with
`stage1_pass_partial`. This is a partial data-recoverability result, not evidence
of strategy profitability. The approved unit is
`observed_pre_cut_file_prefix_cohort` in directories outside the frozen catalog;
known catalog-directory contents were not rescanned.

Contract SHA-256:
`11e5275769540faa7d1b03b3e66cecec074f836c33ff36546afeb75dc1a25de0`.
PR #624's reviewed head was `01497bcb30877a690e48291ae1f4ca90e96f916e`;
its six CI checks passed and its tree matched the resulting merge tree exactly.
The owner explicitly authorized the merge and one new capped scan.

## Actual access

One C3 root and 888 D1 HTML directory requests: 889 attempts, all HTTP 200,
42,631,655 response bytes (40.657 MiB), zero retries or stops. The caps were
1,500 new attempts and 128 MiB. No CSV, price, funding, trade/mark bar, book,
announcement or AWS request was included. No old allowance was reused.

Admission was delayed until the full snapshot and validation completed. There
are 453 globally admitted pre-cut filename series: 452 began before the discovery
window, and one first appears inside 2024-10-01 through 2025-09-30. M2(ii)'s
observed-cohort share is `1 / (263 + 1) = 0.3787878788%`, below 5%. There are zero
unverified pre-cut filename directories and zero prefix-collision directories.
`not_assessed` and `stop_codes` are empty.

The original 228 primary episodes and 48 ISO weeks are unchanged. M3 identity
limitations, M4's coarse funding check, M5's deferred minute continuity and M6's
static-only execution check remain. This does not prove asset identity, no
renames, first trading-open time, or coverage of every real launch.

## Integrity and reproducibility

All three sealed sources were validated before and after HTTP and again by a
local verifier. The original Stage 1 and v2 reports remain `stage1_incomplete`;
the diagnostic remains `diagnostic_complete_unlinked`. None was rewritten or
retroactively promoted. The v3 root is a fresh private sibling, mode 0700.
Private admitted prefixes/dates stay outside Git.

The local cross-check covered all 2,233 captured v3 inputs, receipt/attempt
accounting, pre-send reserves, code/contract binding, admission shapes, cohort
counts, and inherited checks. All assertions passed. Raw HTML is deliberately
not retained, so the global collision operation cannot be independently rerun
from persisted admitted facts alone; review and synthetic tests cover that code.

Original v3 report SHA-256:
`38b569aa5dc713179ded3db70f7cdf788cea560db7c0c99c65d9332a879070b8`.
Evidence-map SHA-256:
`532d101483c2691c422e1ddce2187423dfce5d460c4de1b719d626b66645ad03`.
The embedded JSON below is a semantic aggregate copy, not a claim that the
Markdown preserves the original JSON file's exact bytes.

The report's request total combines the original 1,726 Stage 1 attempts with
this v3 run only. Earlier stopped supplements, diagnostics and v2 remain spent
separately; that total is not the entire project's access history.

## Next gate

The protocol's next coverage stage is Stage 1b on the backward holdout,
2023-02-01 through 2024-09-15, with sealed output. It needs its own reviewed
window-specific code/contract, registry plan and owner authorization. This record
permits no further fetch, price/outcome read, paper/live execution or AWS access.

## Aggregate report

```json
{
  "api_errors": {},
  "base_requests": {
    "attempts": 1726,
    "by_class": {
      "C1": 4,
      "C3": 1,
      "D1": 263,
      "D2": 258,
      "D2h": 258,
      "D3": 228,
      "D4": 228,
      "D5": 228,
      "D6": 258
    },
    "bytes": 6764743564,
    "bytes_by_class": {
      "C1": 1876816,
      "C3": 105062,
      "D1": 9431967,
      "D2": 6737590608,
      "D2h": 0,
      "D3": 45874,
      "D4": 5508702,
      "D5": 7887088,
      "D6": 2297447
    }
  },
  "iso_weeks": 48,
  "launches": 263,
  "m1": {
    "ambiguous_share": 0.022813688212927757,
    "classes": {
      "aligned": 184,
      "delayed_open": 72,
      "no_archive": 1,
      "premarket_or_ambiguous": 6
    },
    "first_file_over_cap": 0,
    "lifecycle_unrebuildable_share": 0.0038022813688212928,
    "pass": true,
    "readable_share": 0.973384030418251
  },
  "m2": {
    "archive_only_assessed": true,
    "archive_only_proven": false,
    "archive_only_scope": "unique canonical pre-cut filename series in directories outside frozen catalog",
    "archive_only_status": "observed_archive_file_prefix_partial",
    "counts": {
      "admitted_pre_cut_series": 453,
      "archive_only_in_window": 1,
      "archive_only_pre_window": 452,
      "archive_only_unlisted": 0,
      "candidates_listed": 1,
      "catalog_launches": 263,
      "no_file_near_launch": 5,
      "path_days_missing_launches": 0,
      "prefix_collision_unverified": 0,
      "unverified_pre_cut_filename": 0
    },
    "partial": true,
    "pass": true,
    "shares": {
      "archive_only": 0.003787878787878788,
      "no_file_near_launch": 0.019011406844106463,
      "path_days_missing": 0.0
    },
    "unit": "observed_pre_cut_file_prefix_cohort"
  },
  "m3": {
    "change_known_at_entry": "not_observable_at_stage_1",
    "episodes_remaining": 256,
    "identity_unverifiable": 0,
    "pass": true,
    "shares": {
      "contradicting": 0.0,
      "identity_unverifiable": 0.0
    },
    "statuses": {
      "multiplier_ambiguous": 0,
      "relist": 0,
      "ticker_reuse_suspected": 0
    },
    "survivors": 256
  },
  "m4": {
    "assessed": true,
    "by_launch_month": {},
    "by_reason": {},
    "by_status": {
      "delisted": 0,
      "listed": 0
    },
    "covered_by_interval_snapshot": {
      "snapshot_is_8h": 22,
      "snapshot_shorter_or_unknown": 206
    },
    "eligible": 228,
    "funding_uncovered": 0,
    "interval_note": "the catalog fundingInterval is a current snapshot, not the historical schedule",
    "partial": true,
    "pass": true,
    "rule": "pagination finished and no gap longer than 8 h (a coarse check, not proof of every settlement)",
    "share": 0.0,
    "status": "partial_pass"
  },
  "m5": {
    "assessed": true,
    "episodes": 228,
    "level": "entry minute bar (trade) and hourly trade and mark series over the V1 path",
    "minute_level_path": "not_assessed_at_stage_1",
    "pass": true,
    "share": 1.0,
    "states": {
      "entry": {
        "present": 228
      },
      "mark": {
        "covered": 228
      },
      "trade": {
        "covered": 228
      }
    },
    "with_bars": 228
  },
  "m6": {
    "counts": {
      "feasible": 228,
      "infeasible": 0,
      "unknown": 0
    },
    "episodes": 228,
    "not_checked": [
      "quantity step and minimum quantity against USD 50 at the entry price",
      "risk limit and position cap",
      "depth at entry"
    ],
    "partial": true,
    "pass": true,
    "share": 1.0,
    "status": "partial_pass"
  },
  "n_primary": 228,
  "not_assessed": [],
  "outcome": "stage1_pass_partial",
  "partial_checks": [
    "m6: static catalog fields only; no price, quantity rounding, risk limit or depth",
    "m4: funding is a coarse check (no gap longer than 8 h), not proof of every settlement; the catalog interval is a current snapshot, and 206 covered launches have a snapshot interval shorter than 8 h or unknown",
    "m5: minute-level continuity is deferred to stage 4 (protocol Amendment 1)",
    "m2(ii): observed canonical pre-cut filename series in unknown directories only; not verified instrument identity, renames, trading-open or all market launches"
  ],
  "reasons": {
    "entry_unavailable": 0,
    "funding_uncovered": 0,
    "identity_excluded": 0,
    "lookback_censored": 12,
    "primary": 228,
    "t0_ambiguous": 6,
    "t0_unavailable": 1,
    "window_censored": 16
  },
  "requests": {
    "attempts": 2615,
    "bytes": 6807375219
  },
  "stop": null,
  "stop_codes": [],
  "supplement": {
    "binding_valid": true,
    "code": {
      "git_clean_before": true,
      "publication": {
        "apps/analytics/schurfer_analytics/bybit_carry_feasibility.py": "685dac395ebfe30d650ea89a08e50609550dbf0eb9542b4a1582aa5acb0a4961",
        "apps/analytics/schurfer_analytics/bybit_presettlement_bounded_fetch.py": "11d2dc538815ea3609d1e5e5c1a930297e2bfb53c2629c994889ab98eaa0e288",
        "apps/analytics/schurfer_analytics/new_perp_m2ii.py": "4d776fc2c451ab753ef7ae75aa6d672527acdcf7700ff173e0c33fa408d2874c",
        "apps/analytics/schurfer_analytics/new_perp_m2ii_identity_probe.py": "43e50f0f4aec6c3fdb18d703cf3a9210b8aa0c2181d3155692924868c9e1f81d",
        "apps/analytics/schurfer_analytics/new_perp_m2ii_v3.py": "189ac93adb055d761ccabaa4ded0545d9d19d4229b6aa0e3ce6fa2c86f30d2ed",
        "apps/analytics/schurfer_analytics/new_perp_stage1.py": "e82f2aa8e7c6734cd6f06fda732f29997aa299dfa64292e6db4828a7e819a98c",
        "docs/research/evidence/new-perp-m2ii-identity-probe-v1/contract.json": "c8efa38ef7df05d28b6490581d67e1d90a9a625354636f40f5b63d3e834f428d",
        "docs/research/evidence/new-perp-m2ii-v1/contract.json": "8f5daa58dd72f3a6157e14f5d3d834f244edc955e7b68c2ac3cf5ea6e1059860",
        "docs/research/evidence/new-perp-m2ii-v3/contract.json": "d2ce22a7f431e1d4329f442297630e5c82a5748c4edbc63d4d75bab64987725e",
        "docs/research/new-perp-m2ii-file-prefix-v3.md": "a096cc0e265babf73db6549c34452e3d6e0d132b07009e1a18f0900f5dad58c2",
        "docs/research/new-perp-m2ii-identity-probe-v1.md": "5836cf2f677512aa31905f321dadb01da6d57a8e3c6883e6f59291d9bc9155d2",
        "docs/research/new-perp-m2ii-supplement-v1.md": "c48e3e8e429bff2252cabe5b970ebd778658e34957d1521b0f289a821b26a29e",
        "docs/research/new-perp-short-lab-protocol-v1.md": "997de348c3d5e6059d372db7c93ea27d38b14536514916d681d92ac114f41d16",
        "docs/research/seen-windows-registry.md": "5fafa32eecd59f688320e13527199486b293a86668aa43b1c9019465bee721bd"
      },
      "revision": "49edd5d6f8ab07c1d79f6b801b9f9a2b5a5a00e5"
    },
    "collision_directories": 0,
    "complete": true,
    "diagnostic_report_sha256": "4449921d31dc9efdb61925d8f83b33c5ca0a39885f80de910cd5344b96017b49",
    "evidence_sha256": "532d101483c2691c422e1ddce2187423dfce5d460c4de1b719d626b66645ad03",
    "file_prefix_check_complete": true,
    "finished_at_utc": "2026-11-19T16:23:55.445872+00:00",
    "prior_v2_report_sha256": "0590a619d8bc5ddac11e928a958038f360ad4616d3685b11c53dc11be31a1f23",
    "request_list_sha256": "11e5275769540faa7d1b03b3e66cecec074f836c33ff36546afeb75dc1a25de0",
    "requests": {
      "attempts": 889,
      "attempts_by_class": {
        "C3": 1,
        "D1": 888
      },
      "bytes": 42631655,
      "bytes_by_class": {
        "C3": 105062,
        "D1": 42526593
      },
      "go": 1,
      "outcomes": {
        "C3": {
          "ok": 1
        },
        "D1": {
          "ok": 888
        }
      }
    },
    "rules": "new_perp_m2ii_v3",
    "source": {
      "input_map_sha256": "cc20f7d5b06208a18772d3df4f0a3427e1975750aea6f94a17845e5f627b4c5e",
      "manifest_sha256": "759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d",
      "report_sha256": "400f635ea29ac52dc5999caa866b152fa14cea1b82381ce91270012821a853ca",
      "revision": "8748d9ef664b4b0c65f885420692166dd8ad7a54"
    },
    "unit": "observed_pre_cut_file_prefix_cohort",
    "unverified_directories": 0
  },
  "version": "new_perp_stage1_verdict_v1+m2ii_v3"
}
```

## Local verification

```json
{
  "verified_at_utc": "2026-11-19T16:24:07.502526+00:00",
  "all_passed": true,
  "checks_passed": 7190,
  "revision": "49edd5d6f8ab07c1d79f6b801b9f9a2b5a5a00e5",
  "contract_sha256": "11e5275769540faa7d1b03b3e66cecec074f836c33ff36546afeb75dc1a25de0",
  "report_sha256": "38b569aa5dc713179ded3db70f7cdf788cea560db7c0c99c65d9332a879070b8",
  "evidence_map_sha256": "532d101483c2691c422e1ddce2187423dfce5d460c4de1b719d626b66645ad03",
  "evidence_files": 2233,
  "outcome": "stage1_pass_partial",
  "complete": true,
  "attempts": 889,
  "bytes": 42631655,
  "receipts": 889,
  "http_statuses": {
    "200": 889
  },
  "receipt_outcomes": {
    "ok": 889
  },
  "attempts_by_class": {
    "C3": 1,
    "D1": 888
  },
  "bytes_by_class": {
    "C3": 105062,
    "D1": 42526593
  },
  "retries": 0,
  "admitted_series": 453,
  "archive_only_in_window": 1,
  "archive_only_pre_window": 452,
  "unverified_directories": 0,
  "collision_directories": 0,
  "not_assessed": [],
  "stop_codes": [],
  "limitations": [
    "Raw HTML is not retained; global collision detection cannot be independently recomputed from persisted admitted facts alone. Code review and synthetic tests cover that operation. Lexical prefixes do not prove asset identity."
  ]
}
```
