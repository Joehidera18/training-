"""Causality, learning, atomic bookkeeping, and workflow tests using artificial data."""
import copy
import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.adaptive import (AdaptivePolicy, POLICY_VERSION, feature_vector, action_key,
    profile_key, current_policy, approved_profile, record_outcome)
from lab.autolearn import AutoLearner, HISTORY_DAYS
from lab.coinbase_live import CoinbaseTrader
from lab.continuous import ContinuousLearner, DEFAULTS
from lab.engine import ENGINE_VERSION, build_feature_cache
from lab.execution import simulate
from lab.learning_research import learn_history
from lab.paper_store import db_connect, load_state, save_state, recent_trades
from lab.research import cost_signature, ResearchManager
from lab.service import Service
from lab.strategies import profit_candidates
from tests.test_execution import candles

BASE=Path(__file__).resolve().parents[1]
F={"rsi":55.,"adx":20.,"volume_z":1.,"atr_pct":.02,"regime":"BULL",
   "_pullback_long":True,"_trend_long":True,"breakout":True,"breakout55":True,
   "sweep_low":False,"lower_wick":.2,"signed_volume_pressure":.1,"obv_slope":.1,
   "atr_regime":1.,"range_expansion":1.,"_atr":2.,"_close":100.}


def trained_state():
    policy=AdaptivePolicy()
    params=profit_candidates()[0]
    for i in range(80): policy.observe(params,feature_vector(F),1.,i)
    return policy.export()


def install(db, settings, stamp=None):
    profile={"fingerprint":"fixture","engine_version":ENGINE_VERSION,
        "cost_signature":cost_signature(settings),"model":trained_state(),
        "data_end_ts":int(time.time()*1000)-10000 if stamp is None else stamp}
    save_state(db,profile_key("BTC-USD"),profile)
    return profile


class OnlineModelTests(unittest.TestCase):
    def test_losses_lower_and_wins_raise_the_same_setup_estimate(self):
        policy=AdaptivePolicy(trained_state())
        params=profit_candidates()[0]; vector=feature_vector(F)
        before=policy.predict(params,vector)
        policy.observe(params,vector,-1.,100)
        loss=policy.predict(params,vector)
        policy.observe(params,vector,2.,101)
        self.assertLess(loss,before)
        self.assertGreater(policy.predict(params,vector),loss)

    def test_small_samples_cannot_trade_and_choices_do_not_train(self):
        policy=AdaptivePolicy(); params=profit_candidates()[0]
        for i in range(29): policy.observe(params,feature_vector(F),1.,i)
        self.assertIsNone(policy.choose(F))
        policy.observe(params,feature_vector(F),1.,30)
        before=policy.export()
        for _ in range(10): self.assertIsNotNone(policy.choose(F))
        self.assertEqual(policy.export(),before)

    def test_json_roundtrip_bounded_updates_and_invalid_inputs(self):
        policy=AdaptivePolicy(trained_state()); params=profit_candidates()[0]
        policy.observe(params,feature_vector(F),-1000.,100)
        model=policy.state["models"][action_key(params)]
        self.assertEqual(model["sum_r"],-920.)
        self.assertTrue(all(math.isfinite(w) and abs(w)<=3 for w in model["weights"]))
        restored=AdaptivePolicy(json.loads(json.dumps(policy.export(),allow_nan=False)))
        self.assertEqual(restored.export(),policy.export())
        with self.assertRaises(ValueError): policy.observe(params,feature_vector(F),1.,99)
        with self.assertRaises(ValueError): feature_vector(dict(F,rsi=float("nan")))
        with self.assertRaises(ValueError): AdaptivePolicy({"version":"old"})

    def test_compact_features_match_full_features_and_do_not_repaint(self):
        rows=candles(320)
        for i,row in enumerate(rows):
            close=100+i*.02+math.sin(i/3)*.15
            row.update(open=close-.01,close=close,high=close+.2,low=close-.2)
        compact=build_feature_cache(rows[:290],"15m",simple_only=True)["features"]
        full=build_feature_cache(rows,"15m")["features"]
        extended=build_feature_cache(rows,"15m",simple_only=True)["features"]
        self.assertEqual(compact,extended[:290])
        for i in range(240,290):
            self.assertEqual(compact[i],{k:full[i][k] for k in compact[i]})

    def test_simulated_feedback_waits_for_closure_and_uses_the_frozen_entry_vector(self):
        rows=candles(248)
        rows[241].update(low=96.,close=98.)
        fs=[dict(F,_ts=r["ts"],rsi=55 if i==240 else 65) if i>=240 else None for i,r in enumerate(rows)]
        events=[]
        class Policy:
            def choose(self,f):
                events.append(("choose",f["_ts"]+900000))
                return {"params":profit_candidates()[0],"score":70,
                        "learning":{"vector":feature_vector(f)}}
            def observe(self,p,v,r,available_ts): events.append(("learn",available_ts,v,r))
        _,trades=simulate(rows,fs,240,len(rows),500,.0075,0,0,
            {"family":"adaptive_policy","direction":"LONG"},policy=Policy())
        learned=[e for e in events if e[0]=="learn"]
        self.assertEqual(learned[0][1],rows[241]["ts"]+900000)
        self.assertEqual(learned[0][2],feature_vector(fs[240]))
        self.assertLess(learned[0][3],0)
        self.assertEqual(trades[0]["entry_ts"],rows[241]["ts"])
        self.assertLess(events.index(learned[0]),next(i for i,e in enumerate(events) if i>0 and e[0]=="choose"))


class LearningProtocolTests(unittest.TestCase):
    def test_empty_market_evidence_is_rejected_without_inventing_training(self):
        result=learn_history(candles(3000),"BTC-USD",dict(DEFAULTS))
        self.assertFalse(result["validated"])
        self.assertEqual(result["historical_examples"],0)
        self.assertFalse(result["holdout_trades"])
        self.assertEqual(result["data_hours"],750)
        json.dumps(result,allow_nan=False)

    def test_future_outcomes_cannot_change_the_pre_holdout_model_or_select_frozen_results(self):
        rows=candles(5000)
        calls=[]
        def fake_sim(data,features,start,end,balance,risk,fee,slip,params,**kwargs):
            training=kwargs.get("training_examples",False)
            calls.append((start,end,training,kwargs.get("policy")))
            indices=[240+i*35 for i in range(100)] if training else [start+i for i in range(40)]
            payoff=(1. if data[-1]["close"]==100 else -1.) if start>=4000 else 1.
            if not training and kwargs["policy"].learn is False:
                payoff=2.  # Diagnostic can look better; it still cannot replace the updating policy.
            trades=[{"features":F,"r_multiple":payoff,"pnl":payoff,"entry_ts":data[i]["ts"],
                "exit_ts":data[i]["ts"],"strategy_family":params["family"],"reason":"TARGET2"} for i in indices]
            return {"net_pnl":len(trades)*payoff,"return_pct":len(trades)*payoff/5,
                "trades":len(trades),"max_drawdown_pct":8 if payoff<0 else 0,"expectancy_r":payoff},trades
        with patch("lab.learning_research.simulate",side_effect=fake_sim),patch(
                "lab.learning_research.build_feature_cache",return_value={"features":[{}]*len(rows)}):
            a=learn_history(rows,"BTC-USD",dict(DEFAULTS))
            rows[-1]["close"]=99.9
            b=learn_history(rows,"BTC-USD",dict(DEFAULTS))
        self.assertEqual(a["pre_holdout_model_sha256"],b["pre_holdout_model_sha256"])
        self.assertFalse(b["validated"])
        self.assertLess(b["holdout"]["net_pnl"],0)
        self.assertGreater(b["frozen_holdout"]["net_pnl"],0)
        self.assertLess(b["learning_pnl_difference"],0)
        self.assertTrue(all(end<=3904 for start,end,training,_ in calls if training))
        self.assertLess(b["training_label_end_ts"],b["holdout_start_ts"])
        for fold in b["folds"]:
            self.assertLess(fold["training_label_end_ts"],fold["test_start_ts"])

    def test_history_learning_can_be_cancelled(self):
        with self.assertRaises(InterruptedError):
            learn_history(candles(3000),"BTC-USD",dict(DEFAULTS),cancelled=lambda:True)


class LearningPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=Path(self.tmp.name)/"account.sqlite3"
        self.agent=ContinuousLearner(self.db,Path(self.tmp.name)/"data")
        self.agent.configure({"learning_enabled":True,"validated_only":False,"fee_rate":.001})
        self.agent.runtime["running"]=True
        self.agent.product_ids=["BTC-USD"]
        self.agent.market["BTC-USD"]=self.agent._empty_market()
        self.agent.market["BTC-USD"]["ticker"]={"price":100.,"best_bid":99.99,"best_ask":100.01,"ts":int(time.time()*1000)}
        self.profile=install(self.db,self.agent.settings)
        self.params=profit_candidates()[0]
        self.learning={"version":POLICY_VERSION,"vector":feature_vector(F),"cost_signature":cost_signature(self.agent.settings)}

    def tearDown(self):
        self.agent.stop(); self.tmp.cleanup()

    def open(self):
        choice={"params":self.params,"learning":self.learning,"score":70,"adjusted_score":.3,
                "learned":{"samples":80,"expectancy_r":.3}}
        return self.agent._open_position("BTC-USD",100.,F,{"key":"fixture","htf_bias":"MIXED"},choice,"learning")

    def test_paper_updates_once_and_survives_restart_without_changing_coinbase(self):
        position=self.open(); self.assertIsNotNone(position)
        self.assertIsNone(load_state(self.db,"adaptive_paper_BTC-USD"))
        initial_coinbase=current_policy(self.db,"BTC-USD",self.agent.settings,"coinbase").export()
        self.agent._close_position("BTC-USD",99.,int(time.time()*1000),"MANUAL")
        self.agent._close_position("BTC-USD",99.,int(time.time()*1000),"MANUAL")
        saved=load_state(self.db,"adaptive_paper_BTC-USD")
        self.assertEqual(saved["forward_trades"],1)
        restarted=ContinuousLearner(self.db,self.agent.data_dir)
        self.assertEqual(current_policy(self.db,"BTC-USD",restarted.settings).export(),saved["model"])
        self.assertEqual(current_policy(self.db,"BTC-USD",self.agent.settings,"coinbase").export(),initial_coinbase)
        restarted.stop()

    def test_paper_close_and_learning_roll_back_together(self):
        self.open()
        with patch("lab.continuous.write_state",side_effect=RuntimeError("injected rollback")):
            with self.assertRaises(RuntimeError):
                self.agent._close_position("BTC-USD",99.,int(time.time()*1000),"MANUAL")
        self.assertIsNone(load_state(self.db,"adaptive_paper_BTC-USD"))
        self.assertEqual(recent_trades(self.db)[0]["status"],"OPEN")
        self.agent._close_position("BTC-USD",99.,int(time.time()*1000),"MANUAL")
        self.assertEqual(load_state(self.db,"adaptive_paper_BTC-USD")["forward_trades"],1)

    def test_stale_and_cost_mismatched_models_cannot_trade_even_in_experiment_mode(self):
        self.assertIsNotNone(approved_profile(self.db,"BTC-USD",self.agent.settings))
        changed=dict(self.agent.settings,fee_rate=.002)
        self.assertIsNone(current_policy(self.db,"BTC-USD",changed))
        install(self.db,self.agent.settings,int(time.time()*1000)-31*86400000)
        self.assertIn("no current qualifying",self.agent._entry_block("BTC-USD"))
        self.assertIsNone(self.open())

    def test_mismatched_cost_feedback_is_not_added_to_a_new_model(self):
        con=db_connect(self.db)
        try:
            with con:
                learned=record_outcome(con,"BTC-USD","paper",self.params,
                    dict(self.learning,cost_signature={}),-1.,int(time.time()*1000))
            self.assertFalse(learned)
            self.assertIsNone(load_state(self.db,"adaptive_paper_BTC-USD"))
        finally: con.close()

    def live_trade(self):
        return {"id":"test-trade","signal_key":"fixture","product_id":"BTC-USD", "created_at":time.time()-100,
            "closed_at":time.time(),"status":"CLOSED","pnl":"-2", "plan":{"risk_usd":"4",
            "signal":{"params":self.params,"learning":self.learning}}}

    def test_live_closure_learns_once_and_never_updates_the_paper_model(self):
        trader=CoinbaseTrader(self.db,self.agent)
        trade=self.live_trade()
        trader._trade_save(trade); trader._trade_save(trade)
        saved=load_state(self.db,"adaptive_coinbase_BTC-USD")
        self.assertEqual(saved["forward_trades"],1)
        self.assertEqual(saved["forward_net_r"],-.5)
        self.assertIsNone(load_state(self.db,"adaptive_paper_BTC-USD"))

    def test_live_journal_failure_rolls_back_feedback(self):
        trader=CoinbaseTrader(self.db,self.agent)
        con=db_connect(self.db)
        with con:
            con.execute("CREATE TRIGGER fail_live BEFORE INSERT ON coinbase_trades BEGIN SELECT RAISE(ABORT,'fixture'); END")
        con.close()
        with self.assertRaises(sqlite3.IntegrityError): trader._trade_save(self.live_trade())
        self.assertIsNone(load_state(self.db,"adaptive_coinbase_BTC-USD"))
        self.assertFalse(trader.trades())


class AutomaticWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(BASE,Path(self.tmp.name)/"api.sqlite3",Path(self.tmp.name)/"data")

    def tearDown(self):
        self.service.autolearn.stop(); self.service.agent.stop(); self.tmp.cleanup()

    def test_start_trains_then_monitors_without_starting_coinbase(self):
        s=self.service; a=s.autolearn
        def start_market():
            s.agent.runtime.update(running=True,bootstrapped=True)
            s.agent.product_ids=["BTC-USD"]
        def study(symbols,settings):
            self.assertEqual(symbols,["BTC-USD"])
            self.assertTrue(settings["learning_enabled"])
            self.assertTrue(settings["validated_only"])
            a.stop_event.set()
        with patch.object(s.agent,"start",side_effect=start_market), patch.object(a,"study",side_effect=study) as run, patch.object(
                s.coinbase,"start",side_effect=AssertionError("must not enable real trading")):
            code,_,_=s.handle("POST","/api/learning/start",body={"fee_rate":.001},headers={"Content-Type":"application/json"})
            self.assertEqual(code,202)
            a.worker.join(timeout=2)
            self.assertFalse(a.worker.is_alive())
            run.assert_called_once()

    def test_restart_keeps_models_but_does_not_start_runners(self):
        save_state(self.service.db_path,"automatic_learning",{"enabled":True,"phase":"watching", "results":[],"next_review_at":123})
        restarted=AutoLearner(self.service.db_path,self.service.data_dir,self.service.agent,self.service.research)
        self.assertFalse(restarted.status()["enabled"])
        self.assertIsNone(restarted.worker)

    def test_study_requests_three_years_and_caches_identical_data(self):
        a=self.service.autolearn; rows=candles(3000)
        result={"symbol":"BTC-USD","validated":False,"data_hours":750,"historical_examples":0,
            "cost_signature":cost_signature(self.service.agent.settings),"rejection_reasons":["fixture"]}
        with patch.object(a.downloader,"_history",return_value=rows) as history, patch(
                "lab.autolearn.learn_history",return_value=result) as train:
            a.study(["BTC-USD"],dict(self.service.agent.settings))
            a.study(["BTC-USD"],dict(self.service.agent.settings))
            self.assertEqual(history.call_args.args[2],HISTORY_DAYS)
            train.assert_called_once()

    def test_learning_endpoints_use_the_existing_access_token(self):
        self.service.token="private-test-token"
        for path in ("/api/learning/status","/api/learning/export"):
            self.assertEqual(self.service.handle("GET",path)[0],401)
            self.assertEqual(self.service.handle("GET",path,headers={"Authorization":"Bearer private-test-token"})[0],200)


if __name__=="__main__": unittest.main()
