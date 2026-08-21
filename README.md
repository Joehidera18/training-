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


## Resumable fast architecture

This build replaces long in-memory research threads with database-checkpointed steps.

- Feature calculations are precomputed once with O(N) rolling algorithms.
- Strategy search is reduced to 24 intentionally separated candidates instead of 96 near-duplicates.
- Every candidate is saved before the next one starts.
- Every fold is saved before advancing.
- Browser reloads automatically resume the active job.
- A Gunicorn/Render restart can resume from the last saved checkpoint as long as the SQLite database remains available.
- For guaranteed persistence across instance replacement, mount a Render persistent disk and set:
  `RESEARCH_DB_PATH=/var/data/research.sqlite3`
  `RESEARCH_DATA_DIR=/var/data/data`

This is research software, not a guarantee of profitable future trading.


## Signal funnel diagnostic upgrade

This version will no longer silently report a zero-trade "best strategy".

Each simulated candidate records:
- candles checked
- candles with valid features
- qualified setups
- entry attempts
- actual entries opened
- rejection counts by cause (trend alignment, RSI, volume, no trigger, resistance, weak learned edge, threshold, sizing, etc.)

Candidates must produce at least 25 training trades to be eligible for selection. If no candidate qualifies, the fold is explicitly marked `insufficient_training_signals` and its most-active candidate is retained only for diagnosis.

The Fold display is clamped correctly, so a completed 4-fold job no longer shows Fold 5/4.


## Persistent learning system

This build adds actual long-term experiment memory.

### What is remembered
Every resolved **out-of-sample** test trade stores:
- symbol / timeframe / entry and exit timestamps
- WIN or LOSS and R multiple
- full feature context
- regime
- model edge estimate
- MFE / MAE
- mistake labels and strength labels

Repeated runs over the same historical event are deduplicated so the model does not
learn the same candle outcome over and over.

### Anti-leakage rule
Persistent memory may only influence a walk-forward fold when the remembered trade
had already exited **before that fold's training cutoff timestamp**. This prevents a
rerun from leaking future outcomes into an earlier historical fold.

### How prior mistakes affect new tests
The fold learner combines:
- current fold training examples at full weight (1.0)
- remembered same-coin/timeframe examples at 0.40 weight
- remembered cross-coin examples at 0.18 weight

This gives the model memory without allowing old history to dominate the current regime.

### Champion / Challenger
Every completed run is a Challenger. It only becomes Champion if it passes all of:
- at least 50 unseen trades
- expectancy > +0.05R
- profit factor >= 1.10
- majority of walk-forward folds profitable
- worst drawdown <= 25%
- Monte Carlo probability profitable >= 60%

If a Champion already exists, the challenger must also improve expectancy by at least
0.03R without materially worsening drawdown.

A losing run is therefore still valuable: its trades enter memory as negative evidence,
but its strategy is **not** promoted.

### Persistence on Render
For learning memory to survive full instance replacement, use a Render persistent disk
and environment variables:
`RESEARCH_DB_PATH=/var/data/research.sqlite3`
`RESEARCH_DATA_DIR=/var/data/data`

Without a persistent disk, memory survives normal process restarts on the same filesystem
but is not guaranteed across service replacement/redeploy filesystem resets.
