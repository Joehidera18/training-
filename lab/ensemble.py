import statistics

def probability_calibration(trades,bins=5):
    pts=[t for t in trades if t.get("edge_probability") is not None]
    if not pts:return {"samples":0,"brier_score":None,"bins":[]}
    brier=sum((float(t["edge_probability"])-(1 if t.get("outcome")=="WIN" else 0))**2 for t in pts)/len(pts)
    out=[]
    for b in range(bins):
        lo=b/bins;hi=(b+1)/bins
        bucket=[t for t in pts if lo<=float(t["edge_probability"])<=(hi if b==bins-1 else hi-1e-12)]
        if bucket:
            out.append({"range":[round(lo,2),round(hi,2)],"samples":len(bucket),
                        "predicted":round(statistics.mean(float(t["edge_probability"]) for t in bucket),3),
                        "actual":round(sum(t.get("outcome")=="WIN" for t in bucket)/len(bucket),3)})
    return {"samples":len(pts),"brier_score":round(brier,4),"bins":out}

def dynamic_risk_multiplier(edge_probability,evidence_samples,regime_fit=1.0):
    if edge_probability is None or evidence_samples<25:return .35
    p=float(edge_probability)
    if p<.48:return .25
    if p<.53:return .40
    if p<.58:return .60
    if p<.63:return .80
    return min(1.0,.90+.10*max(0,min(1,regime_fit)))

def ensemble_vote(specialists):
    valid=[s for s in specialists if s.get("probability") is not None and s.get("weight",0)>0]
    if not valid:return {"probability":None,"agreement":0,"direction":None,"members":0}
    total=sum(s["weight"] for s in valid)
    p=sum(float(s["probability"])*s["weight"] for s in valid)/total
    dirs={"LONG":0.0,"SHORT":0.0}
    for s in valid:
        if s.get("direction") in dirs:dirs[s["direction"]]+=s["weight"]
    direction=max(dirs,key=dirs.get) if max(dirs.values())>0 else None
    agreement=max(dirs.values())/total if total else 0
    return {"probability":round(p,4),"agreement":round(agreement,4),"direction":direction,"members":len(valid)}
