# v2 calibration evidence generators (2026-09-12)

The exact, self-contained scripts that produced the v2 calibration evidence
artifacts, committed so the numbers are reproducible (review round 5, P1). They
were run on 2026-09-12 against a read-only export of the decision window from the
live PostgreSQL, inside the deployed `docker-analytics` container.

- `lag_sla_query.sql` -> `net-buy-accumulation-v2-lag-sla` (finalization-lag
  distribution). Run: `docker exec -i schurfer-postgres psql -U schurfer -d
schurfer < lag_sla_query.sql`.
- `v2_onoff_host.py` -> `net-buy-accumulation-v2-calibration-onoff` (availability
  on/off fire-level parity). Run in the analytics container over a parquet of the
  window: `python v2_onoff_host.py /data/window.parquet`.
- `v2_opendecisions_host.py` -> lag distribution + B-fraction (0.99 vs 0.999)
  sensitivity (the Rule A run; interrupted on prod for load and NOT completed --
  kept only as the intended generator; the real Rule A run belongs in an isolated
  environment).
- `v2_liquidity_floor.py` -> `net-buy-accumulation-v2-liquidity-floor` (executability
  participation floor at theta=0.25). Runs the AVAILABILITY-OFF eligibility on the
  local window subset (no `created_at`; sound because on/off parity is delta 0) and
  reduces fires with the maintained `net_buy_accumulation_v2_liquidity` package. Run:
  `uv run --package schurfer-analytics python
docs/research/evidence/generators/v2_liquidity_floor.py`.

IMPORTANT caveats (do not treat these as the frozen tool):

- These are STANDALONE (they duplicate the package SQL) because the v2 package was
  not deployed to prod at the time. They MIRROR `net_buy_accumulation_v2_repository`
  but are not the package; future runs must use the package in an ISOLATED
  environment, never the live prod host (a prod run degraded the box).
- They predate rev.7: they use the OLD diversity gate (any ISO weeks, no 20/week)
  and a candidate `lag=15s`. So the availability on/off PARITY they show (delta 0)
  stands, but the threshold/window SELECTION in the on/off artifact (0.30/0.35/97d)
  is SUPERSEDED by the rev.7 contract diversity gate and is exploratory only.
- The exported window parquet and csv are NOT committed (multi-GB); the raw data is
  preserved in the `db-2026-09-12` borg pg_dump.
