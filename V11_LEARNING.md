# V11 learning design and evidence

The main workflow now trains and operates a small adaptive trading policy automatically. The delivery includes source code and offline behavior checks. It does not include a pretrained profitable model, downloaded years of Coinbase candles, or a connected trading account.

## The learning loop

1. Load up to 30 active Coinbase USD markets, ranked by current trading volume.
2. Request up to 1,095 days of completed candles for the first five markets. Newer listings may supply less history. Missing or invalid candles cause the affected research run to fail rather than being filled with invented data.
3. Collect hypothetical resolved examples for 16 candidate rules across four families, using only the development portion of the history.
4. Learn an estimated net result in R for each candidate from the features present at entry. One R is that trade's modeled initial stop risk after costs.
5. Test the whole adaptive policy on three later expanding windows and a final 20% holdout, with 24-hour gaps before tests. Models make decisions before learning their outcomes; outcomes become available only at the exit candle's end.
6. Replay the final period with 50% higher modeled fees and slippage. Also show a diagnostic replay with learning disabled. That diagnostic cannot replace the updating policy or change qualification.
7. Install a model only if it passes the historical checks, then update the paper model from actual completed paper trades. The separately enabled Coinbase runner updates a separate model from settled real trades.

Each policy test starts with a $500 account and one position. Results for different coins or windows are not added into a fictitious combined return. The system's simultaneous multi-market paper account still needs forward observation.

## What the model learns

Each of the 16 candidates has its own small regularized linear model of net R. A stochastic-gradient update adjusts its weights after an observed outcome. Feature scales are fixed in code, so future test data cannot influence normalization.

The 13 inputs include a constant, RSI, quote-volume z-score, ADX, ATR percentage, 20- and 50-bar momentum, a quote-volume OBV proxy, candle-volume pressure, close location within the candle, two regime flags, and candle-range expansion. All are taken from the entry signal candle. Model forecasts are estimates, not calibrated win probabilities or promises of profit.

The trade templates stay defined in advance. Learning changes which eligible template and stop/target variant is preferred under current conditions. It does not write new Python code, use a language model, ingest social media, or bypass price, cost, and account-risk controls.

A candidate needs at least 30 completed training observations, a positive exponentially weighted recent result, and an estimated net result of at least 0.10 R to be eligible. The best qualifying estimate wins. These thresholds are engineering hypotheses, not empirically established optimums.

Learning uses bounded influence: the target R and individual gradient errors are clipped to ±3, model weights are bounded, and a small regularizer limits growth. The learning rate declines with observations but has a floor so later feedback still matters. Full losses remain in cash, realized net R, drawdown, and daily risk bookkeeping; only their influence on a single model update is capped. Recent performance uses an exponential update weight of 0.03.

A loss can reduce preference for a similar setup; a success can increase it. Neither result identifies the cause of a trade, and repeatedly updating a weak model can make performance worse. The changing-policy tests therefore also report deterioration relative to the frozen diagnostic.

## Training examples versus account returns

The development labels are independently funded hypothetical trades from each template. This lets the system continue studying later conditions even if a hypothetical template depleted its cash earlier. Their profit totals are not reported as a tradable portfolio. Samples from similar templates can overlap and are correlated.

All later policy evaluations use ordinary $500 account accounting, modeled fees on both sides, slippage, the assumed 0.05% half-spread, position sizing, and the existing entry/exit rules. They do not reset capital after each trade. They allow no same-candle reentry, apply stop-first ambiguity handling, and assume adverse fills through stop gaps.

The historical gate requires at least two positive later windows, at least 30 final-period trades, positive ordinary and stressed final-period net P&L, a positive lower bootstrap bound on mean net R, and final-period drawdown no greater than 15%. The interval is conditional on the chosen learning procedure and sample; it is not a probability of future profit or a complete correction for overfitting.

## Feedback and saved state

Entry vectors and parameters are saved with each paper trade and Coinbase plan. A journal transition to CLOSED and its learning update use the same database transaction. Repeated polling, reconciliation, and restart cannot teach that same closure twice. A database failure rolls both changes back; malformed model feedback is recorded as a learning error without preventing the financial journal from closing.

Paper and Coinbase forward model states are separate. Coinbase feedback uses confirmed net P&L after fees, divided by the planned stop-risk amount. Preview-only results do not train the real model. Changed fee signatures prevent an old-cost result from training a model for different costs.

Both channels initially use the qualified historical model. Saved forward updates survive ordinary process restarts. After a new historical review produces a different model fingerprint, that newly trained model seeds the channel's next use. Previous trade records and reports remain saved; old forward updates are not blindly replayed as if they were independent new examples.

The controller requests a new review every 28 days while running and notices changes to tested settings. Download errors can be retried after an hour. A successful result or rejection is reused until the next review; identical downloaded data and settings reuse the same saved research result. Restarts do not start the controller or exchange trading automatically.

Repeated reviews can use overlapping history and test periods. They are maintenance checks, not successive independent experiments. Selecting today's liquid assets also introduces survivorship and universe-selection bias. Reserve genuinely new prices and examine the forward account journal before drawing conclusions about improvement.

## Controls and practical limits

The paper risk budget remains 0.75% of equity per qualified trade, with 3% total open stop risk, 30% per-position allocation, 100% total allocation, and a daily entry halt at 3% loss from that UTC day's equity baseline. Gaps can exceed these modeled risk amounts. Existing positions remain monitored when entries are paused.

The main button uses paper money. Coinbase remains separately configured and activated, with its original one-position and capital limits. Native protective orders, preview requirements, price caps, order-book checks, settlement reconciliation, and local live opt-in remain in place. Changing model weights does not raise these limits to chase the daily goal.

Three years of 15-minute data is 105,120 candles per coin. A flat artificial dataset of that size completed the learning pipeline in about 5.5 seconds and peaked near 216 MiB in this workspace. It generated zero trade examples and was rejected. This is a compute check, not a market study or a cloud sizing guarantee. Actual downloads, active-trade datasets, concurrent monitoring, and hosted hardware can take longer and use more memory.

The small Render service is an initial configuration. Monitor memory on real multi-year runs and select a larger instance if needed. Do not increase worker or instance count for one trading account.

## Primary references

- [Scikit-learn: stochastic gradient descent](https://scikit-learn.org/stable/modules/sgd.html) documents incremental regularized linear learning. V11 uses a small custom implementation with its own bounded update rule; it does not claim to reproduce a particular library estimator.
- [River: model evaluation](https://riverml.xyz/dev/recipes/model-evaluation/) and [delayed online evaluation](https://maxhalford.github.io/blog/online-learning-evaluation/) explain predicting before learning and respecting when outcomes become available. V11 applies that principle at trade closure.
- [Scikit-learn: time-series splits](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html) explains preserving time order in evaluation. V11 implements its own windows and a 24-hour purge.
- [Coinbase Exchange candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles) documents the public history interface used by the existing downloader. Advanced Trade order integration remains separate.
- [CFTC: AI trading-bot advisory](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/AITradingBots.html) explains why automated or AI-labeled trading cannot support guaranteed-return claims.

The $10–$15 daily target is 2–3% of the starting $500. No result in this delivery demonstrates that level of daily return, a positive expected live return, or an improvement over V10 on actual market data.
