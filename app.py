from flask import Flask,jsonify,render_template,request
from pathlib import Path
import os,uuid,traceback,time
from lab.db import init_db,list_runs,get_run,create_job,update_job,get_job,active_job,cancel_stale_jobs
from lab.engine import prepare_job,load_prepared,simulate,robustness,BASELINE,finalize_result
from lab.learning import HistoricalEdgeModel

app=Flask(__name__)
BASE_DIR=Path(__file__).resolve().parent
DATA_DIR=Path(os.getenv("RESEARCH_DATA_DIR",str(BASE_DIR/"data")))
DB_PATH=Path(os.getenv("RESEARCH_DB_PATH",str(BASE_DIR/"research.sqlite3")))
DATA_DIR.mkdir(parents=True,exist_ok=True);DB_PATH.parent.mkdir(parents=True,exist_ok=True)
init_db(DB_PATH);cancel_stale_jobs(DB_PATH)

@app.route("/")
def home():return render_template("index.html")
@app.route("/api/health")
def health():return {"ok":True,"app":"CryptO Research Lab","mode":"resumable"}
@app.route("/api/runs")
def runs():return jsonify(list_runs(DB_PATH,100))
@app.route("/api/runs/<int:rid>")
def run_detail(rid):
    x=get_run(DB_PATH,rid);return jsonify(x) if x else (jsonify({"error":"not found"}),404)

def public_job(j):
    if not j:return None
    s=j["state"]
    return {"id":j["id"],"status":j["status"],"stage":j["stage"],"progress":j["progress"],
            "message":j["message"],"error":j["error"],"result":j["result"],
            "fold":min(s.get("fold_index",0)+1,len(s.get("folds",[]))) if s.get("folds") else None,
            "folds":len(s.get("folds",[])),"candidate":min(s.get("candidate_index",0)+1,max(1,len(s.get("candidates",[])))),
            "candidates":len(s.get("candidates",[]))}

@app.route("/api/jobs/active")
def active():
    return jsonify(public_job(active_job(DB_PATH)) or {"status":"none"})

@app.route("/api/jobs/start",methods=["POST"])
def start():
    if active_job(DB_PATH):
        return jsonify({"error":"A research job is already active. Resume it instead."}),409
    p=request.get_json(force=True) or {}
    cfg={"symbol":str(p.get("symbol","BTCUSDT")).upper(),"interval":str(p.get("interval","15m")),
         "years":float(p.get("years",3)),"starting_balance":float(p.get("starting_balance",500)),
         "risk_per_trade":float(p.get("risk_per_trade",.01)),"fee_rate":float(p.get("fee_rate",.004)),
         "slippage_rate":float(p.get("slippage_rate",.001)),"notes":str(p.get("notes",""))}
    jid=uuid.uuid4().hex[:12]
    state={"fold_index":0,"candidate_index":0,"candidate_results":[],"fold_results":[],"all_test_trades":[]}
    create_job(DB_PATH,jid,cfg,state)
    return jsonify({"ok":True,"job_id":jid}),202

@app.route("/api/jobs/<jid>")
def status(jid):
    j=get_job(DB_PATH,jid);return jsonify(public_job(j)) if j else (jsonify({"error":"job not found"}),404)

@app.route("/api/jobs/<jid>/step",methods=["POST"])
def step(jid):
    j=get_job(DB_PATH,jid)
    if not j:return jsonify({"error":"job not found"}),404
    if j["status"]!="running":return jsonify(public_job(j))
    cfg=j["config"];s=j["state"]
    try:
        if j["stage"]=="prepare":
            update_job(DB_PATH,jid,message="Preparing data and fast feature cache…",progress=2)
            prep=prepare_job(DATA_DIR,cfg["symbol"],cfg["interval"],cfg["years"])
            s.update(prep);s["fold_index"]=0;s["candidate_index"]=0;s["candidate_results"]=[]
            update_job(DB_PATH,jid,stage="baseline",progress=8,message="Prepared. Building first fold learner…",state=s)

        elif j["stage"]=="baseline":
            rows,features=load_prepared(s);b=s["folds"][s["fold_index"]]
            metrics,trades=simulate(rows,features,240,b["train_end"],cfg["starting_balance"],cfg["risk_per_trade"],
                                    cfg["fee_rate"],cfg["slippage_rate"],BASELINE,None,True)
            edge=HistoricalEdgeModel().fit(trades)
            s["edge_model"]=edge.to_dict();s["candidate_index"]=0;s["candidate_results"]=[]
            fold=s["fold_index"]+1
            progress=10+(fold-1)*21
            update_job(DB_PATH,jid,stage="candidates",progress=progress,
                       message=f"Fold {fold}/{len(s['folds'])}: learner ready. Testing candidates…",state=s)

        elif j["stage"]=="candidates":
            rows,features=load_prepared(s);b=s["folds"][s["fold_index"]]
            edge=HistoricalEdgeModel.from_dict(s["edge_model"])
            idx=s["candidate_index"];cands=s["candidates"]
            if idx>=len(cands):
                update_job(DB_PATH,jid,stage="select",message="Selecting best robust candidate…",state=s)
            else:
                p=cands[idx]
                metrics,_=simulate(rows,features,240,b["train_end"],cfg["starting_balance"],cfg["risk_per_trade"],
                                   cfg["fee_rate"],cfg["slippage_rate"],p,edge,False)
                s["candidate_results"].append({"score":robustness(metrics),"params":p,"metrics":metrics})
                s["candidate_index"]=idx+1
                fold=s["fold_index"]+1
                base=10+(fold-1)*21
                progress=base+int(14*(idx+1)/len(cands))
                update_job(DB_PATH,jid,progress=progress,
                           message=f"Fold {fold}/{len(s['folds'])}: strategy {idx+1}/{len(cands)} complete.",state=s)

        elif j["stage"]=="select":
            rows,features=load_prepared(s);b=s["folds"][s["fold_index"]]
            edge=HistoricalEdgeModel.from_dict(s["edge_model"])
            ranked=sorted(s["candidate_results"],key=lambda x:x["score"],reverse=True)
            eligible=[x for x in ranked if x["score"]>-1e8]
            if not eligible:
                # Keep a diagnostic fold rather than pretending the first zero-trade strategy is best.
                most=max(ranked,key=lambda x:x["metrics"].get("trades",0))
                s["fold_results"].append({
                    "fold":s["fold_index"]+1,"train_rows":b["train_end"],
                    "test_rows":b["test_end"]-b["test_start"],"embargo_bars":b["embargo_bars"],
                    "candidate_count":len(s["candidates"]),"status":"insufficient_training_signals",
                    "best_params":None,"most_active_params":most["params"],
                    "train":most["metrics"],"test":{"trades":0,"signal_funnel":{"rejections":{"optimizer_minimum_training_trades":1}}},
                    "message":"No candidate reached the minimum training-trade requirement."
                })
                s["fold_index"]+=1;s["candidate_index"]=0;s["candidate_results"]=[]
                if s["fold_index"]>=len(s["folds"]):
                    update_job(DB_PATH,jid,stage="finalize",progress=95,message="Diagnostic folds complete. Finalizing…",state=s)
                else:
                    update_job(DB_PATH,jid,stage="baseline",progress=10+s["fold_index"]*21,
                               message=f"Fold {s['fold_index']}/{len(s['folds'])} saved as diagnostic. Starting next fold…",state=s)
                return jsonify(public_job(get_job(DB_PATH,jid)))
            best=eligible[0]
            test_metrics,test_trades=simulate(rows,features,b["test_start"],b["test_end"],
                 cfg["starting_balance"],cfg["risk_per_trade"],cfg["fee_rate"],cfg["slippage_rate"],
                 best["params"],edge,True)
            s["fold_results"].append({"fold":s["fold_index"]+1,"train_rows":b["train_end"],
                 "test_rows":b["test_end"]-b["test_start"],"embargo_bars":b["embargo_bars"],
                 "candidate_count":len(s["candidates"]),"best_params":best["params"],
                 "edge_model":edge.summary(),"train":best["metrics"],"test":test_metrics})
            s["all_test_trades"].extend(test_trades)
            s["fold_index"]+=1;s["candidate_index"]=0;s["candidate_results"]=[]
            if s["fold_index"]>=len(s["folds"]):
                update_job(DB_PATH,jid,stage="finalize",progress=95,message="All folds complete. Finalizing stress tests…",state=s)
            else:
                update_job(DB_PATH,jid,stage="baseline",progress=10+s["fold_index"]*21,
                           message=f"Fold {s['fold_index']}/{len(s['folds'])} saved. Starting next fold…",state=s)

        elif j["stage"]=="finalize":
            result=finalize_result(DB_PATH,cfg,s)
            update_job(DB_PATH,jid,status="complete",stage="complete",progress=100,
                       message="Research complete.",state=s,result=result)
        return jsonify(public_job(get_job(DB_PATH,jid)))
    except Exception as e:
        update_job(DB_PATH,jid,status="error",stage="error",progress=0,error=str(e),
                   message="Research step failed.",state=s)
        return jsonify(public_job(get_job(DB_PATH,jid))),500

@app.route("/api/jobs/<jid>/cancel",methods=["POST"])
def cancel(jid):
    j=get_job(DB_PATH,jid)
    if not j:return jsonify({"error":"job not found"}),404
    update_job(DB_PATH,jid,status="error",stage="cancelled",error="Cancelled by user.",message="Cancelled.")
    return jsonify(public_job(get_job(DB_PATH,jid)))

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")))
