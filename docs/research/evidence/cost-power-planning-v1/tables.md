Report `research_cost_power_planning_v1`, code `68fe658063e58438dda9fc7607c00250bb9dbfb3` (dirty: False), parameters `21c577ec3d5c`.

### Inputs

| Key | Status | SHA-256 |
| --- | --- | --- |
| preblind_result | verified | `d5505fadc046347b02e851b5ea169251ae2f12df0f409451dd801d7bb7828b39` |
| hyp012b_inputs | verified | `539894aaa6aa90fe31a932891d6afb7247c2dfa84d84c75bd6f70abea54b8daf` |
| hyp012b_result | verified | `8a6e0865aad8c8b85571a6e1d49eb8bd44d6dd1a4af18970731446b3d9d74d08` |
| hyp012c_inputs | verified | `8cca5341129c515f290887cda347393d66cd01e91dd38a2abf8a726aeb7df6d0` |
| hyp012c_result | verified | `bbe76b757ab9b6be2a208636e89d605cd582fd44b868b138b21d0f29333f9560` |
| hyp029_inputs | verified | `def8d5f1742b9314cdadc174f94cd2e60e02b8673cff72ef3cb4c9d15ced56ea` |
| hyp029_result | verified | `3fda59f79ebe701ec4095b88e7059bdcaf4be4476f01b3e4a3add8501c11e7b7` |

Accrual reference: verified, flow 1.84/day.

### Break-even midpoint move (bps)

| Population | Venue | Spread | Age | n | Mean f0 | Mean f5.5 | p90 f5.5 | Mean f10 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| paper momentum_flow_paper_v1 | bybit | gte_50_bps | fresh | 1 | 49.35 | 60.41 | 60.41 | 69.46 |
| paper momentum_flow_paper_v1 | bybit | lt_20_bps | fresh | 3374 | 11.58 | 22.60 | 28.89 | 31.62 |
| paper momentum_flow_paper_v1 | bybit | lt_20_bps | outside_2s | 403 | 12.58 | 23.60 | 29.50 | 32.63 |
| paper momentum_flow_paper_v1 | bybit | lt_50_bps | fresh | 255 | 24.26 | 35.29 | 44.61 | 44.33 |
| paper momentum_flow_paper_v1 | bybit | lt_50_bps | outside_2s | 30 | 23.84 | 34.87 | 43.43 | 43.91 |
| paper momentum_flow_paper_v1 | bybit | lt_5_bps | fresh | 2015 | 5.12 | 16.13 | 20.47 | 25.15 |
| paper momentum_flow_paper_v1 | bybit | lt_5_bps | outside_2s | 108 | 7.66 | 18.68 | 24.18 | 27.70 |
| paper momentum_flow_paper_v1_hold12h | bybit | lt_20_bps | fresh | 1319 | 12.10 | 23.12 | 29.71 | 32.14 |
| paper momentum_flow_paper_v1_hold12h | bybit | lt_20_bps | outside_2s | 152 | 12.64 | 23.66 | 28.21 | 32.68 |
| paper momentum_flow_paper_v1_hold12h | bybit | lt_50_bps | fresh | 108 | 24.72 | 35.76 | 46.16 | 44.79 |
| paper momentum_flow_paper_v1_hold12h | bybit | lt_50_bps | outside_2s | 8 | 24.48 | 35.51 | 41.90 | 44.55 |
| paper momentum_flow_paper_v1_hold12h | bybit | lt_5_bps | fresh | 629 | 5.73 | 16.74 | 21.72 | 25.76 |
| paper momentum_flow_paper_v1_hold12h | bybit | lt_5_bps | outside_2s | 37 | 7.62 | 18.63 | 23.40 | 27.66 |
| source_immediate_cross source_lead_prospective_capture_v1 | gate->binance | lt_20_bps | unknown | 154 | 8.64 | 19.65 | 24.01 | 28.68 |
| source_immediate_cross source_lead_prospective_capture_v1 | gate->binance | lt_50_bps | unknown | 5 | 26.77 | 37.80 | 42.78 | 46.84 |
| source_immediate_cross source_lead_prospective_capture_v1 | gate->binance | lt_5_bps | unknown | 225 | 3.65 | 14.66 | 16.69 | 23.68 |
| source_immediate_cross source_lead_prospective_capture_v1 | gate->bybit | lt_20_bps | unknown | 187 | 12.18 | 23.20 | 29.79 | 32.22 |
| source_immediate_cross source_lead_prospective_capture_v1 | gate->bybit | lt_50_bps | unknown | 15 | 26.02 | 37.06 | 41.98 | 46.09 |
| source_immediate_cross source_lead_prospective_capture_v1 | gate->bybit | lt_5_bps | unknown | 127 | 3.95 | 14.96 | 17.75 | 23.97 |

Capacity: $50 measured_book_quotes, $500 capacity_not_measured, $5000 capacity_not_measured.

### Dispersion scenarios

| Dataset | Hold | Episodes | SD bps | Assets | Deff asset | ICC asset | Days | Deff day | Weeks | Lag-1 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| hyp012b_discovery_formal | 30 min | 1528 | 400.0 | 258 | 0.94 | 0.084 | 21 | 1.20 | 3 | 0.110 |
| hyp012c_holdout_in_band | 30 min | 334 | 471.1 | 150 | 1.02 | 0.040 | 28 | 0.94 | 4 | -0.047 |
| hyp029_september | 60 min | 81 | 1,225.3 | 46 | 0.92 | -0.205 | 24 | 0.85 | 4 | -0.171 |

### Zero-effect check

| Dataset | Scheme | Smallest evaluable size | Max null pass rate | Check |
| --- | --- | ---: | ---: | --- |
| hyp012b_discovery_formal | asset | 125 | 0.035 | ok |
| hyp012b_discovery_formal | utc_day | 1500 | 0.044 | ok |
| hyp012c_holdout_in_band | asset | 50 | 0.034 | ok |
| hyp012c_holdout_in_band | utc_day | 250 | 0.027 | ok |
| hyp029_september | asset | 50 | 0.033 | ok |
| hyp029_september | utc_day | 75 | 0.037 | ok |

### Linearized rule versus the registered asset-cluster bootstrap

| Dataset | n | Null pass: linear / bootstrap | 50 bps pass: linear / bootstrap | Agreement at 0 / 50 |
| --- | ---: | --- | --- | --- |
| hyp012b_discovery_formal | 100 | 0.030 / 0.035 | 0.250 / 0.240 | 0.995 / 0.950 |
| hyp012b_discovery_formal | 500 | 0.045 / 0.040 | 0.840 / 0.840 | 0.995 / 1.000 |
| hyp012c_holdout_in_band | 100 | 0.035 / 0.035 | 0.190 / 0.185 | 0.990 / 0.965 |
| hyp012c_holdout_in_band | 800 | 0.020 / 0.020 | 0.850 / 0.840 | 1.000 / 0.980 |
| hyp029_september | 100 | 0.035 / 0.030 | 0.060 / 0.065 | 0.995 / 0.995 |
| hyp029_september | 1200 | 0.025 / 0.025 | 0.375 / 0.390 | 1.000 / 0.985 |

### Main table (resolved fraction 0.9, flow 1.84/day)

| Net effect | Dataset | Episodes 80% / 90% (scheme) | Power at n (MC SE) | Clusters drawn | Days at flow | Flow/day for 91 / 183 days | $ over test at $50 | Limitations |
| ---: | --- | --- | --- | ---: | ---: | --- | ---: | --- |
| 10 | hyp012b_discovery_formal | 15,000 / 20,000 (utc_day) | 0.822 (0.012) | 207 | 9,061 | 183.2 / 91.1 | 750 | 3 weeks |
| 10 | hyp012c_holdout_in_band | 20,000 / >20,000 (asset) | 0.836 (0.012) | 8,978 | 12,081 | 244.2 / 121.4 | 1,000 | 4 weeks, more assets than observed |
| 10 | hyp029_september | >20,000 / >20,000 | n/a | n/a | n/a | n/a / n/a | n/a | 4 weeks |
| 25 | hyp012b_discovery_formal | 2,500 / 4,000 (utc_day) | 0.842 (0.012) | 35 | 1,510 | 30.5 / 15.2 | 312 | 3 weeks |
| 25 | hyp012c_holdout_in_band | 3,000 / 4,000 (asset) | 0.835 (0.012) | 1,347 | 1,812 | 36.6 / 18.2 | 375 | 4 weeks, more assets than observed |
| 25 | hyp029_september | 20,000 / >20,000 (asset) | 0.864 (0.011) | 11,357 | 12,081 | 244.2 / 121.4 | 2,500 | 4 weeks, more assets than observed |
| 50 | hyp012b_discovery_formal | 500 / 800 (asset) | 0.852 (0.011) | 85 | 302 | 6.1 / 3.0 | 125 | 3 weeks, day Deff 1.20, day scheme evaluable only from 1,500 |
| 50 | hyp012c_holdout_in_band | 800 / 1,000 (asset) | 0.860 (0.011) | 360 | 483 | 9.8 / 4.9 | 200 | 4 weeks, more assets than observed |
| 50 | hyp029_september | 5,000 / 8,000 (asset) | 0.866 (0.011) | 2,837 | 3,020 | 61.1 / 30.4 | 1,250 | 4 weeks, more assets than observed |
| 100 | hyp012b_discovery_formal | <=125 / 150 (asset) | 0.839 (0.012) | 22 | 76 | 1.5 / 0.8 | 62 | 3 weeks, day Deff 1.20, day scheme evaluable only from 1,500 |
| 100 | hyp012c_holdout_in_band | 200 / 250 (asset) | 0.851 (0.011) | 90 | 121 | 2.4 / 1.2 | 100 | 4 weeks |
| 100 | hyp029_september | 1,200 / 1,500 (asset) | 0.866 (0.011) | 683 | 725 | 14.7 / 7.3 | 600 | 4 weeks, more assets than observed |

### Required mean net bps at $50 (resolved 0.9, rejection 0.2, 1 slot, 60 min hold)

| Flow/day | Entries/month | cost $0 + target $0 | cost $0 + target $10 | cost $0 + target $50 | cost $10 + target $0 | cost $10 + target $10 | cost $10 + target $50 | cost $25 + target $0 | cost $25 + target $10 | cost $25 + target $50 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.00 | 0.0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| 0.50 | 10.8 | 0 | 185 | 926 | 185 | 370 | 1,111 | 463 | 648 | 1,389 |
| 1.00 | 21.3 | 0 | 94 | 470 | 94 | 188 | 564 | 235 | 329 | 705 |
| 1.84 | 38.2 | 0 | 52 | 262 | 52 | 105 | 314 | 131 | 183 | 393 |
| 3.00 | 60.3 | 0 | 33 | 166 | 33 | 66 | 199 | 83 | 116 | 249 |
| 5.00 | 95.3 | 0 | 21 | 105 | 21 | 42 | 126 | 52 | 73 | 157 |

### Missing measurements

- executed fills: the cost read has book quotes only
- latency from signal to order and the quote-to-fill difference
- adverse selection and maker non-fill
- funding for the future line's holding period
- impact and capacity above USD 50
- event frequency of the future universe after its own filters
- dispersion of the future signal (past strategies are scenarios only)
- operating budget and target monthly result (not agreed)
- source-lead quote age (unknown for every source-lead group)
- source-lead exit 30 minutes later (only a same-book immediate cross)
- hyp012b_discovery_formal: week-level dependence (not_estimable: 3 UTC weeks < 8)
- hyp012c_holdout_in_band: week-level dependence (not_estimable: 4 UTC weeks < 8)
- hyp029_september: week-level dependence (not_estimable: 4 UTC weeks < 8)
