import random,statistics,math

def monte_carlo_r_multiples(r_values,start_balance=500,risk_fraction=.01,runs=1500,seed=7,block_size=4):
    vals=[float(x) for x in r_values if x is not None and math.isfinite(float(x))]
    if len(vals)<10:
        return {"runs":0,"samples":len(vals),"probability_profitable":None,"median_return_pct":None,
                "p05_return_pct":None,"p95_return_pct":None,"median_max_drawdown_pct":None,
                "risk_of_30pct_drawdown":None,"risk_of_half_account":None}
    rng=random.Random(seed);returns=[];dds=[];half=0;dd30=0
    n=len(vals)
    for _ in range(runs):
        seq=[]
        while len(seq)<n:
            start=rng.randrange(n)
            block=[vals[(start+j)%n] for j in range(block_size)]
            seq.extend(block)
        seq=seq[:n]
        eq=start_balance;peak=eq;maxdd=0
        touched_half=False
        for r in seq:
            eq*=max(.01,1+r*risk_fraction)
            peak=max(peak,eq);maxdd=max(maxdd,(peak-eq)/peak)
            if eq<=start_balance*.5:touched_half=True
        returns.append((eq/start_balance-1)*100);dds.append(maxdd*100)
        dd30+=int(maxdd>=.30);half+=int(touched_half)
    returns.sort();dds.sort()
    def pct(xs,p):
        return xs[min(len(xs)-1,max(0,int((len(xs)-1)*p)))]
    return {"runs":runs,"samples":len(vals),
            "probability_profitable":round(sum(x>0 for x in returns)/runs*100,1),
            "median_return_pct":round(statistics.median(returns),2),
            "p05_return_pct":round(pct(returns,.05),2),"p95_return_pct":round(pct(returns,.95),2),
            "median_max_drawdown_pct":round(statistics.median(dds),2),
            "p95_max_drawdown_pct":round(pct(dds,.95),2),
            "risk_of_30pct_drawdown":round(dd30/runs*100,1),
            "risk_of_half_account":round(half/runs*100,1),"block_size":block_size}
