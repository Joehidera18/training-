
import math
from datetime import datetime, timezone

def _safe(v, default=0.0):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def _swing_flags(rows, span=3):
    n=len(rows)
    highs=[False]*n; lows=[False]*n
    for i in range(span,n-span):
        hi=rows[i]["high"]; lo=rows[i]["low"]
        if hi>=max(rows[j]["high"] for j in range(i-span,i+span+1)): highs[i]=True
        if lo<=min(rows[j]["low"] for j in range(i-span,i+span+1)): lows[i]=True
    return highs,lows

def build_structure_features(rows, atr_values):
    """
    Turn discretionary market-structure concepts into deterministic features:
    swings, HH/HL/LH/LL state, BOS, CHOCH/MSS, liquidity pools/sweeps,
    previous-day/week liquidity, equilibrium premium/discount, displacement,
    and 3-candle fair-value-gap state.
    """
    n=len(rows)
    swing_hi,swing_lo=_swing_flags(rows,3)
    out=[{} for _ in range(n)]

    recent_swing_highs=[]
    recent_swing_lows=[]
    structure="NEUTRAL"

    bull_fvgs=[]
    bear_fvgs=[]
    last_sweep_low_i=None
    last_sweep_high_i=None
    last_bos_up_i=None
    last_bos_down_i=None
    last_choch_up_i=None
    last_choch_down_i=None

    current_day=None; day_hi=day_lo=None; prev_day_hi=prev_day_lo=None
    current_week=None; week_hi=week_lo=None; prev_week_hi=prev_week_lo=None

    for i,r in enumerate(rows):
        atr=max(_safe(atr_values[i]),1e-9)
        dt=datetime.fromtimestamp(r["ts"]/1000,timezone.utc)
        day_key=(dt.year,dt.month,dt.day)
        week_key=(dt.isocalendar().year,dt.isocalendar().week)

        if current_day is None:
            current_day=day_key;day_hi=r["high"];day_lo=r["low"]
        elif day_key!=current_day:
            prev_day_hi,prev_day_lo=day_hi,day_lo
            current_day=day_key;day_hi=r["high"];day_lo=r["low"]
        else:
            day_hi=max(day_hi,r["high"]);day_lo=min(day_lo,r["low"])

        if current_week is None:
            current_week=week_key;week_hi=r["high"];week_lo=r["low"]
        elif week_key!=current_week:
            prev_week_hi,prev_week_lo=week_hi,week_lo
            current_week=week_key;week_hi=r["high"];week_lo=r["low"]
        else:
            week_hi=max(week_hi,r["high"]);week_lo=min(week_lo,r["low"])

        # FVG creation using only current/past candles.
        if i>=2:
            if rows[i]["low"]>rows[i-2]["high"]:
                bull_fvgs.append({"lo":rows[i-2]["high"],"hi":rows[i]["low"],"created":i,"active":True,"inverted":False})
            if rows[i]["high"]<rows[i-2]["low"]:
                bear_fvgs.append({"lo":rows[i]["high"],"hi":rows[i-2]["low"],"created":i,"active":True,"inverted":False})

        for g in bull_fvgs[-25:]:
            if g["active"] and r["low"]<=g["hi"]:
                if r["close"]<g["lo"]:
                    g["active"]=False;g["inverted"]=True
                elif r["low"]<=g["lo"]:
                    g["active"]=False
        for g in bear_fvgs[-25:]:
            if g["active"] and r["high"]>=g["lo"]:
                if r["close"]>g["hi"]:
                    g["active"]=False;g["inverted"]=True
                elif r["high"]>=g["hi"]:
                    g["active"]=False

        prior_hi=recent_swing_highs[-1][1] if recent_swing_highs else None
        prior_lo=recent_swing_lows[-1][1] if recent_swing_lows else None

        bos_up=bool(prior_hi is not None and r["close"]>prior_hi+.05*atr)
        bos_down=bool(prior_lo is not None and r["close"]<prior_lo-.05*atr)
        prior_structure=structure
        choch_up=bool(prior_structure=="BEAR" and bos_up)
        choch_down=bool(prior_structure=="BULL" and bos_down)

        # A confirmed close through the last swing is itself structure information.
        # This prevents the state from remaining NEUTRAL when swing highs/lows do not
        # alternate perfectly.
        if bos_up and not bos_down:
            structure="BULL"
        elif bos_down and not bos_up:
            structure="BEAR"

        if swing_hi[i]:
            recent_swing_highs.append((i,r["high"]))
            recent_swing_highs=recent_swing_highs[-8:]
        if swing_lo[i]:
            recent_swing_lows.append((i,r["low"]))
            recent_swing_lows=recent_swing_lows[-8:]

        if len(recent_swing_highs)>=2 and len(recent_swing_lows)>=2:
            h1,h2=recent_swing_highs[-2][1],recent_swing_highs[-1][1]
            l1,l2=recent_swing_lows[-2][1],recent_swing_lows[-1][1]
            if h2>h1 and l2>l1: structure="BULL"
            elif h2<h1 and l2<l1: structure="BEAR"

        active_bull=next((g for g in reversed(bull_fvgs) if g["active"]),None)
        active_bear=next((g for g in reversed(bear_fvgs) if g["active"]),None)
        inv_bull=next((g for g in reversed(bull_fvgs) if g.get("inverted")),None)
        inv_bear=next((g for g in reversed(bear_fvgs) if g.get("inverted")),None)

        last_hi=recent_swing_highs[-1][1] if recent_swing_highs else r["high"]
        last_lo=recent_swing_lows[-1][1] if recent_swing_lows else r["low"]
        hi=max(last_hi,last_lo);lo=min(last_hi,last_lo)
        mid=(hi+lo)/2
        pos=(r["close"]-lo)/max(hi-lo,1e-9)

        recent=rows[max(0,i-30):i]
        eqh=sum(abs(x["high"]-last_hi)<=.12*atr for x in recent)
        eql=sum(abs(x["low"]-last_lo)<=.12*atr for x in recent)

        sweep_low=bool(prior_lo is not None and r["low"]<prior_lo and r["close"]>prior_lo)
        sweep_high=bool(prior_hi is not None and r["high"]>prior_hi and r["close"]<prior_hi)
        sweep_low_depth=((prior_lo-r["low"])/atr) if sweep_low else 0.0
        sweep_high_depth=((r["high"]-prior_hi)/atr) if sweep_high else 0.0

        displacement=abs(r["close"]-r["open"])/atr

        if sweep_low:last_sweep_low_i=i
        if sweep_high:last_sweep_high_i=i
        if bos_up:last_bos_up_i=i
        if bos_down:last_bos_down_i=i
        if choch_up:last_choch_up_i=i
        if choch_down:last_choch_down_i=i

        bars_since_sweep_low=(i-last_sweep_low_i) if last_sweep_low_i is not None else 9999
        bars_since_sweep_high=(i-last_sweep_high_i) if last_sweep_high_i is not None else 9999
        bars_since_bos_up=(i-last_bos_up_i) if last_bos_up_i is not None else 9999
        bars_since_bos_down=(i-last_bos_down_i) if last_bos_down_i is not None else 9999
        bars_since_choch_up=(i-last_choch_up_i) if last_choch_up_i is not None else 9999
        bars_since_choch_down=(i-last_choch_down_i) if last_choch_down_i is not None else 9999

        bull_fvg_mid=((active_bull["lo"]+active_bull["hi"])/2 if active_bull else None)
        bear_fvg_mid=((active_bear["lo"]+active_bear["hi"])/2 if active_bear else None)

        bull_shift_recent=min(bars_since_bos_up,bars_since_choch_up)<=20
        bear_shift_recent=min(bars_since_bos_down,bars_since_choch_down)<=20
        bull_sweep_recent=bars_since_sweep_low<=24
        bear_sweep_recent=bars_since_sweep_high<=24

        bull_retrace=bool(bull_fvg_mid is not None and r["low"]<=bull_fvg_mid<=r["high"] and r["close"]>=bull_fvg_mid)
        bear_retrace=bool(bear_fvg_mid is not None and r["low"]<=bear_fvg_mid<=r["high"] and r["close"]<=bear_fvg_mid)

        # Confirmation candle after a recent liquidity event. We intentionally do not
        # require every concept on the same bar; the sequence is allowed to develop.
        bull_confirm=(r["close"]>r["open"] and structure=="BULL")
        bear_confirm=(r["close"]<r["open"] and structure=="BEAR")
        bullish_reversal_sequence=bool(bull_sweep_recent and bull_confirm)
        bearish_reversal_sequence=bool(bear_sweep_recent and bear_confirm)

        # Continuation requires an established structure, a recent structural break,
        # then a small counter-move followed by a confirmation candle.
        prior=rows[i-1] if i>0 else r
        bull_pullback_confirm=bool(r["low"]<prior["low"] and r["close"]>r["open"])
        bear_pullback_confirm=bool(r["high"]>prior["high"] and r["close"]<r["open"])
        bullish_continuation_sequence=bool(
            structure=="BULL" and min(bars_since_bos_up,bars_since_choch_up)<=20 and
            (bull_retrace or bull_pullback_confirm)
        )
        bearish_continuation_sequence=bool(
            structure=="BEAR" and min(bars_since_bos_down,bars_since_choch_down)<=20 and
            (bear_retrace or bear_pullback_confirm)
        )

        sf={
          "swing_high":bool(swing_hi[i]),"swing_low":bool(swing_lo[i]),
          "structure":structure,"bos_up":bos_up,"bos_down":bos_down,
          "choch_up":choch_up,"choch_down":choch_down,
          "equilibrium_position":max(0,min(1,pos)),
          "premium":pos>.5,"discount":pos<.5,
          "equal_highs_count":eqh,"equal_lows_count":eql,
          "liquidity_sweep_low":sweep_low,"liquidity_sweep_high":sweep_high,
          "sweep_low_depth_atr":max(0,sweep_low_depth),
          "sweep_high_depth_atr":max(0,sweep_high_depth),
          "bull_fvg_active":active_bull is not None,"bear_fvg_active":active_bear is not None,
          "bull_fvg_mid":bull_fvg_mid,"bear_fvg_mid":bear_fvg_mid,
          "bull_ifvg_active":inv_bear is not None,"bear_ifvg_active":inv_bull is not None,
          "bars_since_sweep_low":bars_since_sweep_low,"bars_since_sweep_high":bars_since_sweep_high,
          "bars_since_bos_up":bars_since_bos_up,"bars_since_bos_down":bars_since_bos_down,
          "bars_since_choch_up":bars_since_choch_up,"bars_since_choch_down":bars_since_choch_down,
          "bull_retrace":bull_retrace,"bear_retrace":bear_retrace,
          "bullish_reversal_sequence":bullish_reversal_sequence,
          "bearish_reversal_sequence":bearish_reversal_sequence,
          "bullish_continuation_sequence":bullish_continuation_sequence,
          "bearish_continuation_sequence":bearish_continuation_sequence,
          "bull_fvg_distance_atr":((active_bull["lo"]-r["close"])/atr if active_bull else None),
          "bear_fvg_distance_atr":((r["close"]-active_bear["hi"])/atr if active_bear else None),
          "displacement_atr":displacement,
          "bullish_displacement":r["close"]>r["open"] and displacement>=.8,
          "bearish_displacement":r["close"]<r["open"] and displacement>=.8,
          "prev_day_high":prev_day_hi,"prev_day_low":prev_day_lo,
          "prev_week_high":prev_week_hi,"prev_week_low":prev_week_lo,
          "distance_prev_day_high_atr":((prev_day_hi-r["close"])/atr if prev_day_hi else None),
          "distance_prev_day_low_atr":((r["close"]-prev_day_lo)/atr if prev_day_lo else None),
          "distance_prev_week_high_atr":((prev_week_hi-r["close"])/atr if prev_week_hi else None),
          "distance_prev_week_low_atr":((r["close"]-prev_week_lo)/atr if prev_week_lo else None),
        }
        sf["daily_bias_score"]=daily_bias_score(sf)
        out[i]=sf
    return out

def daily_bias_score(f):
    score=0.0
    if f.get("structure")=="BULL":score+=.35
    elif f.get("structure")=="BEAR":score-=.35
    if f.get("bos_up"):score+=.18
    if f.get("bos_down"):score-=.18
    if f.get("choch_up"):score+=.18
    if f.get("choch_down"):score-=.18
    if f.get("discount"):score+=.07
    if f.get("premium"):score-=.07
    if f.get("liquidity_sweep_low"):score+=.12
    if f.get("liquidity_sweep_high"):score-=.12
    return max(-1,min(1,score))
