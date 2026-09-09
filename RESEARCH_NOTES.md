# Crypto strategy research for a $500 account

Original research prepared 7 September 2026 for V7. V10 adds four combined-signal candidates; see V10_UPGRADES.md for the source, exact rules, and evidence limits. The original 12-candidate population below remains as research history. V9 rationale and sources checked 8 September 2026 are in V9_UPGRADES.md. Earlier V7 references below describe that research foundation.

The objective is to improve the chance of positive net returns while making failure visible. The research supports testing a few hypotheses and execution assumptions. It does not establish that this particular bot has an edge, and it does not establish a dependable $10–$15 daily income from $500.

No real V7 historical-performance run was completed during this upgrade. The supplied archive contained no raw market history or running account database, and direct market-data downloads were blocked in the development environment. Synthetic correctness tests are not trading results.

## What the evidence supports

| Primary source | Finding and scope | Implementation choice |
| --- | --- | --- |
| [Liu and Tsyvinski: Risks and returns of cryptocurrencies](https://cepr.org/voxeu/columns/risks-and-returns-cryptocurrencies) | The authors report momentum and attention effects in their historical sample; their weekly evidence does not demonstrate a five-minute edge. | Include a trend hypothesis, then test it on the chosen interval. No attention feed is claimed. |
| [Svogun and Bazán-Palomino: Technical analysis, costs and bubbles](https://faculty.up.edu.pe/en/publications/technical-analysis-in-cryptocurrency-markets-do-transaction-costs/) | Their 2016–2021 analysis studies 69 moving-average and breakout rules at daily and one-minute frequencies. Relative performance depends on the market, bubble periods, and trading costs. | Include distinct pullback and breakout hypotheses, model fees, and compare with cash and buy-and-hold. This is not a replication of the paper. |
| [Wen and colleagues: Intraday return predictability in cryptocurrency markets](https://www.sciencedirect.com/science/article/abs/pii/S1062940822000833) | The publisher abstract reports intraday momentum and reversal in Bitcoin over 2013–2020, with behavior varying with market conditions. Only the abstract was accessible for this review. | Add a separate range-reversal hypothesis rather than applying one momentum rule everywhere. The exact thresholds are our test choices. |
| [Gort and colleagues: Deep Reinforcement Learning for Cryptocurrency Trading—Practical Approach to Address Backtest Overfitting](https://arxiv.org/html/2209.05559v6) | The authors analyze overfitting in crypto reinforcement-learning agents and propose rejection based on estimated overfitting. Their reported test period is short and historical. | Make rejection possible, reserve later data, and avoid presenting model complexity as proof. V7 does not implement their reinforcement-learning model or probability-of-backtest-overfitting procedure. |
| [Bailey: How backtest overfitting leads to false discoveries](https://mathinvestor.org/2022/01/how-backtest-overfitting-in-finance-leads-to-false-discoveries/) | The author explains how repeated strategy searches can manufacture attractive historical results. | Limit the candidate set, record tested settings, cache identical runs, and preserve an untouched holdout within each run. Repeated user retuning remains a risk. |

These implementation choices are inferences from the evidence, not findings that the cited authors tested this program.

## The three hypotheses

1. Trend pullback: seek a pullback in a bullish regime instead of chasing any rising candle. Require moderate RSI and reject strongly negative candle-volume pressure.
2. Volume breakout: require a close above the preceding 55-bar high, volume confirmation, and a non-bearish regime. Reject extreme overextension.
3. Range reclaim: require a ranging regime, a sweep and reclaim of a low, and a substantial lower wick. This is a deliberately testable reversal rule, not evidence of institutional intent.

Each has four fixed stop/target configurations, for 12 candidates total. Parameters such as 55 bars, ATR multipliers, RSI limits, a 12-hour time stop, and a 15-minute cooldown are initial hypotheses. They have not been tuned to demonstrate a claimed return.

The default decision interval is 15 minutes. Faster trading can create more cost-paying opportunities without creating more edge; V7 must establish its own evidence on each interval. This is an engineering reason for starting with a modest candidate set, not a claim that 15 minutes is universally most profitable.

Grid averaging, martingale loss recovery, leverage, token-launch speculation, and uncalibrated social-media signals are not implemented in the new population. Adding them would change the strategy and risk model and require separate validation. The older technical-structure modules remain available as source for future research.

## Costs can consume a small account's edge

Coinbase says its Advanced fees depend on maker/taker status and the account's fee tier. Immediate fills can incur taker fees even for a limit order; submitting a limit order does not by itself justify a maker-fee assumption. The actual tier must be checked in the account. [Coinbase Advanced fee documentation](https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees)

The following figures are arithmetic examples, not current quoted exchange fees. They approximate a buy and sell of $500 of notional at an unchanged market price, before spread or slippage:

| Assumed fee per side | Approximate round-trip fees |
| --- | ---: |
| 0.10% | $1.00 |
| 0.25% | $2.50 |
| 0.40% | $4.00 |
| 0.60% | $6.00 |
| 0.80% | $8.00 |

V7's default 0.40% per-side fee is an unconfirmed placeholder. Historical replay additionally assumes 0.05% slippage and 0.05% half-spread per fill. Together those assumptions are approximately 1% round-trip cost before any price move, or about $1.50 on a $150 position. Actual exit notional changes the exact amount.

If a price stop is too narrow relative to these costs, the trade is rejected. Raising a fee estimate can correctly reduce the number of trades to zero.

Lower verified costs can improve net performance without inventing a stronger signal. That observation alone does not justify moving funds to another exchange. Availability, the user's account tier, execution quality, withdrawal costs, and eligible products must be established before making an exchange comparison.

## What the daily goal means

- $10 on $500 is 2% per day; $15 is 3%.
- The bot records the mean and median realized holdout P&L per calendar day, losing days, worst day, and the fraction of days reaching each dollar goal.
- No-trade days are included. Reporting only active days would overstate everyday earning potential.
- Profits are evaluated net of modeled trading costs, before taxes and hosting.
- A backtest average is neither a reliable daily payment nor a lower bound on returns.
- With capped position sizes and no leverage, smaller or infrequent profits may be the realistic outcome; sustained losses or no qualifying strategy are also valid outcomes.

## How the validation limits false confidence

The researcher chooses rules using earlier data, freezes those rules, and evaluates them in later windows. The final 20% is kept outside final parameter selection. A 24-hour purge exceeds the new strategies' 12-hour maximum planned holding time. Every test starts with $500, so the program does not sum separately reset account returns.

A historical profile requires positive base and 1.5× cost-stressed holdout results, sufficient trades, at least two profitable walk-forward windows, acceptable holdout drawdown, and a positive lower bound for historical mean net R under a moving-block bootstrap.

Those thresholds are screening choices, not a formal guarantee or a statistically calibrated probability of profitability. The bootstrap interval is conditional on the selected strategy and observed sample. It does not fully correct for strategy selection, testing several assets, fat tails, changing regimes, or repeatedly reusing similar holdouts. V7 does not claim a deflated Sharpe ratio or a formal probability-of-backtest-overfitting estimate.

A passing profile is allowed to proceed to paper testing. It is not approved for funded execution. Portfolio interactions, daily entry halts, signal timing, live quotes, and exchange execution must still be evaluated together on new data.

## Market-data and execution corrections

Coinbase limits a candle response to 300 buckets and may omit intervals without trades. V7 paginates, excludes unfinished candles, checks timestamps and OHLC ranges, and refuses gaps. Its 4-hour context is assembled only from four complete hourly candles. [Coinbase candle API](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)

Coinbase's ticker feed can batch matching activity. A ticker's last trade size is therefore unsuitable for reconstructing full candle volume. V7 obtains authoritative OHLCV through REST and uses tickers for fresh prices and simulated exits. Candle-derived volume pressure is still a proxy, not a measured order-book imbalance. [Coinbase WebSocket channels](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels)

Historical stop/target ordering is unknowable from OHLC alone. V7 assumes the stop is hit first in ambiguous candles, handles stops on entry candles, uses adverse gap fills, and deducts both entry and exit fees. Historical fills remain simplified: no partial fills, queue simulation, exchange size increments, or historical order-book replay.

## The next evidence required

Run the packaged historical researcher using actual market data and the user's verified fee tier. Retain rejected results as well as attractive ones. Freeze a candidate before collecting forward paper results. Evaluate the complete account, including losing periods, missed entries, disconnections, fees, and concurrent positions.

If nothing qualifies, the evidence supports remaining in cash while revising the hypothesis against genuinely new validation data. It does not support removing the filters merely to make the bot active.

Before a funded adapter can be implemented correctly, the exchange and account product must be identified. Its order constraints, available protective orders, partial fills, client-order identifiers, and reconciliation behavior need implementation and testing.

Coinbase's Advanced Trade sandbox returns predefined mocked responses. It can help test request/response handling; it cannot establish realistic fills or profitability. [Coinbase sandbox documentation](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox)
