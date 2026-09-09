from pathlib import Path
import csv, math, bisect

MICRO_FIELDS = {
    "ts","bid","ask","bid_depth","ask_depth","aggressive_buy_quote",
    "aggressive_sell_quote","liquidation_long_quote","liquidation_short_quote",
    "open_interest","funding_rate","basis_pct"
}

def micro_path(data_dir,symbol,interval):
    return Path(data_dir)/f"{symbol}_{interval}_micro.csv"

def _num(v,default=0.0):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def import_micro_rows(data_dir,symbol,interval,rows):
    p=micro_path(data_dir,symbol,interval)
    normalized=[]
    for r in rows:
        if "ts" not in r: continue
        x={"ts":int(float(r["ts"]))}
        for k in MICRO_FIELDS-{"ts"}: x[k]=_num(r.get(k))
        normalized.append(x)
    normalized=list({r["ts"]:r for r in normalized}.values())
    normalized.sort(key=lambda x:x["ts"])
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["ts"]+sorted(MICRO_FIELDS-{"ts"}))
        w.writeheader();w.writerows(normalized)
    return p,len(normalized)

def load_micro_rows(data_dir,symbol,interval):
    p=micro_path(data_dir,symbol,interval)
    if not p.exists(): return []
    out=[]
    with p.open() as f:
        for r in csv.DictReader(f):
            x={"ts":int(float(r["ts"]))}
            for k,v in r.items():
                if k!="ts": x[k]=_num(v)
            out.append(x)
    return out

class MicrostructureIndex:
    def __init__(self,rows):
        self.rows=sorted(rows,key=lambda x:x["ts"]);self.ts=[r["ts"] for r in self.rows]
    def nearest_before(self,ts,max_age_ms):
        if not self.rows:return None
        i=bisect.bisect_right(self.ts,int(ts))-1
        if i<0:return None
        r=self.rows[i]
        return r if int(ts)-r["ts"]<=max_age_ms else None

def enrich_feature(feature,micro):
    f=dict(feature or {})
    if not micro:
        f.update({"micro_available":0,"spread_bps":0.0,"book_imbalance":0.0,
                  "aggressor_imbalance":0.0,"liquidation_imbalance":0.0,
                  "open_interest":0.0,"funding_rate":0.0,"basis_pct":0.0})
        return f
    bid=_num(micro.get("bid"));ask=_num(micro.get("ask"));mid=(bid+ask)/2 if bid>0 and ask>0 else 0
    spread=((ask-bid)/mid*10000) if mid>0 and ask>=bid else 0
    bd=max(0,_num(micro.get("bid_depth")));ad=max(0,_num(micro.get("ask_depth")))
    book=(bd-ad)/max(bd+ad,1e-9)
    buys=max(0,_num(micro.get("aggressive_buy_quote")));sells=max(0,_num(micro.get("aggressive_sell_quote")))
    aggr=(buys-sells)/max(buys+sells,1e-9)
    ll=max(0,_num(micro.get("liquidation_long_quote")));sl=max(0,_num(micro.get("liquidation_short_quote")))
    liq=(sl-ll)/max(sl+ll,1e-9)
    f.update({"micro_available":1,"spread_bps":spread,"book_imbalance":book,
              "aggressor_imbalance":aggr,"liquidation_imbalance":liq,
              "open_interest":_num(micro.get("open_interest")),"funding_rate":_num(micro.get("funding_rate")),
              "basis_pct":_num(micro.get("basis_pct"))})
    return f

def micro_summary(rows):
    if not rows:return {"available":False,"rows":0}
    fs=[enrich_feature({},r) for r in rows]
    avg=lambda k:sum(x[k] for x in fs)/len(fs)
    return {"available":True,"rows":len(rows),"avg_spread_bps":round(avg("spread_bps"),3),
            "avg_book_imbalance":round(avg("book_imbalance"),4),
            "avg_aggressor_imbalance":round(avg("aggressor_imbalance"),4)}
