"""One control for historical learning and continuous paper monitoring.

This controller never enables Coinbase live mode or submits exchange orders.
It runs only while the application process and this controller are running.
"""
from __future__ import annotations
import copy
import hashlib
import json
import threading
import time

from .adaptive import approved_profile, profile_key, write_in_transaction
from .engine import ENGINE_VERSION
from .learning_research import learn_history
from .paper_store import db_connect, load_state, save_state
from .research import ResearchManager, cost_signature

HISTORY_DAYS = 1095
TRAINING_MARKETS = 5
REVIEW_SECONDS = 28*86400


class AutoLearner:
    def __init__(self, db_path, data_dir, agent, research_manager):
        self.db_path, self.agent, self.research_manager = db_path, agent, research_manager
        self.lock = threading.RLock()
        self.worker, self.stop_event = None, threading.Event()
        self.state = load_state(db_path, "automatic_learning", {
            "results":[], "next_review_at":0, "tested_costs":None, "history_days":HISTORY_DAYS})
        self.state.update(enabled=False, phase="stopped", message="Ready to learn from history.")
        self.downloader = ResearchManager(db_path, data_dir/"automatic", client=agent.client)
        self.downloader.cancel_event = self.stop_event
        self.downloader._update = self._download_progress

    def _update(self, **patch):
        with self.lock:
            self.state.update(patch)
            save_state(self.db_path, "automatic_learning", self.state)

    def _download_progress(self, **patch):
        self._update(phase="downloading", message=patch.get("message", "Collecting market history"))

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        settings = dict(self.agent.settings)
        reports = result.get("results", [])
        active, updates = [], 0
        for report in reports:
            profile = approved_profile(self.db_path, report["symbol"], settings)
            if profile:
                active.append(report["symbol"])
                saved = load_state(self.db_path, "adaptive_paper_"+report["symbol"], {})
                if saved.get("fingerprint")==profile["fingerprint"]:
                    updates += saved.get("forward_trades", 0)
        result.update(active_markets=active, forward_learning_trades=updates,
            market_data_hours=sum(r.get("data_hours",0) for r in reports),
            historical_examples=sum(r.get("historical_examples",0) for r in reports),
            history_days=HISTORY_DAYS, max_training_markets=TRAINING_MARKETS,
            paper_running=self.agent.runtime["running"])
        return result

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                if self.stop_event.is_set():
                    raise RuntimeError("The previous learning task is still stopping")
                return
            if self.research_manager.worker and self.research_manager.worker.is_alive():
                raise ValueError("Finish or cancel the existing research run before starting automatic learning")
            self.agent.configure({"learning_enabled":True, "validated_only":True})
            self.stop_event.clear()
            self._update(enabled=True, phase="starting", message="Starting market monitoring and learning.")
            self.worker = threading.Thread(target=self._run, daemon=True, name="automatic-learning")
            self.worker.start()

    def stop(self):
        self.stop_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=3)
        stopping = bool(self.worker and self.worker.is_alive())
        self._update(enabled=False, phase="stopping" if stopping else "stopped",
            message="Stopping learning; completed work is saved." if stopping else "Automatic learning stopped. Models are saved.")

    def _fingerprint(self, rows, symbol, settings):
        digest = hashlib.sha256(json.dumps({"symbol":symbol,"engine":ENGINE_VERSION,
            "costs":cost_signature(settings)}, sort_keys=True).encode())
        for row in rows:
            digest.update(json.dumps(row,sort_keys=True,separators=(",",":")).encode())
            digest.update(b"\n")
        return digest.hexdigest()

    def _install(self, result, fingerprint):
        symbol = result["symbol"]
        con = db_connect(self.db_path)
        try:
            with con:
                if result["validated"]:
                    profile = {"fingerprint":fingerprint, "engine_version":ENGINE_VERSION,
                        "cost_signature":result["cost_signature"], "model":result["model"],
                        "data_end_ts":result["data_quality"]["end_ts"]+{
                            "5m":300000,"15m":900000,"1h":3600000}[result["interval"]],
                        "created_at":result["created_at"]}
                    write_in_transaction(con, profile_key(symbol), profile)
                else:
                    con.execute("DELETE FROM continuous_state WHERE key=?", (profile_key(symbol),))
        finally:
            con.close()

    def study(self, symbols, settings):
        """Sequential, cancellable study; failures retain the other market results."""
        results = []
        for symbol in symbols:
            if self.stop_event.is_set():
                raise InterruptedError("Learning cancelled")
            try:
                self._update(phase="downloading", message=f"{symbol}: collecting up to three years of history.")
                rows = self.downloader._history(symbol, settings["decision_interval"], HISTORY_DAYS)
                fingerprint = self._fingerprint(rows, symbol, settings)
                result = load_state(self.db_path, "learning_result_"+fingerprint)
                if result is None:
                    result = learn_history(rows, symbol, settings, self._update, self.stop_event.is_set)
                    result["fingerprint"] = fingerprint
                    save_state(self.db_path, "learning_result_"+fingerprint, result)
                if self.stop_event.is_set():
                    raise InterruptedError("Learning cancelled")
                self._install(result, fingerprint)
                results.append({k:v for k,v in result.items() if k not in ("model","holdout_trades")})
                del rows
            except InterruptedError:
                raise
            except Exception as exc:
                results.append({"symbol":symbol, "validated":False, "error":str(exc),
                    "rejection_reasons":["Historical learning could not finish for this market."]})
            self._update(results=results, completed_markets=len(results), total_markets=len(symbols))
        errors = any(r.get("error") for r in results)
        self._update(results=results, tested_costs=cost_signature(settings),
            tested_engine=ENGINE_VERSION, next_review_at=time.time()+(3600 if errors else REVIEW_SECONDS))
        return results

    def _run(self):
        try:
            # Monitor recovered positions while training; missing models block entries.
            self.agent.start()
            while not self.stop_event.is_set():
                if not self.agent.runtime["running"]:
                    raise RuntimeError(self.agent.runtime.get("last_error") or "Market monitoring stopped; restart after checking the dashboard.")
                if not self.agent.runtime["bootstrapped"]:
                    self._update(phase="starting", message="Loading the Coinbase market scanner.")
                    if self.stop_event.wait(2):
                        break
                    continue
                settings = dict(self.agent.settings)
                if not settings.get("learning_enabled"):
                    break
                needs_review = (self.state.get("tested_costs") != cost_signature(settings) or
                    self.state.get("tested_engine") != ENGINE_VERSION or
                    time.time() >= self.state.get("next_review_at",0))
                if needs_review:
                    # The existing scanner ranks active USD markets by current volume.
                    symbols = list(self.agent.product_ids)[:TRAINING_MARKETS]
                    if not symbols:
                        raise RuntimeError("No Coinbase USD markets are available")
                    self.study(symbols, settings)
                active = self.status()["active_markets"]
                self._update(phase="watching" if active else "waiting",
                    message="Learning from completed paper trades and watching for qualified setups." if active else
                    "No market has passed the checks yet. The system is monitoring without forcing trades.")
                if self.stop_event.wait(15):
                    break
        except InterruptedError:
            pass
        except Exception as exc:
            self._update(phase="error", message=str(exc))
        finally:
            phase = "error" if self.state.get("phase")=="error" else "stopped"
            self._update(enabled=False, phase=phase)

    def export(self):
        result = self.status()
        result["results"] = [load_state(self.db_path, "learning_result_"+r["fingerprint"],r)
            if r.get("fingerprint") else r for r in result.get("results",[])]
        return result
