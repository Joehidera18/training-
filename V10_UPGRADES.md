# V10 combined-signal experiment and website setup

V10 adds four predeclared variants of one combined-signal rule to the existing 12 candidates. It also exposes the earlier-data evidence behind selection and completes the Render hosting template. These are implemented research features; no increased profit has been demonstrated.

## Research source and scope

[Deprez and Frömmel, Are simple technical trading rules profitable in bitcoin markets? (2024)](https://biblio.ugent.be/publication/01HY3C3S169G1N6QNYR55NZMFB) studied many technical rules and combinations across Bitcoin data frequencies, including transaction costs and out-of-sample evaluation. Its historical findings motivated examining combinations of signals. The [accepted paper](https://biblio.ugent.be/publication/01HY3C3S169G1N6QNYR55NZMFB/file/01HY60XZGZYHNQ6188MSVJT0SG.pdf) uses Bitstamp history; that is not evidence for these exact Coinbase rules, timeframes, or your fees.

Our simple confirmation filter does not replicate the paper's selected-rule portfolios or false-discovery controls. The thresholds below are new, fixed hypotheses. They were not optimized using real market returns in this workspace. Correlated confirmations can all be wrong together.

## Exact additional rule

The family identifier is signal_consensus_simple. It is long-only and uses completed candles on the configured decision interval.

1. The close must exceed the highest high of the preceding 20 candles, excluding the signal candle.
2. The existing regime feature must be BULL or CHOP.
3. Current quote-volume z-score must meet the variant's minimum. RSI must be no greater than 78.
4. At least two of these three confirmations must be true:
   - The 8-, 20-, 50-, and 200-period EMAs are ordered from highest to lowest.
   - RSI is between 55 and 70 inclusive.
   - The existing quote-volume OBV proxy has increased over the preceding 20 candles.
5. The existing volatility guards remain: ATR regime at most 2.5 and candle range expansion at most 4.

OBV here is a signed quote-volume proxy, with volume direction inferred from successive closes. It is not exchange aggressor flow or conventional base-volume OBV. If quote volume is missing from candles, the feature builder approximates it with base volume times close. RSI uses the existing rolling gain/loss calculation. These definitions can differ from charting platforms.

A qualifying signal receives a bookkeeping score of 65 for two confirmations or 70 for three. This score is not a win probability. Consecutive new highs can qualify on consecutive bars; position, entry-timing, and cooldown controls govern whether another entry is possible.

| Variant | Stop distance | Gross target distance / stop distance | Minimum volume z-score |
| --- | --- | --- | --- |
| 1 | 1.5 ATR | 2.0 | 0 |
| 2 | 2.0 ATR | 2.5 | 0 |
| 3 | 1.5 ATR | 2.5 | 0.5 |
| 4 | 2.0 ATR | 3.0 | 0.5 |

All four retain the V9 net-reward/cost filter, 12-hour planned time stop, loss-streak cooldown, and sizing constraints. Gross target multiples do not establish net reward after fees or the likelihood of reaching the target.

## How it competes with the original strategies

The population is now 16 candidates in four families. The first 12 remain in their original order. Every candidate receives training evaluation; at most five finalists proceed to validation and higher-cost validation. Only earlier data selects the final rule. The existing walk-forward windows, purges, final holdout, cost stress, and historical rejection checks remain in place.

The dashboard has an expandable candidate table. JSON exports include every final-stage training candidate, validation results where shortlisted, parameters, scores, selection outcomes, and the date boundaries. A rule can survive training yet never reach validation. It can win earlier selection and lose money in the holdout; such a loss is reported and can prevent qualification.

Adding candidates creates additional selection risk. The bootstrap interval remains conditional on the chosen rule and sample; it does not correct fully for repeated research, multiple markets, or overfitting. Reserve new data for subsequent changes. Do not keep changing settings until an old holdout passes.

The engine identity advances to market-structure-v10.0. Earlier historical profiles, including V9, cannot authorize new entries. Rerun research; preserve the database for account and order reconciliation. All strategies may be rejected.

## Hosting additions

WEBSITE_SETUP.md describes extracting the ZIP, uploading its contents to a private GitHub repository, and deploying the included Render Blueprint. The template now includes a paid service, 1 GB persistent disk, persistent state paths, an app token, one worker/instance, an explicit network bind, and live mode disabled. No service was deployed. Hosting and live exchange integration remain unverified end to end.

## What was tested

The four new automated tests cover every combination of confirmations, mandatory trigger and volatility/volume guards, next-bar execution and cost rejection through the shared engine, and a losing holdout that cannot change the frozen winner. Existing tests also check the expanded population, report completeness, preserved chronology, and rejection of all strategies. See VERIFICATION.md for the complete evidence and limits.
