
import sqlite3,json,time,hashlib

def init_db(path):
    con=sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        interval TEXT NOT NULL,
        config_hash TEXT,
        notes TEXT,
        result_json TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS feature_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        split TEXT NOT NULL,
        symbol TEXT NOT NULL,
        entry_ts INTEGER,
        exit_ts INTEGER,
        outcome TEXT,
        r_multiple REAL,
        regime TEXT,
        features_json TEXT,
        params_json TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_feature_run ON feature_trades(run_id);
    """)
    # migration for older database
    cols={r[1] for r in con.execute("PRAGMA table_info(runs)")}
    if "config_hash" not in cols:
        try:con.execute("ALTER TABLE runs ADD COLUMN config_hash TEXT")
        except Exception:pass
    if "notes" not in cols:
        try:con.execute("ALTER TABLE runs ADD COLUMN notes TEXT")
        except Exception:pass
    con.commit();con.close()

def config_hash(config):
    raw=json.dumps(config,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

def save_run(path,result,config=None,notes=""):
    config=config or {}
    con=sqlite3.connect(path)
    cur=con.execute("INSERT INTO runs(created_at,symbol,interval,config_hash,notes,result_json) VALUES(?,?,?,?,?,?)",
      (int(time.time()),result["symbol"],result["interval"],config_hash(config),notes,json.dumps(result,separators=(",",":"),default=str)))
    con.commit();i=cur.lastrowid;con.close();return i

def save_feature_trades(path,run_id,split,symbol,trades,params):
    con=sqlite3.connect(path)
    for t in trades:
        con.execute("""INSERT INTO feature_trades(run_id,split,symbol,entry_ts,exit_ts,outcome,r_multiple,regime,features_json,params_json)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (run_id,split,symbol,t.get("entry_ts"),t.get("exit_ts"),t.get("outcome"),t.get("r_multiple"),t.get("regime"),
           json.dumps(t.get("features") or {},separators=(",",":"),default=str),
           json.dumps(params,separators=(",",":"),default=str)))
    con.commit();con.close()

def list_runs(path,limit):
    con=sqlite3.connect(path);con.row_factory=sqlite3.Row
    rows=con.execute("SELECT id,created_at,symbol,interval,config_hash,notes,result_json FROM runs ORDER BY id DESC LIMIT ?",(limit,)).fetchall();con.close()
    out=[]
    for r in rows:
        x=json.loads(r["result_json"])
        out.append({"id":r["id"],"created_at":r["created_at"],"symbol":r["symbol"],"interval":r["interval"],
                    "config_hash":r["config_hash"],"notes":r["notes"],"walk_forward":x.get("walk_forward"),"monte_carlo":x.get("monte_carlo"),
                    "best_params":x.get("best_params")})
    return out

def get_run(path,run_id):
    con=sqlite3.connect(path);r=con.execute("SELECT result_json FROM runs WHERE id=?",(run_id,)).fetchone();con.close()
    return json.loads(r[0]) if r else None
