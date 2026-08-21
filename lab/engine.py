
import itertools,statistics,math
from .data import data_path,download_binance_history,load_history
from .strategy import signal,BASELINE
from .db import save_run,save_feature_trades
from .learning import HistoricalEdgeModel
from .montecarlo import monte_carlo_r_multiples

def dynamic_slippage(bar, qty, base_slip):
    # Heuristic market-impact model from participation in quote volume.
    notional=qty*bar["open"]
    qv=max(bar.get("quote_volume",0),1)
    participation=notional/qv
    impact=min(.01, max(0, participation)*.20)
    volatility=max(0,(bar["high"]-bar["low"])/max(bar["open"],1e-9))
    return min(.015,base_slip+impact+volatility*.03)

def fee(n,r):return max(0,n)*r

def backtest(rows,balance,risk,fee_rate,base_slip,params,edge_model=None):
    cash=balance;pos=None;trades=[];curve=[]
    for i in range(240,len(rows)-1):
        hist=rows[:i+1];nxt=rows[i+1]
        if pos:
            lo,hi=nxt["low"],nxt["high"]
            # Conservative same-candle ordering: stop first.
            if lo<=pos["stop"]:
                slip=dynamic_slippage(nxt,pos["qty"],base_slip);ex=pos["stop"]*(1-slip)
                pro=pos["qty"]*ex;ef=fee(pro,fee_rate);pnl=pro-ef-pos["cost"];cash+=pro-ef
                pos.update({"exit_ts":nxt["ts"],"exit":ex,"pnl":pnl,"reason":"STOP","outcome":"LOSS"});trades.append(pos);pos=None
            elif hi>=pos["target2"]:
                slip=dynamic_slippage(nxt,pos["qty"],base_slip);ex=pos["target2"]*(1-slip)
                pro=pos["qty"]*ex;ef=fee(pro,fee_rate);pnl=pro-ef-pos["cost"];cash+=pro-ef
                pos.update({"exit_ts":nxt["ts"],"exit":ex,"pnl":pnl,"reason":"TARGET2","outcome":"WIN"});trades.append(pos);pos=None
            elif hi>=pos["target1"] and not pos["t1_hit"]:
                q=pos["qty"]*.5;slip=dynamic_slippage(nxt,q,base_slip);ex=pos["target1"]*(1-slip)
                pro=q*ex;ef=fee(pro,fee_rate);cash+=pro-ef;pos["qty"]-=q;pos["cost"]*=.5;pos["t1_hit"]=True;pos["stop"]=max(pos["stop"],pos["entry"])
            elif nxt["ts"]-pos["entry_ts"]>24*3600*1000:
                slip=dynamic_slippage(nxt,pos["qty"],base_slip);ex=nxt["close"]*(1-slip)
                pro=pos["qty"]*ex;ef=fee(pro,fee_rate);pnl=pro-ef-pos["cost"];cash+=pro-ef
                pos.update({"exit_ts":nxt["ts"],"exit":ex,"pnl":pnl,"reason":"TIME","outcome":"WIN" if pnl>0 else "LOSS"});trades.append(pos);pos=None

        if pos is None:
            s=signal(hist,params,edge_model=edge_model)
            if s:
                # Enter at next candle open: avoids same-candle lookahead.
                provisional=nxt["open"];dist=provisional-s["stop"]
                if dist>0:
                    rb=cash*risk;qty=min(rb/dist,(cash*.98)/(provisional*(1+fee_rate+base_slip)))
                    if qty>0:
                        slip=dynamic_slippage(nxt,qty,base_slip);entry=provisional*(1+slip);dist=entry-s["stop"]
                        if dist>0:
                            qty=min(cash*risk/dist,(cash*.98)/(entry*(1+fee_rate)))
                            notional=qty*entry;ef=fee(notional,fee_rate);cost=notional+ef
                            if qty>0 and cost<=cash:
                                cash-=cost;pos={"entry_ts":nxt["ts"],"entry":entry,"stop":s["stop"],"target1":s["target1"],"target2":s["target2"],
                                  "qty":qty,"qty_initial":qty,"cost":cost,"risk_dollars":qty*dist,"t1_hit":False,
                                  "regime":s["regime"],"features":s["features"],"edge_probability":s.get("edge_probability"),"score":s["score"]}
        curve.append(cash+(pos["qty"]*rows[i]["close"] if pos else 0))
    if pos:
        last=rows[-1];slip=dynamic_slippage(last,pos["qty"],base_slip);ex=last["close"]*(1-slip)
        pro=pos["qty"]*ex;ef=fee(pro,fee_rate);pnl=pro-ef-pos["cost"];cash+=pro-ef
        pos.update({"exit_ts":last["ts"],"exit":ex,"pnl":pnl,"reason":"END","outcome":"WIN" if pnl>0 else "LOSS"});trades.append(pos)
    for x in trades:x["r_multiple"]=x["pnl"]/max(x["risk_dollars"],1e-9)
    pnls=[x["pnl"] for x in trades];wins=[x for x in pnls if x>0];loss=[x for x in pnls if x<0];rs=[x["r_multiple"] for x in trades]
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
    return {"ending_balance":round(cash,2),"net_pnl":round(cash-balance,2),"return_pct":round((cash/balance-1)*100,2),
      "trades":len(trades),"win_rate":round(len(wins)/len(trades)*100,2) if trades else None,
      "profit_factor":round(pf,3) if pf is not None else None,"expectancy_r":round(statistics.mean(rs),3) if rs else None,
      "max_drawdown_pct":round(dd*100,2),"by_regime":regime_stats,"trade_log":trades}

def variants():
    for th,st,r1,r2,chop,edge in itertools.product([58,62,66,70],[1.0,1.2,1.4],[1.5,1.8],[2.3,2.7,3.0],[4,8],[.46,.50,.54]):
        yield {"threshold":th,"rsi_min":48,"rsi_max":72,"volume_z_min":-.2,"stop_atr":st,"rr1":r1,"rr2":r2,
               "chop_penalty":chop,"min_edge_probability":edge}

def robustness(m):
    if not m["trades"] or m["trades"]<20:return -999
    return (m["profit_factor"] or 0)*18+(m["expectancy_r"] or 0)*34-(m["max_drawdown_pct"] or 100)*.9+min(m["trades"],120)*.06

def walk_forward(rows,balance,risk,fee_rate,slip,folds=4):
    # Expanding-window walk forward. Each test fold is never used for its own optimization.
    n=len(rows);start=max(800,int(n*.35));remaining=n-start;test_size=max(300,remaining//folds)
    results=[];all_test_trades=[]
    train_end=start
    for fold in range(folds):
        test_start=train_end
        test_end=n if fold==folds-1 else min(n,test_start+test_size)
        if test_end-test_start<250: break
        train=rows[:train_end];test=rows[test_start:test_end]
        # First generate training trades from baseline to train edge model.
        base_train=backtest(train,balance,risk,fee_rate,slip,BASELINE,None)
        edge=HistoricalEdgeModel().fit(base_train["trade_log"])
        candidates=[]
        for p in variants():
            m=backtest(train,balance,risk,fee_rate,slip,p,edge)
            candidates.append((robustness(m),p,m))
        candidates.sort(key=lambda x:x[0],reverse=True)
        _,best,trainm=candidates[0]
        testm=backtest(test,balance,risk,fee_rate,slip,best,edge)
        results.append({"fold":fold+1,"train_rows":len(train),"test_rows":len(test),"best_params":best,
                        "edge_model":edge.summary(),
                        "train":{k:v for k,v in trainm.items() if k!="trade_log"},
                        "test":{k:v for k,v in testm.items() if k!="trade_log"},
                        "test_trades":testm["trade_log"]})
        all_test_trades.extend(testm["trade_log"])
        train_end=test_end
    return results,all_test_trades

def aggregate_walk_forward(folds):
    tests=[f["test"] for f in folds]
    trades=sum(x["trades"] for x in tests)
    # Weight trade-dependent metrics by trade count.
    def wavg(key):
        vals=[(x.get(key),x["trades"]) for x in tests if x.get(key) is not None and x["trades"]>0]
        return round(sum(v*w for v,w in vals)/sum(w for _,w in vals),3) if vals else None
    return {"folds":len(folds),"trades":trades,"avg_win_rate":wavg("win_rate"),"avg_profit_factor":wavg("profit_factor"),
            "avg_expectancy_r":wavg("expectancy_r"),"worst_drawdown_pct":max([x["max_drawdown_pct"] for x in tests],default=None),
            "profitable_folds":sum(x["return_pct"]>0 for x in tests)}

def run_experiment(db_path,data_dir,symbol,interval,years,balance,risk,fee_rate,slip,train_fraction,strategy_name,download_if_missing,notes=""):
    path=data_path(data_dir,symbol,interval)
    if not path.exists():
        if not download_if_missing:raise FileNotFoundError(path)
        download_binance_history(symbol,interval,years,data_dir)
    rows=load_history(path)
    if len(rows)<1200:raise ValueError("not enough historical candles for robust walk-forward testing")
    folds,trades=walk_forward(rows,balance,risk,fee_rate,slip,folds=4)
    agg=aggregate_walk_forward(folds)
    rs=[t.get("r_multiple") for t in trades]
    mc=monte_carlo_r_multiples(rs,balance,risk,1000)
    final_params=folds[-1]["best_params"] if folds else BASELINE
    result={"symbol":symbol,"interval":interval,"rows":len(rows),"walk_forward":agg,
            "fold_details":[{k:v for k,v in f.items() if k!="test_trades"} for f in folds],
            "monte_carlo":mc,"best_params":final_params,
            "learning":{"method":"transparent train-only feature-bin learner","last_fold":folds[-1]["edge_model"] if folds else None},
            "assumptions":{"starting_balance":balance,"risk_per_trade":risk,"fee_rate":fee_rate,"base_slippage":slip}}
    config={"symbol":symbol,"interval":interval,"years":years,"balance":balance,"risk":risk,"fee":fee_rate,"slip":slip}
    run_id=save_run(db_path,result,config,notes);result["run_id"]=run_id
    save_feature_trades(db_path,run_id,"walk_forward_test",symbol,trades,final_params)
    return result
