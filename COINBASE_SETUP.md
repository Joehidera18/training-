# Coinbase setup for CryptO V11

Live trading starts OFF. You can use research and paper trading without an exchange key. No account was connected and no real order was placed during development.

The Python program runs on a computer or server. A hosted dashboard can be opened in your iPhone browser; follow WEBSITE_SETUP.md for the research/paper website setup. For local use, the computer must stay awake with the Python process running for monitoring and residual exits.

## 1. Install locally and configure account viewing

Extract the complete ZIP into a new directory. Follow README.md to install Python 3.10+ and requirements. requirements-coinbase.txt pins the official coinbase-advanced-py SDK at 1.8.4; installation and real SDK requests were not executed in this workspace.

Create a Coinbase CDP API key for your own Coinbase Advanced portfolio. Select ECDSA / ES256, not Ed25519. Use View permission for initial account sync. Save the downloaded key JSON outside the project and backups. Never paste it into chat, the dashboard, source control, or a shared folder. Follow the [official authentication instructions](https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication).

Set these environment variables in the terminal that launches the app:

| Variable | Value |
| --- | --- |
| APP_ACCESS_TOKEN | Your own randomly generated private token, at least 16 characters |
| COINBASE_KEY_FILE | Full path to the downloaded ECDSA key JSON |
| COINBASE_ALLOW_LIVE | 0 initially |
| HOST | 127.0.0.1 for local use |

The program does not automatically load a .env file. Set variables in your shell or operating-system launch configuration. Generate an app token locally with: python -c "import secrets; print(secrets.token_urlsafe(32))"

On macOS/Linux, use export VARIABLE='value'. On Windows PowerShell, use $env:VARIABLE='value'. Then run python app.py from the installed project environment. Supply your app token through the dashboard's existing access-token field.

Open Coinbase and click Sync account & fees. This reads account data; it does not buy, sell or transfer. Confirm the intended portfolio and available USD. Fees are read from the account's [transaction summary](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/fees/get-transaction-summary), not assumed from a public headline fee.

## 2. Research and preview before considering activation

Click Use Coinbase fee in research. The main fee field and Settings refresh to the synced taker rate. A change invalidates profiles whose tested fee differs. Click Start learning & paper trading; the controller studies history and tests its models using the updated rate. Collect forward paper results. Additional tax or cost-plus fee schemes are unsupported and block live entries.

The main learning button starts the paper/market runner to maintain completed candles and features. After a model passes the historical checks, start the separate Coinbase preview runner. Signals require a current qualified model and matching current features. Only BTC-USD, ETH-USD, and SOL-USD remain eligible for this Coinbase pilot; a model trained for a different coin does not authorize trading it.

Preview sends an order preview request, never a create or cancel request. A rejected or unsupported protected order remains rejected. A preview is neither a fill nor a reservation. All candidates may fail the historical gate, or no qualifying current signal may appear.

V11 changes the learning policy and engine identity, so all earlier profiles, including V10, cannot authorize new entries. Run V11 learning for fresh qualification. The live runner's one-position cap, price-capped fills, market availability and exchange behavior differ from historical simulation. Neither historical qualification nor these software tests demonstrate a profitable live system.

## 3. Optional local live activation

For this experimental pilot, use a key restricted to one dedicated Coinbase Advanced spot portfolio, funded with USD and without other crypto holdings or existing orders. Funding and portfolio management are manual actions in Coinbase; the program has no transfer or withdrawal feature.

A live key needs View and Trade, with Transfer disabled. The adapter checks these permissions and binds its journal to the portfolio UUID. Accounts with unsupported permissions or mixed holdings block entries.

Stop the Coinbase runner. Set COINBASE_ALLOW_LIVE=1 locally and restart the application. Reconnect with the app token. Start the market/paper runner, then use Enable LIVE and type ENABLE COINBASE LIVE. This allows automatic REAL orders when all program checks pass. Restarting the program alone never starts live trading.

| Pilot control | Behavior |
| --- | --- |
| Capital | Initially up to $500; extra deposits do not increase the risk budget |
| Entry spend | At most $150, including reserved entry commission; cash and risk can reduce it |
| Positions | One bot-owned spot position at a time |
| Markets | BTC-USD, ETH-USD, SOL-USD only; eligibility checked with Coinbase |
| Default modeled risk | 0.75% per entry, about $3.75 on $500; not a guaranteed loss cap |
| Daily entry halt | Configured fraction of marked daily equity, capped at $15; latched until next UTC day |
| Reentry | 15 minutes normally; 6 hours after three consecutive net losing closes under the current rules |
| Maximum holding period | Strategy default 12 hours, with exit handling only while live runner operates |
| Increased fees | A higher current taker rate than the tested rate blocks new entries |
| Borrowing / shorts | Not implemented |

Do not run multiple copies, databases or keys against the same portfolio. The process lock protects a shared database, not separate computers or independent journals.

## Retained trade-quality checks

The potential target gain after modeled fees and exit slippage must be at least 1.5 times modeled stop risk. This does not mean the trade is expected to win; reaching the target is uncertain. The preview displays potential target profit and the net reward/risk ratio.

The adapter checks up to 50 visible order-book levels on each side before preview and again before submission. Insufficient size inside the entry limit or current bid-side slippage range blocks a new entry. Books older than 10 seconds, crossed books and malformed levels are rejected. Existing exits do not wait for this new-entry check.

Six-hour cooldowns apply per market after the third and subsequent consecutive net losing close. They use settled bot-owned trades and survive restart through the journal. A later nonnegative close breaks the streak. Paper cooldowns use only paper trades. These rules are unproven hypotheses and can miss profitable opportunities.

## Orders and recovery

The entry request is a limit fill-or-kill BUY with attached trigger-bracket take-profit/stop-loss instructions. The exact combination must be accepted by the product's preview endpoint. Product-specific compatibility has not been verified on an authenticated account. There is no fallback to an unprotected buy. Coinbase documents [order types and attached protection](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/orders) and the [create-order request](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order).

Native exchange orders can remain active when the app disconnects, but stop execution and the size of losses are not guaranteed during volatility. A partial bracket fill may disable the other side. The runner then confirms cancellation before selling only the remaining quantity. Market residual exits can fill at worse prices than the quote.

The journal saves the client order ID before submission, with SQLite FULL synchronization for live state writes. A timeout means UNKNOWN, not failure: the runner looks up the original ID and never blindly buys again. If the order cannot be found, new entries remain blocked. Check Coinbase directly; do not delete the journal to clear the block.

NEEDS_ATTENTION means human review is needed on Coinbase, including missing protection, below-minimum residual holdings, or repeated incomplete exits. The app does not claim the holding is closed or invent its proceeds. A missing or mismatched protective order is not silently treated as safe.

Pause Coinbase entries keeps existing-position monitoring active. Stop Coinbase runner stops app monitoring and leaves submitted exchange orders active. Close bot position asks the running live service to reconcile and close the owned quantity; it is not instantaneous and does not guarantee execution.

After a restart, Sync reads and reconciles known orders without trading. Resume live monitoring explicitly with the same database and portfolio if needed. Preserve the database and its backups. Do not delete it, replace it with an old backup, switch portfolios, or start a new journal while orders or holdings remain. A paper reset does not clear the Coinbase journal.

Use Coinbase itself for urgent inspection, cancellation or closing when the app cannot reconcile. Manual exchange actions may require manual accounting review; the bot cannot infer external trades as its own fills.

## Profitability and validation limits

A $10–$15 daily goal on $500 is 2–3% per day. There is no evidence this build can deliver that reliably. It does not force trades or increase size to chase the goal. Actual live P&L comes from settled exchange executions and USD fees; paper P&L remains separate. Taxes, hosting costs and deposits/withdrawals are not included in bot P&L.

The 88 automated tests are offline code checks. The account, SDK dependency installation, real protected-order acceptance, real fills, long outages and unattended operation have not been verified. Coinbase's [sandbox uses predefined responses](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox), so a sandbox response would not establish a trading edge either.

Official implementation references: [SDK REST methods](https://coinbase.github.io/coinbase-advanced-py/coinbase.rest.html), [API key permissions](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/data-api/get-api-key-permissions), [order preview](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/preview-orders), [order reconciliation](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/get-order), [SDK releases](https://pypi.org/project/coinbase-advanced-py/). API research was checked on 7 September 2026; integration tests completed on 8 September.

## Learning from real fills

The Coinbase model starts from the qualified historical model and subsequently learns only from its own settled closed-trade journal. Paper trade results do not change it. The closure and model update commit together, so repeated reconciliation does not double-count a trade. Order previews are not learning outcomes. A new historical model seeds a new forward learning state when its data fingerprint changes; the old financial journal remains saved.
