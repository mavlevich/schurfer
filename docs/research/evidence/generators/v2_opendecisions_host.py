# Self-contained: (A) finalization-lag distribution/SLA, (B) B-completeness
# fraction sensitivity (f=0.99 vs f_ref=0.999) fire-set stability. duckdb+stdlib.
import sys, json, hashlib, math
from datetime import datetime, timezone, timedelta
import duckdb
SRC = sys.argv[1]
CS="2026-08-18 00:00:00+00"; CE="2026-09-10 20:00:00+00"; FLOOR=100000.0; COOLDOWN_MIN=1440
MGRID=[0.10,0.15,0.20,0.25,0.30]; SGRID=[0.10,0.15,0.20,0.25,0.35]
CAL_DAYS=(datetime.fromisoformat(CE)-datetime.fromisoformat(CS)).total_seconds()/86400
con=duckdb.connect(); con.execute("SET memory_limit='2GB'"); con.execute("SET threads=2")
R=f"read_parquet('{SRC}')"
# --- A: lag distribution (seconds past bucket_start; bucket_end = +60s) ---
lag=con.execute(f"""SELECT count(*) n, count(*) FILTER(WHERE created_at IS NULL) null_created,
  round(quantile_cont(extract(epoch FROM (created_at-bucket_start)),0.50),1) p50,
  round(quantile_cont(extract(epoch FROM (created_at-bucket_start)),0.99),1) p99,
  round(quantile_cont(extract(epoch FROM (created_at-bucket_start)),0.999),1) p999,
  round(quantile_cont(extract(epoch FROM (created_at-bucket_start)),0.99999),2) p99999,
  round(max(extract(epoch FROM (created_at-bucket_start))),1) mx,
  count(*) FILTER(WHERE created_at>bucket_start+INTERVAL 75 SECOND) gt_be15,
  count(*) FILTER(WHERE created_at>bucket_start+INTERVAL 120 SECOND) gt_be60,
  count(*) FILTER(WHERE created_at>bucket_start+INTERVAL 300 SECOND) gt_5min
  FROM {R} WHERE bucket_start>=TIMESTAMPTZ '{CS}' AND bucket_start<TIMESTAMPTZ '{CE}'""").fetchone()
lagd=dict(zip(["n","nulls","p50","p99","p999","p99999","max","gt_bucketend_15s","gt_bucketend_60s","gt_5min"],lag))
# --- B: fraction sensitivity ---
def base(s): return s[:-4].upper() if s.upper().endswith("USDT") else s.upper()
def materialize(frac):
    con.execute("DROP TABLE IF EXISTS scored")
    con.execute(f"""CREATE TEMP TABLE scored AS
    WITH src AS (SELECT exchange,symbol,bucket_start,(buy_total_notional_usd-sell_total_notional_usd) net_buy,
        (buy_total_notional_usd+sell_total_notional_usd) activity,trades_complete,
        CASE WHEN created_at IS NOT NULL AND created_at<=bucket_start+INTERVAL 75 SECOND THEN 1 ELSE 0 END timely
      FROM {R} WHERE bucket_start>=TIMESTAMPTZ '{CS}'-INTERVAL 11520 MINUTE AND bucket_start<TIMESTAMPTZ '{CE}'),
    pm AS (SELECT s.*,count(*) OVER t7 t7c,sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER t7 t7k,
        sum(CASE WHEN trades_complete THEN activity ELSE 0 END) OVER t7 t7a FROM src s
      WINDOW t7 AS (PARTITION BY exchange,symbol ORDER BY bucket_start RANGE BETWEEN INTERVAL 10080 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING)),
    fl AS (SELECT *,CASE WHEN t7c=10080 AND t7k>={frac}*10080 THEN 1 ELSE 0 END shape_ready,
        CASE WHEN t7c=10080 AND t7k>={frac}*10080 AND t7k>0 AND activity>(t7a/t7k) AND net_buy>0 THEN 1 ELSE 0 END eb FROM pm),
    r AS (SELECT exchange,symbol,bucket_start,count(*) OVER w wc,sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER w wk,
        sum(timely) OVER w wt,sum(shape_ready) OVER w wsr,sum(net_buy) OVER w wnb,sum(eb) OVER w wev,
        count(*) OVER b bc,sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER b bk,sum(timely) OVER b bt,
        sum(CASE WHEN trades_complete THEN activity ELSE 0 END) OVER b bac FROM fl
      WINDOW w AS (PARTITION BY exchange,symbol ORDER BY bucket_start RANGE BETWEEN INTERVAL 1440 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING),
             b AS (PARTITION BY exchange,symbol ORDER BY bucket_start RANGE BETWEEN INTERVAL 11520 MINUTE PRECEDING AND INTERVAL 1441 MINUTE PRECEDING))
    SELECT exchange,symbol,bucket_start,
      (wc=1440 AND wk=1440 AND wt=1440 AND bc=10080 AND bk>={frac}*10080 AND bt=10080 AND bk>0 AND (bac/bk)*1440>={FLOOR}) elig_m,
      (wc=1440 AND wk=1440 AND wt=1440 AND wsr=1440 AND bc=10080 AND bk>={frac}*10080 AND bt=10080 AND bk>0 AND (bac/bk)*1440>={FLOOR}) elig_s,
      CASE WHEN bk>0 THEN wnb/((bac/bk)*1440) END score_m,wev/1440.0 score_s FROM r""")
def cross(scol,ecol,th):
    rows=con.execute(f"""WITH e AS(SELECT exchange,symbol,bucket_start,{scol} s FROM scored WHERE {ecol}),
      c AS(SELECT e.*,lag(s) OVER(PARTITION BY exchange,symbol ORDER BY bucket_start) p FROM e)
      SELECT exchange,symbol,bucket_start FROM c WHERE bucket_start>=TIMESTAMPTZ '{CS}' AND bucket_start<TIMESTAMPTZ '{CE}' AND s>={th} AND p<{th}""").fetchall()
    kept=[];last={}
    for ex,sy,ts in sorted(rows):
        k=(ex,sy)
        if k in last and ts-last[k]<timedelta(minutes=COOLDOWN_MIN): continue
        kept.append((ex,sy,ts));last[k]=ts
    cl={};wk=set()
    for ex,sy,ts in kept: cl[base(sy)]=cl.get(base(sy),0)+1; wk.add(ts.isocalendar()[:2])
    return {"dedup":len(kept),"assets":len(cl),"weeks":len(wk)}
def ntarget(): return 100/(1-0.05)*1.5
def sel(counts):
    best=None
    for th in sorted(counts):
        c=counts[th];rate=c["dedup"]/CAL_DAYS
        if rate<=0:continue
        rd=ntarget()/rate
        if rd<=120.0 and c["assets"]>=30 and c["weeks"]>=4: best=(th,rd)
    return best
def run(frac):
    materialize(frac)
    m={th:cross("score_m","elig_m",th) for th in MGRID}; s={th:cross("score_s","elig_s",th) for th in SGRID}
    sm=sel(m);ss=sel(s);slow=(sm is None or ss is None)
    win=math.ceil(max(sm[1],ss[1])) if not slow else None
    return {"frac":frac,"P-MAG":m,"P-SHAPE":s,"chosen_m":(sm[0] if sm else None),"chosen_s":(ss[0] if ss else None),"window_days":win,"too_slow":slow}
res={"lag_distribution":lagd,"frac_0_99":run(0.99),"frac_0_999":run(0.999),"cal_days":round(CAL_DAYS,3)}
res["fingerprint_sha256"]=hashlib.sha256(json.dumps(res,sort_keys=True,default=str).encode()).hexdigest()
print(json.dumps(res,indent=2,default=str))
