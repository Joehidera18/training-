from flask import Flask,jsonify,render_template,request
from pathlib import Path
import os,threading,time,uuid,traceback
from lab.db import init_db,list_runs,get_run
from lab.engine import run_experiment
from lab.data import download_binance_history,data_path

app=Flask(__name__)
BASE_DIR=Path(__file__).resolve().parent
DATA_DIR=BASE_DIR/"data"
DB_PATH=Path(os.getenv("RESEARCH_DB_PATH",str(BASE_DIR/"research.sqlite3")))
DATA_DIR.mkdir(exist_ok=True);DB_PATH.parent.mkdir(parents=True,exist_ok=True);init_db(DB_PATH)

jobs={};jobs_lock=threading.Lock();research_lock=threading.Lock()
def set_job(jid,**u):
    with jobs_lock:
        jobs.setdefault(jid,{}).update(u);jobs[jid]["updated_at"]=int(time.time())
def copy_job(jid):
    with jobs_lock:return dict(jobs[jid]) if jid in jobs else None

def research_worker(jid,p):
    if not research_lock.acquire(blocking=False):
        set_job(jid,status="error",progress=0,error="Another research job is already running.");return
    try:
        symbol=str(p.get("symbol","BTCUSDT")).upper();interval=str(p.get("interval","15m"))
        years=float(p.get("years",2));balance=float(p.get("starting_balance",500))
        risk=float(p.get("risk_per_trade",.01));fee=float(p.get("fee_rate",.004))
        slip=float(p.get("slippage_rate",.001));train=float(p.get("train_fraction",.70))
        strategy=str(p.get("strategy","radar_baseline"));notes=str(p.get("notes",""))
        def progress(pct,message):
            set_job(jid,status="running",stage="research",progress=max(1,min(99,int(pct))),message=message)
        result=run_experiment(DB_PATH,DATA_DIR,symbol,interval,years,balance,risk,fee,slip,
                              train,strategy,True,notes,progress)
        set_job(jid,status="complete",stage="complete",progress=100,message="Research complete.",result=result)
    except Exception as e:
        set_job(jid,status="error",stage="error",progress=0,error=str(e),
                traceback=traceback.format_exc(limit=12))
    finally:
        research_lock.release()

@app.route("/")
def home():return render_template("index.html")
@app.route("/api/health")
def health():return {"ok":True,"app":"CryptO Research Lab"}
@app.route("/api/runs")
def api_runs():return jsonify(list_runs(DB_PATH,100))
@app.route("/api/runs/<int:run_id>")
def api_run(run_id):
    row=get_run(DB_PATH,run_id);return jsonify(row) if row else (jsonify({"error":"run not found"}),404)
@app.route("/api/download",methods=["POST"])
def api_download():
    p=request.get_json(force=True) or {}
    try:
        path,rows=download_binance_history(str(p.get("symbol","BTCUSDT")).upper(),
                                           str(p.get("interval","15m")),float(p.get("years",2)),DATA_DIR)
        return jsonify({"ok":True,"path":path.name,"rows":rows})
    except Exception as e:return jsonify({"error":str(e)}),500
@app.route("/api/backtest/start",methods=["POST"])
def start():
    p=request.get_json(force=True) or {}
    with jobs_lock:
        if any(j.get("status") in ("queued","running") for j in jobs.values()):
            return jsonify({"error":"A research job is already running."}),409
    jid=uuid.uuid4().hex[:12]
    set_job(jid,id=jid,status="queued",stage="queued",progress=1,message="Research job queued.",created_at=int(time.time()))
    threading.Thread(target=research_worker,args=(jid,p),daemon=True).start()
    return jsonify({"ok":True,"job_id":jid}),202
@app.route("/api/backtest/status/<jid>")
def status(jid):
    j=copy_job(jid);return jsonify(j) if j else (jsonify({"error":"job not found"}),404)
@app.route("/api/backtest",methods=["POST"])
def old():return jsonify({"error":"Use /api/backtest/start."}),409

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")))
