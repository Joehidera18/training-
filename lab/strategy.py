
from .indicators import ema,rsi,atr,zscore

def regime(rows):
    c=[r["close"] for r in rows]
    if len(c)<220:return "UNKNOWN"
    e50,e200=ema(c,50),ema(c,200)
    s20=c[-1]/c[-20]-1 if c[-20] else 0
    if e50>e200 and s20>0:return "BULL"
    if e50<e200 and s20<0:return "BEAR"
    return "CHOP"

def liquidity(rows):
    r=rows[-80:];cur=r[-1];prev=r[:-1]
    lo=min(x["low"] for x in prev[-20:]);hi=max(x["high"] for x in prev[-20:])
    ranges=[x["high"]-x["low"] for x in prev]
    recent=sum(ranges[-8:])/8;older=sum(ranges[-32:-8])/max(1,len(ranges[-32:-8]))
    lower_wick=(min(cur["open"],cur["close"])-cur["low"])/max(cur["high"]-cur["low"],1e-12)
    upper_wick=(cur["high"]-max(cur["open"],cur["close"]))/max(cur["high"]-cur["low"],1e-12)
    return {
        "sweep_low":cur["low"]<lo and cur["close"]>lo,
        "breakout":cur["close"]>hi,
        "compression":older>0 and recent/older<.72,
        "lower_wick":lower_wick,
        "upper_wick":upper_wick,
    }

def signal(rows,params,edge_model=None):
    if len(rows)<240:return None
    c=[r["close"] for r in rows];q=[r["quote_volume"] for r in rows];price=c[-1]
    a=atr(rows,14)
    if not a or price<=0:return None
    e20,e50,e200=ema(c,20),ema(c,50),ema(c,200)
    rv=rsi(c);vz=zscore(q[-1],q[-60:]);lf=liquidity(rows);reg=regime(rows)
    trend=e20>e50>e200
    pull=e20*.995<=price<=e20*1.015
    trend_strength=(e20/e200-1) if e200 else 0
    atr_pct=a/price

    score=(25 if trend else 0)+(15 if pull else 0)
    score+=(15 if params["rsi_min"]<=rv<=params["rsi_max"] else 0)
    score+=(10 if vz>=params["volume_z_min"] else 0)
    score+=(15 if lf["sweep_low"] else 0)+(12 if lf["compression"] else 0)+(8 if lf["breakout"] else 0)
    score+=(6 if lf["lower_wick"]>.35 else 0)
    if reg=="BEAR":score-=30
    if reg=="CHOP":score-=params.get("chop_penalty",6)
    features={"rsi":rv,"volume_z":vz,"sweep_low":lf["sweep_low"],"breakout":lf["breakout"],
              "compression":lf["compression"],"trend_strength":trend_strength,"atr_pct":atr_pct,
              "regime":reg,"lower_wick":lf["lower_wick"],"upper_wick":lf["upper_wick"]}

    edge_probability=None
    if edge_model is not None:
        edge_probability=edge_model.predict(features)
        if edge_probability<params.get("min_edge_probability",.48):
            return None
        score += (edge_probability-.5)*30

    if score<params["threshold"]:return None
    stop=price-a*params["stop_atr"];risk=price-stop
    if risk<=0:return None
    return {"entry":price,"stop":stop,"target1":price+risk*params["rr1"],"target2":price+risk*params["rr2"],
            "score":round(score,2),"regime":reg,"edge_probability":edge_probability,"features":features}

BASELINE={"threshold":60,"rsi_min":48,"rsi_max":72,"volume_z_min":-.25,"stop_atr":1.2,"rr1":1.6,"rr2":2.5,"chop_penalty":6,"min_edge_probability":.48}
