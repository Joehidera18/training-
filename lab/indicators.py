import statistics
def ema(xs,n):
    if len(xs)<n:return None
    a=2/(n+1);v=xs[-n]
    for x in xs[-n+1:]:v=a*x+(1-a)*v
    return v
def rsi(xs,n=14):
    if len(xs)<n+1:return None
    g=[];l=[]
    for a,b in zip(xs[-n-1:-1],xs[-n:]):
        d=b-a;g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g)/n;al=sum(l)/n
    return 100 if al==0 else 100-100/(1+ag/al)
def atr(rows,n=14):
    if len(rows)<n+1:return None
    vals=[]
    for i in range(-n,0):
        h,l,c=rows[i]["high"],rows[i]["low"],rows[i-1]["close"]
        vals.append(max(h-l,abs(h-c),abs(l-c)))
    return sum(vals)/len(vals)
def zscore(x,xs):
    if len(xs)<10:return 0
    s=statistics.pstdev(xs) or 1e-9
    return (x-statistics.mean(xs))/s
