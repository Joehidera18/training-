"""Bounded strategy selection with chronological validation and an untouched holdout.

Only real downloaded or supplied OHLCV is accepted by the research service.
Repeatedly viewing an identical run reuses it; changed settings consume a new test.
"""
from __future__ import annotations
import hashlib
import json
import math
import random
import statistics
import threading
import time
from pathlib import Path

from .coinbase_feed import CoinbaseClient
from .data import INTERVAL_MS, load_history, validate_history
from .engine import build_feature_cache, ENGINE_VERSION
from .strategies import profit_candidates
from .execution import simulate
from .paper_store import load_state, save_state

COST_KEYS = ("decision_interval", "fee_rate", "slippage_rate", "risk_per_trade", "max_notional_fraction")


def cost_signature(settings):
    return {key: settings[key] for key in COST_KEYS}


def bootstrap_interval(values, seed=7, runs=500):
    """Moving-block bootstrap interval of historical average net R; not a forecast."""
    if len(values) < 20:
        return {"lower_r": None, "upper_r": None, "samples": len(values)}
    rng = random.Random(seed)
    block = max(2, min(8, round(math.sqrt(len(values)))))
    means = []
    for _ in range(runs):
        seq = []
        while len(seq) < len(values):
            start = rng.randrange(len(values))
            seq.extend(values[(start + j) % len(values)] for j in range(block))
        means.append(statistics.mean(seq[:len(values)]))
    means.sort()
    return {"lower_r": means[int(.025 * (runs - 1))], "upper_r": means[int(.975 * (runs - 1))],
            "samples": len(values), "method": "95% moving-block bootstrap of historical net R"}


def validate_rows(rows, interval):
    if interval not in ("5m", "15m", "1h"):
        raise ValueError("Use 5m, 15m, or 1h candles")
    if len(rows) < 3000:
        raise ValueError("Research needs at least 3,000 completed candles")
    step = INTERVAL_MS[interval]
    previous = None
    for row in rows:
        if not all(math.isfinite(float(row[k])) for k in ("open", "high", "low", "close", "volume")):
            raise ValueError("The history contains non-finite prices or volumes")
        if min(row["open"], row["high"], row["low"], row["close"]) <= 0 or row["volume"] < 0:
            raise ValueError("The history contains invalid prices or volumes")
        if "quote_volume" in row and (not math.isfinite(float(row["quote_volume"])) or row["quote_volume"] < 0):
            raise ValueError("The history contains invalid quote volume")
        if int(row["ts"]) != row["ts"] or row["ts"] % step:
            raise ValueError("Candle timestamps must align to their interval in milliseconds")
        if previous is not None and row["ts"] - previous != step:
            raise ValueError("The history must be chronological with no missing or duplicate candles")
        if row["ts"] + step > int(time.time() * 1000):
            raise ValueError("The history includes an unfinished or future candle")
        previous = row["ts"]
    quality = validate_history(rows, interval)
    if not quality["valid"]:
        raise ValueError("Invalid OHLC price ranges")
    return quality


def daily_goal_report(trades, start_ts, end_ts):
    # Include no-trade days; do not report profit per active day as profit per day.
    first_day, last_day = start_ts // 86400000, end_ts // 86400000
    daily = {day: 0.0 for day in range(first_day, last_day + 1)}
    for trade in trades:
        day = trade["exit_ts"] // 86400000
        if day in daily:
            daily[day] += trade["pnl"]
    values = list(daily.values())
    return {"account_start": 500, "calendar_days": len(values),
            "mean_net_per_day": statistics.mean(values) if values else None,
            "median_net_per_day": statistics.median(values) if values else None,
            "days_at_least_10": sum(x >= 10 for x in values),
            "days_at_least_15": sum(x >= 15 for x in values),
            "losing_days": sum(x < 0 for x in values),
            "no_realized_pnl_days": sum(x == 0 for x in values),
            "worst_day": min(values) if values else None,
            "basis": "Realized holdout P&L across all calendar days, before taxes; not a forecast."}


def research(rows, symbol, settings, progress=None, cancelled=None):
    progress = progress or (lambda *args: None)
    cancelled = cancelled or (lambda: False)
    interval = settings["decision_interval"]
    quality = validate_rows(rows, interval)
    features = build_feature_cache(rows, interval)["features"]
    candidates = [dict(p, max_notional_fraction=settings["max_notional_fraction"])
                  for p in profit_candidates()]
    # Persisting hundreds of near-duplicates does not make evidence stronger.
    candidate_count = len(candidates)
    fee, slip = settings["fee_rate"], settings["slippage_rate"] + .0005
    risk = settings["risk_per_trade"]
    purge = math.ceil(24 * 3600000 / INTERVAL_MS[interval])
    holdout_start = int(len(rows) * .80)
    development = holdout_start - purge
    fold_starts = [int(development * f) for f in (.45, .63, .81)]
    fold_ends = fold_starts[1:] + [development]
    all_oos, folds, evaluations = [], [], 0
    cache = {}
    selection_records = []

    def run(p, start, end, stress=1):
        if cancelled():
            raise InterruptedError("Research cancelled")
        key = (json.dumps(p, sort_keys=True), start, end, stress)
        if key not in cache:
            cache[key] = simulate(rows, features, start, end, 500, risk, fee * stress, slip * stress, p)
        return cache[key]

    def rank(metrics, trades):
        rs = [t["r_multiple"] for t in trades]
        if len(rs) < 12 or metrics["net_pnl"] <= 0 or metrics["max_drawdown_pct"] > 15:
            return -math.inf
        # Prefer conservative net expectancy with a sample-size penalty.
        std = statistics.stdev(rs) if len(rs) > 1 else 0
        return statistics.mean(rs) - 1.64 * std / math.sqrt(len(rs))

    def select(end, record=False):
        nonlocal evaluations
        validation_start = int(end * .70)
        training_end = validation_start - purge
        ranked = []
        records = []
        for index, p in enumerate(candidates):
            metrics, trades = run(p, 240, training_end)
            value = rank(metrics, trades)
            if math.isfinite(value):
                ranked.append((value, index, p))
            records.append({"candidate_id": index + 1, "params": dict(p),
                "training": metrics, "training_score": value if math.isfinite(value) else None,
                "validation": None, "validation_stressed": None, "validation_score": None,
                "selection_status": "not_shortlisted" if math.isfinite(value) else "failed_training"})
            evaluations += 1
            progress("training", evaluations, candidate_count * 4, f"Training {symbol}: candidate {index + 1}/{candidate_count}")
        ranked.sort(key=lambda x: x[0], reverse=True)
        finalists = []
        for _, index, p in ranked[:5]:
            metrics, trades = run(p, validation_start, end)
            score = rank(metrics, trades)
            stressed, _ = run(p, validation_start, end, 1.5)
            records[index].update(validation=metrics, validation_stressed=stressed,
                validation_score=score if math.isfinite(score) else None,
                selection_status="rejected_validation")
            if score > 0 and stressed["net_pnl"] > 0:
                records[index]["selection_status"] = "survived_validation"
                finalists.append((score, index, p))
        winner = max(finalists, key=lambda x: x[0]) if finalists else None
        if winner:
            records[winner[1]]["selection_status"] = "selected"
        if record:
            selection_records.extend(records)
        return winner[2] if winner else None

    for idx, (start, end) in enumerate(zip(fold_starts, fold_ends), 1):
        params = select(start - purge)
        if params is None:
            folds.append({"fold": idx, "status": "no_validated_candidate", "test_start_ts": rows[start]["ts"],
                          "test_end_ts": rows[end - 1]["ts"], "metrics": None})
            continue
        metrics, trades = run(params, start, end)
        all_oos.extend(trades)
        folds.append({"fold": idx, "status": "tested", "params": params, "metrics": metrics,
                      "test_start_ts": rows[start]["ts"], "test_end_ts": rows[end - 1]["ts"]})
    # Freeze the final parameters before opening the final 20% of history.
    chosen = select(development, record=True)
    holdout = stressed_holdout = comparison = None
    holdout_trades = []
    if chosen:
        holdout, holdout_trades = run(chosen, holdout_start, len(rows))
        stressed_holdout, _ = run(chosen, holdout_start, len(rows), 1.5)
        # Diagnostic ablation of a frozen rule, never another candidate search.
        baseline_params = dict(chosen,min_net_rr=0,loss_streak_limit=0)
        baseline, _ = run(baseline_params,holdout_start,len(rows))
        baseline_stressed, _ = run(baseline_params,holdout_start,len(rows),1.5)
        comparison = {"label":"Same selected rule without V9 payoff filter or loss cooldown",
            "baseline":baseline,"baseline_stressed":baseline_stressed,
            "net_pnl_difference":holdout["net_pnl"]-baseline["net_pnl"],
            "stress_net_pnl_difference":stressed_holdout["net_pnl"]-baseline_stressed["net_pnl"],
            "trade_count_difference":holdout["trades"]-baseline["trades"],
            "drawdown_pp_difference":holdout["max_drawdown_pct"]-baseline["max_drawdown_pct"],
            "selection_uses_comparison":False,
            "scope":"Diagnostic single-market replay only. Excludes live order-book filtering. Not a separately optimized V8 strategy or a forecast."}
    tested = [f for f in folds if f["status"] == "tested"]
    positive = sum(f["metrics"]["net_pnl"] > 0 for f in tested)
    rs = [t["r_multiple"] for t in holdout_trades]
    interval_estimate = bootstrap_interval(rs)
    reasons = []
    if chosen is None:
        reasons.append("No candidate survived training, validation, and higher trading costs.")
    if len(tested) < 2 or positive < 2:
        reasons.append("Fewer than two walk-forward test windows were profitable.")
    if len(holdout_trades) < 30:
        reasons.append("Fewer than 30 trades in the final holdout.")
    if not holdout or holdout["net_pnl"] <= 0:
        reasons.append("The final holdout did not earn a net profit.")
    if not stressed_holdout or stressed_holdout["net_pnl"] <= 0:
        reasons.append("The final holdout did not stay profitable with 50% higher fees and slippage.")
    if interval_estimate["lower_r"] is None or interval_estimate["lower_r"] <= 0:
        reasons.append("The lower bootstrap bound on holdout net expectancy is not above zero.")
    if holdout and holdout["max_drawdown_pct"] > 15:
        reasons.append("Holdout drawdown exceeded 15%.")
    validated = not reasons
    # Benchmarks use the same untouched period and configured execution costs.
    first, last = rows[holdout_start]["open"], rows[-1]["close"]
    quantity = 500 / (first * (1 + slip) * (1 + fee))
    buy_hold = quantity * last * (1 - slip) * (1 - fee)
    return {"engine_version": ENGINE_VERSION, "symbol": symbol, "interval": interval,
            "created_at": int(time.time()), "data_quality": quality,
            "costs": {"fee_per_side": fee, "slippage_per_fill": settings["slippage_rate"],
                      "assumed_half_spread": .0005, "stress_multiplier": 1.5},
            "candidate_count": candidate_count, "evaluations": evaluations,
            "candidate_selection": {"candidates": selection_records,
                "training_start_ts": rows[240]["ts"],
                "training_end_ts": rows[int(development * .70) - purge - 1]["ts"],
                "validation_start_ts": rows[int(development * .70)]["ts"],
                "validation_end_ts": rows[development - 1]["ts"],
                "scope": "Final training and validation only. At most five training finalists reach validation. Holdout results never choose the winner."},
            "folds": folds, "walk_forward_trades": len(all_oos), "profitable_folds": positive,
            "selected_params": chosen, "holdout_start_ts": rows[holdout_start]["ts"],
            "holdout": holdout, "holdout_stressed": stressed_holdout,
            "upgrade_comparison":comparison,
            "holdout_expectancy_interval": interval_estimate,
            "cash_benchmark_return_pct": 0, "buy_hold_return_pct": (buy_hold / 500 - 1) * 100,
            "validated": validated, "rejection_reasons": reasons,
            "cost_signature": cost_signature(settings),
            "scope": "Single-market, long-only, taker-fill paper research. Portfolio overlap and live execution remain unvalidated.",
            "daily_goal": daily_goal_report(holdout_trades, rows[holdout_start]["ts"], rows[-1]["ts"]),
            "warning": "Repeated tuning against this holdout weakens its independence. Reserve new data for subsequent changes.",
            "holdout_trades": [{k: t[k] for k in ("entry_ts", "exit_ts", "direction", "strategy_family", "pnl", "r_multiple", "reason")}
                               for t in holdout_trades]}


class ResearchManager:
    def __init__(self, db_path, data_dir, client=None):
        self.db_path, self.data_dir = Path(db_path), Path(data_dir)
        self.client = client or CoinbaseClient()
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.worker = None
        self.state = load_state(db_path, "profitability_research", {"status": "idle", "message": "No historical validation yet", "results": []})
        if self.state.get("status") in ("running", "downloading", "training"):
            self.state.update(status="interrupted", message="Research was interrupted. Run again to reuse downloaded candles.")

    def status(self):
        with self.lock:
            result = json.loads(json.dumps(self.state, allow_nan=False))
            result["active_engine_version"] = ENGINE_VERSION
            return result

    def _update(self, **patch):
        with self.lock:
            self.state.update(patch)
            save_state(self.db_path, "profitability_research", self.state)

    def start(self, symbols, days, settings):
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 5:
            raise ValueError("Choose one to five Coinbase USD markets")
        import re
        symbols = list(dict.fromkeys(str(s).upper().strip() for s in symbols))
        if any(not re.fullmatch(r"[A-Z0-9]{2,16}-USD", s) for s in symbols):
            raise ValueError("Use Coinbase market names such as BTC-USD")
        if isinstance(days, bool) or not isinstance(days, int) or not 30 <= days <= 365:
            raise ValueError("History length must be 30–365 whole days")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("Research is already running")
            self.cancel_event.clear()
            self._update(status="running", results=[], message="Preparing historical research", completed=0, total=len(symbols))
            self.worker = threading.Thread(target=self._run, args=(symbols, days, dict(settings)), daemon=True, name="strategy-research")
            self.worker.start()

    def cancel(self):
        self.cancel_event.set()

    def _history(self, symbol, interval, days):
        import csv
        step = INTERVAL_MS[interval]
        end = int(time.time() * 1000) // step * step
        start = end - days * 86400000
        folder = self.data_dir / "history"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{symbol}_{interval}.csv"
        existing = load_history(path) if path.exists() else []
        rows = {r["ts"]: r for r in existing if start <= r["ts"] < end}
        def checkpoint():
            ordered = [rows[k] for k in sorted(rows)]
            temp = path.with_suffix(".tmp")
            with temp.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["ts", "open", "high", "low", "close", "volume", "quote_volume", "trades"])
                writer.writeheader()
                writer.writerows(ordered)
            temp.replace(path)
        # Cached continuous history allows later runs to request only the missing ends.
        cursor = end
        while cursor > start:
            if self.cancel_event.is_set():
                raise InterruptedError("Research cancelled")
            window_start = max(start, cursor - 1500 * step)
            required = range(window_start, cursor, step)
            if not all(ts in rows for ts in required):
                chunk = self.client.candles(symbol, interval, min(1500, (cursor - window_start) // step), cursor)
                for r in chunk:
                    if start <= r["ts"] < end:
                        rows[r["ts"]] = r
                checkpoint()
                self._update(status="downloading", message=f"{symbol}: {len(rows):,} historical candles collected")
            cursor = window_start
        ordered = [rows[k] for k in sorted(rows)]
        checkpoint()
        return ordered

    def _run(self, symbols, days, settings):
        try:
            results = []
            for symbol in symbols:
                rows = self._history(symbol, settings["decision_interval"], days)
                digest = hashlib.sha256(json.dumps({"symbol": symbol, "settings": cost_signature(settings),
                    "engine": ENGINE_VERSION, "history": rows}, sort_keys=True).encode()).hexdigest()
                cached = load_state(self.db_path, "research_result_" + digest, None)
                if cached:
                    result = cached
                else:
                    def progress(stage, done, total, message):
                        self._update(status=stage, evaluations_done=done, evaluations_total=total, message=message)
                    result = research(rows, symbol, settings, progress, self.cancel_event.is_set)
                    result["data_fingerprint"] = digest
                    save_state(self.db_path, "research_result_" + digest, result)
                results.append(result)
                profiles = load_state(self.db_path, "validated_profiles", {})
                if result["validated"]:
                    profiles[symbol] = {"params": result["selected_params"], "interval": result["interval"],
                        "engine_version": result["engine_version"], "cost_signature": result["cost_signature"],
                        "created_at": result["created_at"], "data_end_ts": result["data_quality"]["end_ts"],
                        "data_fingerprint": digest, "holdout_expectancy_r": result["holdout"]["expectancy_r"]}
                else:
                    profiles.pop(symbol, None)
                save_state(self.db_path, "validated_profiles", profiles)
                self._update(results=results, completed=len(results))
            self._update(status="complete", message="Historical validation complete")
        except InterruptedError:
            self._update(status="cancelled", message="Research cancelled")
        except Exception as exc:
            self._update(status="error", message=str(exc))
