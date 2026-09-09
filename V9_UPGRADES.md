# V9: testable changes aimed at improving returns after costs

Prepared 8 September 2026. This version adds execution hypotheses, not a verified increase in profit. No real historical comparison or funded account test was completed here.

## Why these changes

Coinbase charges according to maker/taker status and the account's fee tier at the time of the order. A limit order that immediately takes liquidity can still pay the taker fee. V9 continues to use the synced taker fee conservatively and does not assume an unverified maker rebate or lower fee. [Coinbase Advanced fees](https://help.coinbase.com/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees)

A recent working paper examines hourly Bitcoin forecasting with chronological walk-forward tests. It reports that some gross predictive performance disappears after costs and that cost-aware trade selection can help in selected configurations. Its models, exchange data and test assumptions differ from this program, and it does not demonstrate dependable daily profits. Our changes are an engineering inference from that distinction, not a replication of the authors' results. [Bysik and Ślepaczuk, Machine Learning-Based Bitcoin Trading Under Transaction Costs](https://arxiv.org/abs/2606.00060)

## 1. Potential reward after costs

The original stop-distance-to-target ratio can look attractive while costs make the net payoff poor. V9 calculates:

- Modeled loss: entry cost plus entry commission, minus proceeds at a slipped stop exit after exit commission.
- Potential target gain: proceeds at a slipped target exit after exit commission, minus entry cost and commission.
- Net reward/risk: that potential gain divided by modeled loss.

Each of the 12 fixed candidates requires a net ratio of at least 1.5. The same rule is applied to research, paper entries and Coinbase plans. Coinbase uses exchange-rounded prices and Decimal arithmetic.

The number 1.5 is a predeclared hypothesis, not an optimized threshold. A high ratio cannot compensate for an unknown or sufficiently low win probability. Gaps can exceed the estimated stop loss. The displayed potential gain is conditional on reaching the target; it is not expected profit.

## 2. Liquidity before buying

Coinbase exposes bid and ask prices and sizes through its product-book endpoint. V9 requests 50 levels, validates identity, ordering, sizes and timestamp, then checks that the proposed quantity fits both the entry price limit and the current bid-side slippage allowance. It repeats the check after preview before creating an entry. [Product-book API](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product-book), [official Python SDK](https://coinbase.github.io/coinbase-advanced-py/coinbase.rest.html)

The check can avoid attempting entries during thin or changing liquidity. It does not guarantee a fill, forecast order flow, or establish that enough bids will remain when a future stop triggers. Visible orders can disappear. The exit manager continues its existing reconciliation and residual-close behavior even when a new-entry liquidity check would fail.

Depth is not available in the OHLC historical dataset, so the research report does not pretend to backtest this liquidity filter.

## 3. Cooldown after repeated losses

After three consecutive net losing closes in a market, V9 waits six hours after that close before another entry in that market. Subsequent consecutive losses trigger another six-hour wait. A nonnegative close resets the streak, restoring the normal 15-minute cooldown.

The rule uses resolved trades only; future outcomes cannot affect current entries. Coinbase and paper results are separate. Backtests wait from the exit candle's end because the exact intrabar exit time is unknown. Live and paper runners use their recorded close time. Existing positions remain monitored while entries wait.

The threshold of three and duration of six hours are unproven choices. A losing streak need not imply a strategy has stopped working. The pause can reduce exposure during poor conditions, but it can also miss a recovery. It never increases risk to recover losses.

## 4. A comparison that can show the changes made things worse

For the selected rule, the final holdout now includes two extra diagnostic replays: disable the net-payoff and loss-cooldown filters, run at ordinary costs, then run at 1.5 times modeled fees and slippage.

The report shows net P&L differences, stressed P&L differences, trade-count difference and drawdown difference. The dashboard shows both P&L differences, including negative values. These results never change the selected rule or its historical gate after viewing the holdout.

This is a comparison of one frozen rule with and without two filters. It is not a comparison of separately optimized V8 and V9 systems, a live portfolio simulation, or proof of statistical superiority. Choosing settings repeatedly after viewing it would contaminate the holdout.

## How to evaluate V9

1. Keep the previous ZIP and an online database backup.
2. Extract V9 separately. Preserve the same live journal for any known open orders; never discard order state during an upgrade.
3. Sync the actual Coinbase taker fee and apply it to research.
4. Rerun research. The new market-structure-v9.0 identity invalidates V7/V8 qualifying profiles; older reports remain readable.
5. Inspect net results, drawdown, rejected trades and the comparison. A negative difference is a legitimate outcome. Do not promote the version merely because its filters sound sensible.
6. Collect fresh forward paper observations before judging effectiveness. Real execution and liquidity still need separate verification.

All 88 offline code tests pass. They establish the specified software behavior on fixtures, not a crypto trading edge. The $10–$15/day goal on $500 remains unverified.
