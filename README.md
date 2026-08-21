# CryptO Research Lab

Historical research platform for CryptO Radar.

## Major capabilities
- historical Binance.US candle downloader
- no-lookahead next-candle execution
- expanding walk-forward validation
- transparent train-only "edge learner"
- controlled strategy-parameter search
- regime-specific performance
- dynamic slippage from candle volatility + estimated volume participation
- fees and risk-based sizing
- feature store for every resolved walk-forward test trade
- permanent experiment notebook with config hashes and notes
- Monte Carlo resampling of R-multiples
- $500 / 1% risk defaults

## Render
Build:
`pip install -r requirements.txt`

Start:
`gunicorn app:app --workers 1 --threads 4 --timeout 300`

## Important
This is research software, not proof of future profitability. Historical data can be incomplete, exchange microstructure changes over time, and OHLC bars cannot perfectly reconstruct intra-candle event order. The engine deliberately assumes the stop was hit first whenever a stop and target are both reachable in the same candle.

Use many symbols and market cycles, then confirm promising ideas with live paper trading.


## Accuracy / modeling upgrade

This build adds:
- corrected partial-exit P&L accounting
- targets recalculated from actual simulated fill price
- capped rolling chart lookback for dramatically faster backtests
- 96 controlled strategy variants instead of hundreds of near-duplicates
- purged/embargoed walk-forward testing between training and unseen periods
- higher-timeframe trend alignment derived without future data
- liquidity sweep, rejection, compression, resistance-room and range-position features
- regime-aware Bayesian-shrunk historical edge learning with uncertainty bounds
- pooled out-of-sample performance metrics
- data quality / missing-candle checks
- MFE/MAE and entry-gap execution diagnostics
- block-bootstrap Monte Carlo with 30% drawdown and half-account risk estimates
- real fold/candidate progress reporting

None of these changes guarantees profitability. They are intended to reduce backtest bias, expose weak assumptions and make any discovered edge harder to fake.
