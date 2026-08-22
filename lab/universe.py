DEFAULT_UNIVERSE=["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT","AVAXUSDT","LTCUSDT","BCHUSDT","DOTUSDT","UNIUSDT","AAVEUSDT","ATOMUSDT","NEARUSDT","FILUSDT","ETCUSDT","ALGOUSDT","XTZUSDT","MKRUSDT"]

def parse_symbols(raw):
    if not raw:return list(DEFAULT_UNIVERSE)
    if isinstance(raw,str):raw=[x.strip().upper() for x in raw.split(",")]
    out=[]
    for x in raw:
        x=str(x).strip().upper()
        if x and x not in out:out.append(x)
    return out[:60]
