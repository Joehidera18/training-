"""Paper account persistence, controls, API and regression tests. No live orders."""
import copy
import io
import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.continuous import ContinuousLearner, RuntimeLease, DEFAULTS, INTERVAL_MS, estimate_memory
from lab.paper_store import db_connect, load_state, save_state, recent_trades, memory_row
from lab.research import cost_signature
from lab.engine import ENGINE_VERSION
from lab.service import Service, wsgi_application
from lab.strategies import profit_candidates
from tests.test_execution import candles

BASE = Path(__file__).resolve().parents[1]


class PaperAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/"account.sqlite3"
        self.agent = ContinuousLearner(self.db,Path(self.tmp.name)/"data")
        self.agent.configure({"validated_only":False,"fee_rate":.001})
        self.agent.runtime["running"] = True
        self.pid = "BTC-USD"
        self.quote()
        self.choice = {"params":profit_candidates()[0],"adjusted_score":70,"raw_score":70,
                       "learned":{"samples":0,"expectancy_r":0,"win_probability":.5}}
        self.f = {"_atr":2.,"_close":100.}
        self.ctx = {"key":"test","htf_bias":"MIXED"}

    def tearDown(self):
        self.agent.stop()
        self.tmp.cleanup()

    def quote(self,pid="BTC-USD",price=100.,ts=None):
        self.agent.market.setdefault(pid,self.agent._empty_market())
        self.agent.market[pid]["ticker"] = {"product_id":pid,"price":price,"best_bid":price-.01,
            "best_ask":price+.01,"ts":int(time.time()*1000) if ts is None else ts}
        if pid not in self.agent.product_ids: self.agent.product_ids.append(pid)

    def open(self,pid="BTC-USD",f=None):
        return self.agent._open_position(pid,100.,f or self.f,self.ctx,self.choice,"experiment")

    def test_settings_reject_invalid_values_without_partial_changes(self):
        before = dict(self.agent.settings)
        for patch_value in ({"risk_per_trade":.01,"fee_rate":float("nan")},
                            {"max_positions":1.2},{"max_positions":True},
                            {"fee_rate":float("inf")},{"unexpected":1},{"validated_only":"false"},
                            {"max_gross_exposure":.1,"max_notional_fraction":.3}):
            with self.subTest(patch=patch_value):
                with self.assertRaises(ValueError): self.agent.configure(patch_value)
                self.assertEqual(self.agent.settings,before)

    def test_open_close_restart_preserves_cash_positions_and_cooldown(self):
        position = self.open()
        self.assertIsNotNone(position)
        recovered = ContinuousLearner(self.db,self.agent.data_dir)
        self.assertEqual(recovered.open_positions[self.pid]["trade_id"],position["trade_id"])
        self.quote(price=104.)
        result = self.agent.close_manual(self.pid)
        recovered = ContinuousLearner(self.db,self.agent.data_dir)
        self.assertFalse(recovered.open_positions)
        self.assertAlmostEqual(recovered.portfolio["balance"],500+result["pnl"])
        self.assertGreater(recovered.cooldown_until[self.pid],time.time())
        self.assertEqual(recent_trades(self.db)[0]["status"],"CLOSED")

    def test_fees_and_slippage_are_frozen_for_open_trades(self):
        p = self.open()
        self.agent.configure({"fee_rate":.02,"slippage_rate":.01})
        self.quote(price=102.)
        result = self.agent.close_manual(self.pid)
        exit_price = 101.99*(1-p["slippage_rate"])
        expected = ((exit_price-p["entry"]) - (exit_price+p["entry"])*p["fee_rate"])*p["qty"]
        self.assertAlmostEqual(result["pnl"],expected)

    def test_no_duplicate_position_and_position_cap_is_real(self):
        self.agent.configure({"fee_rate":0,"slippage_rate":0})
        p = self.open(f={"_atr":.2,"_close":100.})
        self.assertIsNotNone(p)
        self.assertLessEqual(p["qty"]*p["entry"],150.000001)
        self.assertAlmostEqual(p["risk_usd"],p["qty"]*p["stop_dist"])
        self.assertLess(p["risk_usd"],500*self.agent.settings["risk_per_trade"])
        self.assertIsNone(self.open())
        self.assertEqual(len(recent_trades(self.db)),1)

    def test_open_risk_and_cash_caps_apply_across_markets(self):
        self.agent.configure({"fee_rate":0,"slippage_rate":0,"risk_per_trade":.02,"max_total_risk":.02,
                              "max_positions":10,"max_notional_fraction":.3})
        for i in range(10):
            pid = "COIN"+str(i)+"-USD"; self.quote(pid)
            self.open(pid,{"_atr":.2,"_close":100.})
        positions = list(self.agent.open_positions.values())
        self.assertLessEqual(sum(p["entry"]*p["qty"] for p in positions),500)
        self.assertLessEqual(sum(p["risk_usd"] for p in positions),10)
        self.assertLess(len(positions),10)

    def test_close_is_atomic_when_memory_write_fails(self):
        p = self.open()
        before = copy.deepcopy(self.agent.portfolio)
        with patch("lab.continuous.update_memory",side_effect=RuntimeError("injected database failure")):
            with self.assertRaises(RuntimeError): self.agent.close_manual(self.pid)
        self.assertEqual(self.agent.portfolio,before)
        self.assertIn(self.pid,self.agent.open_positions)
        self.assertNotIn(self.pid,self.agent.cooldown_until)
        self.assertEqual(recent_trades(self.db)[0]["status"],"OPEN")
        self.agent.close_manual(self.pid)
        self.assertEqual(memory_row(self.db,"global","*","test",p["family"],"LONG")["samples"],1)
        self.assertEqual(memory_row(self.db,"coin",self.pid,"test",p["family"],"LONG")["samples"],1)

    def test_learning_does_not_count_the_same_trade_twice(self):
        row = {"samples":10,"wins":6,"sum_r":4}
        estimate = estimate_memory(row,row)
        self.assertEqual(estimate["samples"],10)
        self.assertEqual(estimate["effective_samples"],10)
        self.assertAlmostEqual(estimate["expectancy_r"],4/22)

    def test_paused_entries_still_process_existing_stops(self):
        self.open()
        self.agent.configure({"entries_paused":True})
        tick = dict(self.agent.market[self.pid]["ticker"],ts=int(time.time()*1000)+1,
                    price=95.,best_bid=94.99,best_ask=95.01)
        self.agent.on_tick(tick)
        self.assertFalse(self.agent.open_positions)
        self.assertEqual(recent_trades(self.db)[0]["exit_reason"],"STOP")
        self.assertIn("paused",self.agent._entry_block(self.pid))

    def test_stale_or_crossed_quotes_block_entries_and_manual_close(self):
        self.quote(ts=int(time.time()*1000)-120000)
        self.assertIsNone(self.open())
        self.quote(); self.open()
        self.quote(ts=int(time.time()*1000)-120000)
        with self.assertRaisesRegex(ValueError,"stale"): self.agent.close_manual(self.pid)
        self.quote()
        self.agent.market[self.pid]["ticker"]["best_bid"] = 101.
        self.assertIsNone(self.agent._fresh_quote(self.pid))

    def test_daily_loss_latches_and_survives_restart(self):
        self.agent._check_daily_limit()
        self.agent.portfolio["balance"] = 480
        self.assertTrue(self.agent._check_daily_limit())
        self.agent.portfolio["balance"] = 500  # recovery cannot undo a same-day halt
        self.assertTrue(self.agent._check_daily_limit())
        self.agent._save()
        recovered = ContinuousLearner(self.db,self.agent.data_dir)
        self.assertTrue(recovered.risk_day["halted"])

    def test_reset_refuses_open_positions_and_backs_up_completed_journal(self):
        self.open()
        with self.assertRaises(ValueError): self.agent.reset()
        self.assertTrue(self.agent.runtime["running"])
        self.agent.close_manual(self.pid)
        self.agent.reset(keep_memory=True)
        self.assertEqual(self.agent.portfolio["balance"],500)
        self.assertFalse(recent_trades(self.db))
        backups = list((self.agent.data_dir/"backups").glob("*.sqlite3"))
        self.assertEqual(len(backups),1)
        con = sqlite3.connect(backups[0])
        self.assertEqual(con.execute("SELECT count(*) FROM paper_trades").fetchone()[0],1)
        con.close()
        self.assertEqual(memory_row(self.db,"global","*","test",self.choice["params"]["family"],"LONG")["samples"],1)

    def test_current_profile_is_required_and_cost_changes_invalidate_it(self):
        self.agent.configure({"validated_only":True})
        self.assertIsNone(self.open())
        profile = {"params":self.choice["params"],"engine_version":ENGINE_VERSION,
                   "cost_signature":cost_signature(self.agent.settings),"data_end_ts":int(time.time()*1000)}
        save_state(self.db,"validated_profiles",{self.pid:profile})
        self.assertIsNotNone(self.agent._approved_params(self.pid))
        self.assertIn(self.pid,self.agent.status()["active_profiles"])
        self.agent.configure({"fee_rate":.002})
        self.assertIsNone(self.agent._approved_params(self.pid))
        self.assertFalse(self.agent.status()["active_profiles"])

    def test_decision_uses_configured_interval_not_last_warmup_interval(self):
        now = 1700006400000//14400000*14400000
        market = self.agent.market[self.pid]
        for iv,step in INTERVAL_MS.items():
            end = now//step*step
            market["bars"][iv] = candles(241,step,end-241*step)
        self.agent.settings["validated_only"] = True
        feature = {"_atr":2.,"_close":100.,"interval":"15m"}
        with patch("lab.continuous.now_ms",return_value=now), \
             patch.object(self.agent,"_latest_feature",return_value=feature) as latest, \
             patch.object(self.agent,"_context",return_value=self.ctx), \
             patch.object(self.agent,"_candidate_opportunities",return_value=[self.choice]), \
             patch.object(self.agent,"_entry_block",return_value=None), \
             patch.object(self.agent,"_open_position") as opened:
            self.agent._decision_cycle(self.pid)
        latest.assert_called_once_with(self.pid,"15m")
        self.assertEqual(opened.call_args.args[2]["interval"],"15m")

    def test_status_is_read_only_and_never_recomputes_features(self):
        with patch("lab.continuous.build_feature_cache",side_effect=AssertionError("expensive request")):
            status = self.agent.status()
        json.dumps(status,allow_nan=False)
        self.assertTrue(status["paper_only"])

    def test_another_process_cannot_run_the_same_account(self):
        first, second = RuntimeLease(self.db), RuntimeLease(self.db)
        first.acquire()
        try:
            with self.assertRaises(RuntimeError): second.acquire()
        finally: first.release()
        second.acquire(); second.release()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(BASE,Path(self.tmp.name)/"api.sqlite3",Path(self.tmp.name)/"data")
        self.app = wsgi_application(self.service)

    def tearDown(self):
        self.service.agent.stop()
        self.tmp.cleanup()

    def request(self,path,method="GET",body=None,raw=None,ctype="application/json",auth=""):
        raw = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        path,_,query = path.partition("?")
        environment = {"REQUEST_METHOD":method,"PATH_INFO":path,"QUERY_STRING":query,
                       "CONTENT_TYPE":ctype,"CONTENT_LENGTH":str(len(raw)),
                       "HTTP_AUTHORIZATION":auth,"wsgi.input":io.BytesIO(raw)}
        result = {}
        def begin(status,headers):
            result.update(code=int(status.split()[0]),headers=dict(headers))
        payload = b"".join(self.app(environment,begin))
        result["body"] = json.loads(payload) if result["headers"]["Content-Type"]=="application/json" else payload
        return result

    def test_dashboard_assets_and_offline_api_routes_work(self):
        for path in ("/","/static/app.js","/static/style.css","/api/health","/api/continuous/status",
                     "/api/continuous/settings","/api/continuous/analytics","/api/continuous/trades",
                     "/api/continuous/activity","/api/continuous/opportunities","/api/continuous/memory",
                     "/api/research/status","/api/runs"):
            with self.subTest(path=path): self.assertEqual(self.request(path)["code"],200)
        self.assertEqual(self.request("/api/health")["body"]["default_mode"],"paper")

    def test_bad_limits_and_payloads_return_clear_errors(self):
        self.assertEqual(self.request("/api/continuous/trades?limit=NaN")["code"],400)
        self.assertEqual(self.request("/api/continuous/settings","POST",raw=b"{")["code"],400)
        self.assertEqual(self.request("/api/continuous/settings","POST",body=[],ctype="application/json")["code"],400)
        self.assertEqual(self.request("/api/continuous/settings","POST",body={},ctype="text/plain")["code"],415)
        self.assertEqual(self.request("/api/continuous/settings","POST",raw=b" "*32769)["code"],413)
        self.assertEqual(self.request("/api/continuous/reset","POST",body={})["code"],400)

    def test_optional_app_token_is_enforced_on_private_api(self):
        self.service.token = "test-local-token"
        self.assertEqual(self.request("/api/continuous/status")["code"],401)
        self.assertEqual(self.request("/api/continuous/status",auth="Bearer test-local-token")["code"],200)
        self.assertEqual(self.request("/api/health")["code"],200)

    def test_backup_is_usable_sqlite_and_exports_have_correct_headers(self):
        backup = self.request("/api/continuous/backup")
        target = Path(self.tmp.name)/"restored.sqlite3"; target.write_bytes(backup["body"])
        con = sqlite3.connect(target)
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0],"ok")
        con.close()
        export = self.request("/api/continuous/export")
        self.assertIn(b"risk_usd,status,pnl,result_r",export["body"])
        self.assertIn("attachment",export["headers"]["Content-Disposition"])
        research = self.request("/api/research/export")
        self.assertEqual(research["body"]["status"],"idle")

    def test_dashboard_script_references_existing_element_ids(self):
        import re
        from html.parser import HTMLParser
        class IDs(HTMLParser):
            def __init__(self): super().__init__(); self.ids=[]
            def handle_starttag(self,tag,attrs):
                self.ids.extend(v for k,v in attrs if k=="id")
        parser = IDs(); parser.feed((BASE/"templates/index.html").read_text())
        self.assertEqual(len(parser.ids),len(set(parser.ids)))
        refs = set(re.findall(r'\$\("([^"]+)"\)',(BASE/"static/app.js").read_text()))
        self.assertFalse(refs-set(parser.ids))

    def test_unimplemented_generic_order_endpoint_is_absent(self):
        self.assertEqual(self.request("/api/live/order","POST",body={})["code"],404)
