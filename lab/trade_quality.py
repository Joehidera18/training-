"""Small shared execution rules; thresholds are test hypotheses, not proven alpha."""


def net_payoff(entry, stop, target, fee, slip, direction="LONG"):
    """Return modeled stop risk and target reward after fees and adverse exit slippage.

    Supports floats for simulation and Decimal for exchange order construction.
    Entry must already include its modeled slippage.
    """
    sign = 1 if direction == "LONG" else -1
    stop_fill = stop * (1-sign*slip)
    target_fill = target * (1-sign*slip)
    risk = sign*(entry-stop_fill)+(entry+stop_fill)*fee
    reward = sign*(target_fill-entry)-(entry+target_fill)*fee
    if risk <= 0:
        raise ValueError("Net stop risk must be positive")
    return {"unit_risk":risk, "unit_reward":reward, "net_rr":reward/risk,
            "break_even_win_rate":risk/(risk+reward) if reward>0 else None}


def cooldown_minutes(pnls, params):
    """Longer per-market cooldown after consecutive resolved net losses."""
    base = params.get("cooldown_minutes",15)
    threshold = params.get("loss_streak_limit",0)
    streak = 0
    for pnl in reversed(pnls):
        if pnl >= 0:
            break
        streak += 1
    if threshold > 0 and streak >= threshold:
        return max(base,params.get("loss_cooldown_hours",6)*60)
    return base
