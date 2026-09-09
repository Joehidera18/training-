"""Learn from earlier resolved examples; test an updating policy on later prices."""
from __future__ import annotations
import math
import hashlib
import json
import time

from .adaptive import AdaptivePolicy, POLICY_VERSION, feature_vector
from .engine import ENGINE_VERSION, build_feature_cache
from .execution import simulate
from .research import validate_rows, cost_signature, bootstrap_interval, daily_goal_report
from .data import INTERVAL_MS


def learn_history(rows, symbol, settings, progress=None, cancelled=None):
    progress = progress or (lambda **kwargs:None)
    cancelled = cancelled or (lambda:False)
    interval = settings["decision_interval"]
    quality = validate_rows(rows, interval)
    step = INTERVAL_MS[interval]
    purge = math.ceil(24*3600000/step)
    holdout_start = int(len(rows)*.80)
    development = holdout_start-purge
    features = build_feature_cache(rows, interval, simple_only=True)["features"]
    fee, slip = settings["fee_rate"], settings["slippage_rate"]+.0005
    candidates = AdaptivePolicy(max_notional_fraction=settings["max_notional_fraction"]).candidates
    examples = []
    # These independently funded hypothetical examples are training labels, not
    # a multi-strategy portfolio. The actual policy tests use one $500 account.
    for index, params in enumerate(candidates):
        if cancelled():
            raise InterruptedError("Learning cancelled")
        progress(phase="learning", message=f"{symbol}: studying setup {index+1}/{len(candidates)}")
        _, trades = simulate(rows, features, 240, development, 500,
            settings["risk_per_trade"], fee, slip, params,
            cancelled=cancelled, training_examples=True)
        for trade in trades:
            if trade["reason"] != "END":
                examples.append((trade["exit_ts"]+step, index,
                                 feature_vector(trade["features"]), trade["r_multiple"]))
        del trades
    examples.sort(key=lambda x:(x[0],x[1]))
    seed = AdaptivePolicy(max_notional_fraction=settings["max_notional_fraction"])
    cursor = 0

    def train_until(cut_ts):
        nonlocal cursor
        while cursor < len(examples) and examples[cursor][0] <= cut_ts:
            if cursor % 500 == 0 and cancelled():
                raise InterruptedError("Learning cancelled")
            stamp, index, vector, reward = examples[cursor]
            seed.observe(candidates[index], vector, reward, stamp)
            cursor += 1
        return seed.export()

    def test(initial, start, end, stress=1, learn=True):
        policy = AdaptivePolicy(initial, settings["max_notional_fraction"], learn=learn)
        metrics, trades = simulate(rows, features, start, end, 500,
            settings["risk_per_trade"], fee*stress, slip*stress,
            {"family":"adaptive_policy", "direction":"LONG"},
            policy=policy, cancelled=cancelled)
        return metrics, trades, policy.export()

    starts = [int(development*f) for f in (.45,.63,.81)]
    ends = starts[1:]+[development]
    folds = []
    for index,(start,end) in enumerate(zip(starts,ends),1):
        initial = train_until(rows[start-purge]["ts"])
        progress(phase="testing", message=f"{symbol}: checking later period {index}/3")
        metrics, _, _ = test(initial,start,end)
        folds.append({"fold":index, "training_labels":initial["observations"],
            "training_label_end_ts":initial["last_label_ts"], "test_start_ts":rows[start]["ts"],
            "test_end_ts":rows[end-1]["ts"]+step, "metrics":metrics})

    initial = train_until(rows[development]["ts"])
    progress(phase="testing", message=f"{symbol}: checking the final unseen period and higher costs")
    holdout, trades, trained = test(initial,holdout_start,len(rows))
    stressed, _, _ = test(initial,holdout_start,len(rows),stress=1.5)
    frozen, _, _ = test(initial,holdout_start,len(rows),learn=False)
    positive = sum(f["metrics"]["net_pnl"]>0 for f in folds)
    uncertainty = bootstrap_interval([t["r_multiple"] for t in trades])
    reasons = []
    if positive < 2:
        reasons.append("Fewer than two later test periods made money after costs.")
    if len(trades) < 30:
        reasons.append("Fewer than 30 trades in the final test period.")
    if holdout["net_pnl"] <= 0 or stressed["net_pnl"] <= 0:
        reasons.append("The final test did not stay profitable at both ordinary and higher costs.")
    if uncertainty["lower_r"] is None or uncertainty["lower_r"] <= 0:
        reasons.append("The uncertainty in final trade results is too large to qualify.")
    if holdout["max_drawdown_pct"] > 15:
        reasons.append("The final test lost more than 15% from a prior equity peak.")
    return {"engine_version":ENGINE_VERSION, "policy_version":POLICY_VERSION,
        "symbol":symbol, "interval":interval, "created_at":int(time.time()),
        "data_quality":quality, "data_hours":len(rows)*step/3600000,
        "historical_examples":len(examples), "candidate_count":len(candidates),
        "training_label_end_ts":initial["last_label_ts"],
        "pre_holdout_model_sha256":hashlib.sha256(json.dumps(initial,sort_keys=True).encode()).hexdigest(),
        "holdout_start_ts":rows[holdout_start]["ts"],
        "folds":folds, "profitable_folds":positive, "holdout":holdout,
        "holdout_stressed":stressed, "frozen_holdout":frozen,
        "learning_pnl_difference":holdout["net_pnl"]-frozen["net_pnl"],
        "holdout_learning_updates":trained["observations"]-initial["observations"],
        "holdout_expectancy_interval":uncertainty, "validated":not reasons,
        "rejection_reasons":reasons, "cost_signature":cost_signature(settings),
        "costs":{"fee_per_side":fee, "slippage_per_fill":settings["slippage_rate"],
                 "assumed_half_spread":.0005, "stress_multiplier":1.5},
        "daily_goal":daily_goal_report(trades,rows[holdout_start]["ts"],rows[-1]["ts"]),
        "model":trained,
        "scope":"One-market $500 policy tests. Training examples overlap across variants and are not independent market hours or account profits. Forward updates learn only completed trades. Model estimates are not calibrated probabilities.",
        "warning":"Repeated runs can reuse test periods. The frozen comparison cannot change qualification. Portfolio execution, latency, live fills, and future profit remain unvalidated.",
        "holdout_trades":[{k:t[k] for k in ("entry_ts","exit_ts","strategy_family","pnl","r_multiple","reason")} for t in trades]}
