"""Causal quality-filter and liquidity regressions using artificial data only."""
import copy
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import MagicMock, patch
from lab.trade_quality import net_payoff, cooldown_minutes
from lab.execution import simulate
from lab.coinbase_broker import CoinbaseAdapter, BrokerError, book_execution
from lab.continuous import ContinuousLearner
from tests.test_execution import candles, features, PARAMS
from tests.test_coinbase import FakeBroker, SIGNAL, NOW, PRODUCT, SNAPSHOT
from lab.coinbase_live import CoinbaseTrader
from lab.paper_store import init_continuous_db
from types import SimpleNamespace
from lab.continuous import DEFAULTS
from lab.strategies import profit_candidates

class QualityTests(unittest.TestCase):
    def test_gross_two_to_one_can_be_rejected_after_costs(self):
        q=net_payoff(D("100"),D("98"),D("104"),D(".004"),D(".0005"))
        self.assertLess(q["net_rr"],D("1.5"))
        self.assertGreater(q["break_even_win_rate"],D("0.333333"))
        rows=candles(243)
        with patch("lab.engine.evaluate_signal",return_value=(70,None)):
            metrics,trades=simulate(rows,features(243),240,243,500,.0075,.004,.0005,
                dict(PARAMS,min_net_rr=1.5))
        self.assertFalse(trades)
        self.assertGreater(metrics["signal_funnel"]["rejections"]["net_reward_too_small"],0)
    def test_fee_free_net_ratio_matches_target_and_short_math(self):
        for values in ((100,98,104,"LONG"),(100,102,96,"SHORT")):
            q=net_payoff(*values[:3],0,0,values[3])
            self.assertEqual(q["net_rr"],2)
            self.assertAlmostEqual(q["break_even_win_rate"],1/3)
    def test_loss_cooldown_resets_after_nonnegative_close(self):
        p={"loss_streak_limit":3,"loss_cooldown_hours":6,"cooldown_minutes":15}
        self.assertEqual(cooldown_minutes([-1,-1],p),15)
        self.assertEqual(cooldown_minutes([-1,-1,-1],p),360)
        self.assertEqual(cooldown_minutes([-1,-1,-1,0],p),15)
        self.assertEqual(cooldown_minutes([-1,-1,-1,2],p),15)
    def test_cooldown_uses_only_losses_already_closed(self):
        rows=candles(300)
        for row in rows[241:]: row.update(open=100,high=100.1,low=97,close=100)
        params=dict(PARAMS,loss_streak_limit=3,loss_cooldown_hours=6)
        with patch("lab.engine.evaluate_signal",return_value=(70,None)):
            _,trades=simulate(rows,features(len(rows)),240,len(rows),500,.0075,0,0,params)
        self.assertGreaterEqual(len(trades),4)
        self.assertLess(trades[2]["entry_ts"]-trades[1]["exit_ts"],6*3600000)
        self.assertGreaterEqual(trades[3]["entry_ts"]-trades[2]["exit_ts"],6*3600000)
    def test_shared_paper_cooldown_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/"paper.sqlite3"
            agent=ContinuousLearner(db,Path(directory)/"data")
            agent.configure({"validated_only":False,"fee_rate":0,"slippage_rate":0})
            agent.runtime["running"]=True
            pid="BTC-USD"; agent.market[pid]=agent._empty_market()
            p=profit_candidates()[0]
            choice={"params":p,"adjusted_score":70,"raw_score":70,"learned":{}}
            now=NOW*1000
            with patch("lab.continuous.time.time",return_value=NOW),patch("lab.continuous.now_ms",return_value=now):
                for i in range(3):
                    agent.cooldown_until[pid]=0  # advance through prior ordinary cooldowns in this fixture
                    agent.market[pid]["ticker"]={"product_id":pid,"price":100,"best_bid":99.99,"best_ask":100.01,"ts":now}
                    position=agent._open_position(pid,100,{"_atr":2,"_close":100},{"key":"quality"},choice,"experiment")
                    self.assertIsNotNone(position)
                    agent._close_position(pid,97,now,"STOP")
                self.assertEqual(agent.cooldown_until[pid],NOW+6*3600)
                restored=ContinuousLearner(db,Path(directory)/"data")
                self.assertEqual(restored.cooldown_until[pid],NOW+6*3600)
            agent.stop()

class BookTests(unittest.TestCase):
    def setUp(self):
        self.plan={"payload":{"product_id":"BTC-USD"},"base_size":"1","max_entry_price":"100.05"}
        self.book={"product_id":"BTC-USD","ts":NOW,
            "asks":[{"price":"100.01","size":".4"},{"price":"100.03","size":".6"}],
            "bids":[{"price":"100","size":".4"},{"price":"99.98","size":".6"}]}
    def test_vwap_uses_quantity_at_each_level(self):
        result=book_execution(self.plan,self.book,DEFAULTS,NOW)
        self.assertEqual(D(result["entry_vwap"]),D("100.022"))
        self.assertEqual(D(result["immediate_exit_vwap"]),D("99.988"))
    def test_depth_outside_entry_limit_is_not_counted(self):
        self.book["asks"][1]["price"]="100.10"
        with self.assertRaisesRegex(BrokerError,"asks depth"):
            book_execution(self.plan,self.book,DEFAULTS,NOW)
    def test_bad_exit_liquidity_or_stale_book_blocks_entry(self):
        self.book["bids"][1]["price"]="99"
        with self.assertRaisesRegex(BrokerError,"bids depth"):
            book_execution(self.plan,self.book,DEFAULTS,NOW)
        with self.assertRaises(BrokerError):
            book_execution(self.plan,self.book,DEFAULTS,NOW+11)
    def test_adapter_rejects_unsorted_duplicate_nonfinite_and_wrong_books(self):
        sdk=MagicMock(); adapter=CoinbaseAdapter(sdk=sdk,clock=lambda:NOW)
        base={"product_id":"BTC-USD","time":"2026-09-08T00:00:00Z",
              "bids":[{"price":"100","size":"1"},{"price":"99.99","size":"1"}],
              "asks":[{"price":"100.01","size":"1"}]}
        from lab.coinbase_broker import iso
        base["time"]=iso(NOW)
        sdk.get_product_book.return_value={"pricebook":base}
        with patch("lab.coinbase_broker.time.sleep"):
            self.assertEqual(adapter.book("BTC-USD")["bids"][0]["size"],"1")
            for bad in (dict(base,product_id="ETH-USD"),dict(base,time=iso(NOW-11)),
                        dict(base,bids=list(reversed(base["bids"]))),
                        dict(base,asks=[{"price":"100.01","size":"NaN"}]),
                        dict(base,bids=[base["bids"][0],base["bids"][0]])):
                sdk.get_product_book.return_value={"pricebook":bad}
                with self.subTest(book=bad):
                    with self.assertRaises(BrokerError): adapter.book("BTC-USD")

class LiveQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/"test.sqlite3"; init_continuous_db(self.db)
        self.broker=FakeBroker()
        self.now=NOW
        def signal(pid):
            if pid!="BTC-USD": raise ValueError("No signal")
            return dict(copy.deepcopy(SIGNAL),params=profit_candidates()[0])
        self.agent=SimpleNamespace(settings=dict(DEFAULTS,fee_rate=.001),coinbase_signal=signal)
        self.trader=CoinbaseTrader(self.db,self.agent,self.broker,clock=lambda:self.now)
        self.trader.mode="live"
    def tearDown(self):
        self.trader.stop(); self.tmp.cleanup()
    def test_liquidity_disappearing_during_preview_prevents_create(self):
        book=self.broker.book("BTC-USD")
        thin=copy.deepcopy(book); thin["asks"][0]["size"]=".000001"
        with patch.object(self.broker,"book",side_effect=[book,thin]):
            self.trader.tick()
        self.assertFalse(self.broker.submissions)
        self.assertIsNone(self.trader._active())
    def test_live_loss_cooldown_survives_restart_and_expires_without_new_close(self):
        params=profit_candidates()[0]
        for i in range(3):
            trade={"id":str(i),"signal_key":str(i),"created_at":NOW-4000+i*1000,"closed_at":NOW-3000+i*1000,
                   "product_id":"BTC-USD","status":"CLOSED","pnl":"-1","plan":{"signal":{"params":params}}}
            self.trader._trade_save(trade)
        self.trader.tick()
        self.assertFalse(self.broker.submissions)
        recovered=CoinbaseTrader(self.db,self.agent,self.broker,clock=lambda:self.now)
        recovered.mode="live"; recovered.tick()
        self.assertFalse(self.broker.submissions)
        self.now=NOW+6*3600
        with patch.object(self.broker,"book",side_effect=lambda pid:dict(FakeBroker.book(self.broker,pid),ts=self.now)):
            recovered.tick()
        self.assertEqual(len(self.broker.submissions),1)
