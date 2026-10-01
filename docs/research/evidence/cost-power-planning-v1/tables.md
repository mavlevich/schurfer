Report `research_cost_power_planning_v1`, code `1bbdd274713e1bb02526c5abc6e6ae82c634e61a` (dirty: False), parameters `21c577ec3d5c`.

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

### Funnel at 1.84 eligible events/day

| Refused before entry | Resolved | Slots | Hold min | Accepted/day | Research resolved/day | Slot loss | Opened/day | Executable resolved/day |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0% | 100% | 1 | 30 | 1.84 | 1.84 | 3.7% | 1.77 | 1.77 |
| 0% | 100% | 1 | 60 | 1.84 | 1.84 | 7.1% | 1.71 | 1.71 |
| 0% | 100% | 3 | 30 | 1.84 | 1.84 | 0.0% | 1.84 | 1.84 |
| 0% | 100% | 3 | 60 | 1.84 | 1.84 | 0.0% | 1.84 | 1.84 |
| 0% | 90% | 1 | 30 | 1.84 | 1.66 | 3.7% | 1.77 | 1.59 |
| 0% | 90% | 1 | 60 | 1.84 | 1.66 | 7.1% | 1.71 | 1.54 |
| 0% | 90% | 3 | 30 | 1.84 | 1.66 | 0.0% | 1.84 | 1.66 |
| 0% | 90% | 3 | 60 | 1.84 | 1.66 | 0.0% | 1.84 | 1.66 |
| 0% | 70% | 1 | 30 | 1.84 | 1.29 | 3.7% | 1.77 | 1.24 |
| 0% | 70% | 1 | 60 | 1.84 | 1.29 | 7.1% | 1.71 | 1.20 |
| 0% | 70% | 3 | 30 | 1.84 | 1.29 | 0.0% | 1.84 | 1.29 |
| 0% | 70% | 3 | 60 | 1.84 | 1.29 | 0.0% | 1.84 | 1.29 |
| 20% | 100% | 1 | 30 | 1.47 | 1.47 | 3.0% | 1.43 | 1.43 |
| 20% | 100% | 1 | 60 | 1.47 | 1.47 | 5.8% | 1.39 | 1.39 |
| 20% | 100% | 3 | 30 | 1.47 | 1.47 | 0.0% | 1.47 | 1.47 |
| 20% | 100% | 3 | 60 | 1.47 | 1.47 | 0.0% | 1.47 | 1.47 |
| 20% | 90% | 1 | 30 | 1.47 | 1.32 | 3.0% | 1.43 | 1.28 |
| 20% | 90% | 1 | 60 | 1.47 | 1.32 | 5.8% | 1.39 | 1.25 |
| 20% | 90% | 3 | 30 | 1.47 | 1.32 | 0.0% | 1.47 | 1.32 |
| 20% | 90% | 3 | 60 | 1.47 | 1.32 | 0.0% | 1.47 | 1.32 |
| 20% | 70% | 1 | 30 | 1.47 | 1.03 | 3.0% | 1.43 | 1.00 |
| 20% | 70% | 1 | 60 | 1.47 | 1.03 | 5.8% | 1.39 | 0.97 |
| 20% | 70% | 3 | 30 | 1.47 | 1.03 | 0.0% | 1.47 | 1.03 |
| 20% | 70% | 3 | 60 | 1.47 | 1.03 | 0.0% | 1.47 | 1.03 |

### Main table (flow 1.84/day, 20% refused before entry, 90% resolved; executable: 1 slot, 60 min hold)

| Net effect | Dataset | Episodes 80% | Episodes 90% | Scheme setting the upper bound, power there (MC SE) | Research days | Executable days | Research flow/day for 91 / 183 days | Measured $ over test at $50 | Limitations |
| ---: | --- | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| 10 | hyp012b_discovery_formal | 12,001-15,000 | 15,001-20,000 | utc_day, 0.822 (0.012) | 9,062-11,326 | 9,617-12,021 | 183.2-228.9 / 91.1-113.8 | 600-750 | 3 weeks |
| 10 | hyp012c_holdout_in_band | 15,001-20,000 | >20,000 | asset, 0.836 (0.012) | 11,327-15,101 | 12,021-16,027 | 229.0-305.3 / 113.9-151.8 | 750-1,000 | 4 weeks, more assets than observed |
| 10 | hyp029_september | >20,000 | >20,000 | n/a | >15,102 | >16,028 | >305.3 / >151.8 | >1,000 | 4 weeks |
| 25 | hyp012b_discovery_formal | 2,001-2,500 | 3,001-4,000 | utc_day, 0.842 (0.012) | 1,511-1,888 | 1,604-2,003 | 30.5-38.2 / 15.2-19.0 | 250-312 | 3 weeks |
| 25 | hyp012c_holdout_in_band | 2,501-3,000 | 3,001-4,000 | asset, 0.835 (0.012) | 1,888-2,265 | 2,004-2,404 | 38.2-45.8 / 19.0-22.8 | 313-375 | 4 weeks, more assets than observed |
| 25 | hyp029_september | 15,001-20,000 | >20,000 | asset, 0.864 (0.011) | 11,327-15,101 | 12,021-16,027 | 229.0-305.3 / 113.9-151.8 | 1,875-2,500 | 4 weeks, more assets than observed |
| 50 | hyp012b_discovery_formal | 401-1,500 | 601-1,500 | utc_day, 0.999 (0.001) | 303-1,133 | 321-1,202 | 6.1-22.9 / 3.0-11.4 | 100-375 | 3 weeks, utc_day evaluable only from 1,500 |
| 50 | hyp012c_holdout_in_band | 601-800 | 801-1,000 | asset, 0.860 (0.011) | 454-604 | 482-641 | 9.2-12.2 / 4.6-6.1 | 150-200 | 4 weeks, more assets than observed |
| 50 | hyp029_september | 4,001-5,000 | 6,001-8,000 | asset, 0.866 (0.011) | 3,021-3,775 | 3,206-4,007 | 61.1-76.3 / 30.4-37.9 | 1,000-1,250 | 4 weeks, more assets than observed |
| 100 | hyp012b_discovery_formal | 100-1,500 | 126-1,500 | utc_day, 1.000 (0.000) | 76-1,133 | 80-1,202 | 1.5-22.9 / 0.8-11.4 | 50-750 | 3 weeks, asset evaluable only from 125, utc_day evaluable only from 1,500 |
| 100 | hyp012c_holdout_in_band | 151-250 | 201-250 | utc_day, 0.981 (0.004) | 114-189 | 121-200 | 2.3-3.8 / 1.1-1.9 | 76-125 | 4 weeks, utc_day evaluable only from 250 |
| 100 | hyp029_september | 1,001-1,200 | 1,201-1,500 | asset, 0.866 (0.011) | 756-906 | 802-962 | 15.3-18.3 / 7.6-9.1 | 500-600 | 4 weeks, more assets than observed |

### Required mean net bps per opened trade at $50 (20% refused, 1 slot, 60 min hold; unresolved opened trades still count)

| Flow/day | Opened/month | Resolved/month | Opened, outcome unknown/month | cost $0 + target $0 | cost $0 + target $10 | cost $0 + target $50 | cost $10 + target $0 | cost $10 + target $10 | cost $10 + target $50 | cost $25 + target $0 | cost $25 + target $10 | cost $25 + target $50 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.00 | 0.0 | 0.0 | 0.0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| 0.50 | 12.0 | 10.8 | 1.2 | 0 | 167 | 835 | 167 | 334 | 1,002 | 417 | 584 | 1,252 |
| 1.00 | 23.6 | 21.2 | 2.4 | 0 | 85 | 424 | 85 | 170 | 509 | 212 | 297 | 636 |
| 1.84 | 42.2 | 38.0 | 4.2 | 0 | 47 | 237 | 47 | 95 | 284 | 118 | 166 | 355 |
| 3.00 | 66.4 | 59.8 | 6.6 | 0 | 30 | 151 | 30 | 60 | 181 | 75 | 105 | 226 |
| 5.00 | 104.4 | 93.9 | 10.4 | 0 | 19 | 96 | 19 | 38 | 115 | 48 | 67 | 144 |

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
