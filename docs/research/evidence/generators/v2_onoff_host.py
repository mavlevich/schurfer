# Self-contained v2 availability on/off comparison + fingerprinted artifact.
# Only duckdb + stdlib, so it runs in the deployed analytics container (no need
# for the not-yet-deployed v2 package). Reads a local parquet/csv.gz, emits JSON.
import sys, json, hashlib, math
from datetime import datetime, timezone, timedelta
import duckdb

SRC = sys.argv[1]  # parquet or csv.gz path
CS = "2026-08-18 00:00:00+00"; CE = "2026-09-10 20:00:00+00"
FRAC = 0.99; FLOOR = 100000.0; COOLDOWN_MIN = 1440
MGRID = [0.10,0.15,0.20,0.25,0.30]; SGRID = [0.10,0.15,0.20,0.25,0.35]
CAL_DAYS = (datetime.fromisoformat(CE) - datetime.fromisoformat(CS)).total_seconds()/86400
con = duckdb.connect(); con.execute("SET memory_limit='2GB'"); con.execute("SET threads=2")
reader = f"read_parquet('{SRC}')" if SRC.endswith(".parquet") else f"read_csv_auto('{SRC}', header=true)"

def materialize(lag):
    con.execute("DROP TABLE IF EXISTS scored")
    con.execute(f"""
    CREATE TEMP TABLE scored AS
    WITH src AS (
      SELECT exchange,symbol,bucket_start,
        (buy_total_notional_usd - sell_total_notional_usd) AS net_buy,
        (buy_total_notional_usd + sell_total_notional_usd) AS activity, trades_complete,
        CASE WHEN created_at IS NOT NULL AND created_at <= bucket_start + INTERVAL 1 MINUTE + INTERVAL {int(lag)} SECOND THEN 1 ELSE 0 END AS timely
      FROM {reader}
      WHERE bucket_start >= TIMESTAMPTZ '{CS}' - INTERVAL 11520 MINUTE AND bucket_start < TIMESTAMPTZ '{CE}'),
    pm AS (SELECT s.*, count(*) OVER t7 AS t7c, sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER t7 AS t7k,
        sum(CASE WHEN trades_complete THEN activity ELSE 0 END) OVER t7 AS t7a
      FROM src s WINDOW t7 AS (PARTITION BY exchange,symbol ORDER BY bucket_start RANGE BETWEEN INTERVAL 10080 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING)),
    fl AS (SELECT *, CASE WHEN t7c=10080 AND t7k>={FRAC}*10080 THEN 1 ELSE 0 END AS shape_ready,
        CASE WHEN t7c=10080 AND t7k>={FRAC}*10080 AND t7k>0 AND activity>(t7a/t7k) AND net_buy>0 THEN 1 ELSE 0 END AS eb FROM pm),
    r AS (SELECT exchange,symbol,bucket_start,
        count(*) OVER w wc, sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER w wk, sum(timely) OVER w wt,
        sum(shape_ready) OVER w wsr, sum(net_buy) OVER w wnb, sum(eb) OVER w wev,
        count(*) OVER b bc, sum(CASE WHEN trades_complete THEN 1 ELSE 0 END) OVER b bk, sum(timely) OVER b bt,
        sum(CASE WHEN trades_complete THEN activity ELSE 0 END) OVER b bac
      FROM fl
      WINDOW w AS (PARTITION BY exchange,symbol ORDER BY bucket_start RANGE BETWEEN INTERVAL 1440 MINUTE PRECEDING AND INTERVAL 1 MINUTE PRECEDING),
             b AS (PARTITION BY exchange,symbol ORDER BY bucket_start RANGE BETWEEN INTERVAL 11520 MINUTE PRECEDING AND INTERVAL 1441 MINUTE PRECEDING))
    SELECT exchange,symbol,bucket_start,
      (wc=1440 AND wk=1440 AND wt=1440 AND bc=10080 AND bk>={FRAC}*10080 AND bt=10080 AND bk>0 AND (bac/bk)*1440>={FLOOR}) AS elig_m,
      (wc=1440 AND wk=1440 AND wt=1440 AND wsr=1440 AND bc=10080 AND bk>={FRAC}*10080 AND bt=10080 AND bk>0 AND (bac/bk)*1440>={FLOOR}) AS elig_s,
      CASE WHEN bk>0 THEN wnb/((bac/bk)*1440) END AS score_m, wev/1440.0 AS score_s
    FROM r""")

def base(sym): return sym[:-4].upper() if sym.upper().endswith("USDT") else sym.upper()
def crossings(scol, ecol, th):
    rows = con.execute(f"""WITH e AS (SELECT exchange,symbol,bucket_start,{scol} s FROM scored WHERE {ecol}),
      c AS (SELECT e.*, lag(s) OVER (PARTITION BY exchange,symbol ORDER BY bucket_start) p FROM e)
      SELECT exchange,symbol,bucket_start FROM c WHERE bucket_start>=TIMESTAMPTZ '{CS}' AND bucket_start<TIMESTAMPTZ '{CE}' AND s>={th} AND p<{th}
      ORDER BY exchange,symbol,bucket_start""").fetchall()
    kept=[]; last={}
    for ex,sy,ts in rows:
        k=(ex,sy)
        if k in last and ts-last[k] < timedelta(minutes=COOLDOWN_MIN): continue
        kept.append((ex,sy,ts)); last[k]=ts
    cl={}; ven=set(); wks=set()
    for ex,sy,ts in kept:
        cl[base(sy)]=cl.get(base(sy),0)+1; ven.add(ex); wks.add(ts.isocalendar()[:2])
    return {"dedup":len(kept),"assets":len(cl),"venues":len(ven),"weeks":len(wks),
            "conc":round(max(cl.values())/len(kept),3) if kept else 0.0}
def ntarget(): return 100/(1-0.05)*1.5
def select(counts):
    best=None
    for th in sorted(counts):
        c=counts[th]; rate=c["dedup"]/CAL_DAYS
        if rate<=0: continue
        rd=ntarget()/rate
        if rd<=120.0 and c["assets"]>=30 and c["weeks"]>=4: best=(th,rate,rd)
    return best
def run(lag):
    materialize(lag)
    m={th:crossings("score_m","elig_m",th) for th in MGRID}
    s={th:crossings("score_s","elig_s",th) for th in SGRID}
    sm=select(m); ss=select(s)
    win=None; slow = (sm is None or ss is None)
    if not slow: win=math.ceil(max(sm[2],ss[2]))
    return {"lag":lag,"P-MAG":m,"P-SHAPE":s,"chosen_m":(sm[0] if sm else None),"chosen_s":(ss[0] if ss else None),"window_days":win,"too_slow":slow}
res={"availability_on":run(15),"availability_off":run(10**9),"cal_days":round(CAL_DAYS,3)}
res["fingerprint_sha256"]=hashlib.sha256(json.dumps(res,sort_keys=True,default=str).encode()).hexdigest()
print(json.dumps(res, indent=2, default=str))
