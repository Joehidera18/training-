from flask import Flask, jsonify, render_template, request
from pathlib import Path
import os
from lab.db import init_db, list_runs, get_run
from lab.engine import run_experiment
from lab.data import download_binance_history

app=Flask(__name__)
BASE_DIR=Path(__file__).resolve().parent
DATA_DIR=BASE_DIR/"data"
DB_PATH=BASE_DIR/"research.sqlite3"
DATA_DIR.mkdir(exist_ok=True)
init_db(DB_PATH)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/health")
def health():
    return {"ok":True,"app":"CryptO Research Lab"}

@app.route("/api/runs")
def api_runs():
    return jsonify(list_runs(DB_PATH,100))

@app.route("/api/runs/<int:run_id>")
def api_run(run_id):
    row=get_run(DB_PATH,run_id)
    return jsonify(row) if row else (jsonify({"error":"run not found"}),404)

@app.route("/api/download",methods=["POST"])
def api_download():
    p=request.get_json(force=True) or {}
    try:
        path,rows=download_binance_history(
            str(p.get("symbol","BTCUSDT")).upper(),
            str(p.get("interval","15m")),
            float(p.get("years",2)),
            DATA_DIR
        )
        return jsonify({"ok":True,"path":path.name,"rows":rows})
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/api/backtest",methods=["POST"])
def api_backtest():
    p=request.get_json(force=True) or {}
    try:
        return jsonify(run_experiment(
            DB_PATH,DATA_DIR,
            str(p.get("symbol","BTCUSDT")).upper(),
            str(p.get("interval","15m")),
            float(p.get("years",2)),
            float(p.get("starting_balance",500)),
            float(p.get("risk_per_trade",0.01)),
            float(p.get("fee_rate",0.004)),
            float(p.get("slippage_rate",0.001)),
            float(p.get("train_fraction",0.70)),
            str(p.get("strategy","radar_baseline")),
            bool(p.get("download_if_missing",True)),
            str(p.get("notes","")),
        ))
    except Exception as e:
        return jsonify({"error":str(e)}),500

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")))
