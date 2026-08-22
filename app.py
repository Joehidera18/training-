from flask import Flask,jsonify,render_template,request
from pathlib import Path
import os,uuid,traceback,time
from lab.db import init_db,list_runs,get_run,create_job,update_job,get_job,active_job,cancel_stale_jobs
from lab.engine import prepare_job,load_prepared,simulate,robustness,BASELINE,finalize_result,adaptive_variants
from lab.learning import HistoricalEdgeModel
from lab.memory import load_memory_trades,memory_summary,get_champion,challenger_history
from lab.microstructure import import_micro_rows,load_micro_rows,micro_summary
from lab.universe import DEFAULT_UNIVERSE

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
            mem=memory_summary(DB_PATH,cfg["symbol"],cfg["interval"])
            prep["candidates"]=adaptive_variants(mem)
            prep["adaptive_memory_snapshot"]=mem
            s.update(prep);s["fold_index"]=0;s["candidate_index"]=0;s["candidate_results"]=[]
            update_job(DB_PATH,jid,stage="baseline",progress=8,message="Prepared. Building first fold learner…",state=s)

        elif j["stage"]=="baseline":
            rows,features=load_prepared(s);b=s["folds"][s["fold_index"]]
            fit_end=max(700,int(b["train_end"]*.72))
            validation_start=fit_end+max(2,b.get("embargo_bars",2)//2)
            if b["train_end"]-validation_start<250:
                validation_start=max(500,b["train_end"]-500);fit_end=validation_start-2
            metrics,trades=simulate(rows,features,240,fit_end,cfg["starting_balance"],cfg["risk_per_trade"],
                                    cfg["fee_rate"],cfg["slippage_rate"],BASELINE,None,True)
            cutoff_ts=rows[max(0,fit_end-1)]["ts"]
            remembered=load_memory_trades(DB_PATH,cfg["symbol"],cfg["interval"],cutoff_ts)
            edge=HistoricalEdgeModel().fit(trades+remembered)
            s["fit_end"]=fit_end;s["validation_start"]=validation_start
            s["memory_examples_used"]=len(remembered)
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
                metrics,_=simulate(rows,features,s["validation_start"],b["train_end"],cfg["starting_balance"],cfg["risk_per_trade"],
                                   cfg["fee_rate"],cfg["slippage_rate"],p,edge,False)
                s["candidate_results"].append({"score":robustness(metrics),"params":p,"metrics":metrics})
                s["candidate_index"]=idx+1
                fold=s["fold_index"]+1
                base=10+(fold-1)*21
                progress=base+int(14*(idx+1)/len(cands))
                update_job(DB_PATH,jid,progress=progress,
                           message=f"Fold {fold}/{len(s['folds'])}: {p.get('family','strategy')} {idx+1}/{len(cands)} complete.",state=s)

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
                    "candidate_count":len(s["candidates"]),"status":"no_validated_edge",
                    "best_params":None,"most_active_params":most["params"],
                    "train":most["metrics"],"test":{"trades":0,"signal_funnel":{"rejections":{"optimizer_minimum_training_trades":1}}},
                    "message":"No candidate produced a positive validated edge after costs; outer test skipped."
                })
                s["fold_index"]+=1;s["candidate_index"]=0;s["candidate_results"]=[]
                if s["fold_index"]>=len(s["folds"]):
                    update_job(DB_PATH,jid,stage="finalize",progress=95,message="Diagnostic folds complete. Finalizing…",state=s)
                else:
                    update_job(DB_PATH,jid,stage="baseline",progress=10+s["fold_index"]*21,
                               message=f"Fold {s['fold_index']}/{len(s['folds'])} saved as diagnostic. Starting next fold…",state=s)
                return jsonify(public_job(get_job(DB_PATH,jid)))
            # Keep the best validated specialist from each family for ensemble diagnostics.
            family_best={}
            for cand in eligible:
                fam=cand["params"].get("family","unknown")
                if fam not in family_best:family_best[fam]=cand
            family_leaderboard=[{"family":fam,"score":cand["score"],"params":cand["params"],"metrics":cand["metrics"]}
                                for fam,cand in family_best.items()]
            best=eligible[0]
            test_metrics,test_trades=simulate(rows,features,b["test_start"],b["test_end"],
                 cfg["starting_balance"],cfg["risk_per_trade"],cfg["fee_rate"],cfg["slippage_rate"],
                 best["params"],edge,True)
            s["fold_results"].append({"fold":s["fold_index"]+1,"train_rows":b["train_end"],
                 "test_rows":b["test_end"]-b["test_start"],"embargo_bars":b["embargo_bars"],
                 "candidate_count":len(s["candidates"]),"best_params":best["params"],
                 "edge_model":edge.summary(),"memory_examples_used":s.get("memory_examples_used",0),
                 "fit_rows":s.get("fit_end"),"validation_start":s.get("validation_start"),
                 "validation":best["metrics"],"train":best["metrics"],"family_leaderboard":family_leaderboard,
                 "test":test_metrics})
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

@app.route("/api/learning/summary")
def learning_summary_api():
    symbol=str(request.args.get("symbol") or "BTCUSDT").upper()
    interval=str(request.args.get("interval") or "1h")
    return jsonify({
      "memory":memory_summary(DB_PATH,symbol,interval),
      "champion":get_champion(DB_PATH,symbol,interval),
      "challengers":challenger_history(DB_PATH,symbol,interval,10)
    })

@app.route("/api/microstructure/import",methods=["POST"])
def import_microstructure_api():
    p=request.get_json(force=True) or {}
    symbol=str(p.get("symbol") or "BTCUSDT").upper();interval=str(p.get("interval") or "1h")
    rows=p.get("rows") or []
    try:
        path,count=import_micro_rows(DATA_DIR,symbol,interval,rows)
        return jsonify({"ok":True,"rows":count,"path":path.name,"summary":micro_summary(load_micro_rows(DATA_DIR,symbol,interval))})
    except Exception as e:return jsonify({"error":str(e)}),400

@app.route("/api/microstructure/summary")
def microstructure_summary_api():
    symbol=str(request.args.get("symbol") or "BTCUSDT").upper();interval=str(request.args.get("interval") or "1h")
    return jsonify(micro_summary(load_micro_rows(DATA_DIR,symbol,interval)))

@app.route("/api/universe")
def universe_api():
    return jsonify({"default":DEFAULT_UNIVERSE,"max_symbols":60,
                    "note":"Cross-coin memory shares historical evidence at reduced weight; research runs remain checkpointed per symbol."})

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")))
