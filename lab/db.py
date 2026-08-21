import sqlite3,json,time,hashlib

def connect(path):
    con=sqlite3.connect(path,timeout=30)
    con.row_factory=sqlite3.Row
    return con

def init_db(path):
    con=connect(path)
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

      CREATE TABLE IF NOT EXISTS research_jobs(
        id TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        status TEXT NOT NULL,
        stage TEXT NOT NULL,
        progress REAL NOT NULL DEFAULT 0,
        message TEXT,
        config_json TEXT NOT NULL,
        state_json TEXT NOT NULL,
        result_json TEXT,
        error TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_jobs_status ON research_jobs(status,updated_at);
    """)
    cols={r["name"] for r in con.execute("PRAGMA table_info(runs)")}
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
    con=connect(path);config=config or {}
    cur=con.execute("INSERT INTO runs(created_at,symbol,interval,config_hash,notes,result_json) VALUES(?,?,?,?,?,?)",
      (int(time.time()),result["symbol"],result["interval"],config_hash(config),notes,
       json.dumps(result,separators=(",",":"),default=str)))
    con.commit();rid=cur.lastrowid;con.close();return rid

def save_feature_trades(path,run_id,split,symbol,trades,params):
    con=connect(path)
    rows=[]
    for t in trades:
        rows.append((run_id,split,symbol,t.get("entry_ts"),t.get("exit_ts"),t.get("outcome"),
          t.get("r_multiple"),t.get("regime"),
          json.dumps(t.get("features") or {},separators=(",",":"),default=str),
          json.dumps(params,separators=(",",":"),default=str)))
    con.executemany("""INSERT INTO feature_trades(run_id,split,symbol,entry_ts,exit_ts,outcome,r_multiple,regime,features_json,params_json)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",rows)
    con.commit();con.close()

def list_runs(path,limit=100):
    con=connect(path)
    rows=con.execute("SELECT id,created_at,symbol,interval,config_hash,notes,result_json FROM runs ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    con.close();out=[]
    for r in rows:
        x=json.loads(r["result_json"])
        out.append({"id":r["id"],"created_at":r["created_at"],"symbol":r["symbol"],"interval":r["interval"],
                    "config_hash":r["config_hash"],"notes":r["notes"],"walk_forward":x.get("walk_forward"),
                    "monte_carlo":x.get("monte_carlo"),"best_params":x.get("best_params")})
    return out

def get_run(path,run_id):
    con=connect(path);r=con.execute("SELECT result_json FROM runs WHERE id=?",(run_id,)).fetchone();con.close()
    return json.loads(r[0]) if r else None

def create_job(path,job_id,config,state):
    now=int(time.time());con=connect(path)
    con.execute("""INSERT INTO research_jobs(id,created_at,updated_at,status,stage,progress,message,config_json,state_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_id,now,now,"running","prepare",1,"Preparing research job…",
                 json.dumps(config,separators=(",",":")),json.dumps(state,separators=(",",":"))))
    con.commit();con.close()

def update_job(path,job_id,**fields):
    allowed={"status","stage","progress","message","state","result","error"}
    fields={k:v for k,v in fields.items() if k in allowed}
    mapping={"state":"state_json","result":"result_json"}
    sets=[];vals=[]
    for k,v in fields.items():
        col=mapping.get(k,k)
        if k in ("state","result") and v is not None:v=json.dumps(v,separators=(",",":"),default=str)
        sets.append(f"{col}=?");vals.append(v)
    sets.append("updated_at=?");vals.append(int(time.time()));vals.append(job_id)
    con=connect(path);con.execute(f"UPDATE research_jobs SET {','.join(sets)} WHERE id=?",vals);con.commit();con.close()

def get_job(path,job_id):
    con=connect(path);r=con.execute("SELECT * FROM research_jobs WHERE id=?",(job_id,)).fetchone();con.close()
    if not r:return None
    return {"id":r["id"],"created_at":r["created_at"],"updated_at":r["updated_at"],"status":r["status"],
            "stage":r["stage"],"progress":r["progress"],"message":r["message"],
            "config":json.loads(r["config_json"]),"state":json.loads(r["state_json"]),
            "result":json.loads(r["result_json"]) if r["result_json"] else None,"error":r["error"]}

def active_job(path):
    con=connect(path)
    r=con.execute("""SELECT id FROM research_jobs WHERE status='running'
                     ORDER BY updated_at DESC LIMIT 1""").fetchone()
    con.close()
    return get_job(path,r["id"]) if r else None

def cancel_stale_jobs(path,max_age_seconds=86400):
    cutoff=int(time.time())-max_age_seconds;con=connect(path)
    con.execute("""UPDATE research_jobs SET status='error',stage='error',error='Job expired before completion.'
                   WHERE status='running' AND updated_at<?""",(cutoff,))
    con.commit();con.close()
