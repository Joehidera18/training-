# CryptO Research Lab V10 — Coinbase

Complete source for chronological strategy research, automatic paper trading, and an optional Coinbase Advanced spot runner. V10 adds a combined-signal research candidate, a complete candidate-selection report, and a Render hosting template with persistent storage.

**Live trading is disabled by default. This version can submit real orders only after local key configuration and explicit activation. Profitability has not been established.** The $10–$15 daily target equals 2–3% of the starting account every day; it is a reporting goal, not a forecast or an entry trigger.

Start with [COINBASE_SETUP.md](COINBASE_SETUP.md) for Coinbase connection, permissions, controls, and recovery. Read [RESEARCH_NOTES.md](RESEARCH_NOTES.md) for the research and its limits, [CHANGELOG.md](CHANGELOG.md) for fixes, and [VERIFICATION.md](VERIFICATION.md) for what was tested.

For a website you can open on your phone, follow [WEBSITE_SETUP.md](WEBSITE_SETUP.md).

## Start the app

Use Python 3.10 or newer. Verification was performed on Python 3.12.

- Windows: extract the ZIP into a new folder and double-click run_windows.bat.
- Mac / Linux: extract, open a terminal in the folder, and run:

      chmod +x run_mac_linux.sh
      ./run_mac_linux.sh

The launcher creates a local virtual environment, installs requirements, starts the app, and opens http://127.0.0.1:5000. Keep its terminal open. Closing the terminal, sleeping the computer, or stopping the trader stops its paper exit monitoring.

For a manual start:

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt
    python app.py

On Windows, activate with .venv\Scripts\activate instead. You can run the local dashboard and offline research with Python's standard library; the continuous price feed additionally needs websocket-client. The requirements also retain dependencies for the older research modules and Gunicorn hosting.

## Use it with the $500 plan

1. Open Settings. Enter the actual fee **per side** shown in your exchange account. The displayed default of 0.4% is an assumption, not a confirmed Coinbase fee.
2. Keep the initial 15-minute decision interval while establishing a baseline. This is a starting hypothesis, not an optimized timeframe.
3. In Strategy research, test a small, predefined list of Coinbase USD markets. The form starts with BTC-USD, ETH-USD, and SOL-USD as research candidates, not buy recommendations. Choose 180 days initially, or 365 for a broader sample. A complete year still need not cover all market regimes.
4. Let the chronological research finish. It may reject every strategy. Inspect the final holdout, cost stress, drawdown, losing days, and days that reached $10 or $15. Export the full JSON to retain the evidence.
5. Start the paper trader. With the default validation requirement enabled, only markets with matching passing profiles can open new positions. Profiles expire when their data is more than 30 days old; changing tested costs, sizing, or interval also invalidates them.
6. Collect forward paper results on prices that were unavailable during research. The combined portfolio and real execution have not been validated by the single-market historical test. Do not treat the historical gate as a live-trading approval.
7. Export trades and download a database backup before moving the app or experimenting with settings.

The program does not force a daily trade count or raise risk to recover losses. It can remain in cash.

## Strategies and learning

The new research path evaluates **16 fixed candidates in four long-only families**:

| Family | Trigger | Main guard |
| --- | --- | --- |
| Trend pullback | Bullish regime, pullback near the 20-period EMA, RSI 40–65 | Avoid strongly negative candle-volume pressure |
| Volume breakout | Close above the preceding 55-bar high in a non-bearish regime | Volume confirmation and an overextension check |
| Range reclaim | Ranging regime, sweep and reclaim of a low, substantial lower wick | Low trend strength and RSI no greater than 50 |
| Signal consensus | Close above the preceding 20-bar high, with at least two of EMA alignment, RSI momentum, and positive quote-volume OBV change | Non-bearish regime, volume confirmation, RSI no greater than 78 |

Each family has four predeclared stop/target variants. The combined-signal rule requires at least two of: ordered 8/20/50/200 EMAs, RSI 55–70, and positive 20-bar quote-volume OBV change. Its confirmations are correlated and are not independent probabilities.

The candidate table and JSON export retain final training results for all 16 rules, validation results for the five shortlisted rules where available, and the selection outcome. Rules are ranked by conservative net R, not by the largest dollar gain. No later holdout result chooses the winner.

Stops use ATR; targets use 2–3 times the price distance to the stop. That target multiple is **not** the net reward multiple after costs. Volatility spikes, wide spreads, stale prices, late signals, excessive entry gaps, and expensive stops can block entries. Current rules additionally require at least 1.5 units of potential net target reward for each unit of modeled stop risk. This is payoff geometry after costs, not expected profit or a predicted win probability.

The older structure, sequence, model, and research modules remain in the source. They are not the population selected by the new profitability-research screen.

Paper observations update global and coin-specific statistics. Their confidence starts near neutral and grows with distinct resolved observations. In validated mode, the selected rule remains fixed; the learner does not silently tune it using the holdout. Optional unvalidated experiment mode can rank and explore hypotheses with one quarter of normal paper risk.

This is automatic rule selection and statistical bookkeeping. It does not contain a newly trained language model or a proven predictive AI model.

## Default paper account controls

| Setting | Default | At an untouched $500 balance |
| --- | --- | --- |
| Risk budget per qualified trade | 0.75% | Up to $3.75 estimated loss at stop, including modeled costs |
| Unvalidated experiment risk | One quarter of normal | Up to about $0.94 |
| Total open stop risk | 3% | Up to $15 |
| Maximum position value | 30% | Up to $150 including the reserved entry fee |
| Total allocated exposure | 100% | No borrowing |
| Maximum simultaneous positions | 5 | May be fewer because other limits bind |
| Daily entry loss threshold | 3% of the UTC day's baseline equity | About $15 if the baseline is $500 |
| Quote freshness / signal delay | 60 seconds / 60 seconds | Later data blocks new entries |
| Spread ceiling | 0.30% | A ceiling, not an assumed typical spread |
| Per-market cooldown | 15 minutes after exit; 6 hours after 3 consecutive net losses | Applied to the current rules; a nonnegative close resets the loss streak |

These are initial engineering choices, not empirically optimal settings. Gap losses can exceed the estimated stop risk and the daily threshold. The daily threshold blocks new entries; it does not guarantee a maximum account loss or liquidate all existing positions.

Marked equity deducts modeled entry and exit fees, spread, and slippage on open trades. The balance chart shows realized balance after closed trades, not intratrade equity. Taxes and hosting costs are not included. Paper simulation also omits exchange quantity rounding, queue position, and partial fills. The separate Coinbase journal uses confirmed quantities, execution values, and fees.

Pause entries keeps paper stops and targets monitored while the trader runs. Stop halts the whole paper trader. Closing the browser alone does not stop the Python process.

## Research protocol

- Completed, aligned, chronological candles only; at least 3,000 bars, no gaps or duplicates.
- Three expanding walk-forward windows. Each choice is made using earlier training and validation data, separated by a 24-hour purge.
- A final 20% holdout is kept outside final parameter selection. A 24-hour gap separates development from that holdout.
- Signals use completed bars and fills use the following bar's open. The entry bar can hit a stop. If the same bar reaches both target and stop, the stop is assumed first. Stop gaps fill at the worse open.
- Full position exits align the new backtest with the continuous trader. Legacy partial-profit behavior is not used in this execution engine.
- Fees apply on both sides. Historical fills add configured slippage plus an explicit 0.05% assumed half-spread. A 1.5× cost replay also changes cost gates and sizing.
- The gate requires at least two profitable walk-forward windows, at least 30 final holdout trades, positive base and stressed holdout P&L, holdout drawdown no greater than 15%, and a positive lower bound on a moving-block bootstrap interval for historical mean net R.
- A diagnostic replay compares the selected rule with the same rule without the V9 net-payoff and loss-cooldown filters. Negative differences are shown. This replay never changes the selected parameters or qualification result; it does not model live order-book filtering.
- Each test window begins with $500. Fold returns are not added into a fabricated continuous portfolio return.
- Daily goal statistics include no-trade days. They use realized exit-day P&L and are not a prediction.
- Identical data and tested settings reuse a saved result. Changing the history or configuration performs a new test; repeatedly trying settings against the same holdout can still overfit.

A passing result means a rule passed these historical checks. The uncertainty interval does not measure the probability of future profit. Multi-market exposure limits, observed quote spreads, the 60-second signal delay, daily entry halts, and real-time latency create differences from the single-market backtest. Forward paper testing is necessary to assess the actual combined system.

## Research an existing CSV

The CSV requires ts, open, high, low, close, and volume columns. Timestamp ts must be the candle's opening time as Unix milliseconds. Optional quote_volume and trades columns are accepted. If quote volume is absent, it is approximated as base volume times close; it is not order-book or aggressor flow.

    python run_research.py --csv my_btc_15m.csv --symbol BTC-USD --interval 15m --fee 0.004 --slippage 0.0005 --out research-result.json

CLI rates are fractions: 0.004 means 0.4% per side. The CSV must match the symbol and exchange you intend to study; the program cannot infer provenance from prices alone. Do not relabel Binance data as Coinbase evidence.

The CLI exports a report. It does not install a passing profile into the paper account; use the app's research screen to download and register matching Coinbase research.

At 1-hour resolution, 90 days is below the 3,000-candle minimum. Choose 180 or 365 days. Never fill absent candles with fabricated prices to make a test pass.

## Saved state, backups, and upgrades

The app normally writes research.sqlite3 and its SQLite journal files beside app.py. Data downloads and automatic pre-reset backups go in data/. The ZIP contains neither a trading history nor a preapproved strategy.

Extract V10 into a separate folder. Retain your previous archive and database backup. Stop an old process before copying its database. Use the old app's online database backup if available; copying just a database while it is running can omit transactions in its WAL file. Keep the original backup. The V6 schema is retained, but old results used different execution assumptions and should not be combined with V7 as fresh validation evidence.

Paper reset requires an explicit typed RESET and refuses open paper positions. It does not reset Coinbase state or close exchange orders. It backs up the database before clearing trades, returns the paper account to $500, and preserves research. The checkbox controls whether learned paper observations are kept.

Environment variables:

| Name | Purpose |
| --- | --- |
| RESEARCH_DB_PATH | Absolute path to the SQLite account database |
| RESEARCH_DATA_DIR | Absolute path for candles and backup files |
| APP_ACCESS_TOKEN | Private app token; at least 16 characters required for all Coinbase controls |
| COINBASE_KEY_FILE | Absolute local path to an ECDSA Coinbase API key JSON file; never send it through the dashboard |
| COINBASE_ALLOW_LIVE | Defaults off; set to 1 locally to permit explicit live activation |
| HOST | Local server bind address; default 127.0.0.1 |
| PORT | Local port; default 5000 |
| OPEN_BROWSER | Set to 1 for the local launcher to open a browser |

For unattended hosting, use a single always-on process and persistent storage, with HTTPS and an app access token. The included Render Blueprint provisions a paid web service, a 1 GB disk, the two persistent path variables, and a generated app token. Follow [WEBSITE_SETUP.md](WEBSITE_SETUP.md) to upload the extracted source to GitHub and deploy it. A sleeping or ephemeral web instance cannot provide dependable 24/7 monitoring. Nothing in this archive has been deployed.

The included Gunicorn command uses exactly one worker and multiple request threads. Do not add workers or replicas to one account. The trading runtime also uses an operating-system file lock to prevent concurrent trading workers on the same database.

## Coinbase integration

The Coinbase tab can sync account balances and actual fee rates, preview qualified signals, and optionally run a separately journaled spot position. Its pilot limits are $500 maximum capital, $150 maximum entry spend, one position, and BTC-USD / ETH-USD / SOL-USD candidates. Actual sizing can be smaller. A dedicated USD-funded portfolio is required for live entries.

Entries use a price-capped fill-or-kill order with attached take-profit/stop-loss instructions. Unsupported previews block entry without falling back to an unprotected buy. The runner records stable order IDs before submission, reconciles uncertain responses without resubmitting, and waits for confirmed protective-order cancellation before selling residual quantities. See COINBASE_SETUP.md for limitations and recovery.

V9 checks up to 50 visible bid and ask levels before a preview and again before a live entry. It requires enough displayed asks within the buy limit and enough displayed bids within the current exit-slippage range. This is a current-liquidity check, not a guarantee of future stop execution.

The V10 engine invalidates earlier profiles, including V9. Rerun research; the dashboard labels older engine results. Preserve the existing database to reconcile open orders, and do not start a fresh journal while a position remains.

The historical qualification gate cannot certify live performance. Coinbase SDK installation, account authentication, product-specific acceptance of the protected order combination, and real fills were not tested here. This is an experimental integration, not a validated unattended income system. No real orders were submitted during development.

See [V10_UPGRADES.md](V10_UPGRADES.md) for the new combined-signal experiment and [V9_UPGRADES.md](V9_UPGRADES.md) for the retained trade-quality controls.
