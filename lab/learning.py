
import math, statistics

FEATURES = ("rsi","volume_z","sweep_low","breakout","compression","trend_strength","atr_pct")

def _bin_value(name, v):
    if name in ("sweep_low","breakout","compression"):
        return int(bool(v))
    if v is None: return "NA"
    x=float(v)
    if name=="rsi":
        return int(x//10)*10
    if name=="volume_z":
        return round(max(-3,min(3,x))*2)/2
    if name=="trend_strength":
        return round(max(-.2,min(.2,x))*100)/2
    if name=="atr_pct":
        return round(max(0,min(.2,x))*1000)/5
    return round(x,2)

class HistoricalEdgeModel:
    """
    Transparent train-only learner.
    Learns smoothed hit rates for feature bins from resolved training trades.
    It never sees the test period during fit.
    """
    def __init__(self, min_samples=8, prior_strength=12):
        self.min_samples=min_samples
        self.prior_strength=prior_strength
        self.base_rate=.5
        self.tables={}
        self.samples=0

    def fit(self, trades):
        resolved=[t for t in trades if t.get("outcome") in ("WIN","LOSS")]
        self.samples=len(resolved)
        wins=sum(t["outcome"]=="WIN" for t in resolved)
        self.base_rate=wins/max(1,len(resolved))
        self.tables={f:{} for f in FEATURES}
        for f in FEATURES:
            buckets={}
            for t in resolved:
                b=_bin_value(f,(t.get("features") or {}).get(f))
                w,n=buckets.get(b,(0,0))
                buckets[b]=(w+int(t["outcome"]=="WIN"),n+1)
            self.tables[f]=buckets
        return self

    def predict(self, features):
        if self.samples<20:
            return self.base_rate
        probs=[]
        for f in FEATURES:
            b=_bin_value(f,(features or {}).get(f))
            w,n=self.tables.get(f,{}).get(b,(0,0))
            if n<self.min_samples: continue
            p=(w+self.base_rate*self.prior_strength)/(n+self.prior_strength)
            probs.append(p)
        if not probs: return self.base_rate
        # Shrink feature consensus toward overall base rate.
        p=sum(probs)/len(probs)
        return .65*p+.35*self.base_rate

    def summary(self):
        return {"samples":self.samples,"base_win_rate":round(self.base_rate*100,2)}
