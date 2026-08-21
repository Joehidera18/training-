from pathlib import Path
from datetime import datetime,timezone,timedelta
import csv,time,requests

BINANCE="https://api.binance.us"
INTERVAL_MS={"1m":60000,"5m":300000,"15m":900000,"1h":3600000,"4h":14400000,"1d":86400000}

def data_path(out_dir,symbol,interval):
    return out_dir/f"{symbol}_{interval}.csv"

def download_binance_history(symbol,interval,years,out_dir):
    if interval not in INTERVAL_MS: raise ValueError("unsupported interval")
    path=data_path(out_dir,symbol,interval)
    start=int((datetime.now(timezone.utc)-timedelta(days=365.25*years)).timestamp()*1000)
    end=int(datetime.now(timezone.utc).timestamp()*1000)
    rows=[];cursor=start;s=requests.Session()
    while cursor<end:
        r=s.get(f"{BINANCE}/api/v3/klines",params={"symbol":symbol,"interval":interval,"startTime":cursor,"endTime":end,"limit":1000},timeout=12)
        r.raise_for_status();batch=r.json()
        if not batch: break
        for k in batch:
            rows.append({"ts":int(k[0]),"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4]),"volume":float(k[5]),"quote_volume":float(k[7]),"trades":int(k[8])})
        nxt=int(batch[-1][0])+INTERVAL_MS[interval]
        if nxt<=cursor: break
        cursor=nxt;time.sleep(.05)
    rows=list({r["ts"]:r for r in rows}.values());rows.sort(key=lambda x:x["ts"])
    with path.open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["ts","open","high","low","close","volume","quote_volume","trades"]);w.writeheader();w.writerows(rows)
    return path,len(rows)

def load_history(path):
    out=[]
    with path.open() as f:
        for r in csv.DictReader(f):
            out.append({k:(int(v) if k in ("ts","trades") else float(v)) for k,v in r.items()})
    return out
