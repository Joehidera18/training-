"""Data quality and chronological selection, using artificial test fixtures only."""
import copy
import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.coinbase_feed import CoinbaseClient, CoinbaseTickerStream
from lab.continuous import DEFAULTS, INTERVAL_MS, resample
from lab.research import research, daily_goal_report, validate_rows, ResearchManager
from lab.strategies import profit_candidates, simple_signal
from lab.paper_store import init_continuous_db
from tests.test_execution import candles


class MarketDataTests(unittest.TestCase):
    def test_four_hour_bars_require_all_four_closed_hours(self):
        rows = candles(8,step=3600000)
        end = rows[-1]["ts"]+3600000
        grouped = resample(rows,14400000,asof_ms=end)
        self.assertEqual(len(grouped),2)
        self.assertEqual(grouped[0]["volume"],40)
        self.assertEqual(len(resample(rows,14400000,asof_ms=end-1)),1)
        self.assertEqual(len(resample(rows[:2]+rows[3:],14400000,asof_ms=end)),1)

    def test_paginated_history_excludes_open_and_invalid_bars(self):
        client = CoinbaseClient()
        end = 1700000000000 // 3600000 * 3600000
        calls = []
        def fake_get(path, query):
            step = query["granularity"]
            start = int(datetime.datetime.fromisoformat(query["start"]).timestamp())
            stop = int(datetime.datetime.fromisoformat(query["end"]).timestamp())
            self.assertLessEqual((stop-start)//step,300)
            calls.append((start,stop))
            data = [[ts,99,101,100,100,10] for ts in range(start,stop+step,step)]
            data.append([start,99,101,100,100,10])
            data.append([start+1,99,101,100,100,10])
            return list(reversed(data))
        client._get = fake_get
        rows = client.candles("BTC-USD","1h",1040,end)
        self.assertEqual(len(rows),1040)
        self.assertEqual(len(calls),4)
        self.assertEqual(rows[-1]["ts"],end-3600000)
        self.assertTrue(all(b["ts"]-a["ts"]==3600000 for a,b in zip(rows,rows[1:])))

    def test_candle_filter_rejects_nonfinite_prices(self):
        client = CoinbaseClient()
        end = 1700000000000 // 900000 * 900000
        client._get = lambda *_: [[end//1000-900,99,float("inf"),100,100,10]]
        self.assertFalse(client.candles("BTC-USD","15m",1,end))

    def test_stream_deduplicates_and_validates_tickers(self):
        seen, messages = [], []
        stream = CoinbaseTickerStream(["BTC-USD"],seen.append,lambda *x:messages.append(x))
        payload = {"type":"ticker","product_id":"BTC-USD","price":"100","best_bid":"99.9","best_ask":"100.1",
                   "time":datetime.datetime.now(datetime.timezone.utc).isoformat(),"trade_id":20,"last_size":"999999"}
        stream._message(None,json.dumps(payload))
        stream._message(None,json.dumps(payload))
        stream._message(None,json.dumps(dict(payload,trade_id=19)))
        stream._message(None,json.dumps(dict(payload,trade_id=21,price="NaN")))
        self.assertEqual(len(seen),1)
        self.assertNotIn("volume",seen[0])
        stream._message(None,"{")
        self.assertTrue(messages)

    def test_market_selection_does_not_invent_products(self):
        client = CoinbaseClient()
        client.products = lambda: [{"id":"BTC-USD","quote_currency":"USD","base_currency":"BTC","status":"online"},
                                   {"id":"USDC-USD","quote_currency":"USD","base_currency":"USDC","status":"online"},
                                   {"id":"BAD-USD","quote_currency":"USD","base_currency":"BAD","status":"offline"}]
        client.stats = lambda pid: {"last":100,"volume":20}
        self.assertEqual(client.discover_top_usd(30),["BTC-USD"])
        client.products = lambda: []
        with self.assertRaises(RuntimeError): client.discover_top_usd()


class ResearchTests(unittest.TestCase):
    def test_data_rejects_gaps_duplicates_nonfinite_and_unfinished(self):
        rows = candles(3000)
        self.assertTrue(validate_rows(rows,"15m")["valid"])
        cases = []
        gap = copy.deepcopy(rows); gap[10]["ts"] += 900000; cases.append(gap)
        nan = copy.deepcopy(rows); nan[10]["close"] = float("nan"); cases.append(nan)
        future = copy.deepcopy(rows)
        for i,r in enumerate(future): r["ts"] = int(time.time()*1000)//900000*900000+i*900000
        cases.append(future)
        for values in cases:
            with self.subTest(case=cases.index(values)):
                with self.assertRaises(ValueError): validate_rows(values,"15m")

    def test_no_trade_days_are_included_in_goal_statistics(self):
        day = 1609459200000
        result = daily_goal_report([{"exit_ts":day,"pnl":15},{"exit_ts":day+86400000,"pnl":-3}],day,day+3*86400000)
        self.assertEqual(result["calendar_days"],4)
        self.assertEqual(result["mean_net_per_day"],3)
        self.assertEqual(result["days_at_least_10"],1)
        self.assertEqual(result["no_realized_pnl_days"],2)

    def test_candidate_set_is_small_distinct_and_long_only(self):
        candidates = profit_candidates()
        self.assertEqual(len(candidates),16)
        self.assertEqual(len({json.dumps(x,sort_keys=True) for x in candidates}),16)
        self.assertEqual({x["direction"] for x in candidates},{"LONG"})
        self.assertEqual(len({x["family"] for x in candidates}),4)

    def test_each_strategy_has_an_explicit_trigger_and_volatility_guard(self):
        base = {"atr_regime":1,"range_expansion":1,"regime":"BULL","_pullback_long":True,
                "rsi":50,"adx":20,"signed_volume_pressure":0,"volume_z":1,
                "breakout55":True,"breakout":True,"_trend_long":True,"obv_slope":.1,
                "sweep_low":True,"lower_wick":.5}
        for p in profit_candidates():
            f = dict(base,regime="CHOP") if p["family"]=="range_reclaim_simple" else base
            self.assertIsNotNone(simple_signal(f,p)[0])
            self.assertIsNone(simple_signal(dict(f,atr_regime=3),p)[0])

    def test_selection_never_uses_holdout_to_choose_parameters(self):
        rows = candles(5000)
        calls = []
        def fake_sim(data, fs, start, end, balance, risk, fee, slip, params):
            calls.append((start,end,dict(params),fee))
            # Artificial constant profits isolate the selection protocol, not returns.
            payoff = 1.25 if params.get("min_net_rr")==0 else 1.
            trades = [{"r_multiple":payoff, "pnl":payoff, "entry_ts":rows[start]["ts"],
                       "exit_ts":rows[end-1]["ts"], "direction":"LONG",
                       "strategy_family":params["family"],"reason":"TEST"} for _ in range(40)]
            return {"net_pnl":40*payoff,"return_pct":8*payoff,"trades":40,"expectancy_r":payoff,"max_drawdown_pct":0.}, trades
        with patch("lab.research.simulate",side_effect=fake_sim), patch("lab.research.build_feature_cache",return_value={"features":[{}]*len(rows)}):
            result = research(rows,"BTC-USD",dict(DEFAULTS))
        test_calls = [c for c in calls if c[0] >= 4000]
        self.assertEqual(len(test_calls),4)  # frozen rule and diagnostic ablation, each base + stress
        self.assertEqual(test_calls[0][2],test_calls[1][2])
        self.assertEqual(calls[-4:],test_calls)
        self.assertTrue(all(end <= 3904 for start,end,_,_ in calls[:-4]))
        self.assertEqual(test_calls[2][2],dict(test_calls[0][2],min_net_rr=0,loss_streak_limit=0))
        self.assertEqual(test_calls[2][2],test_calls[3][2])
        self.assertEqual(result["selected_params"],test_calls[0][2])
        self.assertFalse(result["upgrade_comparison"]["selection_uses_comparison"])
        self.assertEqual(result["upgrade_comparison"]["net_pnl_difference"],-10)
        self.assertEqual(result["candidate_count"],16)
        records = result["candidate_selection"]["candidates"]
        self.assertEqual(len(records),16)
        self.assertEqual(sum(r["validation"] is not None for r in records),5)
        chosen = [r for r in records if r["selection_status"]=="selected"]
        self.assertEqual([r["params"] for r in chosen],[result["selected_params"]])
        self.assertLess(result["candidate_selection"]["validation_end_ts"],result["holdout_start_ts"])

    def test_pipeline_can_reject_every_strategy_without_fabricated_trades(self):
        result = research(candles(3000),"BTC-USD",dict(DEFAULTS))
        self.assertFalse(result["validated"])
        self.assertIsNone(result["selected_params"])
        self.assertFalse(result["holdout_trades"])
        self.assertTrue(result["rejection_reasons"])
        self.assertTrue(all(r["selection_status"]=="failed_training"
                            for r in result["candidate_selection"]["candidates"]))
        json.dumps(result,allow_nan=False)

    def test_cancelled_download_preserves_completed_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/"test.sqlite3"; init_continuous_db(db)
            manager = ResearchManager(db,Path(tmp))
            def fake_candles(symbol,iv,limit,end):
                output = candles(limit,step=INTERVAL_MS[iv],start=end-limit*INTERVAL_MS[iv])
                manager.cancel()
                return output
            manager.client.candles = fake_candles
            with self.assertRaises(InterruptedError): manager._history("BTC-USD","15m",30)
            path = Path(tmp)/"history/BTC-USD_15m.csv"
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size,1000)
