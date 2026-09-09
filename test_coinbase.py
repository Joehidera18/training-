"""Offline exchange-failure tests. No credentials, network or real orders."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from decimal import Decimal as D
from lab.coinbase_broker import CoinbaseAdapter, BrokerError, SubmissionUnknown, entry_plan, decimal, rounded, iso
from lab.coinbase_live import CoinbaseTrader
from lab.continuous import DEFAULTS
from lab.paper_store import init_continuous_db
from lab.service import Service

NOW=1788825600
PRODUCT={"product_id":"BTC-USD","base":"BTC","base_increment":".00001","quote_increment":".01",
         "price_increment":".01","base_min_size":".00001","base_max_size":"100",
         "quote_min_size":"1","quote_max_size":"1000000"}
SIGNAL={"product_id":"BTC-USD","signal_ts":NOW*1000,"interval":"15m","atr":2,"close":100,
        "params":{"direction":"LONG","stop_atr":1.5,"rr2":2,"max_cost_r":.5,"max_gap_atr":.5,"time_stop_hours":12}}
SNAPSHOT={"portfolio":"portfolio-one","synced_at":NOW,"permissions":{"can_view":True,"can_trade":True,"can_transfer":False},
          "simple_fees":True,"taker_fee_rate":".001","maker_fee_rate":".0005","fee_tier":"fixture",
          "balances":{"USD":{"available":"500","hold":"0"}}}

class FakeBroker:
    configured=True
    allow_live=True
    def __init__(self):
        self.account=copy.deepcopy(SNAPSHOT)
        self.orders={}
        self.submissions=[]
        self.cancellations=[]
        self.timeout=False
        self.reject=False
        self.excess_fee=False
        self.before_submit=None
    def snapshot(self): return copy.deepcopy(self.account)
    def product(self,pid): return copy.deepcopy(PRODUCT)
    def quote(self,pid): return {"bid":"99.99","ask":"100.01","ts":NOW}
    def book(self,pid):
        return {"product_id":pid,"ts":NOW,"bids":[{"price":"99.99","size":"100"}],
                "asks":[{"price":"100.01","size":"100"}]}
    def preview(self,payload):
        return {"preview_id":"preview","commission_total":"99" if self.excess_fee else "0",
                "order_total":"100","warnings":[]}
    def list_orders(self,pid,since,active_only=False):
        return [copy.deepcopy(o) for o in self.orders.values() if o["product_id"]==pid and
                (not active_only or o["status"]=="OPEN")]
    def lookup(self,cid,pid,since):
        return next((copy.deepcopy(o) for o in self.orders.values() if o.get("client_order_id")==cid),None)
    def order(self,oid): return copy.deepcopy(self.orders[oid])
    def cancel(self,oid):
        self.cancellations.append(oid)
        return True
    def submit(self,payload):
        if not self.allow_live: raise BrokerError("disabled")
        if self.before_submit: self.before_submit(payload)
        self.submissions.append(copy.deepcopy(payload))
        if self.reject: return {"accepted":False}
        oid="order-"+str(len(self.submissions))
        self.orders[oid]={"order_id":oid,"client_order_id":payload["client_order_id"],
            "product_id":payload["product_id"],"side":payload["side"],"status":"OPEN",
            "filled_size":"0","filled_value":"0","total_fees":"0","settled":False,
            "order_configuration":copy.deepcopy(payload["order_configuration"])}
        if self.timeout: raise SubmissionUnknown("timeout")
        return {"accepted":True,"order_id":oid}

class TraderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/"test.sqlite3"
        init_continuous_db(self.db)
        def signal(pid):
            if pid!="BTC-USD": raise ValueError("No signal")
            return copy.deepcopy(SIGNAL)
        self.agent=SimpleNamespace(settings=dict(DEFAULTS,fee_rate=.001),coinbase_signal=signal)
        self.broker=FakeBroker()
        self.trader=CoinbaseTrader(self.db,self.agent,self.broker,clock=lambda:NOW)
        self.trader.mode="live"
    def tearDown(self):
        self.trader.stop()
        self.tmp.cleanup()
    def enter(self):
        self.trader.tick()
        return self.trader._active()
    def fill_entry(self,trade):
        qty=trade["plan"]["base_size"]
        self.broker.orders[trade["entry"]["order_id"]].update(status="FILLED",filled_size=qty,
            filled_value=str(D(qty)*100),total_fees=".12",settled=True,attached_order_id="bracket")
        self.broker.orders["bracket"]={"order_id":"bracket","product_id":"BTC-USD","side":"SELL",
            "originating_order_id":trade["entry"]["order_id"],"status":"OPEN","filled_size":"0",
            "filled_value":"0","total_fees":"0","settled":False,
            "order_configuration":{"trigger_bracket_gtc":{"limit_price":trade["plan"]["target"],
                                                         "stop_trigger_price":trade["plan"]["stop"]}}}
        self.broker.account["balances"]["BTC"]={"available":qty,"hold":"0"}
        return qty
    def test_preview_cannot_submit_even_when_adapter_is_armed(self):
        self.trader.mode="preview"
        self.trader.tick()
        self.assertIsNotNone(self.trader.state["last_preview"])
        self.assertEqual(self.broker.submissions,[])
        self.assertEqual(self.broker.cancellations,[])
        self.assertIsNone(self.trader._active())
    def test_intent_is_persisted_before_order_crosses_network(self):
        def before(payload):
            recovered=CoinbaseTrader(self.db,self.agent,self.broker,clock=lambda:NOW)
            record=recovered._active()["entry"]
            self.assertEqual(record["payload"]["client_order_id"],payload["client_order_id"])
            self.assertTrue(record["submission_started"])
            self.assertEqual(recovered.mode,"idle")
        self.broker.before_submit=before
        self.enter()
        self.assertEqual(len(self.broker.submissions),1)
    def test_timeout_recovers_original_order_without_resubmission(self):
        self.broker.timeout=True
        with self.assertRaises(SubmissionUnknown): self.enter()
        self.broker.timeout=False
        self.trader.tick()
        self.assertEqual(len(self.broker.submissions),1)
        self.assertEqual(self.trader._active()["entry"]["order_id"],"order-1")
    def test_unknown_missing_order_blocks_all_new_entries(self):
        self.broker.timeout=True
        with self.assertRaises(SubmissionUnknown): self.enter()
        self.broker.orders.clear()
        for _ in range(2):
            with self.assertRaisesRegex(BrokerError,"unknown"): self.trader.tick()
        self.assertEqual(len(self.broker.submissions),1)
    def test_explicit_rejection_records_no_fill_or_profit(self):
        self.broker.reject=True
        self.enter()
        self.trader.tick()
        self.assertIsNone(self.trader._active())
        self.assertEqual(self.trader.trades()[0]["status"],"REJECTED")
        self.assertEqual(self.trader._realized(),0)
        self.assertEqual(len(self.broker.submissions),1)
    def test_partial_bracket_waits_for_cancel_confirmation_and_sells_only_residual(self):
        trade=self.enter(); qty=D(self.fill_entry(trade))
        child=self.broker.orders["bracket"]
        child.update(filled_size=".2",filled_value="21",total_fees=".021")
        self.trader.tick()
        self.assertEqual(self.broker.cancellations,["bracket"])
        self.assertEqual(len(self.broker.submissions),1)
        # Another fill arrives during cancellation; the replacement must account for it.
        child.update(status="CANCELLED",filled_size=".3",filled_value="31.5",total_fees=".0315",settled=True)
        self.trader.tick()
        sell=self.broker.submissions[-1]
        self.assertEqual(sell["side"],"SELL")
        self.assertEqual(D(sell["order_configuration"]["market_market_ioc"]["base_size"]),qty-D(".3"))
        self.trader.tick()
        self.assertEqual(len(self.broker.submissions),2)
    def test_partial_market_exit_is_not_replaced_until_terminal(self):
        trade=self.enter(); qty=D(self.fill_entry(trade))
        self.broker.orders["bracket"].update(status="CANCELLED")
        self.trader.tick()
        exit_order=self.broker.orders["order-2"]
        exit_order.update(filled_size=".2",filled_value="20",total_fees=".02")
        self.trader.tick()
        self.assertEqual(len(self.broker.submissions),2)
        exit_order.update(status="CANCELLED",settled=True)
        self.trader.tick()
        self.assertEqual(D(self.broker.submissions[-1]["order_configuration"]["market_market_ioc"]["base_size"]),qty-D(".2"))
    def test_actual_fees_and_settlement_control_realized_profit(self):
        trade=self.enter(); qty=D(self.fill_entry(trade))
        child=self.broker.orders["bracket"]
        child.update(status="FILLED",filled_size=str(qty),filled_value=str(qty*105),total_fees=".15",settled=False)
        self.trader.tick()
        self.assertEqual(self.trader._active()["status"],"SETTLING")
        self.assertEqual(self.trader._realized(),0)
        child["settled"]=True
        self.trader.mode="sync"
        self.trader.tick(False); self.trader.tick(False)
        self.assertIsNone(self.trader._active())
        self.assertEqual(self.trader._realized(),qty*5-D(".27"))
    def test_missing_protection_blocks_new_orders(self):
        trade=self.enter(); self.fill_entry(trade)
        self.broker.orders[trade["entry"]["order_id"]].pop("attached_order_id")
        self.broker.orders.pop("bracket")
        self.trader.tick()
        self.assertEqual(self.trader._active()["status"],"NEEDS_ATTENTION")
        self.assertEqual(len(self.broker.submissions),1)
    def test_dust_does_not_become_a_fake_closed_trade(self):
        trade=self.enter(); qty=D(self.fill_entry(trade))
        self.broker.orders["bracket"].update(status="CANCELLED",filled_size=str(qty-D(".00001")),
            filled_value="100",total_fees=".1",settled=True)
        self.trader.tick()
        self.assertEqual(self.trader._active()["status"],"NEEDS_ATTENTION")
        self.assertEqual(self.trader._realized(),0)
    def test_permissions_and_non_dedicated_balances_block_entries(self):
        for patch_account in (
            {"permissions":dict(SNAPSHOT["permissions"],can_trade=False)},
            {"permissions":dict(SNAPSHOT["permissions"],can_transfer=True)},
            {"simple_fees":False},
            {"balances":dict(SNAPSHOT["balances"],ETH={"available":".1","hold":"0"})}):
            with self.subTest(patch=patch_account):
                self.broker.account=copy.deepcopy(SNAPSHOT); self.broker.account.update(patch_account)
                try: self.trader.tick()
                except BrokerError: pass
                self.assertFalse(self.broker.submissions)
    def test_changed_portfolio_is_rejected(self):
        self.trader.tick(False)
        self.broker.account["portfolio"]="another"
        with self.assertRaisesRegex(BrokerError,"different"): self.trader.tick()
    def test_higher_preview_fees_block_entry(self):
        self.broker.excess_fee=True
        self.trader.tick()
        self.assertFalse(self.broker.submissions)
    def test_pause_preserves_exit_management_and_stop_does_not_cancel(self):
        trade=self.enter(); self.fill_entry(trade)
        self.trader.pause(True)
        self.broker.orders["bracket"].update(status="CANCELLED")
        self.trader.tick()
        self.assertEqual(self.broker.submissions[-1]["side"],"SELL")
        count=len(self.broker.cancellations)
        self.trader.stop()
        self.assertEqual(len(self.broker.cancellations),count)
    def test_close_request_survives_restart_and_is_bound_to_trade(self):
        trade=self.enter(); self.fill_entry(trade)
        self.trader.runtime["running"]=True
        self.trader.request_close()
        recovered=CoinbaseTrader(self.db,self.agent,self.broker,clock=lambda:NOW)
        self.assertEqual(recovered.state["close_trade_id"],trade["id"])
        self.assertEqual(recovered.mode,"idle")
    def test_daily_loss_is_persistent_and_separate_from_paper(self):
        self.trader.tick(False)
        trade={"id":"closed","signal_key":"closed","created_at":NOW-1,"status":"CLOSED","pnl":"-20"}
        self.trader._trade_save(trade)
        self.trader.tick()
        self.assertTrue(self.trader.state["risk_day"]["halted"])
        self.assertFalse(self.broker.submissions)
        recovered=CoinbaseTrader(self.db,self.agent,self.broker,clock=lambda:NOW)
        self.assertTrue(recovered.state["risk_day"]["halted"])
    def test_local_live_flag_blocks_start(self):
        self.broker.allow_live=False
        with self.assertRaisesRegex(BrokerError,"COINBASE_ALLOW_LIVE"): self.trader.start("live")

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.sdk=MagicMock()
        self.adapter=CoinbaseAdapter(sdk=self.sdk,clock=lambda:NOW)
        self.sleeper=patch("lab.coinbase_broker.time.sleep"); self.sleeper.start()
    def tearDown(self): self.sleeper.stop()
    def test_decimal_rejects_invalid_values_and_rounds_exchange_steps(self):
        for value in ("NaN","Infinity",-1,True,None):
            with self.subTest(value=value):
                with self.assertRaises(BrokerError): decimal(value)
        self.assertEqual(rounded(D("1.239"),".01"),D("1.23"))
    def test_unarmed_adapter_blocks_both_create_and_cancel(self):
        with self.assertRaises(BrokerError): self.adapter.submit({"client_order_id":"persisted"})
        with self.assertRaises(BrokerError): self.adapter.cancel("id")
        self.sdk.create_order.assert_not_called(); self.sdk.cancel_orders.assert_not_called()
    def test_sdk_exceptions_do_not_expose_secret_and_write_is_unknown(self):
        self.sdk.get_product.side_effect=RuntimeError("secret-private-key")
        with self.assertRaises(BrokerError) as caught: self.adapter.product("BTC-USD")
        self.assertNotIn("secret",str(caught.exception))
        self.adapter.allow_live=True
        self.sdk.create_order.side_effect=TimeoutError("secret-private-key")
        with self.assertRaises(SubmissionUnknown): self.adapter.submit({"client_order_id":"x"})
        self.assertEqual(self.sdk.create_order.call_count,1)
    def test_account_pagination_requires_complete_unique_accounts(self):
        def account(uid): return {"uuid":uid,"currency":"USD","ready":True,"available_balance":{"value":"250"},"hold":{"value":"0"}}
        self.sdk.get_accounts.side_effect=[{"accounts":[account("a")],"has_next":True,"cursor":"next"},
                                          {"accounts":[account("b")],"has_next":False}]
        self.assertEqual(len(self.adapter.accounts()),2)
        self.assertEqual(self.sdk.get_accounts.call_args.kwargs["cursor"],"next")
        self.sdk.get_accounts.side_effect=None
        self.sdk.get_accounts.return_value={"accounts":[account("a"),account("a")],"has_next":False}
        with self.assertRaises(BrokerError): self.adapter.accounts()
    def test_missing_permission_flags_fail_closed(self):
        self.sdk.get_api_key_permissions.return_value={"can_view":True,"can_trade":True,"portfolio_uuid":"p"}
        with self.assertRaisesRegex(BrokerError,"permissions"): self.adapter.snapshot()
    def test_stale_and_crossed_quote_rejected(self):
        book={"product_id":"BTC-USD","bids":[{"price":"100"}],"asks":[{"price":"100.01"}],"time":iso(NOW)}
        self.sdk.get_best_bid_ask.return_value={"pricebooks":[book]}
        self.assertEqual(self.adapter.quote("BTC-USD")["bid"],"100")
        book["time"]=iso(NOW-31)
        with self.assertRaises(BrokerError): self.adapter.quote("BTC-USD")
        book["time"]=iso(NOW); book["asks"][0]["price"]="99"
        with self.assertRaises(BrokerError): self.adapter.quote("BTC-USD")
    def test_preview_rejection_does_not_fall_back_to_order(self):
        self.sdk.preview_order.return_value={"errs":["UNSUPPORTED_ORDER_CONFIGURATION"]}
        with self.assertRaises(BrokerError): self.adapter.preview({"product_id":"BTC-USD"})
        self.sdk.create_order.assert_not_called()
    def test_protected_plan_has_fee_cash_risk_caps_and_no_attached_size(self):
        plan=entry_plan(SIGNAL,PRODUCT,{"bid":"99.99","ask":"100.01","ts":NOW},SNAPSHOT,500,
                        dict(DEFAULTS,fee_rate=.001),"stable")
        self.assertLessEqual(D(plan["risk_usd"]),D("3.75"))
        self.assertLessEqual(D(plan["max_spend"]),D("150"))
        attached=plan["payload"]["attached_order_configuration"]["trigger_bracket_gtc"]
        self.assertNotIn("base_size",attached)
        self.assertIn("limit_limit_fok",plan["payload"]["order_configuration"])
    def test_fee_underestimate_or_minimum_size_blocks_plan(self):
        args=(SIGNAL,PRODUCT,{"bid":"99.99","ask":"100.01","ts":NOW},SNAPSHOT,500)
        with self.assertRaisesRegex(BrokerError,"fee"): entry_plan(*args,dict(DEFAULTS,fee_rate=0),"id")
        with self.assertRaises(BrokerError):
            entry_plan(SIGNAL,dict(PRODUCT,quote_min_size="200"),args[2],SNAPSHOT,500,dict(DEFAULTS,fee_rate=.001),"id")

class CoinbaseServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(Path(__file__).resolve().parents[1],Path(self.tmp.name)/"api.sqlite3",Path(self.tmp.name)/"data")
    def tearDown(self):
        self.service.coinbase.stop(); self.service.agent.stop(); self.tmp.cleanup()
    def post(self,path,body):
        return self.service.handle("POST","/api/coinbase/"+path,body=body,
            headers={"Content-Type":"application/json","Authorization":"Bearer "+self.service.token})[0]
    def test_private_status_locked_and_controls_require_token(self):
        self.service.coinbase.state["snapshot"]={"private":"account"}
        code,body,_=self.service.handle("GET","/api/coinbase/status")
        self.assertEqual(code,200); self.assertIsNone(body["snapshot"])
        self.assertEqual(self.post("sync",{}),409)
        self.assertEqual(self.service.handle("GET","/api/continuous/backup")[0],409)
    def test_live_start_requires_phrase_and_local_flag(self):
        self.service.token="offline-token-at-least-16"
        self.assertEqual(self.post("start",{"mode":"live"}),400)
        self.assertEqual(self.post("start",{"mode":"live","confirm":"ENABLE COINBASE LIVE"}),409)
    def test_coinbase_script_ids_exist(self):
        import re
        html=(self.service.base_dir/"templates/index.html").read_text()
        script=(self.service.base_dir/"static/coinbase.js").read_text()
        ids=set(re.findall(r'id="([^"]+)"',html))
        refs=set(re.findall(r'(?:el|button)\("([^"]+)"',script))
        self.assertFalse(refs-ids)

class SignalBridgeTests(unittest.TestCase):
    def test_live_signal_requires_current_profile_even_with_paper_experiments_enabled(self):
        from lab.continuous import ContinuousLearner, INTERVAL_MS
        from tests.test_execution import candles
        from lab.paper_store import save_state
        from lab.research import cost_signature
        from lab.engine import ENGINE_VERSION
        from lab.strategies import profit_candidates
        with tempfile.TemporaryDirectory() as directory:
            agent=ContinuousLearner(Path(directory)/"signal.sqlite3",Path(directory)/"data")
            agent.runtime["running"]=True
            agent.settings["validated_only"]=False
            now=NOW*1000//14400000*14400000
            market=agent._empty_market(); agent.market["BTC-USD"]=market
            for iv,step in INTERVAL_MS.items():
                end=now//step*step
                market["bars"][iv]=candles(241,step,end-241*step)
            stamp=market["bars"]["15m"][-1]["ts"]
            agent._feature_cache[("BTC-USD","15m")]=(None,{"_ts":stamp,"_atr":2.,"_close":100.})
            with patch("lab.continuous.now_ms",return_value=now), patch("lab.continuous.evaluate_signal",return_value=(70,"fixture")):
                with self.assertRaisesRegex(ValueError,"profile"): agent.coinbase_signal("BTC-USD")
                save_state(agent.db_path,"validated_profiles",{"BTC-USD":{"params":profit_candidates()[0],
                    "engine_version":ENGINE_VERSION,"cost_signature":cost_signature(agent.settings),"data_end_ts":now}})
                self.assertEqual(agent.coinbase_signal("BTC-USD")["interval"],"15m")
                agent._feature_cache[("BTC-USD","15m")][1]["_ts"]-=900000
                with self.assertRaisesRegex(ValueError,"features"): agent.coinbase_signal("BTC-USD")
            agent.stop()
