
import json, math, hashlib, statistics, time
from .db import connect

def _safe(v, default=0.0):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def _feature_signature(features):
    f=features or {}
    # Coarse signature deliberately avoids memorizing exact candles.
    payload={
        "rsi":round(_safe(f.get("rsi"),50)/5)*5,
        "volume_z":round(_safe(f.get("volume_z"),0)*2)/2,
        "sweep_low":bool(f.get("sweep_low")),
        "breakout":bool(f.get("breakout")),
        "compression":bool(f.get("compression")),
        "trend_strength":round(_safe(f.get("trend_strength"),0),2),
        "atr_pct":round(_safe(f.get("atr_pct"),0),3),
        "regime":str(f.get("regime") or "UNKNOWN"),
        "mtf_alignment":round(_safe(f.get("mtf_alignment"),0),1),
        "rejection_strength":round(_safe(f.get("rejection_strength"),0),1),
        "distance_to_resistance_atr":round(_safe(f.get("distance_to_resistance_atr"),0)*2)/2,
        "range_position":round(_safe(f.get("range_position"),.5),1),
        "direction_num":int(_safe(f.get("direction_num"),1)),
        "book_imbalance":round(_safe(f.get("book_imbalance"),0),1),
        "aggressor_imbalance":round(_safe(f.get("aggressor_imbalance"),0),1),
    }
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()[:20]

def memory_key(symbol, interval, trade):
    # Same market event should not be learned repeatedly just because the user reruns the lab.
    raw=f"{symbol}|{interval}|{trade.get('entry_ts')}|{trade.get('exit_ts')}|{_feature_signature(trade.get('features'))}"
    return hashlib.sha256(raw.encode()).hexdigest()

def classify_trade(trade):
    """Explain *how* a resolved trade behaved, rather than merely WIN/LOSS."""
    f=trade.get("features") or {}
    pnl=_safe(trade.get("r_multiple"))
    mfe=_safe(trade.get("mfe_r"))
    mae=_safe(trade.get("mae_r"))
    atr_pct=max(_safe(f.get("atr_pct")),1e-9)
    gap_atr=abs(_safe(trade.get("entry_gap_pct")))/(atr_pct*100)
    reason=str(trade.get("reason") or "")
    mistakes=[]
    strengths=[]

    if pnl <= 0:
        if gap_atr >= .60: mistakes.append("late_or_gapped_entry")
        if str(trade.get("regime"))=="BEAR": mistakes.append("counter_bear_regime")
        elif str(trade.get("regime"))=="CHOP": mistakes.append("chop_regime")
        if _safe(f.get("distance_to_resistance_atr"),99) < .55 and not f.get("breakout"):
            mistakes.append("too_close_to_resistance")
        if _safe(trade.get("edge_probability"),.5) < .48:
            mistakes.append("weak_model_edge")
        if mfe >= 1.0 and pnl <= 0:
            mistakes.append("gave_back_large_open_profit")
        elif mfe < .35:
            mistakes.append("poor_follow_through")
        if mae <= -0.80:
            mistakes.append("strong_adverse_excursion")
        if _safe(trade.get("fee_r")) >= .45:
            mistakes.append("cost_drag")
        if "stop_loss_exceeds_2R" in (trade.get("audit_flags") or []):
            mistakes.append("execution_anomaly")
        if reason=="STOP":
            mistakes.append("stopped_out")
        elif reason=="TIME":
            mistakes.append("time_exit_without_edge")
    else:
        if mfe >= 1.5: strengths.append("strong_follow_through")
        if str(trade.get("regime"))=="BULL": strengths.append("bull_regime")
        if f.get("sweep_low"): strengths.append("liquidity_sweep")
        if f.get("breakout"): strengths.append("breakout")
        if f.get("compression"): strengths.append("compression")
        if _safe(f.get("mtf_alignment")) >= .5: strengths.append("multi_timeframe_alignment")
        if _safe(f.get("rejection_strength")) >= .25: strengths.append("strong_rejection")

    return sorted(set(mistakes)), sorted(set(strengths))

def init_learning_schema(db_path):
    con=connect(db_path)
    con.executescript("""
      CREATE TABLE IF NOT EXISTS learning_memory(
        memory_key TEXT PRIMARY KEY,
        learned_at INTEGER NOT NULL,
        source_run_id INTEGER,
        symbol TEXT NOT NULL,
        interval TEXT NOT NULL,
        entry_ts INTEGER NOT NULL,
        exit_ts INTEGER,
        outcome TEXT NOT NULL,
        r_multiple REAL,
        pnl REAL,
        regime TEXT,
        features_json TEXT NOT NULL,
        mistakes_json TEXT NOT NULL,
        strengths_json TEXT NOT NULL,
        edge_probability REAL,
        mfe_r REAL,
        mae_r REAL
      );
      CREATE INDEX IF NOT EXISTS idx_memory_time
        ON learning_memory(symbol,interval,exit_ts);
      CREATE INDEX IF NOT EXISTS idx_memory_scope
        ON learning_memory(symbol,interval,regime);

      CREATE TABLE IF NOT EXISTS model_champions(
        scope TEXT PRIMARY KEY,
        version INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        source_run_id INTEGER,
        symbol TEXT NOT NULL,
        interval TEXT NOT NULL,
        status TEXT NOT NULL,
        params_json TEXT,
        metrics_json TEXT NOT NULL,
        decision_json TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS challenger_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        run_id INTEGER NOT NULL,
        scope TEXT NOT NULL,
        promoted INTEGER NOT NULL,
        challenger_metrics_json TEXT NOT NULL,
        champion_before_json TEXT,
        decision_json TEXT NOT NULL
      );
    """)
    con.commit();con.close()

def store_resolved_trades(db_path, run_id, symbol, interval, trades):
    init_learning_schema(db_path)
    con=connect(db_path)
    inserted=0
    for t in trades:
        if t.get("outcome") not in ("WIN","LOSS") or not t.get("entry_ts"):
            continue
        mistakes,strengths=classify_trade(t)
        key=memory_key(symbol,interval,t)
        cur=con.execute("""INSERT OR IGNORE INTO learning_memory(
          memory_key,learned_at,source_run_id,symbol,interval,entry_ts,exit_ts,outcome,
          r_multiple,pnl,regime,features_json,mistakes_json,strengths_json,
          edge_probability,mfe_r,mae_r
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
          key,int(time.time()),run_id,symbol,interval,int(t.get("entry_ts")),
          int(t.get("exit_ts") or t.get("entry_ts")),t.get("outcome"),_safe(t.get("r_multiple")),
          _safe(t.get("pnl")),str(t.get("regime") or "UNKNOWN"),
          json.dumps(t.get("features") or {},separators=(",",":"),default=str),
          json.dumps(mistakes,separators=(",",":")),json.dumps(strengths,separators=(",",":")),
          _safe(t.get("edge_probability"),.5),_safe(t.get("mfe_r")),_safe(t.get("mae_r"))
        ))
        inserted+=cur.rowcount
    con.commit();con.close()
    return inserted

def load_memory_trades(db_path, symbol, interval, cutoff_ts, limit=1200):
    """
    Critical anti-leakage rule:
    only return remembered outcomes that were already resolved BEFORE the current
    walk-forward training boundary.
    """
    init_learning_schema(db_path)
    con=connect(db_path)
    # Prefer same pair/timeframe, then allow a smaller global crypto-memory sample.
    same=con.execute("""SELECT * FROM learning_memory
        WHERE symbol=? AND interval=? AND exit_ts<?
        ORDER BY exit_ts DESC LIMIT ?""",(symbol,interval,int(cutoff_ts),int(limit*.70))).fetchall()
    global_rows=con.execute("""SELECT * FROM learning_memory
        WHERE NOT (symbol=? AND interval=?) AND exit_ts<?
        ORDER BY learned_at DESC LIMIT ?""",(symbol,interval,int(cutoff_ts),int(limit*.30))).fetchall()
    con.close()

    out=[]
    seen=set()
    for r in list(same)+list(global_rows):
        if r["memory_key"] in seen: continue
        seen.add(r["memory_key"])
        # Persistent memory is deliberately down-weighted vs this fold's own training data.
        weight=.40 if r["symbol"]==symbol and r["interval"]==interval else .18
        out.append({
          "outcome":r["outcome"],"r_multiple":r["r_multiple"],"regime":r["regime"],
          "features":json.loads(r["features_json"]),"_sample_weight":weight,
          "_memory_source":f"{r['symbol']}:{r['interval']}"
        })
    return out

def memory_summary(db_path, symbol=None, interval=None):
    init_learning_schema(db_path)
    con=connect(db_path)
    clauses=[];args=[]
    if symbol: clauses.append("symbol=?");args.append(symbol)
    if interval: clauses.append("interval=?");args.append(interval)
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    rows=con.execute(f"SELECT * FROM learning_memory{where} ORDER BY learned_at DESC",args).fetchall()
    con.close()
    if not rows:
        return {"samples":0,"win_rate":None,"expectancy_r":None,"top_mistakes":[],"top_strengths":[]}
    wins=sum(r["outcome"]=="WIN" for r in rows)
    rs=[_safe(r["r_multiple"]) for r in rows]
    mistakes={};strengths={}
    for r in rows:
        for x in json.loads(r["mistakes_json"] or "[]"): mistakes[x]=mistakes.get(x,0)+1
        for x in json.loads(r["strengths_json"] or "[]"): strengths[x]=strengths.get(x,0)+1
    topm=sorted(mistakes.items(),key=lambda x:x[1],reverse=True)[:8]
    tops=sorted(strengths.items(),key=lambda x:x[1],reverse=True)[:8]
    return {"samples":len(rows),"win_rate":round(wins/len(rows)*100,2),
            "expectancy_r":round(statistics.mean(rs),3) if rs else None,
            "top_mistakes":[{"name":k,"count":v} for k,v in topm],
            "top_strengths":[{"name":k,"count":v} for k,v in tops]}

def _scope(symbol,interval):
    return f"{symbol}:{interval}"

def get_champion(db_path,symbol,interval):
    init_learning_schema(db_path)
    con=connect(db_path)
    r=con.execute("SELECT * FROM model_champions WHERE scope=?",(_scope(symbol,interval),)).fetchone()
    con.close()
    if not r:return None
    return {"scope":r["scope"],"version":r["version"],"source_run_id":r["source_run_id"],
            "params":json.loads(r["params_json"]) if r["params_json"] else None,
            "metrics":json.loads(r["metrics_json"]),"decision":json.loads(r["decision_json"])}

def evaluate_challenger(db_path,run_id,symbol,interval,params,walk_forward,monte_carlo):
    """
    Conservative promotion gate. A bad run becomes useful negative memory but never
    automatically becomes the live 'best model'.
    """
    init_learning_schema(db_path)
    scope=_scope(symbol,interval)
    champion=get_champion(db_path,symbol,interval)
    m={
      "trades":int(walk_forward.get("trades") or 0),
      "expectancy_r":_safe(walk_forward.get("avg_expectancy_r"),-999),
      "profit_factor":_safe(walk_forward.get("avg_profit_factor"),0),
      "profitable_folds":int(walk_forward.get("profitable_folds") or 0),
      "folds":int(walk_forward.get("folds") or 0),
      "worst_drawdown_pct":_safe(walk_forward.get("worst_drawdown_pct"),999),
      "mc_probability_profitable":_safe(monte_carlo.get("probability_profitable"),0)
    }
    absolute={
      "enough_trades":m["trades"]>=50,
      "positive_expectancy":m["expectancy_r"]>0.05,
      "profit_factor":m["profit_factor"]>=1.10,
      "fold_consistency":m["folds"]>=3 and m["profitable_folds"]>=max(2,math.ceil(m["folds"]*.60)),
      "drawdown_control":m["worst_drawdown_pct"]<=25,
      "monte_carlo":m["mc_probability_profitable"]>=60
    }
    promoted=all(absolute.values())
    reason="passed absolute promotion gates" if promoted else "failed one or more absolute promotion gates"

    if promoted and champion:
        cm=champion["metrics"]
        # Challenger must also beat an existing champion on expectancy without
        # materially worsening drawdown.
        better_exp=m["expectancy_r"] >= _safe(cm.get("expectancy_r"),0)+0.03
        acceptable_dd=m["worst_drawdown_pct"] <= _safe(cm.get("worst_drawdown_pct"),999)+3
        promoted=better_exp and acceptable_dd
        reason="beat existing champion" if promoted else "did not beat existing champion robustly"

    decision={"promoted":promoted,"reason":reason,"gates":absolute,
              "champion_before":champion["version"] if champion else None}
    con=connect(db_path)
    con.execute("""INSERT INTO challenger_history(created_at,run_id,scope,promoted,
      challenger_metrics_json,champion_before_json,decision_json) VALUES(?,?,?,?,?,?,?)""",
      (int(time.time()),run_id,scope,int(promoted),json.dumps(m,separators=(",",":")),
       json.dumps(champion,separators=(",",":"),default=str) if champion else None,
       json.dumps(decision,separators=(",",":"))))
    if promoted:
        version=(champion["version"]+1) if champion else 1
        con.execute("""INSERT INTO model_champions(scope,version,updated_at,source_run_id,symbol,
          interval,status,params_json,metrics_json,decision_json)
          VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(scope) DO UPDATE SET
            version=excluded.version,updated_at=excluded.updated_at,source_run_id=excluded.source_run_id,
            status=excluded.status,params_json=excluded.params_json,metrics_json=excluded.metrics_json,
            decision_json=excluded.decision_json""",
          (scope,version,int(time.time()),run_id,symbol,interval,"champion",
           json.dumps(params,separators=(",",":"),default=str),json.dumps(m,separators=(",",":")),
           json.dumps(decision,separators=(",",":"))))
    con.commit();con.close()
    return decision,get_champion(db_path,symbol,interval)

def challenger_history(db_path,symbol,interval,limit=10):
    init_learning_schema(db_path)
    scope=_scope(symbol,interval);con=connect(db_path)
    rows=con.execute("""SELECT * FROM challenger_history WHERE scope=?
      ORDER BY id DESC LIMIT ?""",(scope,limit)).fetchall();con.close()
    return [{"run_id":r["run_id"],"promoted":bool(r["promoted"]),
             "metrics":json.loads(r["challenger_metrics_json"]),
             "decision":json.loads(r["decision_json"])} for r in rows]
