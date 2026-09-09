"""Deterministic execution regressions. Fixtures are NOT market evidence."""
import copy
import unittest
from unittest.mock import patch
from lab.execution import simulate
from lab.engine import build_feature_cache


def candles(n=260, step=900000, start=1609459200000):
    return [{"ts": start+i*step, "open":100., "high":100.2, "low":99.8,
             "close":100., "volume":10., "quote_volume":1000., "trades":10} for i in range(n)]


def features(n=260, atr=2):
    return [{"_atr":atr,"_close":100., "regime":"CHOP","signal_index":i} if i >= 240 else None for i in range(n)]


PARAMS = {"family":"test_fixture","direction":"LONG","stop_atr":1,"rr2":2,
          "max_gap_atr":10,"max_cost_r":5,"max_notional_fraction":.3,
          "time_stop_hours":12,"cooldown_minutes":15}


class ExecutionTests(unittest.TestCase):
    def run_sim(self, rows, fee=0, slip=0, risk=.01, params=None, start=240, end=None):
        with patch("lab.engine.evaluate_signal", return_value=(70,None)):
            return simulate(rows, features(len(rows)), start, end or len(rows),
                            500,risk,fee,slip,params or PARAMS)

    def test_closed_signal_fills_next_candle(self):
        rows = candles(243)
        _, trades = self.run_sim(rows)
        self.assertEqual(trades[0]["entry_ts"], rows[241]["ts"])

    def test_entry_candle_stop_is_not_skipped(self):
        rows = candles(243)
        rows[241].update(low=97.,close=98.)
        _, trades = self.run_sim(rows)
        self.assertEqual(trades[0]["reason"],"STOP")
        self.assertEqual(trades[0]["exit_ts"],rows[241]["ts"])
        self.assertAlmostEqual(trades[0]["r_multiple"],-1.)

    def test_ambiguous_entry_candle_assumes_stop_first(self):
        rows = candles(243)
        rows[241].update(high=105.,low=97.)
        _, trades = self.run_sim(rows)
        self.assertEqual(trades[0]["reason"],"STOP")
        self.assertLess(trades[0]["pnl"],0)

    def test_stop_gap_fills_at_worse_open(self):
        rows = candles(244)
        rows[242].update(open=90.,high=91.,low=89.,close=90.)
        _, trades = self.run_sim(rows)
        self.assertEqual(trades[0]["reason"],"STOP_GAP")
        self.assertEqual(trades[0]["exit"],90.)
        self.assertLess(trades[0]["r_multiple"],-1.)

    def test_costs_make_a_flat_trade_a_loss(self):
        metrics, trades = self.run_sim(candles(243),fee=.004,slip=.001)
        trade = trades[0]
        expected = (trade["exit"]-trade["entry"])*trade["qty"] - (trade["entry"]+trade["exit"])*trade["qty"]*.004
        self.assertAlmostEqual(trade["pnl"],expected)
        self.assertAlmostEqual(metrics["ending_balance"],500+expected)
        self.assertGreater(trade["fees_paid"],0)
        self.assertLess(metrics["net_pnl"],0)

    def test_notional_cap_reports_actual_risk(self):
        params = dict(PARAMS,stop_atr=.1)
        with patch("lab.engine.evaluate_signal",return_value=(70,None)):
            _, trades = simulate(candles(243),features(243),240,243,500,.02,0,0,params)
        self.assertLessEqual(trades[0]["qty_initial"]*trades[0]["entry"],150.000001)
        self.assertAlmostEqual(trades[0]["risk_dollars"],.3)
        self.assertNotEqual(trades[0]["risk_dollars"],10)

    def test_no_reentry_at_open_of_exit_bar(self):
        rows = candles(245)
        rows[242].update(high=105.)
        _, trades = self.run_sim(rows,params=dict(PARAMS,cooldown_minutes=0))
        self.assertEqual(trades[0]["exit_ts"],rows[242]["ts"])
        self.assertTrue(all(t["entry_ts"] != rows[242]["ts"] for t in trades))

    def test_cooldown_blocks_immediate_reentry(self):
        rows = candles(247,step=300000)
        rows[241].update(low=97.)
        _, trades = self.run_sim(rows)
        self.assertGreaterEqual(trades[1]["entry_ts"],rows[245]["ts"])

    def test_simulation_does_not_read_past_end(self):
        rows = candles(250)
        a = self.run_sim(rows,end=243)
        rows[243:].clear()
        b = self.run_sim(rows,end=243)
        self.assertEqual(a,b)

    def test_gap_and_cost_gates_reject_bad_entries(self):
        rows = candles(243)
        rows[241].update(open=110.,high=111.,low=109.,close=110.)
        metrics, trades = self.run_sim(rows,params=dict(PARAMS,max_gap_atr=.5))
        self.assertFalse(trades)
        self.assertEqual(metrics["signal_funnel"]["rejections"]["entry_gap_too_large"],2)
        metrics, trades = self.run_sim(candles(243),fee=.02,params=dict(PARAMS,max_cost_r=.1))
        self.assertFalse(trades)
        self.assertGreater(metrics["signal_funnel"]["rejections"]["trading_cost_too_high"],0)

    def test_zero_drawdown_and_no_loss_metrics_are_finite(self):
        import json
        rows = candles(243)
        rows[241].update(high=105.)
        metrics, trades = self.run_sim(rows)
        self.assertIsNone(metrics["profit_factor"])
        self.assertEqual(metrics["max_drawdown_pct"],0)
        json.dumps(metrics,allow_nan=False)


class FeatureTests(unittest.TestCase):
    def test_latest_completed_candle_has_features(self):
        rows = candles(241)
        result = build_feature_cache(rows,"15m")["features"]
        self.assertIsNotNone(result[-1])
        self.assertEqual(result[-1]["_ts"],rows[-1]["ts"])

    def test_future_candles_cannot_revise_past_features(self):
        import math
        rows = candles(350)
        for i, r in enumerate(rows):
            r.update(close=100+math.sin(i/3),high=102+math.sin(i/3),low=98+math.sin(i/3))
        before = build_feature_cache(rows[:280],"15m")["features"]
        after = build_feature_cache(rows,"15m")["features"]
        self.assertEqual(before,after[:280])

    def test_base_volume_is_used_when_quote_volume_missing(self):
        rows = candles(260)
        for i,r in enumerate(rows):
            r["volume"] += i % 7
            r["quote_volume"] = r["volume"]*r["close"]
        a = build_feature_cache(rows,"15m")
        for r in rows: del r["quote_volume"]
        b = build_feature_cache(rows,"15m")
        self.assertEqual(a,b)
