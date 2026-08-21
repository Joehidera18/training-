from .indicators import ema,rsi,atr,zscore

def regime(rows):
    c=[r["close"] for r in rows]
    if len(c)<220:return "UNKNOWN"
    e50,e200=ema(c,50),ema(c,200);s20=c[-1]/c[-20]-1 if c[-20] else 0
    if e50>e200 and s20>0:return "BULL"
    if e50<e200 and s20<0:return "BEAR"
    return "CHOP"

def liquidity(rows):
    r=rows[-80:];cur=r[-1];prev=r[:-1]
    lo20=min(x["low"] for x in prev[-20:]);hi20=max(x["high"] for x in prev[-20:])
    ranges=[x["high"]-x["low"] for x in prev]
    recent=sum(ranges[-8:])/8;older=sum(ranges[-32:-8])/max(1,len(ranges[-32:-8]))
    full=max(cur["high"]-cur["low"],1e-12)
    lower_wick=(min(cur["open"],cur["close"])-cur["low"])/full
    upper_wick=(cur["high"]-max(cur["open"],cur["close"]))/full
    range_position=(cur["close"]-cur["low"])/full
    return {"sweep_low":cur["low"]<lo20 and cur["close"]>lo20,
            "breakout":cur["close"]>hi20,
            "compression":older>0 and recent/older<.72,
            "lower_wick":lower_wick,"upper_wick":upper_wick,
            "range_position":range_position,"resistance":hi20,"support":lo20}

def _sample_closes(rows,step):
    vals=[r["close"] for r in rows]
    return vals[-1::-step][::-1]

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

    # Approximate higher-timeframe alignment using every 4th and 16th close.
    c4=_sample_closes(rows,4);c16=_sample_closes(rows,16)
    h1_fast,h1_slow=ema(c4,12),ema(c4,36)
    h4_fast,h4_slow=ema(c16,8),ema(c16,20)
    mtf_votes=0
    if h1_fast and h1_slow:mtf_votes += 1 if h1_fast>h1_slow else -1
    if h4_fast and h4_slow:mtf_votes += 1 if h4_fast>h4_slow else -1
    mtf_alignment=mtf_votes/2

    rejection=max(0,lf["lower_wick"]-lf["upper_wick"])
    dist_res_atr=(lf["resistance"]-price)/a if a else 0

    contributions={}
    contributions["trend"]=25 if trend else 0
    contributions["pullback"]=15 if pull else 0
    contributions["rsi"]=15 if params["rsi_min"]<=rv<=params["rsi_max"] else 0
    contributions["volume"]=10 if vz>=params["volume_z_min"] else 0
    contributions["sweep"]=15 if lf["sweep_low"] else 0
    contributions["compression"]=12 if lf["compression"] else 0
    contributions["breakout"]=8 if lf["breakout"] else 0
    contributions["rejection"]=8 if rejection>.25 else 0
    contributions["mtf"]=10 if mtf_alignment>=.5 else (-10 if mtf_alignment<=-.5 else 0)
    contributions["regime"]=-30 if reg=="BEAR" else (-params.get("chop_penalty",6) if reg=="CHOP" else 5)

    # Avoid buying straight into nearby resistance unless it is a true breakout.
    if not lf["breakout"] and 0<=dist_res_atr<.55:
        contributions["resistance_room"]=-14
    elif dist_res_atr>=1.25:
        contributions["resistance_room"]=6
    else:
        contributions["resistance_room"]=0

    features={"rsi":rv,"volume_z":vz,"sweep_low":lf["sweep_low"],"breakout":lf["breakout"],
              "compression":lf["compression"],"trend_strength":trend_strength,"atr_pct":atr_pct,
              "regime":reg,"lower_wick":lf["lower_wick"],"upper_wick":lf["upper_wick"],
              "mtf_alignment":mtf_alignment,"rejection_strength":rejection,
              "distance_to_resistance_atr":dist_res_atr,"range_position":lf["range_position"]}

    edge_detail=None
    edge_bonus=0
    if edge_model is not None:
        edge_detail=edge_model.predict_detail(features)
        # Use lower confidence bound as the gate, not only point estimate.
        min_edge=params.get("min_edge_probability",.48)
        if edge_detail["evidence_samples"]>=40 and edge_detail["lower_bound"]<min_edge-.06:
            return None
        edge_bonus=(edge_detail["probability"]-.5)*24

    score=sum(contributions.values())+edge_bonus
    if score<params["threshold"]:return None

    stop=price-a*params["stop_atr"];risk=price-stop
    if risk<=0:return None
    return {"entry":price,"stop":stop,"target1":price+risk*params["rr1"],"target2":price+risk*params["rr2"],
            "score":round(score,2),"regime":reg,"edge_probability":edge_detail["probability"] if edge_detail else None,
            "edge_detail":edge_detail,"features":features,"contributions":contributions}

BASELINE={"threshold":60,"rsi_min":48,"rsi_max":72,"volume_z_min":-.25,
          "stop_atr":1.2,"rr1":1.6,"rr2":2.5,"chop_penalty":6,"min_edge_probability":.48}
