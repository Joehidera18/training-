# V11 automatic learning — 9 September 2026 UTC

- Replace the main manual research flow with one Start learning & paper trading control.
- Request up to three years of completed history for five currently liquid Coinbase USD markets, with saved downloads and cached results.
- Train 16 small online net-R models from resolved hypothetical examples; select eligible templates using learned entry-condition estimates.
- Test the entire updating policy chronologically, including delayed outcomes, cost stress, a final holdout, and a frozen-policy diagnostic that cannot choose the winner.
- Learn from completed paper trades and settled Coinbase trades in separate, atomically updated model states. Preserve duplicate-closure protection and account limits.
- Add scheduled review while the controller runs, model expiry, cost-signature checks, report export, and a compact main dashboard.
- Reduce historical feature-cache memory by omitting unused structure features in the learning path.
- Add 18 regression tests; all 110 tests pass. The three-year-sized artificial-data performance check does not establish profitability.
- Advance engine identity to market-structure-v11.0. Existing profiles need fresh qualification; existing account/order journals must be preserved.

# V10 combined-signal research and hosting — 8 September 2026

- Add four fixed combined-signal variants, bringing the population to 16 in four families. A 20-bar breakout requires at least two of EMA alignment, RSI momentum, and positive quote-volume OBV change.
- Preserve the original 12 candidates and existing cost, exposure, cooldown, and execution controls.
- Export final training results for all candidates and validation outcomes for the shortlist; show the evidence in an expandable dashboard table.
- Advance engine identity to market-structure-v10.0; earlier profiles require new qualification.
- Complete the Render Blueprint with a persistent disk and state paths, token protection, explicit bind, one worker, and live submissions disabled. Add WEBSITE_SETUP.md.
- Add four focused regression tests. No historical profit or hosted deployment result is claimed.

# V9 trade-quality upgrades — 8 September 2026

- Require potential net target reward / modeled stop risk of at least 1.5 in all 12 research candidates, paper entries and Coinbase plans.
- Apply a six-hour per-market cooldown after three consecutive net losing closes; ordinary cooldown remains 15 minutes.
- Check visible Coinbase entry and immediate-exit depth before preview and again before submission.
- Show potential target profit and net reward/risk in the Coinbase preview.
- Add a frozen-rule historical comparison with the V9 payoff and cooldown filters disabled. Report improvements or deterioration without using that comparison for selection.
- Advance the engine identity to market-structure-v9.0, invalidate earlier qualifying profiles and label older dashboard results.
- Add 11 regression tests, for 88 total. No real historical superiority or live profit is established.

# V8 Coinbase changes — 8 September 2026

- Add the official Coinbase Advanced SDK adapter, loaded only when a local connection is requested.
- Sync portfolio balances, key permissions and actual maker/taker fee tier.
- Add separate preview and explicitly enabled live modes, with no automatic live resumption.
- Add a one-position spot runner with $500 capital / $150 entry-spend caps.
- Require matching historical profiles, current quotes and a successful protected-order preview.
- Record order intent durably before submission; recover unknown orders without duplicate submissions.
- Reconcile partial fills, exchange-held protection, confirmed cancellations and actual fees.
- Persist live daily loss state and trade-specific manual close requests.
- Add Coinbase controls and order journal; keep paper accounting separate.
- Add 30 offline regression tests, for 77 total; fix dashboard fee-setting refresh.
- Include setup and recovery instructions. Real account behavior and profitability remain unverified.

# V7 changes from V6

This release upgrades the supplied CryptO_Continuous_Learning_Lab_V6_COMPLETE.zip. It preserves the earlier archive and provides a separate complete source package.

## Trading and accounting fixes

- Include the latest completed candle in feature computation.
- Add a configurable decision interval and a regression check that prevents higher-timeframe processing from changing the selected signal interval.
- Fetch enough hourly history to warm up 4-hour features; omit partial 4-hour bars.
- Use completed exchange candles for volume rather than reconstructing volume from ticker messages.
- Cache features and setup rankings so dashboard reads do not repeat full strategy computation.
- Deduplicate market messages, reject stale/crossed/non-finite quotes, and stop stale data from creating entries or fabricated exits.
- Require new entries within 60 seconds of the signal candle closing.
- Check entry-candle stops; assume stop-first for ambiguous candles; simulate gaps at their worse available price.
- Measure actual stop risk after position sizing and notional caps, including modeled fees and slippage.
- Freeze each open paper position's cost assumptions so later settings changes do not rewrite its P&L.
- Commit trade closure, account balance, and learned observations in one database transaction.
- Recover open journal positions after restart and persist cooldowns and the daily entry halt.
- Remove double counting of the same trade in global and coin-specific learning.
- Validate all settings before changing any of them.
- Refuse resets with open positions and save a database backup before clearing a paper journal.
- Prevent a second process from running the same paper trading account.

## Research and reporting

- Add 12 explicit long-only candidates from three distinct hypotheses.
- Add chronological training/validation, three walk-forward windows, a final holdout, and higher-cost stress.
- Allow every candidate to fail. Default new entries require a current passing historical profile.
- Track daily dollar goals across all calendar days, including days with no realized P&L.
- Add cash and buy-and-hold benchmarks, net expectancy, drawdown, trade counts, rejection reasons, and a historical uncertainty interval.
- Download real Coinbase history in pages and checkpoint completed chunks for reuse after cancellation.
- Add CSV trade export, JSON research export, usable SQLite backups, a standalone CSV research command, and operating notes.
- Preserve manual refresh of setup rankings.
- Add 47 automated offline regression tests.

## Compatibility and scope

The V7 dashboard is served by a standard-library WSGI application. The existing Gunicorn one-worker entry point remains app:app. Legacy research modules and stored runs remain available, but the new dashboard uses the bounded V7 research path.

New research uses full target exits; legacy partial-profit backtest assumptions are not reproduced. New validated strategies are long-only. The code remains a research and paper-trading application, with no live-order endpoint or exchange-key handling.

No historical V7 profit result was verified during this upgrade. See VERIFICATION.md.
