"""Paper-only continuous learner. Network I/O never runs in a dashboard request."""
from __future__ import annotations

import copy
import json
import math
import random
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

from .coinbase_feed import CoinbaseClient, CoinbaseTickerStream
from .engine import build_feature_cache, evaluate_signal
from .strategies import profit_candidates
from .trade_quality import net_payoff, cooldown_minutes
from .paper_store import (init_continuous_db, db_connect, load_state, log_activity,
                         recent_trades, activity_rows, memory_leaderboard)

INTERVAL_MS = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
MAX_BARS = {"5m": 600, "15m": 400, "1h": 1200, "4h": 300}
DEFAULTS = {
    "starting_balance": 500.0, "risk_per_trade": .0075, "max_positions": 5,
    "max_total_risk": .03, "max_notional_fraction": .30, "max_gross_exposure": 1.0,
    "exploration_rate": .10, "fee_rate": .004, "slippage_rate": .0005,
    "universe_size": 30, "daily_loss_limit": .03, "max_spread": .003,
    "stale_after_seconds": 60, "allow_shorts": False, "entries_paused": False,
    "decision_interval": "15m", "validated_only": True,
}
BOUNDS = {
    "risk_per_trade": (.001, .02), "max_positions": (1, 10),
    "max_total_risk": (.005, .08), "max_notional_fraction": (.05, 1),
    "max_gross_exposure": (.1, 1), "exploration_rate": (0, .3),
    "fee_rate": (0, .02), "slippage_rate": (0, .01), "universe_size": (1, 30),
    "daily_loss_limit": (.005, .10), "max_spread": (.0001, .02),
    "stale_after_seconds": (10, 300),
}
INTEGER_SETTINGS = {"max_positions", "universe_size", "stale_after_seconds"}


def now_ms():
    return int(time.time() * 1000)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def resample(rows, bucket_ms, max_rows=300, asof_ms=None):
    """Only emit complete 4-hour buckets containing all four hourly candles."""
    asof_ms = now_ms() if asof_ms is None else asof_ms
    groups = {}
    for row in sorted({r["ts"]: r for r in rows}.values(), key=lambda r: r["ts"]):
        bucket = row["ts"] // bucket_ms * bucket_ms
        groups.setdefault(bucket, []).append(row)
    result = []
    for bucket, group in sorted(groups.items()):
        expected = list(range(bucket, bucket + bucket_ms, INTERVAL_MS["1h"]))
        if bucket + bucket_ms > asof_ms or [r["ts"] for r in group] != expected:
            continue
        result.append({"ts": bucket, "open": group[0]["open"], "close": group[-1]["close"],
                       "high": max(r["high"] for r in group), "low": min(r["low"] for r in group),
                       "volume": sum(r.get("volume", 0) for r in group),
                       "quote_volume": sum(r.get("quote_volume", 0) for r in group), "trades": 0})
    return result[-max_rows:]


def estimate_memory(global_row, coin_row):
    """Exclude this coin from the global contribution; never double-count trades."""
    g, c = global_row, coin_row
    other_n = max(0, g["samples"] - c["samples"])
    weight = .45
    effective = c["samples"] + weight * other_n
    weighted_r = c["sum_r"] + weight * (g["sum_r"] - c["sum_r"])
    weighted_wins = c["wins"] + weight * max(0, g["wins"] - c["wins"])
    return {"samples": max(g["samples"], c["samples"]), "coin_samples": c["samples"],
            "effective_samples": effective, "expectancy_r": weighted_r / (effective + 12),
            "win_probability": (weighted_wins + 6) / (effective + 12)}


def write_state(con, key, value):
    con.execute("""INSERT INTO continuous_state(key,value_json,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
        (key, json.dumps(value, allow_nan=False, separators=(",", ":")), int(time.time())))


def update_memory(con, scope, symbol, context, family, direction, result):
    con.execute("""INSERT INTO continuous_memory
        (scope,symbol,context_key,family,direction,samples,wins,sum_r,sumsq_r,updated_at)
        VALUES(?,?,?,?,?,1,?,?,?,?)
        ON CONFLICT(scope,symbol,context_key,family,direction) DO UPDATE SET
        samples=samples+1,wins=wins+excluded.wins,sum_r=sum_r+excluded.sum_r,
        sumsq_r=sumsq_r+excluded.sumsq_r,updated_at=excluded.updated_at""",
        (scope, symbol, context, family, direction, int(result > 0), result, result * result, int(time.time())))


class RuntimeLease:
    """An OS-held lock prevents two processes from running the same paper account."""
    def __init__(self, path):
        self.path = Path(str(path) + ".learner.lock")
        self.handle = None

    def acquire(self):
        handle = self.path.open("a+b")
        try:
            if __import__("os").name == "nt":
                import msvcrt
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("This account is already running in another process")
        self.handle = handle

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class ContinuousLearner:
    def __init__(self, db_path, data_dir, client=None):
        self.db_path, self.data_dir = Path(db_path), Path(data_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        init_continuous_db(self.db_path)
        self.lock = threading.RLock()
        self.client = client or CoinbaseClient()
        self.stream, self.worker = None, None
        self.stop_event = threading.Event()
        self.lease = RuntimeLease(self.db_path)
        self.product_ids, self.market, self.open_positions = [], {}, {}
        self.strategy_variants = profit_candidates()
        self._feature_cache, self._board, self._processed = {}, {}, {}
        self.runtime = {"running": False, "stream_status": "stopped", "stream_message": "",
                        "last_tick_ts": None, "last_cycle_ts": None, "bootstrapped": False,
                        "bootstrap_done": 0, "bootstrap_total": 0, "last_error": None}
        self._load()

    def _load(self):
        self.settings = {**DEFAULTS, **load_state(self.db_path, "continuous_settings", {})}
        self.portfolio = load_state(self.db_path, "continuous_portfolio", {
            "balance": 500.0, "starting_balance": 500.0, "peak_balance": 500.0,
            "realized_pnl": 0.0, "completed_trades": 0, "wins": 0, "losses": 0,
            "started_at": None, "cycles": 0})
        saved = load_state(self.db_path, "continuous_open_positions", {})
        self.cooldown_until = load_state(self.db_path, "continuous_cooldowns", {})
        self.risk_day = load_state(self.db_path, "continuous_risk_day", {})
        # Journal status is authoritative if an older version crashed between saves.
        con = db_connect(self.db_path)
        rows = con.execute("SELECT * FROM paper_trades WHERE status='OPEN' ORDER BY id").fetchall()
        con.close()
        recovered = {}
        for row in rows:
            r = dict(row)
            p = saved.get(r["product_id"], {})
            if p.get("trade_id") != r["id"]:
                context = json.loads(r["context_json"])
                p = {"product_id": r["product_id"], "family": r["family"], "direction": r["direction"],
                     "opened_at": r["opened_at"] * 1000, "entry": r["entry"], "stop": r["stop"],
                     "target": r["target"], "qty": r["qty"], "risk_usd": r["risk_usd"],
                     "trade_id": r["id"], "context_key": context.get("key", "legacy"),
                     "context": context, "decision": json.loads(r["decision_json"]),
                     "mfe_r": r["mfe_r"] or 0, "mae_r": r["mae_r"] or 0,
                     "stop_dist": abs(r["entry"] - r["stop"]), "last_price": r["entry"],
                     "mode": "legacy"}
            p.setdefault("fee_rate", self.settings["fee_rate"])
            p.setdefault("slippage_rate", self.settings["slippage_rate"])
            p.setdefault("last_quote_ts", None)
            recovered[r["product_id"]] = p
        self.open_positions = recovered

    def _persist(self, con, portfolio=None, positions=None):
        for key, value in {
            "continuous_settings": self.settings,
            "continuous_portfolio": self.portfolio if portfolio is None else portfolio,
            "continuous_open_positions": self.open_positions if positions is None else positions,
            "continuous_cooldowns": self.cooldown_until,
            "continuous_risk_day": self.risk_day,
        }.items():
            write_state(con, key, value)

    def _save(self):
        with self.lock:
            con = db_connect(self.db_path)
            try:
                with con:
                    self._persist(con)
            finally:
                con.close()

    def configure(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("Settings must be a JSON object")
        unknown = set(patch) - set(BOUNDS) - {"allow_shorts", "entries_paused", "validated_only", "decision_interval"}
        if unknown:
            raise ValueError("Unknown setting: " + ", ".join(sorted(unknown)))
        with self.lock:
            candidate = dict(self.settings)
            for key, value in patch.items():
                if key == "decision_interval":
                    if value not in ("5m", "15m", "1h"):
                        raise ValueError("Use a 5m, 15m, or 1h decision interval")
                    candidate[key] = value
                    continue
                if key in ("allow_shorts", "entries_paused", "validated_only"):
                    if not isinstance(value, bool):
                        raise ValueError(f"{key} must be true or false")
                    candidate[key] = value
                    continue
                if isinstance(value, bool):
                    raise ValueError(f"{key} must be a number")
                try:
                    number = float(value)
                except (ValueError, TypeError):
                    raise ValueError(f"{key} must be a number")
                lo, hi = BOUNDS[key]
                if not math.isfinite(number) or not lo <= number <= hi:
                    raise ValueError(f"{key} must be between {lo} and {hi}")
                if key in INTEGER_SETTINGS and number != int(number):
                    raise ValueError(f"{key} must be a whole number")
                candidate[key] = int(number) if key in INTEGER_SETTINGS else number
            if candidate["risk_per_trade"] > candidate["max_total_risk"]:
                raise ValueError("Total risk must be at least the risk per trade")
            if candidate["max_notional_fraction"] > candidate["max_gross_exposure"]:
                raise ValueError("Per-position exposure must fit within total exposure")
            if self.runtime["running"] and candidate["universe_size"] != self.settings["universe_size"]:
                raise ValueError("Stop the learner before changing its number of markets")
            con = db_connect(self.db_path)
            try:
                with con:
                    write_state(con, "continuous_settings", candidate)
            finally:
                con.close()
            self.settings = candidate
            return dict(candidate)

    def _status_cb(self, status, message):
        with self.lock:
            if self.stop_event.is_set():
                return
            changed = (status, message) != (self.runtime["stream_status"], self.runtime["stream_message"])
            self.runtime.update(stream_status=status, stream_message=message)
        if changed:
            log_activity(self.db_path, "info" if status == "live" else "warning", message)

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                if self.stop_event.is_set():
                    raise RuntimeError("The previous worker is still stopping; try again shortly")
                return
            self.lease.acquire()
            try:
                self._load()
                self.stop_event.clear()
                self.runtime.update(running=True, bootstrapped=False, bootstrap_done=0, last_error=None)
                self.portfolio["started_at"] = self.portfolio.get("started_at") or int(time.time())
                self._save()
                self.worker = threading.Thread(target=self._main, daemon=True, name="paper-learner")
                self.worker.start()
            except Exception:
                self.runtime["running"] = False
                self.lease.release()
                raise

    def stop(self):
        self.stop_event.set()
        with self.lock:
            self.runtime["stream_status"] = "stopping"
        if self.stream:
            self.stream.stop()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=3)
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                self.runtime.update(running=False, stream_status="stopped")
            self._save()

    def reset(self, keep_memory=True):
        with self.lock:
            if self.open_positions:
                raise ValueError("Close open paper positions before resetting the account")
        self.stop()
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise RuntimeError("Wait for the learner to stop before resetting")
            if self.open_positions:
                raise ValueError("Close open paper positions before resetting the account")
            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)
            backup_path = backup_dir / f"before-reset-{time.time_ns()}.sqlite3"
            self.backup(backup_path)
            portfolio = {"balance": 500.0, "starting_balance": 500.0, "peak_balance": 500.0,
                         "realized_pnl": 0.0, "completed_trades": 0, "wins": 0, "losses": 0,
                         "started_at": None, "cycles": 0}
            con = db_connect(self.db_path)
            old_day, old_cooldown = self.risk_day, self.cooldown_until
            try:
                with con:
                    con.execute("DELETE FROM paper_trades")
                    if not keep_memory:
                        con.execute("DELETE FROM continuous_memory")
                    self.risk_day, self.cooldown_until = {}, {}
                    self._persist(con, portfolio, {})
            except Exception:
                self.risk_day, self.cooldown_until = old_day, old_cooldown
                raise
            finally:
                con.close()
            self.portfolio = portfolio
            self._board.clear()
            log_activity(self.db_path, "info", "Paper account reset; backup saved",
                         {"kept_memory": keep_memory, "backup": backup_path.name})

    def backup(self, destination):
        source = db_connect(self.db_path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def _empty_market(self):
        return {"bars": {iv: [] for iv in INTERVAL_MS}, "ticker": {}, "readiness": "Loading candles",
                "last_sync_ts": None, "last_decision": "Waiting for history", "rejections": {}}

    def _sync_product(self, pid, bootstrap=False):
        # Signals use authoritative, completed REST candles; tickers are not OHLCV.
        now = now_ms()
        with self.lock:
            existing = copy.deepcopy(self.market[pid]["bars"])
        for iv in ("5m", "15m", "1h"):
            if self.stop_event.is_set():
                return
            step = INTERVAL_MS[iv]
            expected = now // step * step - step
            if existing[iv] and existing[iv][-1]["ts"] >= expected:
                continue
            if bootstrap or not existing[iv]:
                count = 1040 if iv == "1h" else 300
            else:
                count = min(MAX_BARS[iv], max(3, (expected - existing[iv][-1]["ts"]) // step + 3))
            try:
                fetched = self.client.candles(pid, iv, limit=count, end_ms=now)
                combined = {r["ts"]: r for r in existing[iv] + fetched if r["ts"] + step <= now}
                existing[iv] = [combined[t] for t in sorted(combined)][-MAX_BARS[iv]:]
            except Exception as exc:
                with self.lock:
                    self.market[pid]["last_decision"] = f"{iv} history unavailable: {exc}"
                log_activity(self.db_path, "warning", f"{pid}: {iv} candle refresh failed", {"error": str(exc)})
        existing["4h"] = resample(existing["1h"], INTERVAL_MS["4h"], asof_ms=now)
        with self.lock:
            self.market[pid]["bars"] = existing
            self.market[pid]["last_sync_ts"] = now
            for iv in INTERVAL_MS:
                self._feature_cache.pop((pid, iv), None)
        for iv in INTERVAL_MS:
            self._latest_feature(pid, iv)
        self._decision_cycle(pid)

    def _main(self):
        try:
            log_activity(self.db_path, "info", "Finding active Coinbase USD markets")
            selected = self.client.discover_top_usd(self.settings["universe_size"], self.stop_event)
            if self.stop_event.is_set():
                return
            with self.lock:
                # Continue to monitor positions even if they leave the ranked universe.
                self.product_ids = list(dict.fromkeys(selected + list(self.open_positions)))
                self.market = {pid: self._empty_market() for pid in self.product_ids}
                self._feature_cache.clear()
                self._board.clear()
                self.runtime["bootstrap_total"] = len(self.product_ids)
            self.stream = CoinbaseTickerStream(self.product_ids, self.on_tick, self._status_cb)
            self.stream.start()
            for pid in self.product_ids:
                if self.stop_event.is_set():
                    return
                self._sync_product(pid, bootstrap=True)
                with self.lock:
                    self.runtime["bootstrap_done"] += 1
            with self.lock:
                self.runtime["bootstrapped"] = True
            last_bucket, last_save = None, 0
            while not self.stop_event.wait(1):
                self._manage_time_exits()
                bucket = (now_ms() - 4000) // INTERVAL_MS["5m"]
                if bucket != last_bucket:
                    for pid in list(self.product_ids):
                        if self.stop_event.is_set():
                            break
                        self._sync_product(pid)
                    last_bucket = bucket
                if time.time() - last_save > 15:
                    self._save()
                    last_save = time.time()
        except Exception as exc:
            with self.lock:
                self.runtime.update(last_error=str(exc), stream_message=str(exc), stream_status="error")
            log_activity(self.db_path, "error", "Learner stopped after an error", {"error": str(exc)})
        finally:
            self.stop_event.set()
            if self.stream:
                self.stream.stop()
            with self.lock:
                self.runtime["running"] = False
                if not self.runtime["last_error"]:
                    self.runtime["stream_status"] = "stopped"
            try:
                self._save()
            finally:
                self.lease.release()

    def on_tick(self, tick):
        pid = tick.get("product_id")
        ts = tick.get("ts", 0)
        price = tick.get("price", 0)
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            return
        with self.lock:
            if not self.runtime["running"] or self.stop_event.is_set():
                return
            m = self.market.get(pid)
            if not m or ts <= m["ticker"].get("ts", 0) or ts > now_ms() + 5000:
                return
            if now_ms() - ts > self.settings["stale_after_seconds"] * 1000:
                return
            m["ticker"] = dict(tick)
            self.runtime["last_tick_ts"] = max(self.runtime["last_tick_ts"] or 0, ts)
            self._manage_position_tick(pid, price, ts)
            self._check_daily_limit()

    def _latest_feature(self, pid, iv):
        with self.lock:
            rows = self.market.get(pid, {}).get("bars", {}).get(iv, [])
            signature = (len(rows), rows[-1]["ts"] if rows else None)
            cached = self._feature_cache.get((pid, iv))
            if cached and cached[0] == signature:
                return cached[1]
            snapshot = list(rows)
        feature = None
        if len(snapshot) >= 241:
            feature = build_feature_cache(snapshot, iv)["features"][-1]
        with self.lock:
            self._feature_cache[(pid, iv)] = (signature, feature)
        return feature

    def _context(self, pid, f):
        higher = {iv: self._latest_feature(pid, iv) or {} for iv in ("15m", "1h", "4h")}
        lv = sum(q.get("structure") == "BULL" or bool(q.get("_trend_long")) for q in higher.values())
        sv = sum(q.get("structure") == "BEAR" or bool(q.get("_trend_short")) for q in higher.values())
        bias = "BULL" if lv >= 2 else ("BEAR" if sv >= 2 else "MIXED")
        key = "|".join([str(f.get("regime", "NA")), str(f.get("structure", "NA")), bias,
                       "REV" if f.get("bullish_reversal_sequence") or f.get("bearish_reversal_sequence") else "NOREV",
                       "CONT" if f.get("bullish_continuation_sequence") or f.get("bearish_continuation_sequence") else "NOCONT",
                       f"H{f.get('hour_block', 0)}"])
        return {"key": key, "htf_bias": bias, "htf": {iv: {"structure": q.get("structure"),
                "regime": q.get("regime")} for iv, q in higher.items()}}

    def _candidate_opportunities(self, pid, f, ctx):
        con = db_connect(self.db_path)
        rows = con.execute("SELECT * FROM continuous_memory WHERE context_key=? AND (symbol='*' OR symbol=?)",
                           (ctx["key"], pid)).fetchall()
        con.close()
        memory = {(r["scope"], r["family"], r["direction"]): dict(r) for r in rows}
        zero = {"samples": 0, "wins": 0, "sum_r": 0.0}
        result, reasons = [], Counter()
        approved = self._approved_params(pid)
        population = [approved] if self.settings["validated_only"] and approved else self.strategy_variants
        for p in population:
            if p["direction"] == "SHORT" and not self.settings["allow_shorts"]:
                reasons["shorts_disabled"] += 1
                continue
            score, reason = evaluate_signal(f, p, None, None, 0)
            if score is None:
                reasons[reason or "no_setup"] += 1
                continue
            if ctx["htf_bias"] != "MIXED":
                aligned = (p["direction"] == "LONG") == (ctx["htf_bias"] == "BULL")
                score += 7 if aligned else -7
            learned = estimate_memory(memory.get(("global", p["family"], p["direction"]), zero),
                                      memory.get(("coin", p["family"], p["direction"]), zero))
            adjusted = score + clamp(learned["expectancy_r"] * 18, -16, 16) + (learned["win_probability"] - .5) * 8
            result.append({"params": p, "raw_score": round(score, 2), "adjusted_score": round(adjusted, 2), "learned": learned})
        with self.lock:
            self.market[pid]["rejections"] = dict(reasons)
        return sorted(result, key=lambda x: x["adjusted_score"], reverse=True)

    def _decision_cycle(self, pid):
        with self.lock:
            m = self.market[pid]
            decision_iv = self.settings["decision_interval"]
            rows = list(m["bars"][decision_iv])
            ready = all(len(m["bars"][iv]) >= 241 for iv in INTERVAL_MS)
            m["readiness"] = "Ready" if ready else "Warming up higher timeframes"
            if not ready:
                m["last_decision"] = "Waiting for 241 completed candles in each timeframe"
                return
            # Missing buckets are reported instead of inventing zero-volume market data.
            for iv in INTERVAL_MS:
                recent = m["bars"][iv][-241:]
                if any(b["ts"] - a["ts"] != INTERVAL_MS[iv] for a, b in zip(recent, recent[1:])):
                    m["last_decision"] = f"Waiting: missing {iv} candles"
                    return
                if now_ms() - (recent[-1]["ts"] + INTERVAL_MS[iv]) > INTERVAL_MS[iv] + 15000:
                    m["last_decision"] = f"Waiting: stale {iv} candles"
                    return
            candle_ts = rows[-1]["ts"]
        f = self._latest_feature(pid, decision_iv)
        if not f:
            return
        ctx = self._context(pid, f)
        opportunities = self._candidate_opportunities(pid, f, ctx)
        with self.lock:
            if opportunities:
                top = opportunities[0]
                self._board[pid] = {"product_id": pid, "family": top["params"]["family"],
                    "direction": top["params"]["direction"], "score": top["adjusted_score"],
                    "raw_score": top["raw_score"], "learned": top["learned"], "htf_bias": ctx["htf_bias"],
                    "signal_ts": candle_ts, "regime": f.get("regime"), "structure": f.get("structure")}
            else:
                self._board.pop(pid, None)
            if self._processed.get(pid) == candle_ts:
                return
            reason = self._entry_block(pid)
            if reason:
                m["last_decision"] = reason
                return
            if now_ms() - (candle_ts + INTERVAL_MS[decision_iv]) > 60000:
                m["last_decision"] = "Waiting for a new signal candle; the last close is over 60 seconds old"
                return
            self._processed[pid] = candle_ts
            self.portfolio["cycles"] += 1
            self.runtime["last_cycle_ts"] = now_ms()
            if not opportunities:
                m["last_decision"] = "No strategy passed its signal checks"
                return
            exploration = not self.settings["validated_only"] and random.random() < self.settings["exploration_rate"]
            choice = random.choice(opportunities[:8]) if exploration else opportunities[0]
            if not self.settings["validated_only"] and not exploration and choice["learned"]["samples"] >= 25 and choice["learned"]["expectancy_r"] < -.10:
                m["last_decision"] = "Waiting: this setup has negative learned expectancy"
                return
            if not self.settings["validated_only"] and choice["adjusted_score"] < 54 and not exploration:
                m["last_decision"] = "Waiting: setup score too low"
                return
            self._open_position(pid, m["ticker"]["price"], f, ctx, choice,
                                "exploration" if exploration else "exploitation")

    def _fresh_quote(self, pid):
        tick = self.market.get(pid, {}).get("ticker", {})
        if not tick or now_ms() - tick.get("ts", 0) > self.settings["stale_after_seconds"] * 1000:
            return None
        if tick.get("ts", 0) > now_ms() + 5000:
            return None
        try:
            bid, ask, price = float(tick["best_bid"]), float(tick["best_ask"]), float(tick["price"])
            if not all(math.isfinite(v) and v > 0 for v in (bid, ask, price)) or bid > ask:
                return None
        except (TypeError, KeyError, ValueError):
            return None
        return tick

    def _entry_block(self, pid):
        if not self.runtime["running"] or self.stop_event.is_set():
            return "Learner stopped"
        if self.settings["entries_paused"]:
            return "New entries paused; open positions are still monitored"
        if self.settings["validated_only"] and self._approved_params(pid) is None:
            return "No current validated strategy for these settings; run historical research"
        if self._check_daily_limit():
            return "Daily loss limit reached; entries paused until the next UTC day"
        if pid in self.open_positions:
            return "Already holding a paper position"
        if len(self.open_positions) >= self.settings["max_positions"]:
            return "Maximum open positions reached"
        if self.cooldown_until.get(pid, 0) > time.time():
            return "Waiting for the market cooldown"
        tick = self._fresh_quote(pid)
        if not tick:
            return "Waiting for a fresh bid and ask"
        if (tick["best_ask"] - tick["best_bid"]) / tick["price"] > self.settings["max_spread"]:
            return "Spread is above the configured limit"
        return None

    def _approved_params(self, pid):
        from .research import cost_signature
        from .engine import ENGINE_VERSION
        profile = load_state(self.db_path, "validated_profiles", {}).get(pid)
        if not profile or profile.get("engine_version") != ENGINE_VERSION:
            return None
        if profile.get("cost_signature") != cost_signature(self.settings):
            return None
        if now_ms() - profile.get("data_end_ts", 0) > 30 * 86400000:
            return None
        return profile.get("params")

    def _current_total_risk(self):
        return sum(p["risk_usd"] for p in self.open_positions.values())

    def _mark_pnl(self, p, tick=None):
        tick = tick or self._fresh_quote(p["product_id"])
        mark = p.get("last_price", p["entry"])
        if tick:
            mark = tick["best_bid"] if p["direction"] == "LONG" else tick["best_ask"]
        slip, fee = p["slippage_rate"], p["fee_rate"]
        exit_price = mark * (1 - slip if p["direction"] == "LONG" else 1 + slip)
        gross = (exit_price - p["entry"]) * p["qty"] * (1 if p["direction"] == "LONG" else -1)
        return gross - (p["entry"] + exit_price) * p["qty"] * fee

    def _equity(self):
        return self.portfolio["balance"] + sum(self._mark_pnl(p) for p in self.open_positions.values())

    def _check_daily_limit(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        equity = self._equity()
        before = dict(self.risk_day)
        if self.risk_day.get("date") != day:
            self.risk_day = {"date": day, "start_equity": equity, "halted": False}
        if equity <= self.risk_day["start_equity"] * (1 - self.settings["daily_loss_limit"]):
            self.risk_day["halted"] = True
        if self.risk_day != before:
            con = db_connect(self.db_path)
            try:
                with con:
                    write_state(con, "continuous_risk_day", self.risk_day)
            finally:
                con.close()
        return self.risk_day["halted"]

    def _open_position(self, pid, market_price, f, ctx, choice, mode):
        with self.lock:
            reason = self._entry_block(pid)
            if reason:
                self.market[pid]["last_decision"] = reason
                return None
            tick, p = self._fresh_quote(pid), choice["params"]
            direction = p["direction"]
            fee, slip = self.settings["fee_rate"], self.settings["slippage_rate"]
            entry = (tick["best_ask"] * (1 + slip) if direction == "LONG" else tick["best_bid"] * (1 - slip))
            atr = max(float(f.get("_atr") or 0), market_price * .002)
            stop_dist = max(atr * p.get("stop_atr", 1.4), market_price * .0015)
            stop = entry - stop_dist if direction == "LONG" else entry + stop_dist
            target = entry + stop_dist * p.get("rr2", 2) * (1 if direction == "LONG" else -1)
            if stop <= 0 or target <= 0:
                self.market[pid]["last_decision"] = "Invalid stop or target"
                return None
            # Sequence strategies must actually be near their retracement, not enter a chase.
            if p.get("entry_mode") == "RETRACE_LIMIT":
                retrace = f.get("bull_retrace" if direction == "LONG" else "bear_retrace")
                if not retrace:
                    self.market[pid]["last_decision"] = "Waiting for the sequence retracement"
                    return None
            if abs(market_price - f.get("_close", market_price)) > atr * p.get("max_gap_atr", .6):
                self.market[pid]["last_decision"] = "Entry spread and slippage exceed the gap limit"
                return None
            stop_fill = stop * (1 - slip if direction == "LONG" else 1 + slip)
            unit_risk = abs(entry - stop_fill) + (entry + stop_fill) * fee
            cost_r = ((entry + stop_fill) * fee + abs(stop - stop_fill) + abs(entry - market_price)) / stop_dist
            if cost_r > p.get("max_cost_r", .8):
                self.market[pid]["last_decision"] = "Fees and slippage are too large for this stop distance"
                return None
            quality = net_payoff(entry,stop,target,fee,slip,direction)
            if quality["net_rr"] < p.get("min_net_rr",0):
                self.market[pid]["last_decision"] = "Potential reward after costs is too small relative to stop risk"
                return None
            equity = max(0, self._equity())
            risk_scale = 1.0 if self._approved_params(pid) else .25
            risk_budget = min(equity * self.settings["risk_per_trade"] * risk_scale,
                              equity * self.settings["max_total_risk"] - self._current_total_risk())
            used = sum(pos["entry"] * pos["qty"] * (1 + pos["fee_rate"]) for pos in self.open_positions.values())
            budget = min(equity * self.settings["max_notional_fraction"],
                         equity * self.settings["max_gross_exposure"] - used)
            qty = min(risk_budget / max(unit_risk, 1e-12), budget / (entry * (1 + fee)))
            if not math.isfinite(qty) or qty <= 0 or qty * entry < 1:
                self.market[pid]["last_decision"] = "Portfolio risk or available exposure is fully used"
                return None
            position = {"product_id": pid, "family": p["family"], "direction": direction,
                "opened_at": now_ms(), "entry": entry, "stop": stop, "target": target, "qty": qty,
                "risk_usd": qty * unit_risk, "planned_net_rr":quality["net_rr"], "stop_dist": stop_dist, "mode": mode, "context_key": ctx["key"],
                "context": ctx, "decision": {**choice, "params": p}, "mfe_r": 0.0, "mae_r": 0.0,
                "last_price": market_price, "last_quote_ts": tick["ts"], "fee_rate": fee, "slippage_rate": slip}
            con = db_connect(self.db_path)
            try:
                with con:
                    cur = con.execute("""INSERT INTO paper_trades
                        (opened_at,product_id,family,direction,entry,stop,target,qty,risk_usd,status,
                         context_json,decision_json,mfe_r,mae_r)
                        VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?,?,0,0)""",
                        (position["opened_at"] // 1000, pid, p["family"], direction, entry, stop, target, qty,
                         position["risk_usd"], json.dumps(ctx), json.dumps(position["decision"])))
                    position["trade_id"] = cur.lastrowid
                    positions = {**self.open_positions, pid: position}
                    self._persist(con, positions=positions)
            finally:
                con.close()
            self.open_positions = positions
            self.market[pid]["last_decision"] = f"Opened {direction.lower()} paper trade ({mode})"
            log_activity(self.db_path, "trade", f"{pid}: {direction} paper trade opened",
                         {"risk_usd": position["risk_usd"], "mode": mode, "family": p["family"]})
            return position

    def _manage_position_tick(self, pid, price, ts):
        p = self.open_positions.get(pid)
        if not p:
            return
        quote = self._fresh_quote(pid)
        if not quote:
            return
        mark = quote["best_bid"] if p["direction"] == "LONG" else quote["best_ask"]
        excursion = (mark - p["entry"]) / p["stop_dist"] * (1 if p["direction"] == "LONG" else -1)
        p.update(last_price=mark, last_quote_ts=ts, mfe_r=max(p["mfe_r"], excursion), mae_r=min(p["mae_r"], excursion))
        if (mark <= p["stop"] if p["direction"] == "LONG" else mark >= p["stop"]):
            self._close_position(pid, mark, ts, "STOP")
        elif (mark >= p["target"] if p["direction"] == "LONG" else mark <= p["target"]):
            self._close_position(pid, mark, ts, "TARGET")

    def _manage_time_exits(self):
        with self.lock:
            for pid, p in list(self.open_positions.items()):
                quote = self._fresh_quote(pid)
                hours = p["decision"]["params"].get("time_stop_hours", 20)
                if quote and now_ms() - p["opened_at"] >= hours * 3600000:
                    mark = quote["best_bid"] if p["direction"] == "LONG" else quote["best_ask"]
                    self._close_position(pid, mark, quote["ts"], "TIME")

    def close_manual(self, pid):
        with self.lock:
            if pid not in self.open_positions:
                raise ValueError("There is no open paper position for this market")
            quote = self._fresh_quote(pid)
            if not quote:
                raise ValueError("Cannot close at a stale price; start the learner and wait for fresh data")
            pos = self.open_positions[pid]
            mark = quote["best_bid"] if pos["direction"] == "LONG" else quote["best_ask"]
            return self._close_position(pid, mark, quote["ts"], "MANUAL")

    def _close_position(self, pid, market_price, ts, reason):
        with self.lock:
            pos = self.open_positions.get(pid)
            if not pos:
                return None
            d = pos["direction"]
            exit_price = market_price * (1 - pos["slippage_rate"] if d == "LONG" else 1 + pos["slippage_rate"])
            gross = (exit_price - pos["entry"]) * pos["qty"] * (1 if d == "LONG" else -1)
            fees = (pos["entry"] + exit_price) * pos["qty"] * pos["fee_rate"]
            pnl = gross - fees
            result = pnl / max(pos["risk_usd"], 1e-12)
            portfolio = dict(self.portfolio)
            # Keep real arithmetic: do not silently clamp losses out of the ledger.
            portfolio["balance"] += pnl
            portfolio["realized_pnl"] += pnl
            portfolio["completed_trades"] += 1
            portfolio["wins"] += int(pnl > 0)
            portfolio["losses"] += int(pnl <= 0)
            portfolio["peak_balance"] = max(portfolio["peak_balance"], portfolio["balance"])
            positions = {key: value for key, value in self.open_positions.items() if key != pid}
            old_cooldown = dict(self.cooldown_until)
            self.cooldown_until[pid] = time.time() + 900
            con = db_connect(self.db_path)
            try:
                with con:
                    cur = con.execute("""UPDATE paper_trades SET closed_at=?,status='CLOSED',exit=?,pnl=?,
                        result_r=?,balance_after=?,mfe_r=?,mae_r=?,exit_reason=? WHERE id=? AND status='OPEN'""",
                        (ts // 1000, exit_price, pnl, result, portfolio["balance"], pos["mfe_r"],
                         pos["mae_r"], reason, pos["trade_id"]))
                    if cur.rowcount != 1:
                        raise RuntimeError("Trade state changed; restart before continuing")
                    for scope, symbol in (("global", "*"), ("coin", pid)):
                        update_memory(con, scope, symbol, pos["context_key"], pos["family"], d, result)
                    closed = con.execute("SELECT pnl FROM paper_trades WHERE product_id=? AND status='CLOSED' ORDER BY id DESC LIMIT 3",(pid,)).fetchall()
                    delay = cooldown_minutes([row["pnl"] for row in reversed(closed)],pos["decision"].get("params",{}))
                    self.cooldown_until[pid] = time.time()+delay*60
                    self._persist(con, portfolio, positions)
            except Exception:
                self.cooldown_until = old_cooldown
                raise
            finally:
                con.close()
            self.portfolio, self.open_positions = portfolio, positions
            self._check_daily_limit()
            log_activity(self.db_path, "success" if pnl > 0 else "loss", f"{pid} closed {result:+.2f}R",
                         {"reason": reason, "net_pnl": pnl, "fees": fees, "balance": portfolio["balance"]})
            return {"product_id": pid, "pnl": pnl, "result_r": result, "exit_reason": reason}

    def opportunity_board(self, limit=20):
        with self.lock:
            board = copy.deepcopy(list(self._board.values()))
            for item in board:
                item["decision"] = self.market.get(item["product_id"], {}).get("last_decision", "")
        return sorted(board, key=lambda x: x["score"], reverse=True)[:limit]

    def coinbase_signal(self, pid):
        """Export a fresh fixed-rule signal; paper P&L never sizes a real order."""
        with self.lock:
            if not self.runtime["running"] or self.stop_event.is_set():
                raise ValueError("Start the market/paper runner to collect current signals")
            market = self.market.get(pid)
            if not market:
                raise ValueError("This market is not loaded in the signal runner")
            interval = self.settings["decision_interval"]
            for iv, step in INTERVAL_MS.items():
                rows = market["bars"][iv][-241:]
                if len(rows) < 241 or any(b["ts"]-a["ts"]!=step for a,b in zip(rows,rows[1:])):
                    raise ValueError("Waiting for complete signal history")
                if now_ms()-(rows[-1]["ts"]+step)>step+15000:
                    raise ValueError("Signal history is stale")
            latest = market["bars"][interval][-1]
            age = now_ms()-(latest["ts"]+INTERVAL_MS[interval])
            if not 0 <= age <= 60000:
                raise ValueError("Waiting for a newly completed signal candle")
            cached = self._feature_cache.get((pid,interval))
            f = cached[1] if cached else None
            if not f or f.get("_ts") != latest["ts"]:
                raise ValueError("Waiting for current signal features")
            params = self._approved_params(pid)
            if not params:
                raise ValueError("No current passing historical profile for these settings")
            score, reason = evaluate_signal(f,params,None,None,0)
            if score is None or params.get("direction") != "LONG":
                raise ValueError(reason or "No eligible long signal")
            return {"product_id":pid,"interval":interval,"signal_ts":latest["ts"],
                    "atr":f["_atr"],"close":f["_close"],"params":copy.deepcopy(params),
                    "score":score}

    def status(self):
        with self.lock:
            p, settings, runtime = dict(self.portfolio), dict(self.settings), dict(self.runtime)
            positions, coins = [], []
            for pos in self.open_positions.values():
                item = copy.deepcopy(pos)
                item["unrealized_pnl"] = self._mark_pnl(pos)
                item["price_stale"] = self._fresh_quote(pos["product_id"]) is None
                positions.append(item)
            for pid in self.product_ids:
                m = self.market.get(pid, self._empty_market())
                tick = m["ticker"]
                f = self._feature_cache.get((pid, self.settings["decision_interval"]), (None, None))[1] or {}
                coins.append({"product_id": pid, "price": tick.get("price"), "structure": f.get("structure"),
                    "regime": f.get("regime"), "bias": f.get("daily_bias_score"),
                    "position": self.open_positions.get(pid, {}).get("direction"),
                    "quote_age_seconds": round((now_ms() - tick["ts"]) / 1000, 1) if tick.get("ts") else None,
                    "price_stale": self._fresh_quote(pid) is None, "readiness": m["readiness"],
                    "bar_counts": {iv: len(rows) for iv, rows in m["bars"].items()},
                    "last_decision": m["last_decision"], "rejections": dict(m["rejections"])})
            p["unrealized_pnl"] = sum(x["unrealized_pnl"] for x in positions)
            p["equity"] = p["balance"] + p["unrealized_pnl"]
            p["equity_stale"] = any(x["price_stale"] for x in positions)
            p["return_pct"] = (p["equity"] / p["starting_balance"] - 1) * 100
            p["drawdown_pct"] = max(0, (p["peak_balance"] - p["equity"]) / max(p["peak_balance"], 1e-9) * 100)
            total = p["wins"] + p["losses"]
            p["win_rate"] = p["wins"] / total * 100 if total else None
            p["open_risk_usd"] = self._current_total_risk()
            p["gross_exposure_usd"] = sum(x["entry"] * x["qty"] for x in positions)
            day = dict(self.risk_day)
            if day:
                day["pnl"] = p["equity"] - day["start_equity"]
            runtime["last_tick_age_seconds"] = ((now_ms() - runtime["last_tick_ts"]) / 1000
                                                if runtime["last_tick_ts"] else None)
            stored_profiles = load_state(self.db_path, "validated_profiles", {})
            active_profiles = {pid: profile for pid, profile in stored_profiles.items()
                               if self._approved_params(pid) is not None}
            return {"version": "10.0", "paper_only": True, "runtime": runtime, "portfolio": p,
                    "settings": settings, "positions": positions, "coins": coins, "risk_day": day,
                    "universe": list(self.product_ids), "memory_leaderboard": memory_leaderboard(self.db_path, 30),
                    "validated_profiles": stored_profiles, "active_profiles": active_profiles}

    def analytics(self):
        con = db_connect(self.db_path)
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM paper_trades WHERE status='CLOSED' ORDER BY closed_at,id")]
        con.close()
        wins = sum(r["pnl"] > 0 for r in rows)
        profit = sum(max(0, r["pnl"]) for r in rows)
        loss = -sum(min(0, r["pnl"]) for r in rows)
        families = {}
        curve = [{"ts": self.portfolio.get("started_at"), "balance": self.portfolio["starting_balance"]}]
        peak, worst = self.portfolio["starting_balance"], 0.0
        for r in rows:
            key = (r["family"], r["direction"])
            group = families.setdefault(key, {"family": key[0], "direction": key[1], "trades": 0,
                                               "wins": 0, "net_pnl": 0.0, "sum_r": 0.0})
            group["trades"] += 1
            group["wins"] += int(r["pnl"] > 0)
            group["net_pnl"] += r["pnl"]
            group["sum_r"] += r["result_r"]
            balance = r["balance_after"]
            peak = max(peak, balance)
            worst = max(worst, (peak - balance) / max(peak, 1e-9) * 100)
            curve.append({"ts": r["closed_at"], "balance": balance})
        for group in families.values():
            group["expectancy_r"] = group["sum_r"] / group["trades"]
        return {"closed_trades": len(rows), "win_rate": wins / len(rows) * 100 if rows else None,
                "net_pnl": profit - loss, "profit_factor": profit / loss if loss else None,
                "profit_factor_note": "No losing trades yet" if rows and not loss else None,
                "expectancy_r": sum(r["result_r"] for r in rows) / len(rows) if rows else None,
                "max_closed_drawdown_pct": worst, "equity_curve": curve,
                "strategies": sorted(families.values(), key=lambda x: x["net_pnl"], reverse=True),
                "evidence": "No closed trades yet" if not rows else "Paper results; not an out-of-sample validation"}
