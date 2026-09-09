"""Framework-independent request handlers; exercised directly by offline tests."""
import csv
import io
import json
import math
import os
import secrets
import tempfile
from pathlib import Path

from .continuous import ContinuousLearner, activity_rows, recent_trades, memory_leaderboard
from .db import init_db, list_runs, get_run
from .research import ResearchManager
from .coinbase_broker import CoinbaseAdapter
from .coinbase_live import CoinbaseTrader
from .autolearn import AutoLearner


class Service:
    def __init__(self, base_dir, db_path, data_dir, token=None):
        self.base_dir, self.db_path, self.data_dir = Path(base_dir), Path(db_path), Path(data_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(self.db_path)
        self.agent = ContinuousLearner(self.db_path, self.data_dir)
        self.research = ResearchManager(self.db_path, self.data_dir)
        self.token = token or ""
        self.coinbase = CoinbaseTrader(self.db_path,self.agent,CoinbaseAdapter(
            key_file=os.getenv("COINBASE_KEY_FILE"),
            allow_live=os.getenv("COINBASE_ALLOW_LIVE")=="1"))
        self.autolearn = AutoLearner(self.db_path,self.data_dir,self.agent,self.research)

    def _limit(self, query, default=100):
        value = query.get("limit", str(default))
        if not str(value).isdigit() or not 1 <= int(value) <= 500:
            raise ValueError("limit must be a whole number between 1 and 500")
        return int(value)

    def handle(self, method, path, query=None, body=None, headers=None):
        query, headers = query or {}, {k.lower(): v for k, v in (headers or {}).items()}
        try:
            if path.startswith("/api/") and path != "/api/health" and self.token:
                supplied = headers.get("authorization", "").removeprefix("Bearer ")
                if not secrets.compare_digest(supplied, self.token):
                    return 401, {"error": "Enter the access token configured for this app"}, {}
            if method == "POST":
                if headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    return 415, {"error": "Send application/json"}, {}
                if not isinstance(body, dict):
                    return 400, {"error": "Request body must be a JSON object"}, {}
            if method == "GET" and path == "/":
                return 200, (self.base_dir / "templates/index.html").read_bytes(), {"Content-Type": "text/html; charset=utf-8"}
            if method == "GET" and path in ("/static/app.js", "/static/coinbase.js", "/static/style.css"):
                name = path.rsplit("/", 1)[-1]
                mime = "application/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
                return 200, (self.base_dir / "static" / name).read_bytes(), {"Content-Type": mime}
            if method == "GET" and path == "/api/health":
                return 200, {"ok": True, "api_version": "11.0", "default_mode": "paper",
                             "live_capable":True, "starting_balance":500}, {}
            if (path == "/api/continuous/backup" and len(self.token)<16 and
                (self.coinbase.state.get("snapshot") or self.coinbase.trades(1))):
                raise RuntimeError("Set APP_ACCESS_TOKEN (at least 16 characters) before exporting private Coinbase account data")
            if path.startswith("/api/coinbase/"):
                if path=="/api/coinbase/status" and method=="GET":
                    if len(self.token)<16:
                        return 200, {"configured":self.coinbase.adapter.configured,"running":False,
                            "mode":"locked","live_orders_allowed":False,"snapshot":None,
                            "trades":[],"last_preview":None,"realized_pnl":"0","last_equity":None,
                            "equity_stale":True,"paused":False,"last_error":None,
                            "message":"Set APP_ACCESS_TOKEN (at least 16 characters) locally to unlock Coinbase controls"}, {}
                    return 200,self.coinbase.status(),{}
                if len(self.token)<16:
                    raise RuntimeError("Set APP_ACCESS_TOKEN (at least 16 characters) locally before using Coinbase")
                if method=="POST" and path in ("/api/coinbase/start","/api/coinbase/sync"):
                    mode = "sync" if path.endswith("/sync") else body.get("mode","preview")
                    if mode=="live" and body.get("confirm")!="ENABLE COINBASE LIVE":
                        raise ValueError("Enabling real Coinbase orders requires the exact confirmation phrase")
                    self.coinbase.start(mode)
                    return 202,{"ok":True,"coinbase":self.coinbase.status()},{}
                if method=="POST" and path=="/api/coinbase/stop":
                    self.coinbase.stop()
                    return 200,{"ok":True,"coinbase":self.coinbase.status()},{}
                if method=="POST" and path=="/api/coinbase/pause":
                    self.coinbase.pause(body.get("paused",True))
                    return 200,{"ok":True,"coinbase":self.coinbase.status()},{}
                if method=="POST" and path=="/api/coinbase/close":
                    self.coinbase.request_close()
                    return 202,{"ok":True,"message":"Close requested; cancellation and fills must reconcile first"},{}
                if method=="POST" and path=="/api/coinbase/apply-fees":
                    import time
                    snapshot = self.coinbase.status().get("snapshot")
                    if not snapshot or time.time()-snapshot["synced_at"]>300 or not snapshot["simple_fees"]:
                        raise ValueError("Sync a recent supported Coinbase fee tier first")
                    settings = self.agent.configure({"fee_rate":float(snapshot["taker_fee_rate"])})
                    return 200,{"ok":True,"settings":settings},{}
            if path == "/api/learning/status" and method == "GET":
                return 200, self.autolearn.status(), {}
            if path == "/api/learning/start" and method == "POST":
                if set(body)-{"fee_rate"}:
                    raise ValueError("Automatic start accepts only the fee_rate setting")
                if "fee_rate" in body:
                    self.agent.configure({"fee_rate":body["fee_rate"]})
                self.autolearn.start()
                return 202, {"ok":True,"learning":self.autolearn.status()}, {}
            if path == "/api/learning/stop" and method == "POST":
                self.autolearn.stop()
                return 200, {"ok":True,"learning":self.autolearn.status()}, {}
            if path == "/api/learning/export" and method == "GET":
                return 200, json.dumps(self.autolearn.export(),indent=2,allow_nan=False).encode(), {
                    "Content-Type":"application/json", "Content-Disposition":'attachment; filename="learning-results.json"'}
            if path == "/api/continuous/status" and method == "GET":
                return 200, self.agent.status(), {}
            if path == "/api/continuous/settings":
                if method == "GET":
                    return 200, dict(self.agent.settings), {}
                if method == "POST":
                    return 200, {"ok": True, "settings": self.agent.configure(body)}, {}
            if path == "/api/continuous/start" and method == "POST":
                if body:
                    self.agent.configure(body)
                self.agent.start()
                return 200, {"ok": True, "status": self.agent.status()}, {}
            if path == "/api/continuous/stop" and method == "POST":
                self.autolearn.stop()
                self.agent.stop()
                return 200, {"ok": True, "status": self.agent.status()}, {}
            if path == "/api/continuous/pause" and method == "POST":
                self.agent.configure({"entries_paused": body.get("paused", True)})
                return 200, {"ok": True, "status": self.agent.status()}, {}
            if path == "/api/continuous/reset" and method == "POST":
                if body.get("confirm") != "RESET":
                    raise ValueError("Reset requires confirm: RESET")
                keep = body.get("keep_memory", True)
                if not isinstance(keep, bool):
                    raise ValueError("keep_memory must be true or false")
                if self.autolearn.worker and self.autolearn.worker.is_alive():
                    raise ValueError("Stop automatic learning before resetting the account")
                if self.research.worker and self.research.worker.is_alive():
                    raise ValueError("Cancel research and wait for it to finish before resetting")
                self.agent.reset(keep)
                return 200, {"ok": True, "status": self.agent.status()}, {}
            if path == "/api/continuous/close" and method == "POST":
                return 200, self.agent.close_manual(str(body.get("product_id", ""))), {}
            if path == "/api/continuous/trades" and method == "GET":
                return 200, recent_trades(self.db_path, self._limit(query)), {}
            if path == "/api/continuous/activity" and method == "GET":
                return 200, activity_rows(self.db_path, self._limit(query)), {}
            if path == "/api/continuous/opportunities" and method == "GET":
                return 200, self.agent.opportunity_board(30), {}
            if path == "/api/continuous/memory" and method == "GET":
                return 200, memory_leaderboard(self.db_path, 50), {}
            if path == "/api/continuous/analytics" and method == "GET":
                return 200, self.agent.analytics(), {}
            if path == "/api/research/status" and method == "GET":
                return 200, self.research.status(), {}
            if path == "/api/research/start" and method == "POST":
                if self.autolearn.worker and self.autolearn.worker.is_alive():
                    raise ValueError("Stop automatic learning before running a manual research experiment")
                self.research.start(body.get("symbols", ["BTC-USD"]), body.get("days", 180), self.agent.settings)
                return 202, {"ok": True, "research": self.research.status()}, {}
            if path == "/api/research/cancel" and method == "POST":
                self.research.cancel()
                return 200, {"ok": True}, {}
            if path == "/api/research/export" and method == "GET":
                data = json.dumps(self.research.status(), indent=2, allow_nan=False).encode()
                return 200, data, {"Content-Type": "application/json", "Content-Disposition": 'attachment; filename="research-results.json"'}
            if path == "/api/continuous/export" and method == "GET":
                from .paper_store import db_connect
                con = db_connect(self.db_path)
                records = [dict(r) for r in con.execute("""SELECT id,opened_at,closed_at,product_id,family,direction,
                    entry,exit,qty,risk_usd,status,pnl,result_r,balance_after,exit_reason FROM paper_trades ORDER BY id""")]
                con.close()
                fields = ["id", "opened_at", "closed_at", "product_id", "family", "direction", "entry", "exit", "qty",
                          "risk_usd", "status", "pnl", "result_r", "balance_after", "exit_reason"]
                output = io.StringIO(newline="")
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                for record in records:
                    for k, value in record.items():
                        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                            record[k] = "'" + value
                    writer.writerow(record)
                return 200, output.getvalue().encode(), {"Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="paper-trades.csv"'}
            if path == "/api/continuous/backup" and method == "GET":
                handle, filename = tempfile.mkstemp(suffix=".sqlite3")
                os.close(handle)
                try:
                    self.agent.backup(filename)
                    data = Path(filename).read_bytes()
                finally:
                    Path(filename).unlink(missing_ok=True)
                return 200, data, {"Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="crypto-account-backup.sqlite3"'}
            if path == "/api/runs" and method == "GET":
                return 200, list_runs(self.db_path, 100), {}
            if path.startswith("/api/runs/") and method == "GET":
                value = get_run(self.db_path, int(path.rsplit("/", 1)[-1]))
                return (200, value, {}) if value else (404, {"error": "Run not found"}, {})
            return 404, {"error": "Route not found"}, {}
        except (ValueError, TypeError) as exc:
            return 400, {"error": str(exc)}, {}
        except RuntimeError as exc:
            return 409, {"error": str(exc)}, {}


def wsgi_application(service):
    from urllib.parse import parse_qs
    from http import HTTPStatus

    def application(environ, start_response):
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        body = None
        headers = {"content-type": environ.get("CONTENT_TYPE", ""),
                   "authorization": environ.get("HTTP_AUTHORIZATION", "")}
        extra = {}
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if length > 32768:
                status, payload = 413, {"error": "Request is too large"}
            else:
                if method == "POST":
                    raw = environ["wsgi.input"].read(length)
                    try:
                        body = json.loads(raw) if raw else {}
                    except (ValueError, UnicodeError):
                        status, payload = 400, {"error": "Malformed JSON"}
                    else:
                        status, payload, extra = service.handle(method, path,
                            {k: v[-1] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}, body, headers)
                else:
                    status, payload, extra = service.handle(method, path,
                        {k: v[-1] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}, body, headers)
        except Exception:
            import logging
            logging.exception("Request failed")
            status, payload = 500, {"error": "Internal error; see the local application log"}
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, allow_nan=False).encode()
            extra.setdefault("Content-Type", "application/json")
        extra.update({"Content-Length": str(len(payload)), "Cache-Control": "no-store",
                      "X-Content-Type-Options": "nosniff"})
        start_response(f"{status} {HTTPStatus(status).phrase}", list(extra.items()))
        return [payload]
    return application
