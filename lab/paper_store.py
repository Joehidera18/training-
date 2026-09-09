from __future__ import annotations
import json, random, sqlite3, threading, time
from pathlib import Path

INTERVAL_MS={"5m":300000,"15m":900000,"1h":3600000,"4h":14400000}
MAX_BARS={"5m":1200,"15m":800,"1h":500,"4h":300}
def now_ms():return int(time.time()*1000)
def clamp(x,a,b):return max(a,min(b,x))

def init_continuous_db(path):
    con=sqlite3.connect(path,timeout=30)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS continuous_state(key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS continuous_memory(
      scope TEXT NOT NULL,symbol TEXT NOT NULL,context_key TEXT NOT NULL,family TEXT NOT NULL,direction TEXT NOT NULL,
      samples INTEGER NOT NULL DEFAULT 0,wins INTEGER NOT NULL DEFAULT 0,sum_r REAL NOT NULL DEFAULT 0,sumsq_r REAL NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL,PRIMARY KEY(scope,symbol,context_key,family,direction));
    CREATE TABLE IF NOT EXISTS paper_trades(
      id INTEGER PRIMARY KEY AUTOINCREMENT,opened_at INTEGER NOT NULL,closed_at INTEGER,product_id TEXT NOT NULL,
      family TEXT NOT NULL,direction TEXT NOT NULL,entry REAL NOT NULL,stop REAL NOT NULL,target REAL NOT NULL,qty REAL NOT NULL,
      risk_usd REAL NOT NULL,status TEXT NOT NULL,exit REAL,pnl REAL,result_r REAL,balance_after REAL,mfe_r REAL,mae_r REAL,
      exit_reason TEXT,context_json TEXT NOT NULL,decision_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_paper_trades_closed ON paper_trades(closed_at DESC);
    CREATE TABLE IF NOT EXISTS continuous_activity(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL,details_json TEXT);
    ''');con.commit();con.close()

def db_connect(path):
    con=sqlite3.connect(path,timeout=30);con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA synchronous=NORMAL');con.execute('PRAGMA busy_timeout=30000');return con

def load_state(path,key,default=None):
    con=db_connect(path);r=con.execute('SELECT value_json FROM continuous_state WHERE key=?',(key,)).fetchone();con.close();return json.loads(r[0]) if r else default

def save_state(path,key,value):
    con=db_connect(path);con.execute('''INSERT INTO continuous_state(key,value_json,updated_at) VALUES(?,?,?)
      ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at''',(key,json.dumps(value,separators=(',',':'),allow_nan=False),int(time.time())))
    con.commit();con.close()

def log_activity(path,level,message,details=None):
    con=db_connect(path);con.execute('INSERT INTO continuous_activity(ts,level,message,details_json) VALUES(?,?,?,?)',(int(time.time()),level,message,json.dumps(details or {},separators=(',',':'),default=str)))
    con.execute('DELETE FROM continuous_activity WHERE id NOT IN (SELECT id FROM continuous_activity ORDER BY id DESC LIMIT 5000)');con.commit();con.close()

def memory_update(path,scope,symbol,context_key,family,direction,r):
    con=db_connect(path);ts=int(time.time());win=1 if r>0 else 0
    con.execute('''INSERT INTO continuous_memory(scope,symbol,context_key,family,direction,samples,wins,sum_r,sumsq_r,updated_at)
      VALUES(?,?,?,?,?,1,?,?,?,?) ON CONFLICT(scope,symbol,context_key,family,direction) DO UPDATE SET
      samples=samples+1,wins=wins+excluded.wins,sum_r=sum_r+excluded.sum_r,sumsq_r=sumsq_r+excluded.sumsq_r,updated_at=excluded.updated_at''',
      (scope,symbol,context_key,family,direction,win,float(r),float(r)*float(r),ts));con.commit();con.close()

def memory_row(path,scope,symbol,context_key,family,direction):
    con=db_connect(path);r=con.execute('SELECT samples,wins,sum_r,sumsq_r FROM continuous_memory WHERE scope=? AND symbol=? AND context_key=? AND family=? AND direction=?',(scope,symbol,context_key,family,direction)).fetchone();con.close()
    return dict(r) if r else {"samples":0,"wins":0,"sum_r":0.0,"sumsq_r":0.0}

def memory_estimate(path,symbol,context_key,family,direction):
    from .continuous import estimate_memory
    g=memory_row(path,'global','*',context_key,family,direction)
    c=memory_row(path,'coin',symbol,context_key,family,direction)
    return estimate_memory(g,c)

def memory_leaderboard(path,limit=20):
    con=db_connect(path);rows=con.execute('''SELECT scope,symbol,context_key,family,direction,samples,wins,sum_r,
      CASE WHEN samples>0 THEN sum_r/samples ELSE 0 END expectancy_r FROM continuous_memory WHERE samples>=5 ORDER BY expectancy_r DESC LIMIT ?''',(limit,)).fetchall();con.close();return [dict(r) for r in rows]

def recent_trades(path,limit=100):
    con=db_connect(path);rows=con.execute('''SELECT id,opened_at,closed_at,product_id,family,direction,entry,exit,stop,target,qty,risk_usd,status,pnl,result_r,balance_after,mfe_r,mae_r,exit_reason FROM paper_trades ORDER BY id DESC LIMIT ?''',(limit,)).fetchall();con.close();return [dict(r) for r in rows]

def activity_rows(path,limit=100):
    con=db_connect(path);rows=con.execute('SELECT id,ts,level,message,details_json FROM continuous_activity ORDER BY id DESC LIMIT ?',(limit,)).fetchall();con.close();out=[]
    for r in rows:
        x=dict(r)
        try:x['details']=json.loads(x.pop('details_json') or '{}')
        except Exception:x['details']={}
        out.append(x)
    return out
