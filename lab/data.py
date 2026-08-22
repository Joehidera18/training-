from pathlib import Path
from datetime import datetime,timezone,timedelta
import csv,time,requests

BINANCE="https://api.binance.us"
INTERVAL_MS={"1m":60000,"5m":300000,"15m":900000,"1h":3600000,"4h":14400000,"1d":86400000}

def data_path(out_dir,symbol,interval):
    return out_dir/f"{symbol}_{interval}.csv"

def download_binance_history(symbol,interval,years,out_dir):
    if interval not in INTERVAL_MS:
        raise ValueError("unsupported interval")
    out_dir.mkdir(parents=True,exist_ok=True)
    path=data_path(out_dir,symbol,interval)
    start=int((datetime.now(timezone.utc)-timedelta(days=365.25*years)).timestamp()*1000)
    end=int(datetime.now(timezone.utc).timestamp()*1000)
    rows=[];cursor=start;s=requests.Session()
    while cursor<end:
        r=s.get(f"{BINANCE}/api/v3/klines",
                params={"symbol":symbol,"interval":interval,"startTime":cursor,"endTime":end,"limit":1000},
                timeout=12)
        r.raise_for_status();batch=r.json()
        if not batch: break
        for k in batch:
            rows.append({"ts":int(k[0]),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),
                         "close":float(k[4]),"volume":float(k[5]),"quote_volume":float(k[7]),"trades":int(k[8])})
        nxt=int(batch[-1][0])+INTERVAL_MS[interval]
        if nxt<=cursor: break
        cursor=nxt;time.sleep(.04)
    rows=list({r["ts"]:r for r in rows}.values());rows.sort(key=lambda x:x["ts"])
    with path.open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["ts","open","high","low","close","volume","quote_volume","trades"])
        w.writeheader();w.writerows(rows)
    return path,len(rows)

def load_history(path):
    out=[]
    with path.open() as f:
        for r in csv.DictReader(f):
            out.append({k:(int(v) if k in ("ts","trades") else float(v)) for k,v in r.items()})
    return out

def validate_history(rows,interval):
    expected=INTERVAL_MS.get(interval)
    if not rows:
        return {"valid":False,"rows":0,"gaps":0,"duplicates":0,"bad_ohlc":0,"coverage_pct":0}
    duplicates=len(rows)-len({r["ts"] for r in rows})
    bad=0;gaps=0
    for i,r in enumerate(rows):
        if r["low"]>min(r["open"],r["close"],r["high"]) or r["high"]<max(r["open"],r["close"],r["low"]):
            bad+=1
        if i and expected:
            delta=r["ts"]-rows[i-1]["ts"]
            if delta>expected*1.5:
                gaps+=max(1,round(delta/expected)-1)
    span=max(rows[-1]["ts"]-rows[0]["ts"],1)
    possible=max(1,round(span/expected)+1) if expected else len(rows)
    coverage=min(100.0,len(rows)/possible*100)
    return {"valid":bad==0 and duplicates==0,"rows":len(rows),"gaps":gaps,"duplicates":duplicates,
            "bad_ohlc":bad,"coverage_pct":round(coverage,2),
            "start_ts":rows[0]["ts"],"end_ts":rows[-1]["ts"]}
