import math,statistics,pickle,hashlib,json
from collections import deque
from pathlib import Path
from .data import data_path,download_binance_history,load_history,validate_history,INTERVAL_MS
from .learning import HistoricalEdgeModel
from .montecarlo import monte_carlo_r_multiples
from .db import save_run,save_feature_trades

BASELINE={"threshold":64,"rsi_min":48,"rsi_max":72,"volume_z_min":-.2,
          "stop_atr":1.2,"rr1":1.7,"rr2":2.6,"chop_penalty":6,"min_edge_probability":.48}

def variants():
    # 24 intentionally separated candidates. Less multiple-testing, far faster than 96 near-duplicates.
    out=[]
    for threshold in (60,66,70):
        for stop in (1.1,1.35):
            for rr2 in (2.4,2.9):
                for edge in (.48,.52):
                    out.append({"threshold":threshold,"rsi_min":48,"rsi_max":72,"volume_z_min":-.2,
                                "stop_atr":stop,"rr1":1.7,"rr2":rr2,"chop_penalty":6,
                                "min_edge_probability":edge})
    return out

def cache_path(data_dir,symbol,interval,rows):
    sig=f"{symbol}|{interval}|{len(rows)}|{rows[0]['ts']}|{rows[-1]['ts']}"
    h=hashlib.sha256(sig.encode()).hexdigest()[:12]
    return Path(data_dir)/f"{symbol}_{interval}_{h}.features.pkl"

def _ema(vals,n):
    out=[None]*len(vals);a=2/(n+1);v=None
    for i,x in enumerate(vals):
        v=x if v is None else a*x+(1-a)*v
        if i>=n-1:out[i]=v
    return out

def build_feature_cache(rows,interval):
    n=len(rows)
    close=[r["close"] for r in rows];open_=[r["open"] for r in rows]
    high=[r["high"] for r in rows];low=[r["low"] for r in rows]
    qv=[r.get("quote_volume",0.0) for r in rows]
    e20=_ema(close,20);e50=_ema(close,50);e200=_ema(close,200)

    # RSI14 rolling simple gains/losses
    rsi=[None]*n;gq=deque();lq=deque();gs=0.0;ls=0.0
    for i in range(1,n):
        d=close[i]-close[i-1];g=max(d,0);l=max(-d,0)
        gq.append(g);lq.append(l);gs+=g;ls+=l
        if len(gq)>14:gs-=gq.popleft();ls-=lq.popleft()
        if len(gq)==14:rsi[i]=100.0 if ls==0 else 100-100/(1+(gs/14)/(ls/14))

    # ATR14
    atr=[None]*n;tq=deque();ts=0.0
    for i in range(1,n):
        tr=max(high[i]-low[i],abs(high[i]-close[i-1]),abs(low[i]-close[i-1]))
        tq.append(tr);ts+=tr
        if len(tq)>14:ts-=tq.popleft()
        if len(tq)==14:atr[i]=ts/14

    # quote-volume rolling z60
    vz=[0.0]*n;vq=deque();vs=0.0;vss=0.0
    for i,x in enumerate(qv):
        vq.append(x);vs+=x;vss+=x*x
        if len(vq)>60:
            y=vq.popleft();vs-=y;vss-=y*y
        if len(vq)>=20:
            m=vs/len(vq);var=max(0,vss/len(vq)-m*m);sd=math.sqrt(var) or 1e-9
            vz[i]=(x-m)/sd

    # Previous-20 rolling high/low using deques, excluding current candle.
    prev_hi=[None]*n;prev_lo=[None]*n;dqhi=deque();dqlo=deque()
    for i in range(n):
        while dqhi and dqhi[0] < i-20:dqhi.popleft()
        while dqlo and dqlo[0] < i-20:dqlo.popleft()
        if i>0:
            if dqhi:prev_hi[i]=high[dqhi[0]]
            if dqlo:prev_lo[i]=low[dqlo[0]]
        # Add current for future indices.
        while dqhi and high[dqhi[-1]]<=high[i]:dqhi.pop()
        while dqlo and low[dqlo[-1]]>=low[i]:dqlo.pop()
        dqhi.append(i);dqlo.append(i)

    ranges=[high[i]-low[i] for i in range(n)]
    prefix=[0.0]
    for x in ranges:prefix.append(prefix[-1]+x)
    def avg(a,b):
        if a<0 or b<=a:return None
        return (prefix[b]-prefix[a])/(b-a)

    interval_ms=INTERVAL_MS.get(interval,900000)
    one_h=max(1,round(3600000/interval_ms))
    four_h=max(1,round(14400000/interval_ms))

    F=[None]*n
    for i in range(240,n-1):
        a=atr[i]
        if not a or not e20[i] or not e50[i] or not e200[i]:continue
        full=max(high[i]-low[i],1e-12)
        lower=(min(open_[i],close[i])-low[i])/full
        upper=(high[i]-max(open_[i],close[i]))/full
        recent=avg(i-7,i+1);older=avg(max(0,i-31),i-7)
        compression=bool(older and recent is not None and recent/older<.72)
        ph=prev_hi[i] if prev_hi[i] is not None else high[i]
        pl=prev_lo[i] if prev_lo[i] is not None else low[i]
        breakout=close[i]>ph
        sweep=low[i]<pl and close[i]>pl
        slope20=close[i]/close[i-20]-1
        regime="BULL" if e50[i]>e200[i] and slope20>0 else ("BEAR" if e50[i]<e200[i] and slope20<0 else "CHOP")
        votes=0
        if i>=one_h:votes += 1 if close[i]>close[i-one_h] else -1
        if i>=four_h:votes += 1 if close[i]>close[i-four_h] else -1
        mtf=votes/2
        dist_res=(ph-close[i])/a
        F[i]={
          "rsi":rsi[i] if rsi[i] is not None else 50.0,"volume_z":vz[i],
          "sweep_low":sweep,"breakout":breakout,"compression":compression,
          "trend_strength":e20[i]/e200[i]-1,"atr_pct":a/close[i],"regime":regime,
          "lower_wick":lower,"upper_wick":upper,"mtf_alignment":mtf,
          "rejection_strength":max(0,lower-upper),"distance_to_resistance_atr":dist_res,
          "range_position":(close[i]-low[i])/full,
          "_atr":a,"_trend":e20[i]>e50[i]>e200[i],
          "_pullback":e20[i]*.995<=close[i]<=e20[i]*1.015,
          "_resistance":ph
        }
    return {"features":F,"interval":interval,"rows":len(rows)}

def ensure_feature_cache(data_dir,symbol,interval,rows):
    path=cache_path(data_dir,symbol,interval,rows)
    if path.exists():
        try:
            with path.open("rb") as f:
                obj=pickle.load(f)
            if obj.get("rows")==len(rows):return path,obj
        except Exception:pass
    obj=build_feature_cache(rows,interval)
    with path.open("wb") as f:pickle.dump(obj,f,pickle.HIGHEST_PROTOCOL)
    return path,obj

def _edge_arrays(features,start,end,edge_model):
    probs=[None]*(end-start);lowers=[None]*(end-start);samples=[0]*(end-start)
    if edge_model is None:return probs,lowers,samples
    for i in range(start,end):
        f=features[i]
        if not f:continue
        d=edge_model.predict_detail(f)
        j=i-start;probs[j]=d["probability"];lowers[j]=d["lower_bound"];samples[j]=d["evidence_samples"]
    return probs,lowers,samples

def dynamic_slippage(bar,qty,base_slip):
    notional=qty*bar["open"];qv=max(bar.get("quote_volume",0),1)
    impact=min(.01,max(0,notional/qv)*.20)
    volatility=max(0,(bar["high"]-bar["low"])/max(bar["open"],1e-9))
    return min(.015,base_slip+impact+volatility*.03)

def _signal_score(f,p,edge_prob=None,edge_lower=None,edge_samples=0):
    if not f:return None
    score=0.0
    score+=25 if f["_trend"] else 0
    score+=15 if f["_pullback"] else 0
    score+=15 if p["rsi_min"]<=f["rsi"]<=p["rsi_max"] else 0
    score+=10 if f["volume_z"]>=p["volume_z_min"] else 0
    score+=15 if f["sweep_low"] else 0
    score+=12 if f["compression"] else 0
    score+=8 if f["breakout"] else 0
    score+=8 if f["rejection_strength"]>.25 else 0
    score+=10 if f["mtf_alignment"]>=.5 else (-10 if f["mtf_alignment"]<=-.5 else 0)
    score+=-30 if f["regime"]=="BEAR" else (-p["chop_penalty"] if f["regime"]=="CHOP" else 5)
    if not f["breakout"] and 0<=f["distance_to_resistance_atr"]<.55:score-=14
    elif f["distance_to_resistance_atr"]>=1.25:score+=6
    if edge_prob is not None:
        if edge_samples>=40 and edge_lower is not None and edge_lower<p["min_edge_probability"]-.06:return None
        score+=(edge_prob-.5)*24
    return score if score>=p["threshold"] else None

def simulate(rows,features,start,end,balance,risk,fee_rate,base_slip,params,edge_model=None,keep_trades=True):
    cash=balance;pos=None;trades=[];curve=[]
    probs,lowers,samples=_edge_arrays(features,start,end,edge_model)
    for i in range(max(start,240),min(end,len(rows)-1)):
        nxt=rows[i+1];f=features[i]
        if pos:
            pos["mfe_price"]=max(pos["mfe_price"],nxt["high"]);pos["mae_price"]=min(pos["mae_price"],nxt["low"])
            lo,hi=nxt["low"],nxt["high"]
            reason=None;ex=None
            if lo<=pos["stop"]:reason="STOP";ex=pos["stop"]
            elif hi>=pos["target2"]:reason="TARGET2";ex=pos["target2"]
            elif hi>=pos["target1"] and not pos["t1_hit"]:
                q=pos["qty"]*.5;sl=dynamic_slippage(nxt,q,base_slip);px=pos["target1"]*(1-sl)
                pro=q*px;ef=pro*fee_rate;cost_leg=pos["cost_remaining"]*(q/pos["qty"])
                pos["realized_partial"]+=pro-ef-cost_leg;cash+=pro-ef
                pos["qty"]-=q;pos["cost_remaining"]-=cost_leg;pos["t1_hit"]=True;pos["stop"]=max(pos["stop"],pos["entry"])
            elif nxt["ts"]-pos["entry_ts"]>24*3600*1000:reason="TIME";ex=nxt["close"]
            if reason:
                sl=dynamic_slippage(nxt,pos["qty"],base_slip);px=ex*(1-sl)
                pro=pos["qty"]*px;ef=pro*fee_rate;total=pos["realized_partial"]+pro-ef-pos["cost_remaining"];cash+=pro-ef
                pos.update({"exit_ts":nxt["ts"],"exit":px,"pnl":total,"reason":reason,"outcome":"WIN" if total>0 else "LOSS"})
                trades.append(pos);pos=None

        if pos is None and f:
            j=i-start
            ep=probs[j] if 0<=j<len(probs) else None
            el=lowers[j] if 0<=j<len(lowers) else None
            es=samples[j] if 0<=j<len(samples) else 0
            sc=_signal_score(f,params,ep,el,es)
            if sc is not None:
                signal_price=rows[i]["close"];stop=signal_price-f["_atr"]*params["stop_atr"]
                provisional=nxt["open"];dist=provisional-stop
                if dist>0:
                    qty=min(cash*risk/dist,(cash*.98)/(provisional*(1+fee_rate+base_slip)))
                    if qty>0:
                        sl=dynamic_slippage(nxt,qty,base_slip);entry=provisional*(1+sl);dist=entry-stop
                        if dist>0:
                            qty=min(cash*risk/dist,(cash*.98)/(entry*(1+fee_rate)))
                            notional=qty*entry;ef=notional*fee_rate;cost=notional+ef
                            if qty>0 and cost<=cash:
                                cash-=cost
                                feat={k:v for k,v in f.items() if not k.startswith("_")}
                                pos={"entry_ts":nxt["ts"],"entry":entry,"signal_entry":signal_price,
                                     "entry_gap_pct":(entry/signal_price-1)*100,"stop":stop,
                                     "target1":entry+dist*params["rr1"],"target2":entry+dist*params["rr2"],
                                     "qty":qty,"qty_initial":qty,"cost_remaining":cost,"risk_dollars":qty*dist,
                                     "t1_hit":False,"realized_partial":0.0,"regime":f["regime"],"features":feat,
                                     "edge_probability":ep,"score":round(sc,2),"mfe_price":entry,"mae_price":entry}
        curve.append(cash+(pos["qty"]*rows[i]["close"] if pos else 0))

    if pos:
        last=rows[min(end,len(rows))-1];sl=dynamic_slippage(last,pos["qty"],base_slip);px=last["close"]*(1-sl)
        pro=pos["qty"]*px;ef=pro*fee_rate;total=pos["realized_partial"]+pro-ef-pos["cost_remaining"];cash+=pro-ef
        pos.update({"exit_ts":last["ts"],"exit":px,"pnl":total,"reason":"END","outcome":"WIN" if total>0 else "LOSS"})
        trades.append(pos)

    for t in trades:
        rd=max(t["risk_dollars"],1e-9);t["r_multiple"]=t["pnl"]/rd
        t["mfe_r"]=(t["mfe_price"]-t["entry"])*t["qty_initial"]/rd
        t["mae_r"]=(t["mae_price"]-t["entry"])*t["qty_initial"]/rd
    pnls=[t["pnl"] for t in trades];wins=[x for x in pnls if x>0];loss=[x for x in pnls if x<0]
    rs=[t["r_multiple"] for t in trades];gp=sum(wins);gl=abs(sum(loss));pf=gp/gl if gl>0 else (999 if gp>0 else None)
    peak=None;dd=0.0
    for e in curve:
        peak=e if peak is None else max(peak,e)
        if peak:dd=max(dd,(peak-e)/peak)
    metrics={"ending_balance":round(cash,2),"return_pct":round((cash/balance-1)*100,2),
             "trades":len(trades),"win_rate":round(len(wins)/len(trades)*100,2) if trades else None,
             "profit_factor":round(pf,3) if pf is not None else None,
             "expectancy_r":round(statistics.mean(rs),3) if rs else None,
             "max_drawdown_pct":round(dd*100,2)}
    return metrics,trades if keep_trades else []

def robustness(m):
    if m["trades"]<18:return -999
    return min(m["profit_factor"] or 0,4)*18+(m["expectancy_r"] or 0)*36-(m["max_drawdown_pct"] or 100)*.95+min(m["trades"],100)*.05

def fold_bounds(n,interval,folds=4):
    start=max(1000,int(n*.35));remaining=n-start;test_size=max(350,remaining//folds)
    embargo=max(2,math.ceil((24*3600*1000)/INTERVAL_MS.get(interval,900000)))
    bounds=[];train_end=start
    for f in range(folds):
        test_start=train_end+embargo
        test_end=n if f==folds-1 else min(n,test_start+test_size)
        if test_end-test_start<300:break
        bounds.append({"fold":f+1,"train_end":train_end,"test_start":test_start,"test_end":test_end,"embargo_bars":embargo})
        train_end=test_end
    return bounds

def pooled_metrics(trades):
    if not trades:return {"trades":0,"avg_win_rate":None,"avg_profit_factor":None,"avg_expectancy_r":None}
    pnls=[t["pnl"] for t in trades];rs=[t["r_multiple"] for t in trades]
    gp=sum(x for x in pnls if x>0);gl=abs(sum(x for x in pnls if x<0)
    )
    return {"trades":len(trades),"avg_win_rate":round(sum(x>0 for x in pnls)/len(pnls)*100,2),
            "avg_profit_factor":round(gp/gl,3) if gl>0 else (999 if gp>0 else None),
            "avg_expectancy_r":round(statistics.mean(rs),3)}

def prepare_job(data_dir,symbol,interval,years):
    path=data_path(data_dir,symbol,interval)
    if not path.exists():download_binance_history(symbol,interval,years,data_dir)
    rows=load_history(path);quality=validate_history(rows,interval)
    if len(rows)<1200:raise ValueError("not enough historical candles")
    cp,cache=ensure_feature_cache(data_dir,symbol,interval,rows)
    return {"data_path":str(path),"cache_path":str(cp),"rows":len(rows),"quality":quality,
            "folds":fold_bounds(len(rows),interval),"candidates":variants()}

def load_prepared(state):
    rows=load_history(Path(state["data_path"]))
    with Path(state["cache_path"]).open("rb") as f:cache=pickle.load(f)
    return rows,cache["features"]

def finalize_result(db_path,config,state):
    all_trades=state.get("all_test_trades",[])
    folds=state.get("fold_results",[])
    pooled=pooled_metrics(all_trades);tests=[f["test"] for f in folds]
    pooled.update({"folds":len(folds),"profitable_folds":sum(t["return_pct"]>0 for t in tests),
                   "worst_drawdown_pct":max([t["max_drawdown_pct"] for t in tests],default=None),
                   "median_fold_return_pct":round(statistics.median([t["return_pct"] for t in tests]),2) if tests else None})
    mc=monte_carlo_r_multiples([t["r_multiple"] for t in all_trades],config["starting_balance"],config["risk_per_trade"],1500,block_size=4)
    best=folds[-1]["best_params"] if folds else BASELINE
    result={"symbol":config["symbol"],"interval":config["interval"],"rows":state["rows"],
            "data_quality":state["quality"],"walk_forward":pooled,"fold_details":folds,
            "monte_carlo":mc,"best_params":best,
            "learning":{"method":"resumable fast walk-forward; regime-aware train-only learner"},
            "architecture":{"resumable":True,"candidate_count":len(state["candidates"]),
                            "checkpoint":"after every candidate and fold"}}
    rid=save_run(db_path,result,config,config.get("notes",""));result["run_id"]=rid
    save_feature_trades(db_path,rid,"walk_forward_test",config["symbol"],all_trades,best)
    return result
