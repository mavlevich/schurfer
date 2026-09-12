SELECT exchange,
  count(*) n,
  count(*) FILTER (WHERE created_at IS NULL) null_ct,
  round(percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM (created_at-bucket_start)))::numeric,2) p50,
  round(percentile_cont(0.999) WITHIN GROUP (ORDER BY extract(epoch FROM (created_at-bucket_start)))::numeric,2) p999,
  round(percentile_cont(0.99999) WITHIN GROUP (ORDER BY extract(epoch FROM (created_at-bucket_start)))::numeric,2) p99999,
  round(max(extract(epoch FROM (created_at-bucket_start)))::numeric,1) mx,
  count(*) FILTER (WHERE created_at > bucket_start + interval '75 seconds')  gt_be15s,
  count(*) FILTER (WHERE created_at > bucket_start + interval '90 seconds')  gt_be30s,
  count(*) FILTER (WHERE created_at > bucket_start + interval '120 seconds') gt_be60s,
  count(*) FILTER (WHERE created_at > bucket_start + interval '300 seconds') gt_5min
FROM timeseries.bybit_momentum_bars_1m
WHERE market_type='linear' AND capture_version='v1'
  AND bucket_start >= '2026-08-18 00:00+00' AND bucket_start < '2026-09-10 20:00+00'
GROUP BY exchange ORDER BY exchange;
