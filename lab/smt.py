
def relative_strength_divergence(primary_rows,peer_rows,lookback=20,tolerance=.002):
    """
    SMT-style relative-strength divergence. Rows should be aligned to the same timestamps.
    Positive spread = primary stronger than peer; negative = weaker.
    """
    n=min(len(primary_rows),len(peer_rows))
    out=[{"smt_bullish":False,"smt_bearish":False,"smt_spread":0.0} for _ in range(n)]
    for i in range(lookback,n):
        pa=primary_rows[i-lookback]["close"];pb=primary_rows[i]["close"]
        qa=peer_rows[i-lookback]["close"];qb=peer_rows[i]["close"]
        pr=(pb/pa-1) if pa else 0.0
        qr=(qb/qa-1) if qa else 0.0
        spread=pr-qr
        out[i]={"smt_bullish":spread>tolerance,"smt_bearish":spread<-tolerance,"smt_spread":spread}
    return out
