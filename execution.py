"""Causal OHLC paper execution shared by research callers.

A signal observes a closed candle and can fill only in the following candle.
When intrabar order is unknown, assume the stop was reached first.
"""
import math
import statistics
from .trade_quality import net_payoff, cooldown_minutes


def simulate(rows, features, start, end, balance, risk, fee_rate, base_slip,
             params, edge_model=None, keep_trades=True, policy=None, cancelled=None,
             training_examples=False):
    from .engine import evaluate_signal
    cash, position, next_entry_ts = float(balance), None, 0
    trades, curve = [], [cash]
    direction = params.get("direction", "LONG")
    sign = 1 if direction == "LONG" else -1
    end = min(int(end), len(rows))
    funnel = {"candles_checked": 0, "features_available": 0, "qualified_setups": 0,
              "entry_attempts": 0, "entries_opened": 0, "rejections": {}}

    def reject(reason):
        funnel["rejections"][reason] = funnel["rejections"].get(reason, 0) + 1

    def close(raw, candle, reason):
        nonlocal cash, position, next_entry_ts
        p = position
        exit_price = raw * (1 - sign * base_slip)
        gross = (exit_price - p["entry"]) * p["qty"] * sign
        exit_fee = exit_price * p["qty"] * fee_rate
        cash += gross - exit_fee
        pnl = p["realized_partial"] + gross - exit_fee
        p["gross_pnl"] += gross
        p["fees_paid"] += exit_fee
        p["slippage_notional"] += abs(raw - exit_price) * p["qty"]
        p.update(exit_ts=candle["ts"], exit=exit_price, pnl=pnl, reason=reason,
                 outcome="WIN" if pnl > 0 else "LOSS", balance_after=cash)
        trades.append(p)
        # OHLC does not reveal a stop's exact touch time; wait from this bar's end.
        bar_ms = rows[1]["ts"] - rows[0]["ts"] if len(rows) > 1 else 0
        next_entry_ts = candle["ts"] + bar_ms + cooldown_minutes([t["pnl"] for t in trades],p["decision_params"]) * 60000
        if policy and reason != "END":
            policy.observe(p["decision_params"], p["learning"]["vector"],
                           pnl/max(p["risk_dollars"], 1e-12), candle["ts"]+bar_ms)
        if training_examples:
            # Label collection: independently funded examples, not account returns.
            cash = float(balance)
        position = None

    for i in range(max(240, int(start)), end - 1):
        if cancelled and i % 500 == 0 and cancelled():
            raise InterruptedError("Learning cancelled")
        signal, candle = rows[i], rows[i + 1]
        f = features[i]
        funnel["candles_checked"] += 1
        funnel["features_available"] += int(bool(f))
        # A position already alive at this candle's open forbids re-entry in it.
        held_at_open = position is not None
        if not held_at_open and f and cash > 1 and candle["ts"] >= next_entry_ts:
            detail = edge_model.predict_detail({**f, "direction_num": sign}) if edge_model else {}
            choice = policy.choose(f) if policy else None
            trade_params = choice["params"] if choice else params
            if policy:
                score, reason = (choice["score"], None) if choice else (None, "no_positive_learned_setup")
            else:
                score, reason = evaluate_signal(f, trade_params, detail.get("probability"),
                                               detail.get("lower_bound"), detail.get("evidence_samples", 0))
            if score is None:
                reject(reason or "no_setup")
            else:
                funnel["qualified_setups"] += 1
                funnel["entry_attempts"] += 1
                atr = max(float(f["_atr"]), signal["close"] * .002)
                mode = trade_params.get("entry_mode", "MARKET_NEXT_OPEN")
                raw = candle["open"]
                if mode == "RETRACE_LIMIT":
                    # Match the continuous learner: require retracement confirmation
                    # at signal close, then simulate a taker fill at the next open.
                    retrace = f.get("bull_retrace" if sign == 1 else "bear_retrace")
                    if not retrace:
                        reject("retrace_not_confirmed")
                        raw = None
                if raw is not None and abs(raw - signal["close"]) / max(atr, 1e-12) > trade_params.get("max_gap_atr", .6):
                    reject("entry_gap_too_large")
                    raw = None
                if raw is not None:
                    entry = raw * (1 + sign * base_slip)
                    distance = max(atr * trade_params["stop_atr"], raw * .0015)
                    stop = entry - sign * distance
                    target = entry + sign * distance * trade_params["rr2"]
                    stop_fill = stop * (1 - sign * base_slip)
                    cost = (entry + stop_fill) * fee_rate + abs(stop - stop_fill) + abs(entry - raw)
                    cost_r = cost / max(distance, 1e-12)
                    unit_risk = abs(entry - stop_fill) + (entry + stop_fill) * fee_rate
                    quality = net_payoff(entry,stop,target,fee_rate,base_slip,direction)
                    if stop <= 0 or target <= 0 or not math.isfinite(unit_risk) or unit_risk <= 0:
                        reject("invalid_stop_distance")
                    elif cost_r > trade_params.get("max_cost_r", .8):
                        reject("trading_cost_too_high")
                    elif quality["net_rr"] < trade_params.get("min_net_rr",0):
                        reject("net_reward_too_small")
                    else:
                        fraction = min(1.0, trade_params.get("max_notional_fraction", .30))
                        qty = min(cash * risk / unit_risk, cash * fraction / (entry * (1 + fee_rate)))
                        if qty * entry < 1:
                            reject("position_size_zero")
                        else:
                            entry_fee = entry * qty * fee_rate
                            cash -= entry_fee
                            position = {"entry_ts": candle["ts"], "entry": entry, "signal_entry": signal["close"],
                                "entry_gap_pct": (entry / signal["close"] - 1) * 100,
                                "entry_slippage_pct": base_slip * 100, "direction": direction,
                                "strategy_family": trade_params["family"], "entry_mode": mode,
                                "decision_params":dict(trade_params),
                                "learning":choice.get("learning") if choice else None,
                                "stop": stop, "target1": target, "target2": target,
                                "qty": qty, "qty_initial": qty, "risk_dollars": qty * unit_risk,
                                "planned_cost_r": cost_r, "planned_net_rr":quality["net_rr"], "risk_multiplier": 1.0,
                                "effective_risk_fraction": risk, "t1_hit": False,
                                "realized_partial": -entry_fee, "gross_pnl": 0.0, "fees_paid": entry_fee,
                                "slippage_notional": abs(entry - raw) * qty, "regime": f.get("regime"),
                                "features": {k: v for k, v in f.items() if not k.startswith("_")},
                                "edge_probability": detail.get("probability"), "score": score,
                                "mfe_price": entry, "mae_price": entry}
                            funnel["entries_opened"] += 1
        if position:
            p = position
            # Handle gaps at the open before any intrabar touch.
            if (candle["open"] <= p["stop"] if sign == 1 else candle["open"] >= p["stop"]):
                p["mae_price"] = candle["open"]
                close(candle["open"], candle, "STOP_GAP")
            else:
                stop_hit = candle["low"] <= p["stop"] if sign == 1 else candle["high"] >= p["stop"]
                target_hit = candle["high"] >= p["target2"] if sign == 1 else candle["low"] <= p["target2"]
                if stop_hit:
                    # Never credit a target touched in a candle that also hits the stop.
                    p["mae_price"] = min(p["mae_price"], p["stop"]) if sign == 1 else max(p["mae_price"], p["stop"])
                    close(p["stop"], candle, "STOP")
                elif target_hit:
                    p["mfe_price"] = max(p["mfe_price"], p["target2"]) if sign == 1 else min(p["mfe_price"], p["target2"])
                    close(p["target2"], candle, "TARGET2")
                else:
                    p["mfe_price"] = max(p["mfe_price"], candle["high"]) if sign == 1 else min(p["mfe_price"], candle["low"])
                    p["mae_price"] = min(p["mae_price"], candle["low"]) if sign == 1 else max(p["mae_price"], candle["high"])
                    if candle["ts"] - p["entry_ts"] >= p["decision_params"].get("time_stop_hours", 24) * 3600000:
                        close(candle["close"], candle, "TIME")
        mark = cash
        if position:
            liquidation = candle["close"] * (1 - sign * base_slip)
            mark += (liquidation - position["entry"]) * position["qty"] * sign
            mark -= liquidation * position["qty"] * fee_rate
        curve.append(mark)
    if position:
        close(rows[end - 1]["close"], rows[end - 1], "END")
        curve.append(cash)
    for trade in trades:
        rd = max(trade["risk_dollars"], 1e-12)
        trade["r_multiple"] = trade["pnl"] / rd
        trade["gross_r"] = trade["gross_pnl"] / rd
        trade["fee_r"] = trade["fees_paid"] / rd
        trade["slippage_r"] = trade["slippage_notional"] / rd
        trade["mfe_r"] = (trade["mfe_price"] - trade["entry"]) * trade["qty_initial"] * sign / rd
        trade["mae_r"] = (trade["mae_price"] - trade["entry"]) * trade["qty_initial"] * sign / rd
        trade["audit_flags"] = ["gap_loss_exceeds_2R"] if trade["r_multiple"] < -2 else []
    returns = [t["r_multiple"] for t in trades]
    profit = sum(max(0, t["pnl"]) for t in trades)
    loss = -sum(min(0, t["pnl"]) for t in trades)
    peak, dd = balance, 0
    for equity in curve:
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / max(peak, 1e-12))
    metrics = {"ending_balance": cash, "return_pct": (cash / balance - 1) * 100,
        "trades": len(trades), "net_pnl": cash - balance,
        "win_rate": sum(r > 0 for r in returns) / len(returns) * 100 if returns else None,
        "profit_factor": profit / loss if loss else None,
        "expectancy_r": statistics.mean(returns) if returns else None,
        "max_drawdown_pct": dd * 100, "signal_funnel": funnel,
        "family": params["family"], "direction": direction}
    return metrics, trades if keep_trades else []
