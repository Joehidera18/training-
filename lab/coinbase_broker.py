"""Coinbase Advanced adapter. Nothing connects at import; live writes default off.

The official SDK owns JWT signing. Exceptions deliberately omit raw SDK messages,
headers, URLs and credentials. All order quantities use Decimal strings.
"""
from __future__ import annotations
import copy
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from pathlib import Path
from .trade_quality import net_payoff

ZERO = Decimal("0")
TERMINAL = {"FILLED", "CANCELLED", "EXPIRED", "FAILED", "REJECTED"}
ACTIVE = {"PENDING", "OPEN", "QUEUED", "CANCEL_QUEUED"}
PRODUCT_ID = re.compile(r"[A-Z0-9]{2,16}-USD")


class BrokerError(RuntimeError):
    pass


class SubmissionUnknown(BrokerError):
    """A write may have reached Coinbase; reconcile, never blindly retry."""


def decimal(value, label="number", minimum=ZERO):
    if isinstance(value, bool):
        raise BrokerError(f"Invalid {label}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BrokerError(f"Invalid {label}") from None
    if not result.is_finite() or result < minimum:
        raise BrokerError(f"Invalid {label}")
    return result


def positive(value, label="number"):
    result = decimal(value, label)
    if result <= 0:
        raise BrokerError(f"{label} must be positive")
    return result


def rounded(value, increment, up=False):
    increment = positive(increment, "exchange increment")
    return (decimal(value) / increment).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * increment


def number(value):
    return format(value, "f")


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def as_dict(value):
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        raise BrokerError("Coinbase returned an unexpected response")
    return value


class CoinbaseAdapter:
    def __init__(self, key_file=None, allow_live=False, sdk=None, clock=time.time):
        self.key_file = str(key_file or "")
        self.allow_live = allow_live is True
        self._sdk, self.clock = sdk, clock
        self._rate_lock, self._next_request = threading.Lock(), 0.0

    @property
    def configured(self):
        return bool(self.key_file or self._sdk is not None)

    def _client(self):
        if self._sdk is None:
            if not self.key_file or not Path(self.key_file).is_file():
                raise BrokerError("Set COINBASE_KEY_FILE to your local Coinbase ECDSA key file")
            try:
                from coinbase.rest import RESTClient
            except ImportError:
                raise BrokerError("Install the Coinbase requirements before connecting") from None
            try:
                self._sdk = RESTClient(key_file=self.key_file, timeout=8, verbose=False)
            except Exception:
                raise BrokerError("Could not load the Coinbase ECDSA key file; check the local setup guide") from None
        return self._sdk

    def _call(self, method, *, write=False, **kwargs):
        if write and not self.allow_live:
            raise BrokerError("Live Coinbase orders are disabled in this process")
        # No automatic POST retries, including after a timeout or an uncertain reply.
        client = self._client()
        with self._rate_lock:
            wait = max(0, self._next_request-time.monotonic())
            if wait:
                time.sleep(wait)
            self._next_request = time.monotonic()+.20
        try:
            return as_dict(getattr(client, method)(**kwargs))
        except BrokerError:
            if write:
                raise SubmissionUnknown("Coinbase write outcome is unknown; reconciliation is required") from None
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if write:
                raise SubmissionUnknown("Coinbase write outcome is unknown; reconciliation is required") from None
            if status in (401, 403):
                raise BrokerError("Coinbase authentication or permission check failed; check the ECDSA key and permissions") from None
            if status == 429:
                raise BrokerError("Coinbase rate limit reached; wait before syncing again") from None
            raise BrokerError("Coinbase request failed; check connectivity and the local key configuration") from None

    def accounts(self):
        accounts, cursor, seen = [], None, set()
        for _ in range(50):
            response = self._call("get_accounts", limit=250, **({"cursor":cursor} if cursor else {}))
            batch = response.get("accounts")
            if not isinstance(batch, list):
                raise BrokerError("Coinbase account data is incomplete")
            for raw in batch:
                if raw.get("active") is False:
                    continue
                available = decimal(raw.get("available_balance", {}).get("value"), "available balance")
                hold = decimal(raw.get("hold", {}).get("value"), "held balance")
                accounts.append({"currency":raw.get("currency"), "available":number(available),
                    "hold":number(hold), "ready":raw.get("ready", False) is True,
                    "portfolio":raw.get("retail_portfolio_id", ""), "account_id":raw.get("uuid", "")})
            if not response.get("has_next"):
                ids = [a["account_id"] for a in accounts]
                if any(not x for x in ids) or len(ids) != len(set(ids)):
                    raise BrokerError("Coinbase returned duplicate or unidentified accounts")
                return accounts
            cursor = response.get("cursor")
            if not cursor or cursor in seen:
                raise BrokerError("Coinbase account pagination did not complete")
            seen.add(cursor)
        raise BrokerError("Too many account pages; no trading balance was assumed")

    def snapshot(self):
        permissions = self._call("get_api_key_permissions")
        if any(not isinstance(permissions.get(k),bool) for k in ("can_view","can_trade","can_transfer")):
            raise BrokerError("Coinbase did not return complete API key permissions")
        if permissions.get("can_view") is not True:
            raise BrokerError("The Coinbase key needs View permission")
        portfolio = permissions.get("portfolio_uuid")
        if not isinstance(portfolio, str) or not portfolio:
            raise BrokerError("Coinbase did not identify the key's portfolio")
        accounts = self.accounts()
        if any(a["portfolio"] and a["portfolio"] != portfolio for a in accounts):
            raise BrokerError("Account balances span portfolios; use a key restricted to the intended portfolio")
        fees = self._call("get_transaction_summary", product_type="SPOT", product_venue="CBE")
        tier = fees.get("fee_tier", {})
        taker = decimal(tier.get("taker_fee_rate"), "taker fee")
        maker = decimal(tier.get("maker_fee_rate"), "maker fee")
        if max(taker, maker) > Decimal(".02"):
            raise BrokerError("The reported fee is outside the supported model")
        gst = decimal((fees.get("goods_and_services_tax") or {}).get("rate") or "0", "fee tax")
        balances = {}
        for account in accounts:
            if not account["ready"]:
                continue
            c = account["currency"]
            item = balances.setdefault(c, {"available":ZERO, "hold":ZERO})
            item["available"] += decimal(account["available"])
            item["hold"] += decimal(account["hold"])
        return {"synced_at":self.clock(), "portfolio":portfolio,
                "permissions":{k:permissions.get(k) is True for k in ("can_view","can_trade","can_transfer")},
                "fee_tier":str(tier.get("pricing_tier","")), "taker_fee_rate":number(taker),
                "maker_fee_rate":number(maker),
                "simple_fees":fees.get("has_cost_plus_commission") is not True and gst == 0,
                "balances":{c:{k:number(v) for k,v in item.items()} for c,item in balances.items()}}

    def product(self, pid):
        if not PRODUCT_ID.fullmatch(pid):
            raise BrokerError("Only Coinbase USD spot markets are supported")
        p = self._call("get_product", product_id=pid)
        if p.get("product_id") != pid or p.get("product_type") != "SPOT" or p.get("quote_currency_id") != "USD":
            raise BrokerError("Coinbase did not confirm a matching USD spot product")
        if str(p.get("status","")).lower() != "online":
            raise BrokerError("This Coinbase market is not online")
        if any(p.get(key) is True for key in ("is_disabled","trading_disabled","cancel_only","limit_only","post_only","view_only","auction_mode")):
            raise BrokerError("This Coinbase market does not permit the required execution")
        return {"product_id":pid, "base":p["base_currency_id"],
                **{k:number(positive(p.get(k),k)) for k in ("base_increment","quote_increment","price_increment",
                    "base_min_size","base_max_size","quote_min_size","quote_max_size")}}

    def quote(self, pid):
        result = self._call("get_best_bid_ask", product_ids=[pid])
        matches = [p for p in result.get("pricebooks",[]) if p.get("product_id") == pid]
        if len(matches) != 1:
            raise BrokerError("Coinbase did not return one matching price book")
        p = matches[0]
        try:
            bid = positive(p["bids"][0]["price"],"bid")
            ask = positive(p["asks"][0]["price"],"ask")
            stamp = datetime.fromisoformat(p["time"].replace("Z","+00:00")).timestamp()
        except (KeyError, IndexError, TypeError, ValueError):
            raise BrokerError("Coinbase price book is incomplete") from None
        if ask < bid or not -5 <= self.clock()-stamp <= 30:
            raise BrokerError("Coinbase price book is stale or crossed")
        return {"bid":number(bid),"ask":number(ask),"ts":stamp}

    def preview(self, payload):
        body = {k:copy.deepcopy(v) for k,v in payload.items() if k not in ("client_order_id","preview_id")}
        result = self._call("preview_order", **body)
        if not isinstance(result.get("errs"), list) or result["errs"]:
            reasons = [x for x in result.get("errs",[]) if isinstance(x,str) and re.fullmatch(r"[A-Z0-9_]{1,100}",x)]
            raise BrokerError("Coinbase rejected the preview" + (": "+", ".join(reasons) if reasons else ""))
        if result.get("is_max"):
            raise BrokerError("Coinbase changed the requested size in preview")
        if not result.get("preview_id"):
            raise BrokerError("Coinbase did not return a preview identifier")
        return {"preview_id":result["preview_id"],"order_total":number(positive(result.get("order_total"),"preview total")),
                "commission_total":number(decimal(result.get("commission_total"),"preview commission")),
                "warnings":[str(x)[:100] for x in result.get("warning",[]) if isinstance(x,str)]}

    def book(self, pid):
        raw = self._call("get_product_book",product_id=pid,limit=50).get("pricebook")
        if not isinstance(raw,dict) or raw.get("product_id") != pid:
            raise BrokerError("Coinbase did not return the matching order book")
        try:
            stamp = datetime.fromisoformat(raw["time"].replace("Z","+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timezone missing")
            ts = stamp.timestamp()
        except (KeyError,TypeError,ValueError):
            raise BrokerError("Coinbase order book has no valid timestamp") from None
        if not -2 <= self.clock()-ts <= 10:
            raise BrokerError("Coinbase order book is stale")
        result = {"product_id":pid,"ts":ts}
        for side in ("bids","asks"):
            levels = raw.get(side)
            if not isinstance(levels,list) or not 1 <= len(levels) <= 1000:
                raise BrokerError("Coinbase order book is incomplete")
            parsed = []
            for level in levels:
                if not isinstance(level,dict):
                    raise BrokerError("Invalid Coinbase order book level")
                price, size = positive(level.get("price")), decimal(level.get("size"))
                if size:
                    parsed.append({"price":number(price),"size":number(size)})
            if not parsed:
                raise BrokerError("Coinbase order book has no executable size")
            prices = [decimal(x["price"]) for x in parsed]
            if len(set(prices)) != len(prices) or prices != sorted(prices,reverse=side=="bids"):
                raise BrokerError("Coinbase order book levels are unordered or duplicated")
            result[side] = parsed
        if decimal(result["bids"][0]["price"]) > decimal(result["asks"][0]["price"]):
            raise BrokerError("Coinbase order book is crossed")
        return result

    def submit(self, payload):
        if not payload.get("client_order_id"):
            raise BrokerError("A persisted client order ID is required")
        result = self._call("create_order", write=True, **copy.deepcopy(payload))
        if result.get("success") is False:
            return {"accepted":False,"reason":"Coinbase rejected this order"}
        success = result.get("success_response",{})
        if result.get("success") is not True or not success.get("order_id"):
            raise SubmissionUnknown("Coinbase did not return an unambiguous order acknowledgement")
        if success.get("client_order_id") != payload["client_order_id"]:
            raise SubmissionUnknown("Coinbase returned a different client order identifier")
        return {"accepted":True,"order_id":success["order_id"]}

    def order(self, order_id):
        order = self._call("get_order", order_id=order_id).get("order")
        if not isinstance(order,dict) or order.get("order_id") != order_id:
            raise BrokerError("Coinbase order reconciliation returned a different order")
        return order

    def list_orders(self, pid, since, active_only=False):
        orders, cursor, seen = [], None, set()
        for _ in range(30):
            options = {"product_ids":[pid],"product_type":"SPOT","limit":250}
            if active_only: options["order_status"] = sorted(ACTIVE)
            else: options["start_date"] = iso(since-60)
            if cursor: options["cursor"] = cursor
            response = self._call("list_orders",**options)
            if not isinstance(response.get("orders"),list):
                raise BrokerError("Coinbase order history is incomplete")
            orders.extend(response["orders"])
            if not response.get("has_next"):
                return orders
            cursor = response.get("cursor")
            if not cursor or cursor in seen:
                raise BrokerError("Coinbase order history pagination did not complete")
            seen.add(cursor)
        raise BrokerError("Coinbase order history exceeded the bounded reconciliation window")

    def lookup(self, client_id, pid, since):
        found = [o for o in self.list_orders(pid,since) if o.get("client_order_id") == client_id]
        if len(found) > 1:
            raise BrokerError("Coinbase returned multiple orders for a client identifier")
        return self.order(found[0]["order_id"]) if found else None

    def cancel(self, order_id):
        result = self._call("cancel_orders",write=True,order_ids=[order_id])
        match = [r for r in result.get("results",[]) if r.get("order_id")==order_id]
        # An accepted cancel is not a confirmed terminal order status.
        return len(match)==1 and match[0].get("success") is True


def entry_plan(signal, product, quote, snapshot, capital, settings, client_id):
    """Build a bounded FOK buy with attached exchange-held TP/SL."""
    if signal["params"].get("direction") != "LONG":
        raise BrokerError("The Coinbase trader only opens long spot positions")
    fee = decimal(snapshot["taker_fee_rate"])
    configured = decimal(settings["fee_rate"])
    if configured < fee:
        raise BrokerError("Your research fee is below the current Coinbase taker fee; update it and rerun research")
    ask, bid = positive(quote["ask"]), positive(quote["bid"])
    slip = decimal(settings["slippage_rate"])
    if (ask-bid)/((ask+bid)/2) > decimal(settings["max_spread"]):
        raise BrokerError("Coinbase spread exceeds the configured limit")
    p = signal["params"]
    atr = max(positive(signal["atr"],"signal ATR"), positive(signal["close"])*Decimal(".002"))
    if abs(ask-positive(signal["close"])) > atr*decimal(p.get("max_gap_atr",.5)):
        raise BrokerError("The Coinbase quote has moved too far from the signal")
    # Limit the entry price; never replace a rejected protected order with a naked buy.
    limit = rounded(ask*(1+slip), product["price_increment"],up=True)
    distance = max(atr*positive(p["stop_atr"]), limit*Decimal(".0015"))
    stop = rounded(limit-distance, product["price_increment"],up=True)
    target = rounded(limit+distance*positive(p["rr2"]), product["price_increment"])
    if not 0 < stop < bid <= ask <= limit < target:
        raise BrokerError("Rounded stop/target prices do not surround the current market")
    stop_fill = stop*(1-slip)
    unit_risk = limit-stop_fill+(limit+stop_fill)*configured
    cost = (limit+stop_fill)*configured+(limit-ask)+(stop-stop_fill)
    if cost/(limit-stop) > decimal(p.get("max_cost_r",.5)):
        raise BrokerError("Trading costs are too large for this signal's stop")
    quality = net_payoff(limit,stop,target,configured,slip)
    if quality["net_rr"] < decimal(p.get("min_net_rr",0)):
        raise BrokerError("Potential target profit after costs is too small relative to stop risk")
    available = decimal(snapshot["balances"].get("USD",{}).get("available","0"))
    capital = min(positive(capital),Decimal("500"))
    risk = capital*decimal(settings["risk_per_trade"])
    budget = min(available, capital*decimal(settings["max_notional_fraction"]),Decimal("150"),
                 positive(product["quote_max_size"]))
    qty = rounded(min(risk/unit_risk,budget/(limit*(1+configured)),positive(product["base_max_size"])),
                  product["base_increment"])
    if qty < positive(product["base_min_size"]) or qty*limit < positive(product["quote_min_size"]):
        raise BrokerError("The risk-sized order is below Coinbase's minimum size")
    if qty*limit*(1+configured) > budget:
        raise BrokerError("Rounded order exceeds the available budget")
    payload = {"client_order_id":client_id,"product_id":product["product_id"],"side":"BUY",
               "order_configuration":{"limit_limit_fok":{"base_size":number(qty),"limit_price":number(limit)}},
               "attached_order_configuration":{"trigger_bracket_gtc":{
                   "limit_price":number(target),"stop_trigger_price":number(stop)}}}
    return {"payload":payload,"base_size":number(qty),"max_entry_price":number(limit),
            "stop":number(stop),"target":number(target),"risk_usd":number(qty*unit_risk),
            "cash_cap":number(budget),"fee_cap":number(qty*limit*configured),
            "max_spend":number(qty*limit*(1+configured)),
            "net_reward_risk":number(quality["net_rr"]),
            "target_profit_usd":number(qty*quality["unit_reward"]),
            "break_even_win_rate":number(quality["break_even_win_rate"]) if quality["break_even_win_rate"] is not None else None,
            "product":product,"signal":signal,"quote_ts":quote["ts"]}


def book_execution(plan, book, settings, now):
    """Check visible entry and immediate liquidation depth, not future liquidity."""
    if book["product_id"] != plan["payload"]["product_id"] or not -2 <= now-book["ts"] <= 10:
        raise BrokerError("The liquidity snapshot does not match the current entry")
    qty = positive(plan["base_size"])
    limit = positive(plan["max_entry_price"])
    bid, ask = positive(book["bids"][0]["price"]), positive(book["asks"][0]["price"])
    if bid > ask or (ask-bid)/((ask+bid)/2) > decimal(settings["max_spread"]):
        raise BrokerError("Order book spread exceeds the configured limit")

    def walk(side, bound):
        remaining, value = qty, ZERO
        for level in book[side]:
            price, size = positive(level["price"]), decimal(level["size"])
            if (side=="asks" and price>bound) or (side=="bids" and price<bound):
                break
            taken = min(remaining,size)
            value += price*taken
            remaining -= taken
            if remaining == 0:
                return value/qty
        raise BrokerError("Insufficient visible "+side+" depth within the allowed price range")

    buy = walk("asks",limit)
    sell = walk("bids",bid*(1-decimal(settings["slippage_rate"])))
    return {"book_ts":book["ts"],"entry_vwap":number(buy),"immediate_exit_vwap":number(sell),
            "entry_impact_bps":number((buy/ask-1)*10000),
            "exit_impact_bps":number((1-sell/bid)*10000),
            "scope":"Visible depth now; no guarantee of a fill or future stop liquidity"}
