
import math,statistics,pickle,hashlib,json
from collections import deque
from pathlib import Path
from .data import data_path,download_binance_history,load_history,validate_history,INTERVAL_MS
from .learning import HistoricalEdgeModel
from .montecarlo import monte_carlo_r_multiples
from .db import save_run,save_feature_trades
from .memory import store_resolved_trades,memory_summary,evaluate_challenger

ENGINE_VERSION="strategy-v2.1"

# The baseline is deliberately simple and broad; it seeds the fold learner.
BASELINE={
  "family":"trend_breakout","direction":"LONG","threshold":66,
  "stop_atr":1.5,"rr1":1.4,"rr2":2.2,"min_edge_probability":.48,
  "volume_z_min":0.0,"adx_min":15,"max_gap_atr":.60
}

def variants():
    """
    Six strategy families commonly used by systematic crypto traders:
    trend/range breakout, trend pullback, liquidity reclaim, chop mean reversion,
    bearish breakdown, and bearish rally fade.

    The point is not to assume any one is profitable. Walk-forward testing decides.
    """
    out=[]
    # Each family gets 6 separated configurations = 36 total candidates.
    families=[
      ("trend_breakout","LONG"),
      ("trend_pullback","LONG"),
      ("liquidity_reclaim","LONG"),
      ("mean_reversion","LONG"),
      ("trend_breakdown","SHORT"),
      ("rally_fade","SHORT"),
    ]
    for fam,direction in families:
        if fam in ("trend_breakout","trend_breakdown"):
            for stop,rr2,vol in ((1.25,2.2,0.0),(1.5,2.6,.25),(1.75,3.0,.5)):
                for threshold in (62,68):
                    out.append({"family":fam,"direction":direction,"threshold":threshold,
                                "stop_atr":stop,"rr1":1.35,"rr2":rr2,
                                "min_edge_probability":.48,"volume_z_min":vol,
                                "adx_min":17,"max_gap_atr":.65})
        elif fam in ("trend_pullback","rally_fade"):
            for stop,rr2 in ((1.15,2.0),(1.4,2.4),(1.7,2.8)):
                for threshold in (60,66):
                    out.append({"family":fam,"direction":direction,"threshold":threshold,
                                "stop_atr":stop,"rr1":1.25,"rr2":rr2,
                                "min_edge_probability":.48,"volume_z_min":-.35,
                                "adx_min":15,"max_gap_atr":.45})
        elif fam=="liquidity_reclaim":
            for stop,rr2 in ((1.0,1.9),(1.25,2.3),(1.5,2.7)):
                for threshold in (58,64):
                    out.append({"family":fam,"direction":direction,"threshold":threshold,
                                "stop_atr":stop,"rr1":1.15,"rr2":rr2,
                                "min_edge_probability":.47,"volume_z_min":-.25,
                                "adx_min":0,"max_gap_atr":.40})
        elif fam=="mean_reversion":
            for stop,rr2 in ((1.0,1.5),(1.25,1.8),(1.5,2.0)):
                for threshold in (58,64):
                    out.append({"family":fam,"direction":direction,"threshold":threshold,
                                "stop_atr":stop,"rr1":.9,"rr2":rr2,
                                "min_edge_probability":.47,"volume_z_min":-1.0,
                                "adx_min":0,"max_gap_atr":.35})
    return out

def cache_path(data_dir,symbol,interval,rows):
    sig=f"{ENGINE_VERSION}|{symbol}|{interval}|{len(rows)}|{rows[0]['ts']}|{rows[-1]['ts']}"
    h=hashlib.sha256(sig.encode()).hexdigest()[:12]
    return Path(data_dir)/f"{symbol}_{interval}_{h}.features.pkl"

def _ema(vals,n):
    out=[None]*len(vals);a=2/(n+1);v=None
    for i,x in enumerate(vals):
        v=x if v is None else a*x+(1-a)*v
        if i>=n-1:out[i]=v
    return out

def _rolling_mean_std(vals,n):
    means=[None]*len(vals);stds=[None]*len(vals)
    q=deque();s=0.0;ss=0.0
    for i,x in enumerate(vals):
        q.append(x);s+=x;ss+=x*x
        if len(q)>n:
            y=q.popleft();s-=y;ss-=y*y
        if len(q)==n:
            m=s/n;var=max(0.0,ss/n-m*m)
            means[i]=m;stds[i]=math.sqrt(var)
    return means,stds

def _rolling_extreme(vals,n,mode="max"):
    out=[None]*len(vals);dq=deque()
    for i,x in enumerate(vals):
        while dq and dq[0] < i-n:dq.popleft()
        if i>0 and dq: out[i]=vals[dq[0]]
        if mode=="max":
            while dq and vals[dq[-1]]<=x:dq.pop()
        else:
            while dq and vals[dq[-1]]>=x:dq.pop()
        dq.append(i)
    return out

def build_feature_cache(rows,interval):
    n=len(rows)
    c=[r["close"] for r in rows];o=[r["open"] for r in rows]
    h=[r["high"] for r in rows];l=[r["low"] for r in rows]
    qv=[r.get("quote_volume",0.0) for r in rows]

    e8=_ema(c,8);e20=_ema(c,20);e50=_ema(c,50);e200=_ema(c,200)
    ma20,sd20=_rolling_mean_std(c,20)

    # RSI14
    rsi=[None]*n;gq=deque();lq=deque();gs=ls=0.0
    for i in range(1,n):
        d=c[i]-c[i-1];g=max(d,0);loss=max(-d,0)
        gq.append(g);lq.append(loss);gs+=g;ls+=loss
        if len(gq)>14:gs-=gq.popleft();ls-=lq.popleft()
        if len(gq)==14:rsi[i]=100.0 if ls==0 else 100-100/(1+(gs/14)/(ls/14))

    # ATR14 + ATR50 reference
    atr=[None]*n;tq=deque();ts=0.0
    for i in range(1,n):
        tr=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
        tq.append(tr);ts+=tr
        if len(tq)>14:ts-=tq.popleft()
        if len(tq)==14:atr[i]=ts/14
    atr_fill=[x or 0.0 for x in atr]
    atr50,_=_rolling_mean_std(atr_fill,50)

    # Volume z-score 60
    vz=[0.0]*n;vq=deque();vs=vss=0.0
    for i,x in enumerate(qv):
        vq.append(x);vs+=x;vss+=x*x
        if len(vq)>60:
            y=vq.popleft();vs-=y;vss-=y*y
        if len(vq)>=20:
            m=vs/len(vq);var=max(0, vss/len(vq)-m*m);sd=math.sqrt(var) or 1e-9
            vz[i]=(x-m)/sd

    # Signed quote-volume proxy / CVD-like rolling pressure.
    signed=[0.0]*n
    for i in range(n):
        body=c[i]-o[i]
        rng=max(h[i]-l[i],1e-12)
        signed[i]=qv[i]*max(-1,min(1,body/rng))
    sprefix=[0.0]
    for x in signed:sprefix.append(sprefix[-1]+x)

    # OBV slope proxy
    obv=[0.0]*n
    for i in range(1,n):
        direction=1 if c[i]>c[i-1] else (-1 if c[i]<c[i-1] else 0)
        obv[i]=obv[i-1]+direction*qv[i]

    hi20=_rolling_extreme(h,20,"max");lo20=_rolling_extreme(l,20,"min")
    hi55=_rolling_extreme(h,55,"max");lo55=_rolling_extreme(l,55,"min")

    ranges=[h[i]-l[i] for i in range(n)]
    rp=[0.0]
    for x in ranges:rp.append(rp[-1]+x)
    def ravg(a,b):
        a=max(0,a)
        if b<=a:return None
        return (rp[b]-rp[a])/(b-a)

    # Lightweight ADX14 approximation.
    adx=[None]*n;tr14=plus14=minus14=0.0;trq=deque();pq=deque();mq=deque()
    dxq=deque()
    for i in range(1,n):
        tr=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
        up=h[i]-h[i-1];dn=l[i-1]-l[i]
        plus=up if up>dn and up>0 else 0.0
        minus=dn if dn>up and dn>0 else 0.0
        trq.append(tr);pq.append(plus);mq.append(minus);tr14+=tr;plus14+=plus;minus14+=minus
        if len(trq)>14:
            tr14-=trq.popleft();plus14-=pq.popleft();minus14-=mq.popleft()
        if len(trq)==14 and tr14>0:
            pdi=100*plus14/tr14;mdi=100*minus14/tr14
            dx=100*abs(pdi-mdi)/max(pdi+mdi,1e-9)
            dxq.append(dx)
            if len(dxq)>14:dxq.popleft()
            if len(dxq)==14:adx[i]=sum(dxq)/14

    interval_ms=INTERVAL_MS.get(interval,900000)
    one_h=max(1,round(3600000/interval_ms))
    four_h=max(1,round(14400000/interval_ms))

    F=[None]*n
    for i in range(240,n-1):
        a=atr[i]
        if not a or not e20[i] or not e50[i] or not e200[i]:continue
        full=max(h[i]-l[i],1e-12)
        lower=(min(o[i],c[i])-l[i])/full
        upper=(h[i]-max(o[i],c[i]))/full
        recent=ravg(i-7,i+1);older=ravg(i-31,i-7)
        compression=bool(older and recent is not None and recent/older<.72)
        ph=hi20[i] if hi20[i] is not None else h[i]
        pl=lo20[i] if lo20[i] is not None else l[i]
        ph55=hi55[i] if hi55[i] is not None else ph
        pl55=lo55[i] if lo55[i] is not None else pl
        breakout=c[i]>ph
        breakdown=c[i]<pl
        breakout55=c[i]>ph55
        breakdown55=c[i]<pl55
        sweep_low=l[i]<pl and c[i]>pl
        sweep_high=h[i]>ph and c[i]<ph
        slope20=c[i]/c[i-20]-1
        slope50=c[i]/c[i-50]-1 if i>=50 else 0
        bull=e50[i]>e200[i] and slope20>0
        bear=e50[i]<e200[i] and slope20<0
        regime="BULL" if bull else ("BEAR" if bear else "CHOP")
        votes=0
        if i>=one_h:votes += 1 if c[i]>c[i-one_h] else -1
        if i>=four_h:votes += 1 if c[i]>c[i-four_h] else -1
        mtf=votes/2
        bbz=(c[i]-ma20[i])/max(sd20[i] or 0,1e-9) if ma20[i] is not None else 0
        signed20=(sprefix[i+1]-sprefix[max(0,i-19)])/max(sum(qv[max(0,i-19):i+1]),1e-9)
        obv_slope=(obv[i]-obv[i-20])/max(abs(obv[i-20]),sum(qv[max(0,i-20):i+1]),1e-9)
        atr_regime=a/max(atr50[i] or a,1e-9)
        range_expansion=full/max(ravg(i-20,i) or full,1e-9)
        close_location=(c[i]-l[i])/full
        dist_res=(ph-c[i])/a
        dist_sup=(c[i]-pl)/a
        F[i]={
          "rsi":rsi[i] if rsi[i] is not None else 50.0,"volume_z":vz[i],
          "sweep_low":sweep_low,"sweep_high":sweep_high,
          "breakout":breakout,"breakdown":breakdown,"breakout55":breakout55,"breakdown55":breakdown55,
          "compression":compression,"trend_strength":e20[i]/e200[i]-1,"atr_pct":a/c[i],
          "regime":regime,"lower_wick":lower,"upper_wick":upper,
          "mtf_alignment":mtf,"rejection_strength":max(0,lower-upper),
          "bear_rejection_strength":max(0,upper-lower),
          "distance_to_resistance_atr":dist_res,"distance_to_support_atr":dist_sup,
          "range_position":close_location,"bb_z":bbz,"adx":adx[i] or 0,
          "signed_volume_pressure":signed20,"obv_slope":obv_slope,
          "atr_regime":atr_regime,"range_expansion":range_expansion,
          "momentum20":slope20,"momentum50":slope50,
          "_atr":a,"_trend_long":e8[i]>e20[i]>e50[i] and e50[i]>e200[i],
          "_trend_short":e8[i]<e20[i]<e50[i] and e50[i]<e200[i],
          "_pullback_long":abs(c[i]-e20[i])/a<=.55 and c[i]>=e50[i],
          "_pullback_short":abs(c[i]-e20[i])/a<=.55 and c[i]<=e50[i],
          "_resistance":ph,"_support":pl
        }
    return {"features":F,"interval":interval,"rows":len(rows),"engine_version":ENGINE_VERSION}

def ensure_feature_cache(data_dir,symbol,interval,rows):
    path=cache_path(data_dir,symbol,interval,rows)
    if path.exists():
        try:
            with path.open("rb") as f:obj=pickle.load(f)
            if obj.get("rows")==len(rows) and obj.get("engine_version")==ENGINE_VERSION:return path,obj
        except Exception:pass
    obj=build_feature_cache(rows,interval)
    with path.open("wb") as f:pickle.dump(obj,f,pickle.HIGHEST_PROTOCOL)
    return path,obj

def _edge_arrays(features,start,end,edge_model,direction):
    probs=[None]*(end-start);lowers=[None]*(end-start);samples=[0]*(end-start)
    if edge_model is None:return probs,lowers,samples
    dnum=1 if direction=="LONG" else -1
    for i in range(start,end):
        f=features[i]
        if not f:continue
        ef=dict(f);ef["direction_num"]=dnum
        detail=edge_model.predict_detail(ef)
        j=i-start;probs[j]=detail["probability"];lowers[j]=detail["lower_bound"];samples[j]=detail["evidence_samples"]
    return probs,lowers,samples

def dynamic_slippage(bar,qty,base_slip):
    notional=qty*bar["open"];qv=max(bar.get("quote_volume",0),1)
    impact=min(.012,max(0,notional/qv)*.25)
    volatility=max(0,(bar["high"]-bar["low"])/max(bar["open"],1e-9))
    return min(.02,base_slip+impact+volatility*.035)

def _common_edge_adjustment(score,p,edge_prob,edge_lower,edge_samples):
    if edge_prob is not None:
        score+=(edge_prob-.5)*22
        if edge_samples>=40 and edge_lower is not None:
            if edge_lower<p["min_edge_probability"]-.10:score-=10
            elif edge_lower<p["min_edge_probability"]-.05:score-=5
    return score

def evaluate_signal(f,p,edge_prob=None,edge_lower=None,edge_samples=0):
    if not f:return None,"no_features"
    fam=p["family"];direction=p["direction"];score=0.0
    vol_ok=f["volume_z"]>=p.get("volume_z_min",-99)
    adx_ok=f["adx"]>=p.get("adx_min",0)

    if fam=="trend_breakout":
        if f["regime"]=="BEAR":return None,"wrong_regime"
        score += 24 if f["_trend_long"] else 0
        score += 22 if f["breakout"] else 0
        score += 8 if f["breakout55"] else 0
        score += 12 if adx_ok else -6
        score += 10 if vol_ok else -8
        score += 8 if f["signed_volume_pressure"]>.03 else 0
        score += 7 if f["range_expansion"]>1.15 else 0
        if not f["breakout"]:return None,"no_range_breakout"

    elif fam=="trend_pullback":
        if f["regime"]!="BULL":return None,"wrong_regime"
        score += 26 if f["_trend_long"] else 0
        score += 22 if f["_pullback_long"] else 0
        score += 12 if 42<=f["rsi"]<=62 else -8
        score += 10 if f["mtf_alignment"]>=.5 else -6
        score += 8 if f["signed_volume_pressure"]>=-.03 else -5
        score += 8 if f["rejection_strength"]>.12 else 0
        score += 6 if adx_ok else 0
        if not f["_pullback_long"]:return None,"no_trend_pullback"

    elif fam=="liquidity_reclaim":
        if f["regime"]=="BEAR" and f["mtf_alignment"]<0:return None,"wrong_regime"
        score += 32 if f["sweep_low"] else 0
        score += 18 if f["rejection_strength"]>.25 else 0
        score += 10 if f["range_position"]>.60 else 0
        score += 9 if vol_ok else 0
        score += 8 if f["signed_volume_pressure"]>0 else 0
        score += 7 if f["distance_to_resistance_atr"]>.8 else -6
        if not f["sweep_low"]:return None,"no_liquidity_sweep"

    elif fam=="mean_reversion":
        if f["regime"]!="CHOP":return None,"not_chop"
        score += 24 if f["bb_z"]<=-1.5 else 0
        score += 18 if f["rsi"]<=38 else 0
        score += 16 if f["rejection_strength"]>.20 else 0
        score += 10 if f["range_position"]>.55 else 0
        score += 7 if f["atr_regime"]<1.25 else -10
        score += 6 if f["signed_volume_pressure"]>-0.12 else -5
        if f["bb_z"]>-1.2:return None,"not_oversold_range"

    elif fam=="trend_breakdown":
        if f["regime"]=="BULL":return None,"wrong_regime"
        score += 24 if f["_trend_short"] else 0
        score += 22 if f["breakdown"] else 0
        score += 8 if f["breakdown55"] else 0
        score += 12 if adx_ok else -6
        score += 10 if vol_ok else -8
        score += 8 if f["signed_volume_pressure"]<-.03 else 0
        score += 7 if f["range_expansion"]>1.15 else 0
        if not f["breakdown"]:return None,"no_range_breakdown"

    elif fam=="rally_fade":
        if f["regime"]!="BEAR":return None,"wrong_regime"
        score += 26 if f["_trend_short"] else 0
        score += 22 if f["_pullback_short"] else 0
        score += 12 if 38<=f["rsi"]<=58 else -8
        score += 10 if f["mtf_alignment"]<=-.5 else -6
        score += 9 if f["bear_rejection_strength"]>.12 else 0
        score += 8 if f["signed_volume_pressure"]<=.03 else -5
        score += 6 if adx_ok else 0
        if not f["_pullback_short"]:return None,"no_bear_rally"

    score=_common_edge_adjustment(score,p,edge_prob,edge_lower,edge_samples)
    if score<p["threshold"]:
        if not vol_ok and fam in ("trend_breakout","trend_breakdown"):return None,"weak_volume"
        if edge_prob is not None and edge_samples>=40 and edge_prob<p["min_edge_probability"]:return None,"weak_learned_edge"
        return None,"score_threshold"
    return score,None

def simulate(rows,features,start,end,balance,risk,fee_rate,base_slip,params,edge_model=None,keep_trades=True):
    """
    One-position research simulator with symmetric LONG/SHORT accounting.
    Equity changes only by realized P&L, which keeps long/short families comparable.
    """
    equity=float(balance);pos=None;trades=[];curve=[equity]
    direction=params.get("direction","LONG")
    probs,lowers,samples=_edge_arrays(features,start,end,edge_model,direction)
    funnel={"candles_checked":0,"features_available":0,"qualified_setups":0,
            "entry_attempts":0,"entries_opened":0,"rejections":{}}

    def reject(name):
        funnel["rejections"][name]=funnel["rejections"].get(name,0)+1

    def close_position(px,ts,reason):
        nonlocal equity,pos
        remaining=pos["qty"]
        if pos["direction"]=="LONG":
            gross=(px-pos["entry"])*remaining
        else:
            gross=(pos["entry"]-px)*remaining
        fees=(pos["entry"]*remaining+px*remaining)*fee_rate
        total=pos["realized_partial"]+gross-fees
        equity+=total
        pos.update({"exit_ts":ts,"exit":px,"pnl":total,"reason":reason,
                    "outcome":"WIN" if total>0 else "LOSS"})
        trades.append(pos);pos=None

    for i in range(max(start,240),min(end,len(rows)-1)):
        funnel["candles_checked"]+=1
        nxt=rows[i+1];f=features[i]
        if f:funnel["features_available"]+=1

        if pos:
            if pos["direction"]=="LONG":
                pos["mfe_price"]=max(pos["mfe_price"],nxt["high"])
                pos["mae_price"]=min(pos["mae_price"],nxt["low"])
                stop_hit=nxt["low"]<=pos["stop"]
                t2_hit=nxt["high"]>=pos["target2"]
                t1_hit=nxt["high"]>=pos["target1"]
            else:
                pos["mfe_price"]=min(pos["mfe_price"],nxt["low"])
                pos["mae_price"]=max(pos["mae_price"],nxt["high"])
                stop_hit=nxt["high"]>=pos["stop"]
                t2_hit=nxt["low"]<=pos["target2"]
                t1_hit=nxt["low"]<=pos["target1"]

            # Conservative same-bar assumption: stop is checked before targets.
            if stop_hit:
                sl=dynamic_slippage(nxt,pos["qty"],base_slip)
                px=pos["stop"]*(1-sl) if pos["direction"]=="LONG" else pos["stop"]*(1+sl)
                close_position(px,nxt["ts"],"STOP")
            elif t2_hit:
                sl=dynamic_slippage(nxt,pos["qty"],base_slip)
                px=pos["target2"]*(1-sl) if pos["direction"]=="LONG" else pos["target2"]*(1+sl)
                close_position(px,nxt["ts"],"TARGET2")
            elif t1_hit and not pos["t1_hit"]:
                q=pos["qty"]*.5
                sl=dynamic_slippage(nxt,q,base_slip)
                px=pos["target1"]*(1-sl) if pos["direction"]=="LONG" else pos["target1"]*(1+sl)
                gross=(px-pos["entry"])*q if pos["direction"]=="LONG" else (pos["entry"]-px)*q
                fees=(pos["entry"]*q+px*q)*fee_rate
                pos["realized_partial"]+=gross-fees
                pos["qty"]-=q;pos["t1_hit"]=True;pos["stop"]=pos["entry"]
            elif nxt["ts"]-pos["entry_ts"]>24*3600*1000:
                sl=dynamic_slippage(nxt,pos["qty"],base_slip)
                px=nxt["close"]*(1-sl) if pos["direction"]=="LONG" else nxt["close"]*(1+sl)
                close_position(px,nxt["ts"],"TIME")

        if pos is None and f and equity>1:
            j=i-start
            ep=probs[j] if 0<=j<len(probs) else None
            el=lowers[j] if 0<=j<len(lowers) else None
            es=samples[j] if 0<=j<len(samples) else 0
            sc,reason=evaluate_signal(f,params,ep,el,es)
            if sc is None:
                reject(reason)
            else:
                funnel["qualified_setups"]+=1;funnel["entry_attempts"]+=1
                signal_price=rows[i]["close"];provisional=nxt["open"]
                gap_atr=abs(provisional-signal_price)/max(f["_atr"],1e-9)
                if gap_atr>params.get("max_gap_atr",99):
                    reject("entry_gap_too_large")
                else:
                    sl=dynamic_slippage(nxt,1.0,base_slip)
                    entry=provisional*(1+sl) if direction=="LONG" else provisional*(1-sl)
                    stop=entry-f["_atr"]*params["stop_atr"] if direction=="LONG" else entry+f["_atr"]*params["stop_atr"]
                    dist=abs(entry-stop)
                    if dist<=0:
                        reject("invalid_stop_distance")
                    else:
                        qty_risk=(equity*risk)/dist
                        qty_notional=(equity*.95)/max(entry,1e-9)
                        qty=min(qty_risk,qty_notional)
                        if qty<=0:
                            reject("position_size_zero")
                        else:
                            if direction=="LONG":
                                t1=entry+dist*params["rr1"];t2=entry+dist*params["rr2"]
                            else:
                                t1=entry-dist*params["rr1"];t2=entry-dist*params["rr2"]
                            feat={k:v for k,v in f.items() if not k.startswith("_")}
                            feat["direction_num"]=1 if direction=="LONG" else -1
                            pos={"entry_ts":nxt["ts"],"entry":entry,"signal_entry":signal_price,
                                 "entry_gap_pct":(entry/signal_price-1)*100,"direction":direction,
                                 "strategy_family":params["family"],"stop":stop,"target1":t1,"target2":t2,
                                 "qty":qty,"qty_initial":qty,"risk_dollars":qty*dist,
                                 "t1_hit":False,"realized_partial":0.0,"regime":f["regime"],"features":feat,
                                 "edge_probability":ep,"score":round(sc,2),"mfe_price":entry,"mae_price":entry}
                            funnel["entries_opened"]+=1

        # Mark-to-market curve for drawdown diagnostics.
        mark=equity
        if pos:
            unreal=(rows[i]["close"]-pos["entry"])*pos["qty"] if pos["direction"]=="LONG" else (pos["entry"]-rows[i]["close"])*pos["qty"]
            mark=equity+pos["realized_partial"]+unreal
        curve.append(mark)

    if pos:
        last=rows[min(end,len(rows))-1]
        sl=dynamic_slippage(last,pos["qty"],base_slip)
        px=last["close"]*(1-sl) if pos["direction"]=="LONG" else last["close"]*(1+sl)
        close_position(px,last["ts"],"END")

    for t in trades:
        rd=max(t["risk_dollars"],1e-9);t["r_multiple"]=t["pnl"]/rd
        if t["direction"]=="LONG":
            t["mfe_r"]=(t["mfe_price"]-t["entry"])*t["qty_initial"]/rd
            t["mae_r"]=(t["mae_price"]-t["entry"])*t["qty_initial"]/rd
        else:
            t["mfe_r"]=(t["entry"]-t["mfe_price"])*t["qty_initial"]/rd
            t["mae_r"]=(t["entry"]-t["mae_price"])*t["qty_initial"]/rd

    rs=[t["r_multiple"] for t in trades];wins=[x for x in rs if x>0];loss=[x for x in rs if x<0]
    gp=sum(wins);gl=abs(sum(loss));pf=gp/gl if gl>0 else (999 if gp>0 else None)
    peak=None;dd=0.0
    for e in curve:
        peak=e if peak is None else max(peak,e)
        if peak and e>0:dd=max(dd,(peak-e)/peak)

    funnel["rejections"]=dict(sorted(funnel["rejections"].items(),key=lambda kv:kv[1],reverse=True))
    metrics={"ending_balance":round(equity,2),"return_pct":round((equity/balance-1)*100,2),
             "trades":len(trades),"win_rate":round(len(wins)/len(trades)*100,2) if trades else None,
             "profit_factor":round(pf,3) if pf is not None else None,
             "expectancy_r":round(statistics.mean(rs),3) if rs else None,
             "max_drawdown_pct":round(dd*100,2),"signal_funnel":funnel,
             "family":params["family"],"direction":direction}
    return metrics,trades if keep_trades else []

MIN_TRAIN_TRADES=22

def robustness(m):
    if m["trades"]<MIN_TRAIN_TRADES:return -1e9
    pf=min(m["profit_factor"] or 0,4)
    exp=m["expectancy_r"] or -9
    dd=m["max_drawdown_pct"] or 100
    # Favor positive expectancy first, then PF and drawdown.
    return exp*50+pf*18-dd*.85+min(m["trades"],120)*.04

def fold_bounds(n,interval,folds=4):
    start=max(1000,int(n*.35));remaining=n-start;test_size=max(350,remaining//folds)
    embargo=max(2,math.ceil((24*3600*1000)/INTERVAL_MS.get(interval,900000)))
    bounds=[];train_end=start
    for f in range(folds):
        test_start=train_end+embargo
        test_end=n if f==folds-1 else min(n,test_start+test_size)
        if test_end-test_start<300:break
        bounds.append({"fold":f+1,"train_end":train_end,"test_start":test_start,
                       "test_end":test_end,"embargo_bars":embargo})
        train_end=test_end
    return bounds

def pooled_metrics(trades):
    if not trades:return {"trades":0,"avg_win_rate":None,"avg_profit_factor":None,"avg_expectancy_r":None}
    rs=[t["r_multiple"] for t in trades]
    gp=sum(x for x in rs if x>0);gl=abs(sum(x for x in rs if x<0))
    return {"trades":len(trades),"avg_win_rate":round(sum(x>0 for x in rs)/len(rs)*100,2),
            "avg_profit_factor":round(gp/gl,3) if gl>0 else (999 if gp>0 else None),
            "avg_expectancy_r":round(statistics.mean(rs),3)}

def prepare_job(data_dir,symbol,interval,years):
    path=data_path(data_dir,symbol,interval)
    if not path.exists():download_binance_history(symbol,interval,years,data_dir)
    rows=load_history(path);quality=validate_history(rows,interval)
    if len(rows)<1200:raise ValueError("not enough historical candles")
    cp,cache=ensure_feature_cache(data_dir,symbol,interval,rows)
    return {"data_path":str(path),"cache_path":str(cp),"rows":len(rows),"quality":quality,
            "folds":fold_bounds(len(rows),interval),"candidates":variants(),
            "engine_version":ENGINE_VERSION}

def load_prepared(state):
    rows=load_history(Path(state["data_path"]))
    with Path(state["cache_path"]).open("rb") as f:cache=pickle.load(f)
    return rows,cache["features"]

def finalize_result(db_path,config,state):
    all_trades=state.get("all_test_trades",[])
    folds=state.get("fold_results",[])
    pooled=pooled_metrics(all_trades);tests=[f["test"] for f in folds if f.get("test")]
    pooled.update({"folds":len(folds),"profitable_folds":sum((t.get("return_pct") or 0)>0 for t in tests),
                   "worst_drawdown_pct":max([t.get("max_drawdown_pct") or 0 for t in tests],default=None),
                   "median_fold_return_pct":round(statistics.median([t.get("return_pct") or 0 for t in tests]),2) if tests else None})
    mc=monte_carlo_r_multiples([t["r_multiple"] for t in all_trades],config["starting_balance"],
                               config["risk_per_trade"],1500,block_size=4)
    best=folds[-1].get("best_params") if folds else BASELINE
    aggregate_rejections={}
    aggregate_funnel={"candles_checked":0,"features_available":0,"qualified_setups":0,
                      "entry_attempts":0,"entries_opened":0,"rejections":aggregate_rejections}
    family_stats={}
    for f in folds:
        sf=(f.get("test") or {}).get("signal_funnel") or {}
        for k in ("candles_checked","features_available","qualified_setups","entry_attempts","entries_opened"):
            aggregate_funnel[k]+=int(sf.get(k,0) or 0)
        for reason,count in (sf.get("rejections") or {}).items():
            aggregate_rejections[reason]=aggregate_rejections.get(reason,0)+int(count)
        fam=(f.get("best_params") or {}).get("family")
        if fam:
            fs=family_stats.setdefault(fam,{"folds_selected":0,"test_trades":0,"expectancies":[]})
            fs["folds_selected"]+=1;fs["test_trades"]+=int((f.get("test") or {}).get("trades") or 0)
            if (f.get("test") or {}).get("expectancy_r") is not None:
                fs["expectancies"].append((f["test"]["expectancy_r"]))
    for fam,fs in family_stats.items():
        fs["avg_test_expectancy_r"]=round(statistics.mean(fs.pop("expectancies")),3) if fs["expectancies"] else None
    aggregate_funnel["rejections"]=dict(sorted(aggregate_rejections.items(),key=lambda kv:kv[1],reverse=True))

    result={"symbol":config["symbol"],"interval":config["interval"],"rows":state["rows"],
            "data_quality":state["quality"],"walk_forward":pooled,"signal_funnel":aggregate_funnel,
            "fold_details":folds,"family_selection":family_stats,
            "monte_carlo":mc,"best_params":best,
            "learning":{"method":"persistent regime-aware learner + six strategy families"},
            "architecture":{"resumable":True,"candidate_count":len(state["candidates"]),
                            "checkpoint":"after every candidate and fold","engine_version":ENGINE_VERSION},
            "capabilities":{"long_and_short":True,
                            "strategy_families":["trend_breakout","trend_pullback","liquidity_reclaim",
                                                 "mean_reversion","trend_breakdown","rally_fade"],
                            "features":["EMA trend structure","Donchian 20/55 breakout","RSI","ATR",
                                        "ADX proxy","Bollinger z-score","volume z-score","signed-volume pressure",
                                        "OBV slope","liquidity sweeps","wick rejection","compression",
                                        "range expansion","multi-timeframe momentum","support/resistance distance"]}}
    rid=save_run(db_path,result,config,config.get("notes",""));result["run_id"]=rid
    save_feature_trades(db_path,rid,"walk_forward_test",config["symbol"],all_trades,best or BASELINE)
    newly=store_resolved_trades(db_path,rid,config["symbol"],config["interval"],all_trades)
    decision,champion=evaluate_challenger(db_path,rid,config["symbol"],config["interval"],
                                         best or BASELINE,pooled,mc)
    result["persistent_learning"]={"new_unique_trades_learned":newly,
        "memory":memory_summary(db_path,config["symbol"],config["interval"]),
        "challenger_decision":decision,"champion":champion}
    con=None
    try:
        from .db import connect
        con=connect(db_path);con.execute("UPDATE runs SET result_json=? WHERE id=?",
          (json.dumps(result,separators=(",",":"),default=str),rid));con.commit()
    finally:
        if con:con.close()
    return result
