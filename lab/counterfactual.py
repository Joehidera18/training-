import statistics

def forward_outcome(rows,i,direction,atr,horizon=12):
    if i+1>=len(rows) or not atr:return None
    entry=rows[i+1]["open"];future=rows[i+1:min(len(rows),i+1+horizon)]
    if entry<=0 or not future:return None
    if direction=="LONG":
        mfe=(max(r["high"] for r in future)-entry)/atr
        mae=(min(r["low"] for r in future)-entry)/atr
        final=(future[-1]["close"]-entry)/atr
    else:
        mfe=(entry-min(r["low"] for r in future))/atr
        mae=(entry-max(r["high"] for r in future))/atr
        final=(entry-future[-1]["close"])/atr
    return {"mfe_atr":mfe,"mae_atr":mae,"final_atr":final,"would_hit_1r":mfe>=1,"would_hit_2r":mfe>=2}

def summarize_counterfactuals(samples):
    if not samples:return {"samples":0}
    return {"samples":len(samples),
            "would_hit_1r_pct":round(sum(s["would_hit_1r"] for s in samples)/len(samples)*100,2),
            "would_hit_2r_pct":round(sum(s["would_hit_2r"] for s in samples)/len(samples)*100,2),
            "avg_mfe_atr":round(statistics.mean(s["mfe_atr"] for s in samples),3),
            "avg_mae_atr":round(statistics.mean(s["mae_atr"] for s in samples),3),
            "avg_final_atr":round(statistics.mean(s["final_atr"] for s in samples),3)}
