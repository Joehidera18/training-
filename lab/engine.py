import itertools,statistics,math
from .data import data_path,download_binance_history,load_history,validate_history,INTERVAL_MS
from .strategy import signal,BASELINE
from .db import save_run,save_feature_trades
from .learning import HistoricalEdgeModel
from .montecarlo import monte_carlo_r_multiples

LOOKBACK=360

def dynamic_slippage(bar,qty,base_slip):
    notional=qty*bar["open"];qv=max(bar.get("quote_volume",0),1)
    participation=notional/qv
    impact=min(.01,max(0,participation)*.20)
    volatility=max(0,(bar["high"]-bar["low"])/max(bar["open"],1e-9))
    return min(.015,base_slip+impact+volatility*.03)

def fee(n,r):return max(0,n)*r

def backtest(rows,balance,risk,fee_rate,base_slip,params,edge_model=None,trade_start=240):
    cash=balance;pos=None;trades=[];curve=[]
    trade_start=max(240,trade_start)
    for i in range(trade_start,len(rows)-1):
        # Critical performance fix: do NOT copy the entire past on every candle.
        hist=rows[max(0,i-LOOKBACK+1):i+1];nxt=rows[i+1]
        if pos:
            # Track excursion before possible exit.
            pos["mfe_price"]=max(pos["mfe_price"],nxt["high"])
            pos["mae_price"]=min(pos["mae_price"],nxt["low"])
            lo,hi=nxt["low"],nxt["high"]
            if lo<=pos["stop"]:
                slip=dynamic_slippage(nxt,pos["qty"],base_slip);ex=pos["stop"]*(1-slip)
                pro=pos["qty"]*ex;ef=fee(pro,fee_rate)
                leg_pnl=pro-ef-pos["cost_remaining"]
                cash+=pro-ef
                total_pnl=pos["realized_partial"]+leg_pnl
                pos.update({"exit_ts":nxt["ts"],"exit":ex,"pnl":total_pnl,"reason":"STOP",
                            "outcome":"WIN" if total_pnl>0 else "LOSS"});trades.append(pos);pos=None
            elif hi>=pos["target2"]:
                slip=dynamic_slippage(nxt,pos["qty"],base_slip);ex=pos["target2"]*(1-slip)
                pro=pos["qty"]*ex;ef=fee(pro,fee_rate)
                leg_pnl=pro-ef-pos["cost_remaining"];cash+=pro-ef
                total_pnl=pos["realized_partial"]+leg_pnl
                pos.update({"exit_ts":nxt["ts"],"exit":ex,"pnl":total_pnl,"reason":"TARGET2",
                            "outcome":"WIN" if total_pnl>0 else "LOSS"});trades.append(pos);pos=None
            elif hi>=pos["target1"] and not pos["t1_hit"]:
                q=pos["qty"]*.5;slip=dynamic_slippage(nxt,q,base_slip);ex=pos["target1"]*(1-slip)
                pro=q*ex;ef=fee(pro,fee_rate)
                # Correct partial-exit accounting.
                cost_leg=pos["cost_remaining"]*(q/pos["qty"])
                partial_pnl=pro-ef-cost_leg
                cash+=pro-ef
                pos["qty"]-=q
                pos["cost_remaining"]-=cost_leg
                pos["realized_partial"]+=partial_pnl
                pos["t1_hit"]=True
                pos["stop"]=max(pos["stop"],pos["entry"])
            elif nxt["ts"]-pos["entry_ts"]>24*3600*1000:
                slip=dynamic_slippage(nxt,pos["qty"],base_slip);ex=nxt["close"]*(1-slip)
                pro=pos["qty"]*ex;ef=fee(pro,fee_rate)
                leg_pnl=pro-ef-pos["cost_remaining"];cash+=pro-ef
                total_pnl=pos["realized_partial"]+leg_pnl
                pos.update({"exit_ts":nxt["ts"],"exit":ex,"pnl":total_pnl,"reason":"TIME",
                            "outcome":"WIN" if total_pnl>0 else "LOSS"});trades.append(pos);pos=None

        if pos is None:
            s=signal(hist,params,edge_model=edge_model)
            if s:
                provisional=nxt["open"]
                dist=provisional-s["stop"]
                if dist>0:
                    rb=cash*risk;qty=min(rb/dist,(cash*.98)/(provisional*(1+fee_rate+base_slip)))
                    if qty>0:
                        slip=dynamic_slippage(nxt,qty,base_slip);entry=provisional*(1+slip)
                        dist=entry-s["stop"]
                        if dist>0:
                            qty=min(cash*risk/dist,(cash*.98)/(entry*(1+fee_rate)))
                            notional=qty*entry;entry_fee=fee(notional,fee_rate);cost=notional+entry_fee
                            if qty>0 and cost<=cash:
                                cash-=cost
                                # Preserve intended R multiples from the ACTUAL fill, not the signal close.
                                t1=entry+dist*params["rr1"];t2=entry+dist*params["rr2"]
                                pos={"entry_ts":nxt["ts"],"entry":entry,"signal_entry":s["entry"],"entry_gap_pct":(entry/s["entry"]-1)*100,
                                     "stop":s["stop"],"target1":t1,"target2":t2,
                                     "qty":qty,"qty_initial":qty,"cost_remaining":cost,"entry_fee":entry_fee,
                                     "risk_dollars":qty*dist,"t1_hit":False,"realized_partial":0.0,
                                     "regime":s["regime"],"features":s["features"],"edge_probability":s.get("edge_probability"),
                                     "edge_detail":s.get("edge_detail"),"score":s["score"],"contributions":s.get("contributions"),
                                     "mfe_price":entry,"mae_price":entry}
        mark=cash+(pos["qty"]*rows[i]["close"] if pos else 0)
        curve.append(mark)

    if pos:
        last=rows[-1];slip=dynamic_slippage(last,pos["qty"],base_slip);ex=last["close"]*(1-slip)
        pro=pos["qty"]*ex;ef=fee(pro,fee_rate)
        total_pnl=pos["realized_partial"]+(pro-ef-pos["cost_remaining"]);cash+=pro-ef
        pos.update({"exit_ts":last["ts"],"exit":ex,"pnl":total_pnl,"reason":"END",
                    "outcome":"WIN" if total_pnl>0 else "LOSS"});trades.append(pos)

    for x in trades:
        riskd=max(x["risk_dollars"],1e-9)
        x["r_multiple"]=x["pnl"]/riskd
        x["mfe_r"]=(x["mfe_price"]-x["entry"])*x["qty_initial"]/riskd
        x["mae_r"]=(x["mae_price"]-x["entry"])*x["qty_initial"]/riskd

    pnls=[x["pnl"] for x in trades];wins=[x for x in pnls if x>0];loss=[x for x in pnls if x<0]
    rs=[x["r_multiple"] for x in trades]
    gp=sum(wins);gl=abs(sum(loss));pf=gp/gl if gl>0 else (999 if gp>0 else None)
    peak=None;dd=0
    for e in curve:
        peak=e if peak is None else max(peak,e)
        if peak:dd=max(dd,(peak-e)/peak)
    by_regime={}
    for tr in trades:
        b=by_regime.setdefault(tr.get("regime","UNKNOWN"),{"trades":0,"wins":0,"r":[]})
        b["trades"]+=1;b["wins"]+=int(tr["pnl"]>0);b["r"].append(tr["r_multiple"])
    regime_stats={k:{"trades":v["trades"],"win_rate":round(v["wins"]/v["trades"]*100,1),
                     "expectancy_r":round(statistics.mean(v["r"]),3)} for k,v in by_regime.items()}
    return {"ending_balance":round(cash,2),"net_pnl":round(cash-balance,2),
            "return_pct":round((cash/balance-1)*100,2),"trades":len(trades),
            "win_rate":round(len(wins)/len(trades)*100,2) if trades else None,
            "profit_factor":round(pf,3) if pf is not None else None,
            "expectancy_r":round(statistics.mean(rs),3) if rs else None,
            "max_drawdown_pct":round(dd*100,2),"gross_profit":round(gp,2),"gross_loss":round(gl,2),
            "by_regime":regime_stats,"trade_log":trades}

def variants():
    # 96 robust candidates instead of 432 near-duplicates: faster and less multiple-testing pressure.
    for th,st,r1,r2,chop,edge in itertools.product([60,64,68],[1.1,1.3],[1.6,1.8],[2.4,2.8],[4,8],[.48,.52]):
        yield {"threshold":th,"rsi_min":48,"rsi_max":72,"volume_z_min":-.2,
               "stop_atr":st,"rr1":r1,"rr2":r2,"chop_penalty":chop,"min_edge_probability":edge}

def robustness(m):
    if not m["trades"] or m["trades"]<18:return -999
    pf=min(m["profit_factor"] or 0,4)
    exp=m["expectancy_r"] or 0;dd=m["max_drawdown_pct"] or 100
    return pf*18+exp*36-dd*.95+min(m["trades"],100)*.05

def _pooled_metrics(trades):
    if not trades:return {"trades":0,"win_rate":None,"profit_factor":None,"expectancy_r":None}
    pnls=[t["pnl"] for t in trades];rs=[t["r_multiple"] for t in trades]
    gp=sum(x for x in pnls if x>0);gl=abs(sum(x for x in pnls if x<0))
    return {"trades":len(trades),"win_rate":round(sum(x>0 for x in pnls)/len(pnls)*100,2),
            "profit_factor":round(gp/gl,3) if gl>0 else (999 if gp>0 else None),
            "expectancy_r":round(statistics.mean(rs),3)}

def walk_forward(rows,balance,risk,fee_rate,slip,interval,folds=4,progress=None):
    n=len(rows);start=max(1000,int(n*.35));remaining=n-start;test_size=max(350,remaining//folds)
    interval_ms=INTERVAL_MS.get(interval,900000)
    embargo_bars=max(2,math.ceil((24*3600*1000)/interval_ms))
    results=[];all_test_trades=[];train_end=start
    candidate_list=list(variants())

    for fold in range(folds):
        test_start=train_end+embargo_bars
        test_end=n if fold==folds-1 else min(n,test_start+test_size)
        if test_end-test_start<300:break
        train=rows[:train_end]

        if progress:progress(18+fold*18,f"Fold {fold+1}/{folds}: learning historical edge…")
        base_train=backtest(train,balance,risk,fee_rate,slip,BASELINE,None)
        edge=HistoricalEdgeModel().fit(base_train["trade_log"])

        candidates=[]
        for idx,p in enumerate(candidate_list):
            m=backtest(train,balance,risk,fee_rate,slip,p,edge)
            candidates.append((robustness(m),p,m))
            if progress and idx%24==0:
                progress(20+fold*18+min(12,int(idx/len(candidate_list)*12)),
                         f"Fold {fold+1}/{folds}: testing strategy {idx+1}/{len(candidate_list)}…")
        candidates.sort(key=lambda x:x[0],reverse=True)
        _,best,trainm=candidates[0]

        # Include past context for indicators, but only allow trades in the unseen test range.
        context_start=max(0,test_start-LOOKBACK)
        test_context=rows[context_start:test_end]
        trade_start=test_start-context_start
        testm=backtest(test_context,balance,risk,fee_rate,slip,best,edge,trade_start=trade_start)

        results.append({"fold":fold+1,"train_rows":len(train),"test_rows":test_end-test_start,
                        "embargo_bars":embargo_bars,"candidate_count":len(candidate_list),"best_params":best,
                        "edge_model":edge.summary(),
                        "train":{k:v for k,v in trainm.items() if k!="trade_log"},
                        "test":{k:v for k,v in testm.items() if k!="trade_log"},
                        "test_trades":testm["trade_log"]})
        all_test_trades.extend(testm["trade_log"]);train_end=test_end
        if progress:progress(34+fold*18,f"Fold {fold+1}/{folds} complete.")

    return results,all_test_trades

def aggregate_walk_forward(folds,trades):
    pooled=_pooled_metrics(trades)
    tests=[f["test"] for f in folds]
    returns=[x["return_pct"] for x in tests]
    return {"folds":len(folds),"trades":pooled["trades"],"avg_win_rate":pooled["win_rate"],
            "avg_profit_factor":pooled["profit_factor"],"avg_expectancy_r":pooled["expectancy_r"],
            "median_fold_return_pct":round(statistics.median(returns),2) if returns else None,
            "worst_drawdown_pct":max([x["max_drawdown_pct"] for x in tests],default=None),
            "profitable_folds":sum(x["return_pct"]>0 for x in tests)}

def run_experiment(db_path,data_dir,symbol,interval,years,balance,risk,fee_rate,slip,
                   train_fraction,strategy_name,download_if_missing,notes="",progress=None):
    path=data_path(data_dir,symbol,interval)
    if not path.exists():
        if not download_if_missing:raise FileNotFoundError(path)
        if progress:progress(5,"Downloading historical candles…")
        download_binance_history(symbol,interval,years,data_dir)
    rows=load_history(path)
    quality=validate_history(rows,interval)
    if len(rows)<1200:raise ValueError("not enough historical candles for robust walk-forward testing")
    if progress:progress(12,"Validating historical data and preparing folds…")

    folds,trades=walk_forward(rows,balance,risk,fee_rate,slip,interval,folds=4,progress=progress)
    agg=aggregate_walk_forward(folds,trades)
    if progress:progress(92,"Running block-bootstrap Monte Carlo stress test…")
    rs=[t.get("r_multiple") for t in trades]
    mc=monte_carlo_r_multiples(rs,balance,risk,1500,block_size=4)

    final_params=folds[-1]["best_params"] if folds else BASELINE
    stable_keys=("threshold","stop_atr","rr1","rr2","chop_penalty","min_edge_probability")
    stability={}
    if folds:
        for k in stable_keys:
            vals=[f["best_params"][k] for f in folds]
            stability[k]={"values":vals,"unique":len(set(vals))}
    avg_gap=statistics.mean(abs(t.get("entry_gap_pct",0)) for t in trades) if trades else None
    avg_mfe=statistics.mean(t.get("mfe_r",0) for t in trades) if trades else None
    avg_mae=statistics.mean(t.get("mae_r",0) for t in trades) if trades else None

    result={"symbol":symbol,"interval":interval,"rows":len(rows),"data_quality":quality,
            "walk_forward":agg,
            "fold_details":[{k:v for k,v in f.items() if k!="test_trades"} for f in folds],
            "monte_carlo":mc,"best_params":final_params,"parameter_stability":stability,
            "execution_diagnostics":{"avg_abs_entry_gap_pct":round(avg_gap,3) if avg_gap is not None else None,
                                     "avg_mfe_r":round(avg_mfe,3) if avg_mfe is not None else None,
                                     "avg_mae_r":round(avg_mae,3) if avg_mae is not None else None},
            "learning":{"method":"regime-aware Bayesian-shrunk feature learner; training data only",
                        "last_fold":folds[-1]["edge_model"] if folds else None},
            "assumptions":{"starting_balance":balance,"risk_per_trade":risk,"fee_rate":fee_rate,
                           "base_slippage":slip,"same_bar_policy":"stop first","max_hold_hours":24}}
    config={"symbol":symbol,"interval":interval,"years":years,"balance":balance,"risk":risk,
            "fee":fee_rate,"slip":slip}
    run_id=save_run(db_path,result,config,notes);result["run_id"]=run_id
    save_feature_trades(db_path,run_id,"walk_forward_test",symbol,trades,final_params)
    if progress:progress(99,"Saving experiment results…")
    return result
