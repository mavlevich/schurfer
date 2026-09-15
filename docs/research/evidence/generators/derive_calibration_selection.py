# Reproducible derivation of the v2 calibration SELECTION (theta + prospective
# window) from the committed real-data fire counts in the on/off artifact, using
# the maintained package algorithm with the rev.7-corrected weekly-rate gate.
# Run: python docs/research/evidence/generators/derive_calibration_selection.py
import json, hashlib
from pathlib import Path
from schurfer_analytics.net_buy_accumulation_v2_calibration import (
    ThresholdCount, n_target, select_primary, decide_window, PRIMARY_MAG, PRIMARY_SHAPE)
EV = Path(__file__).resolve().parents[1]
d = json.loads((EV / "net-buy-accumulation-v2-calibration-onoff.json").read_text())
on = d["availability_on"]; cal_days = d["cal_days"]
def counts(prim):
    return {float(th): ThresholdCount(prim, float(th), c["dedup"], c["dedup"], c["assets"],
            c["venues"], c["weeks"], {}, 0, c["conc"]) for th, c in on[prim].items()}
tgt = n_target(expected_unresolved_rate=0.05, sizing_margin=1.5)
sels = [select_primary(p, counts(p), calibration_days=cal_days, n_target_fires=tgt,
        max_window_days=120.0) for p in (PRIMARY_MAG, PRIMARY_SHAPE)]
dec = decide_window(sels)
out = {
  "source_artifact": "net-buy-accumulation-v2-calibration-onoff.json",
  "source_fingerprint": d["fingerprint_sha256"],
  "cal_days": cal_days, "n_target_fires": tgt,
  "gate": {"weekly_min_fires": 20, "min_clusters": 30, "max_window_days": 120,
           "note": "weekly RATE proxy on calibration window; 4-full-week floor is a cohort read-time check"},
  "per_primary": [s.__dict__ for s in dec.per_primary],
  "window_days": dec.window_days, "too_slow": dec.too_slow,
}
out["fingerprint_sha256"] = hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
print(json.dumps(out, indent=2, sort_keys=True, default=str))
