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


## Strategy Engine v2.1

The research engine now compares six distinct strategy families rather than tuning one
setup repeatedly:

1. Trend/range breakout
2. Trend pullback continuation
3. Liquidity-sweep reclaim
4. Range/chop mean reversion
5. Bearish trend breakdown
6. Bear-market rally fade

New chart / market-state features include 8/20/50/200 EMA structure, 20/55-bar
Donchian levels, RSI, ATR, ADX-like trend strength, Bollinger z-score, quote-volume
z-score, signed-volume pressure, OBV slope, liquidity sweeps above/below recent
extremes, rejection wicks, compression, range expansion, multi-timeframe momentum,
and ATR-normalized support/resistance distance.

Long and short strategies are tested independently with the same walk-forward,
persistent-memory, slippage, fee, Monte Carlo and Champion/Challenger framework.

Important: OHLCV candles can only approximate liquidity behavior. True order-book
imbalance, spread, queue depth, aggressor-side trades, liquidations, open interest and
funding require separate historical microstructure data. Do not interpret these proxies
as actual level-2 order-book reconstruction.


## Research Grade v3.0

This build prioritizes honest edge detection over forcing trades.

- Nested validation inside each outer walk-forward fold: the learner fits on earlier history,
  candidates compete on a later internal validation slice, then the selected candidate is
  tested on completely unseen outer data.
- No-edge/no-trade gate: losing or weak validation candidates are not allowed to become the
  "best" strategy just because every other candidate was worse.
- Adaptive challengers: recurring persistent-memory mistakes generate testable parameter
  hypotheses such as tighter entry-gap limits, stronger confirmation, stricter learned-edge
  thresholds, adjusted stops and earlier exits.
- Cost-to-R gate: a setup is rejected when estimated round-trip fees/slippage consume too
  much of planned trade risk.
- Execution audit: resolved trades record gross R, fee R, approximate slippage R, MFE/MAE
  and anomaly flags. A stopped trade worse than -2R is explicitly surfaced.
- Better OHLCV market-state proxies: rolling VWAP distance, equal-high/equal-low liquidity
  pools, sweep depth and displacement, in addition to trend, volume, volatility and regime
  features already present.

These features do not guarantee profitability. True order-flow imbalance, spread, queue
depth, liquidations, funding and open interest require additional market-microstructure or
derivatives datasets; OHLCV can only approximate them.


## Quant Intelligence V4.0

V4 expands the research stack without assuming profitability.

- Optional historical order-flow / derivatives ingestion via `POST /api/microstructure/import`.
  Supported fields: bid, ask, bid_depth, ask_depth, aggressive_buy_quote, aggressive_sell_quote,
  liquidation_long_quote, liquidation_short_quote, open_interest, funding_rate and basis_pct.
- Counterfactual analysis samples rejected setups and measures what happened over the next 12 bars,
  so the system learns about missed winners as well as taken losers.
- Probability calibration reports Brier score and reliability bins.
- Cross-coin hierarchical memory keeps same-pair evidence at higher weight and other-coin evidence at lower weight.
- Bounded dynamic risk scales from 0.25x to 1.0x of configured risk; it never exceeds the user's maximum.
- Edge-stability reporting checks whether positive expectancy persists across outer walk-forward folds.
- If historical microstructure data is absent, the UI explicitly says OHLCV proxy rather than pretending candles contain L2 data.

True historical L2/order-flow, open interest, funding and liquidation information must be supplied from an external dataset; ordinary candles cannot reconstruct it.


## Market Structure Intelligence V5

V5 adds deterministic, machine-readable versions of:
- HH/HL/LH/LL structure and swing points
- break of structure (BOS)
- change of character / market-structure shift (CHOCH/MSS)
- equal-high / equal-low liquidity pools
- previous-day and previous-week liquidity
- liquidity sweeps and sweep depth
- equilibrium, premium and discount
- FVG and inverse-FVG-style invalidation state
- displacement
- continuous daily bias
- an SMT / relative-strength helper module
- sequence statistics such as sweep + BOS/CHOCH + FVG

These concepts are not assumed profitable. They are treated as hypotheses and must survive
fees, slippage, nested validation, outer walk-forward testing, counterfactual analysis,
edge-stability checks and Champion/Challenger promotion.


## Market Structure V5.1 filter fix

V5.0 over-filtered. It could identify hundreds of qualified setups but daily-bias,
regime, cost, volume and entry gates often reduced those to almost no actual validation
trades.

V5.1 changes the architecture:
- BOS/CHOCH, FVG, premium/discount, daily bias, volume and regime are weighted evidence.
- Only defining setup events and true execution constraints remain hard gates.
- Cost-to-R thresholds are more realistic while still bounded.
- The validation minimum is lower so promising small samples can reach the outer test.
- Negative expectancy, PF below 1.0 and excessive drawdown still fail validation.
- Filter diagnostics show which rules are eliminating candidates.

The engine still has permission to return NO EDGE. The goal is more informative
experimentation, not forcing trades.


## Market Structure V5.2 — Sequence Entry Engine

V5.1 showed that filter starvation was fixed, but the surviving individual-entry families
still had strongly negative validation expectancy.

V5.2 changes entry construction instead of loosening validation:

- Bullish/bearish reversal sequence:
  liquidity sweep -> BOS/CHOCH -> retracement/location confirmation.
- Bullish/bearish continuation sequence:
  directional structure -> BOS/displacement -> FVG/pullback retest.
- Tracks bars since sweep/BOS/CHOCH so order matters.
- Adds FVG midpoint state and retracement detection.
- Sequence candidates use retrace/limit-style next-bar entries rather than chasing next open.
- A limit must actually trade inside the next candle to count as filled.
- Rejected validation setups feed counterfactual diagnostics even if the outer test is skipped.
- The no-edge gate remains: negative validation expectancy still cannot reach the outer test.

These changes test whether timing and ordered context improve expectancy. They do not assume
the concepts are profitable.


## V5.3 — Render restart recovery

The deployment log showed the research job progressing normally until the Render instance
restarted. The browser then kept POSTing the old job ID, causing a continuous 404 loop.

V5.3:
- returns HTTP 410 + `JOB_CHECKPOINT_LOST` for a missing checkpoint;
- stores the active job ID and launch configuration in browser localStorage;
- resumes a surviving SQLite checkpoint after reconnect;
- if the host discarded the checkpoint, automatically rebuilds the experiment once;
- prevents infinite rebuild/retry loops if the host restarts repeatedly;
- slows step polling to reduce request storms;
- reports checkpoint storage configuration from `/api/health`.

For true resume across instance replacement, configure `RESEARCH_DB_PATH` and
`RESEARCH_DATA_DIR` on a persistent Render disk. If the hosting platform deletes the
checkpoint bytes, V5.3 safely restarts the experiment instead of pretending it can resume.


## V5.4 — audited build

This release includes a full consistency and leakage audit.

Fixes:
- **Removed future leakage from swing detection.** Pivots are now confirmed only after the
  required future bars have actually closed; the feature is attached to the confirmation
  bar, never backdated into the past.
- **Fixed FVG lifecycle.** A newly created FVG is no longer immediately marked mitigated on
  its own creation candle. Only later candles can mitigate or invert it.
- Improved sequence continuation detection while remaining causal.
- Engine/UI version labels are consistent at `market-structure-v5.4`.
- UI default candidate count is 48 and lists all 10 strategy families.
- SQLite checkpoint database uses WAL, NORMAL synchronous mode and a busy timeout.
- Render Gunicorn timeout is 300 seconds.
- Includes V5.3 lost-checkpoint/restart recovery.

Important:
A Render persistent disk is still required for true checkpoint survival across host
replacement. Browser recovery can safely rebuild a lost run, but it cannot restore bytes
that the hosting platform deleted.


## V5.5 — Recovery Hardening

Fixes the `Unexpected token '<' / <!DOCTYPE is not valid JSON` crash when Render returns an HTML restart/error page during job recovery. Critical job, recovery, history, and learning-summary calls now validate content type before parsing JSON, retry temporary 408/425/429/500/502/503/504 responses with exponential backoff, wait for Render to stabilize before rebuilding a lost checkpoint, and stop repeated checkpoint-loss loops safely. Flask `/api/*` 404/500 handlers return JSON. `/api/health` reports API version 5.5. Persistent storage is still required to preserve exact checkpoint bytes across host replacement.
