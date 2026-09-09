"""Combined-rule behavior with artificial fixtures, not profitability evidence."""
import itertools
import unittest
from unittest.mock import patch

from lab.continuous import DEFAULTS
from lab.engine import evaluate_signal
from lab.execution import simulate
from lab.research import research
from lab.strategies import profit_candidates
from tests.test_execution import candles


class ConsensusTests(unittest.TestCase):
    def setUp(self):
        self.params = next(p for p in profit_candidates() if p["family"]=="signal_consensus_simple")
        self.feature = {"regime":"BULL", "breakout":True, "volume_z":1,
            "rsi":60, "_trend_long":True, "obv_slope":.1,
            "atr_regime":1, "range_expansion":1, "_atr":1, "_close":100}

    def test_all_confirmation_combinations_require_two_of_three(self):
        for trend, momentum, volume in itertools.product((False,True),repeat=3):
            f = dict(self.feature,_trend_long=trend,rsi=60 if momentum else 50,
                     obv_slope=.1 if volume else 0)
            with self.subTest(trend=trend,momentum=momentum,volume=volume):
                score, reason = evaluate_signal(f,self.params)
                if sum((trend,momentum,volume)) >= 2:
                    self.assertIsNotNone(score)
                    self.assertIsNone(reason)
                else:
                    self.assertIsNone(score)
                    self.assertEqual(reason,"insufficient_consensus_confirmation")

    def test_confirmations_cannot_bypass_trigger_or_guards(self):
        for changes in ({"breakout":False}, {"regime":"BEAR"}, {"volume_z":-.1},
                        {"rsi":79}, {"atr_regime":3}, {"range_expansion":5}):
            with self.subTest(changes=changes):
                self.assertIsNone(evaluate_signal(dict(self.feature,**changes),self.params)[0])
        stricter = dict(self.params,volume_z_min=.5)
        self.assertIsNone(evaluate_signal(dict(self.feature,volume_z=.49),stricter)[0])
        self.assertIsNotNone(evaluate_signal(dict(self.feature,volume_z=.5),stricter)[0])

    def test_shared_execution_uses_next_bar_and_net_cost_gate(self):
        rows = candles(244)
        rows[242].update(high=104.,close=103.)
        fs = [None]*len(rows)
        fs[240] = dict(self.feature,_ts=rows[240]["ts"])
        metrics, trades = simulate(rows,fs,240,len(rows),500,.0075,0,0,self.params)
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]["entry_ts"],rows[241]["ts"])
        self.assertEqual(trades[0]["strategy_family"],self.params["family"])
        self.assertGreater(metrics["net_pnl"],0)
        # Identical prices can become ineligible once entry and exit fees are modeled.
        _, costly = simulate(rows,fs,240,len(rows),500,.0075,.004,0,self.params)
        self.assertFalse(costly)
        fs[240]["breakout"] = False
        _, no_trigger = simulate(rows,fs,240,len(rows),500,.0075,0,0,self.params)
        self.assertFalse(no_trigger)

    def test_losing_consensus_holdout_cannot_change_the_frozen_winner(self):
        rows = candles(5000)
        calls = []
        def fake_sim(data, fs, start, end, balance, risk, fee, slip, params):
            calls.append((start,end,dict(params)))
            combined = params["family"]=="signal_consensus_simple"
            payoff = (-1. if combined else 5.) if start >= 4000 else (2. if combined else 1.)
            trades = [{"r_multiple":payoff,"pnl":payoff,"entry_ts":rows[start]["ts"],
                "exit_ts":rows[end-1]["ts"],"direction":"LONG",
                "strategy_family":params["family"],"reason":"TEST"} for _ in range(40)]
            return {"net_pnl":40*payoff,"return_pct":8*payoff,"trades":40,
                    "expectancy_r":payoff,"max_drawdown_pct":8 if payoff<0 else 0}, trades
        with patch("lab.research.simulate",side_effect=fake_sim), patch(
                "lab.research.build_feature_cache",return_value={"features":[{}]*len(rows)}):
            result = research(rows,"BTC-USD",dict(DEFAULTS))
        self.assertEqual(result["selected_params"]["family"],"signal_consensus_simple")
        self.assertFalse(result["validated"])
        self.assertLess(result["holdout"]["net_pnl"],0)
        self.assertTrue(all(p["family"]=="signal_consensus_simple" for start,end,p in calls if start>=4000))
        records = result["candidate_selection"]["candidates"]
        self.assertEqual(len(records),16)
        self.assertEqual(sum(r["selection_status"]=="selected" for r in records),1)
        self.assertTrue(all(r["training"]["net_pnl"]>0 for r in records))


if __name__ == "__main__":
    unittest.main()
