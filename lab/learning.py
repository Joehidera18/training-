import math

FEATURES=("rsi","volume_z","sweep_low","breakout","compression","trend_strength","atr_pct",
          "mtf_alignment","rejection_strength","distance_to_resistance_atr","range_position")

def _bin(name,v):
    if name in ("sweep_low","breakout","compression"):
        return int(bool(v))
    if v is None:return "NA"
    x=float(v)
    if name=="rsi":return int(x//5)*5
    if name=="volume_z":return round(max(-3,min(3,x))*2)/2
    if name in ("trend_strength","mtf_alignment"):return round(max(-1,min(1,x))*10)/10
    if name=="atr_pct":return round(max(0,min(.20,x))*200)/2
    if name in ("rejection_strength","range_position"):return round(max(0,min(1,x))*10)/10
    if name=="distance_to_resistance_atr":return round(max(-5,min(10,x))*2)/2
    return round(x,2)

def _wilson(w,n,z=1.28):
    if n<=0:return (0.0,1.0)
    p=w/n;d=1+z*z/n
    center=(p+z*z/(2*n))/d
    half=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return max(0,center-half),min(1,center+half)

class HistoricalEdgeModel:
    """
    Transparent train-only learner.
    Learns feature-bin and regime-conditioned hit rates with Bayesian shrinkage.
    Predicts probability + evidence strength, never just a naked confidence number.
    """
    def __init__(self,min_samples=10,prior_strength=18):
        self.min_samples=min_samples;self.prior_strength=prior_strength
        self.base_rate=.5;self.samples=0;self.tables={};self.regime_tables={}

    def fit(self,trades):
        resolved=[t for t in trades if t.get("outcome") in ("WIN","LOSS")]
        self.samples=len(resolved)
        wins=sum(t["outcome"]=="WIN" for t in resolved)
        self.base_rate=wins/max(1,self.samples)
        self.tables={f:{} for f in FEATURES};self.regime_tables={}
        for t in resolved:
            win=int(t["outcome"]=="WIN");reg=str(t.get("regime") or "UNKNOWN")
            rw,rn=self.regime_tables.get(reg,(0,0));self.regime_tables[reg]=(rw+win,rn+1)
            feats=t.get("features") or {}
            for f in FEATURES:
                b=_bin(f,feats.get(f))
                w,n=self.tables[f].get(b,(0,0));self.tables[f][b]=(w+win,n+1)
        return self

    def predict_detail(self,features):
        if self.samples<20:
            return {"probability":self.base_rate,"evidence_samples":self.samples,"features_used":0,
                    "lower_bound":0.0,"upper_bound":1.0}
        weighted=[];effective=0
        for f in FEATURES:
            b=_bin(f,(features or {}).get(f))
            w,n=self.tables.get(f,{}).get(b,(0,0))
            if n<self.min_samples:continue
            p=(w+self.base_rate*self.prior_strength)/(n+self.prior_strength)
            weight=min(1.0,n/40)
            weighted.append((p,weight,n));effective+=n
        reg=str((features or {}).get("regime") or "UNKNOWN")
        rw,rn=self.regime_tables.get(reg,(0,0))
        if rn>=self.min_samples:
            rp=(rw+self.base_rate*self.prior_strength)/(rn+self.prior_strength)
            weighted.append((rp,1.15,min(rn,60)));effective+=rn
        if not weighted:
            return {"probability":self.base_rate,"evidence_samples":0,"features_used":0,
                    "lower_bound":0.0,"upper_bound":1.0}
        raw=sum(p*w for p,w,_ in weighted)/sum(w for _,w,_ in weighted)
        # Strong shrinkage protects against overconfident small samples.
        evidence=min(1.0,effective/180)
        p=self.base_rate*(1-.60*evidence)+raw*(.60*evidence)
        approx_n=max(1,min(120,effective//max(1,len(weighted))))
        approx_w=round(p*approx_n)
        lo,hi=_wilson(approx_w,approx_n)
        return {"probability":p,"evidence_samples":effective,"features_used":len(weighted),
                "lower_bound":lo,"upper_bound":hi}

    def predict(self,features):
        return self.predict_detail(features)["probability"]

    def summary(self):
        regs={k:{"samples":n,"win_rate":round(w/n*100,1) if n else None} for k,(w,n) in self.regime_tables.items()}
        return {"samples":self.samples,"base_win_rate":round(self.base_rate*100,2),"regimes":regs}
