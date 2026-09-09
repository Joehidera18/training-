# CryptO V11 — automatic learning and Coinbase

A trading program that studies market history, learns which setups work in different conditions, tests its decisions on later prices, and updates from completed trades.

**Goal: work toward $10–$15 a day from a $500 account. That return has not been demonstrated.** No real historical training run or Coinbase account connection was completed here. The supplied models start untrained; the program downloads and studies history when you run it.

## Start here

1. Open the app locally or follow [WEBSITE_SETUP.md](WEBSITE_SETUP.md) to put it on Render.
2. Enter your actual Coinbase fee per side. The initial 0.4% is an assumption until confirmed.
3. Click **Start learning & paper trading**.

The program handles the research work in the background. It loads the market scanner, requests up to three years of history for five liquid Coinbase USD markets, trains models, and tests them on later data. Qualified models can open paper trades and update after those trades close. Unqualified markets stay out of the account.

The first download can take time. Completed downloads and results are saved, so a cancelled run can reuse its progress. A complete first study of five markets with three years each represents 131,400 market-data hours; these are summed across coins and are not 131,400 independent hours of market history. The dashboard shows the hours actually processed.

Pause entries keeps existing paper positions monitored. Stop stops automatic learning and the paper runner. Closing a browser leaves a running server alone; a computer or server restart stops the runners. Reopen the app and resume them after checking its status.

## What learns

The model learns relationships between entry-time indicators and the trade's eventual result after costs. Successful and failed trades both update its estimates. It chooses among 16 stop/target variants within four defined trade types: trend pullback, volume breakout, range reclaim, and combined confirmations.

This is a small online machine-learning model. It learns entry preferences and strategy selection within those trade types; it does not autonomously invent arbitrary executable strategies or train a language model.

The scanner watches up to 30 active Coinbase USD markets under the default settings. Automatic historical learning studies the first five in that volume-ranked list. It does not study every Coinbase asset at once, and it does not include news, sentiment, or on-chain data.

## Paper and real trading

The main button starts **paper trading**. Real Coinbase orders still require account configuration and separate explicit activation. Follow [COINBASE_SETUP.md](COINBASE_SETUP.md). The existing Coinbase pilot remains limited to BTC-USD, ETH-USD, and SOL-USD, $500 maximum capital, $150 maximum entry spend, and one position at a time.

Paper and Coinbase models begin from the qualifying historical model, then learn from separate journals. Simulated paper wins do not train the real Coinbase model. Real feedback is added only when the position is fully closed and its quantities, values, and fees have settled.

## Run locally

Use Python 3.10 or newer; offline verification used Python 3.12.

- Windows: extract the ZIP into a new folder and run run_windows.bat.
- Mac/Linux: open a terminal in the extracted folder and run chmod +x run_mac_linux.sh, then ./run_mac_linux.sh.

The launcher installs dependencies and opens the app in your browser. Keep the computer awake and its Python process running. For the website route, upload all extracted files and folders to GitHub and use the included Render configuration.

## Upgrading an existing installation

Use the V11 source files, preserve your database and persistent storage, and stop an older process before starting another. The new engine requires fresh qualification. Do not replace a live order journal with a blank one while a Coinbase position remains.

Your database is research.sqlite3 by default. Set RESEARCH_DB_PATH and RESEARCH_DATA_DIR for persistent hosted storage; the included Render template already does this. Keep Coinbase keys outside the source folder and GitHub.

Automatic review is scheduled every 28 days while the controller is running, or after tested fee/sizing/interval settings change. Models expire after 30 days without fresh qualification. Retried reviews may overlap old test periods; they are not independent proof of improvement. A new historical model replaces the forward model on its next use, while the old trade journal and reports remain saved.

[What the learner does and how it is tested](V11_LEARNING.md) · [Verification and limits](VERIFICATION.md) · [Change history](CHANGELOG.md)
