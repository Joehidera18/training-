
import random, statistics, math

def monte_carlo_r_multiples(r_values, start_balance=500, risk_fraction=.01, runs=1000, seed=7):
    vals=[float(x) for x in r_values if x is not None and math.isfinite(float(x))]
    if len(vals)<10:
        return {"runs":0,"samples":len(vals),"probability_profitable":None,"median_return_pct":None,"p05_return_pct":None,"p95_return_pct":None,"median_max_drawdown_pct":None}
    rng=random.Random(seed)
    returns=[];dds=[]
    for _ in range(runs):
        eq=start_balance;peak=eq;maxdd=0
        seq=[rng.choice(vals) for _ in range(len(vals))]
        for r in seq:
            eq*=max(.01,1+r*risk_fraction)
            peak=max(peak,eq)
            maxdd=max(maxdd,(peak-eq)/peak)
        returns.append((eq/start_balance-1)*100)
        dds.append(maxdd*100)
    returns.sort();dds.sort()
    def pct(xs,p):
        i=min(len(xs)-1,max(0,int((len(xs)-1)*p)))
        return xs[i]
    return {
        "runs":runs,"samples":len(vals),
        "probability_profitable":round(sum(x>0 for x in returns)/runs*100,1),
        "median_return_pct":round(statistics.median(returns),2),
        "p05_return_pct":round(pct(returns,.05),2),
        "p95_return_pct":round(pct(returns,.95),2),
        "median_max_drawdown_pct":round(statistics.median(dds),2),
        "p95_max_drawdown_pct":round(pct(dds,.95),2),
    }
