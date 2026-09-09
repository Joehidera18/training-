"""One-position Coinbase spot runner with separate, durable live bookkeeping.

Preview mode cannot place or cancel orders. Live mode requires local opt-in and
a deliberate application control. Unknown submissions are looked up, not retried.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import threading
import time
import uuid
from decimal import Decimal

from .coinbase_broker import (CoinbaseAdapter, BrokerError, SubmissionUnknown,
    ACTIVE, TERMINAL, ZERO, decimal, positive, number, rounded, entry_plan, book_execution)
from .trade_quality import cooldown_minutes
from .continuous import RuntimeLease, write_state
from .paper_store import db_connect, load_state

DONE = {"CLOSED", "REJECTED"}


class CoinbaseTrader:
    def __init__(self, db_path, agent, adapter=None, clock=time.time):
        self.db_path, self.agent, self.clock = db_path, agent, clock
        self.adapter = adapter or CoinbaseAdapter()
        self.lock = threading.RLock()
        self.worker, self.stop_event = None, threading.Event()
        self.lease = RuntimeLease(str(db_path)+".coinbase")
        self.mode, self.paused = "idle", False
        self.runtime = {"running":False,"message":"Coinbase is not connected","last_error":None}
        con = db_connect(db_path)
        try:
            with con:
                con.execute("""CREATE TABLE IF NOT EXISTS coinbase_trades(
                    id TEXT PRIMARY KEY, signal_key TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL, trade_json TEXT NOT NULL)""")
                con.execute("""CREATE TABLE IF NOT EXISTS coinbase_previews(
                    signal_key TEXT PRIMARY KEY, created_at REAL NOT NULL, preview_json TEXT NOT NULL)""")
        finally: con.close()
        self.state = load_state(db_path,"coinbase_state",
            {"snapshot":None,"portfolio":None,"capital_start":None,"risk_day":{},
             "last_preview":None,"last_equity":None,"equity_at":None})

    def _state_save(self, **patch):
        with self.lock:
            self.state.update(patch)
            con = db_connect(self.db_path)
            try:
                con.execute("PRAGMA synchronous=FULL")
                with con: write_state(con,"coinbase_state",self.state)
            finally: con.close()

    def _message(self, message, error=None):
        with self.lock:
            self.runtime.update(message=message,last_error=error)

    def trades(self, limit=100):
        con = db_connect(self.db_path)
        try:
            rows = con.execute("SELECT trade_json FROM coinbase_trades ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
            return [json.loads(r[0]) for r in rows]
        finally: con.close()

    def _active(self):
        con = db_connect(self.db_path)
        try:
            rows = con.execute("SELECT trade_json FROM coinbase_trades WHERE status NOT IN ('CLOSED','REJECTED')").fetchall()
        finally: con.close()
        if len(rows) > 1:
            raise BrokerError("Multiple active live records require reconciliation; entries are blocked")
        return json.loads(rows[0][0]) if rows else None

    def _trade_save(self, trade):
        con = db_connect(self.db_path)
        try:
            # Order intent must survive a power failure before a POST can begin.
            con.execute("PRAGMA synchronous=FULL")
            with con:
                con.execute("BEGIN IMMEDIATE")
                previous = con.execute("SELECT status FROM coinbase_trades WHERE id=?",(trade["id"],)).fetchone()
                if trade["status"]=="CLOSED" and (not previous or previous["status"]!="CLOSED"):
                    from .adaptive import record_outcome, write_in_transaction
                    signal = trade.get("plan",{}).get("signal",{})
                    if signal.get("learning"):
                        try:
                            risk = float(trade["plan"]["risk_usd"])
                            if not math.isfinite(risk) or risk <= 0:
                                raise ValueError("Invalid learning risk denominator")
                            record_outcome(con,trade["product_id"],"coinbase",signal["params"],
                                signal["learning"],float(trade["pnl"])/risk,int(trade["closed_at"]*1000))
                        except (KeyError, TypeError, ValueError) as exc:
                            write_in_transaction(con,"adaptive_error_coinbase_"+trade["product_id"],
                                {"message":str(exc),"trade_id":trade["id"],"time":self.clock()})
                con.execute("""INSERT INTO coinbase_trades(id,signal_key,created_at,status,trade_json)
                    VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,trade_json=excluded.trade_json""",
                    (trade["id"],trade["signal_key"],trade["created_at"],trade["status"],
                     json.dumps(trade,allow_nan=False,separators=(",",":"))))
        finally: con.close()

    def _realized(self):
        con = db_connect(self.db_path)
        try:
            return sum((decimal(json.loads(r[0])["pnl"],"P&L",minimum=Decimal("-1000000"))
                        for r in con.execute("SELECT trade_json FROM coinbase_trades WHERE status='CLOSED'")),ZERO)
        finally: con.close()

    def _closed_market_trades(self, pid, limit=3):
        con = db_connect(self.db_path)
        try:
            matches = []
            for row in con.execute("SELECT trade_json FROM coinbase_trades WHERE status='CLOSED' ORDER BY created_at DESC"):
                trade = json.loads(row[0])
                if trade.get("product_id")==pid:
                    matches.append(trade)
                if len(matches)>=limit:
                    break
            return matches
        finally: con.close()

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            result.update(self.runtime, mode=self.mode, paused=self.paused,
                          configured=self.adapter.configured, live_orders_allowed=self.adapter.allow_live)
        result["trades"] = self.trades(25)
        result["realized_pnl"] = number(self._realized())
        result["equity_stale"] = not result["equity_at"] or self.clock()-result["equity_at"]>45
        result["maximum_capital_usd"], result["maximum_positions"] = 500, 1
        return result

    def _account_sync(self):
        snapshot = self.adapter.snapshot()
        bound = self.state.get("portfolio")
        if bound and bound != snapshot["portfolio"]:
            raise BrokerError("This live journal belongs to a different Coinbase portfolio; do not reuse it with this key")
        self._state_save(snapshot=snapshot,portfolio=snapshot["portfolio"])
        return snapshot

    def _permissions(self, snapshot):
        if snapshot["permissions"].get("can_trade") is not True:
            raise BrokerError("The Coinbase key needs Trade permission for live execution")
        if snapshot["permissions"].get("can_transfer") is not False:
            raise BrokerError("Use a Coinbase key without Transfer permission")
        if not snapshot["simple_fees"]:
            raise BrokerError("This account has additional fee pricing that the live risk model does not yet support")

    def _flat_checks(self, snapshot):
        # A dedicated portfolio prevents this bot from selling preexisting holdings.
        for currency, balance in snapshot["balances"].items():
            if currency != "USD" and decimal(balance["available"])+decimal(balance["hold"]) > 0:
                raise BrokerError("Use a dedicated USD-funded spot portfolio without other crypto holdings")
        usd = snapshot["balances"].get("USD",{"available":"0","hold":"0"})
        if decimal(usd["hold"]) != 0:
            raise BrokerError("Existing USD holds must settle before the live trader can enter")
        if self.state["capital_start"] is None:
            capital = min(decimal(usd["available"]),Decimal("500"))
            if capital < 25:
                raise BrokerError("At least $25 of available USD is needed for this bounded live runner")
            self._state_save(capital_start=number(capital))
        return min(Decimal("500"),positive(self.state["capital_start"])+self._realized())

    def _equity_and_day(self, trade=None, quote=None):
        if self.state["capital_start"] is None:
            return None
        equity = positive(self.state["capital_start"])+self._realized()
        if trade and trade.get("entry_fill"):
            entry = trade["entry_fill"]
            sold_qty, sold_value, sold_fees = self._sold(trade)
            remaining = decimal(entry["qty"])-sold_qty
            if remaining > 0 and quote is None:
                return None
            fee = decimal(self.state["snapshot"]["taker_fee_rate"])
            marked = remaining*decimal(quote["bid"])*(1-fee) if remaining else ZERO
            equity += sold_value-sold_fees+marked-decimal(entry["value"])-decimal(entry["fees"])
        day = time.strftime("%Y-%m-%d",time.gmtime(self.clock()))
        risk_day = copy.deepcopy(self.state["risk_day"])
        if risk_day.get("date") != day:
            risk_day = {"date":day,"start_equity":number(equity),"halted":False}
        daily_limit = min(Decimal("15"),positive(risk_day["start_equity"])*decimal(self.agent.settings["daily_loss_limit"]))
        if equity <= decimal(risk_day["start_equity"])-daily_limit:
            risk_day["halted"] = True
        risk_day["pnl"] = number(equity-decimal(risk_day["start_equity"]))
        self._state_save(risk_day=risk_day,last_equity=number(equity),equity_at=self.clock())
        return equity

    def _mutations_allowed(self):
        if self.mode != "live" or self.stop_event.is_set() or not self.adapter.allow_live:
            raise BrokerError("Live writes are not armed in this runner")
        self._permissions(self.state["snapshot"])

    def _record_submit(self, trade, record):
        self._mutations_allowed()
        # Persist the intent and its stable UUID before crossing the network boundary.
        record["submission_started"] = True
        self._trade_save(trade)
        try:
            ack = self.adapter.submit(record["payload"])
            if ack["accepted"]:
                record["order_id"] = ack["order_id"]
                record["submission_state"] = "ACKNOWLEDGED"
            else:
                record["submission_state"] = "REJECTED"
                record["snapshot"] = {"status":"REJECTED","qty":"0","value":"0","fees":"0","settled":True}
        except SubmissionUnknown:
            record["submission_state"] = "UNKNOWN"
            self._trade_save(trade)
            raise
        self._trade_save(trade)

    def _parse_order(self, raw, trade, side, client_id=None):
        if raw.get("product_id") != trade["product_id"] or raw.get("side") != side:
            raise BrokerError("Order identity does not match the local live journal")
        if client_id and raw.get("client_order_id") != client_id:
            raise BrokerError("The reconciled client order identifier does not match")
        status = raw.get("status")
        if status not in ACTIVE | TERMINAL:
            raise BrokerError("Unknown Coinbase order status; further execution is blocked")
        qty = decimal(raw.get("filled_size"),"filled quantity")
        value = decimal(raw.get("filled_value"),"filled value")
        fees = decimal(raw.get("total_fees"),"actual fees")
        native = raw.get("total_fees_native") or {}
        if native.get("currency") not in (None,"","USD"):
            raise BrokerError("Non-USD commission requires manual reconciliation")
        if qty > 0 and value <= 0:
            raise BrokerError("Filled quantity has no confirmed execution value")
        return {"status":status,"qty":number(qty),"value":number(value),"fees":number(fees),
                "settled":raw.get("settled") is True,"attached_order_id":raw.get("attached_order_id"),
                "last_fill_time":raw.get("last_fill_time"),"order_id":raw["order_id"]}

    def _refresh_record(self, trade, record, side):
        if record.get("submission_state") == "REJECTED":
            return record["snapshot"], None
        if record.get("order_id"):
            raw = self.adapter.order(record["order_id"])
        else:
            raw = self.adapter.lookup(record["payload"]["client_order_id"],trade["product_id"],record["created_at"])
        if raw is None:
            record["submission_state"] = "UNKNOWN"
            trade["status"] = "UNKNOWN"
            self._trade_save(trade)
            raise BrokerError("Order outcome is unknown. No retry or new position will be submitted; check Coinbase")
        record["order_id"], record["submission_state"] = raw["order_id"], "ACKNOWLEDGED"
        record["snapshot"] = self._parse_order(raw,trade,side,record["payload"]["client_order_id"])
        return record["snapshot"], raw

    def _sold(self, trade):
        fills = [trade["bracket_fill"]] if trade.get("bracket_fill") else []
        fills += [r["snapshot"] for r in trade.get("exits",[]) if r.get("snapshot")]
        return tuple(sum((decimal(f[k]) for f in fills),ZERO) for k in ("qty","value","fees"))

    def _reconcile(self, trade):
        entry, raw = self._refresh_record(trade,trade["entry"],"BUY")
        trade["entry_fill"] = entry
        if entry["status"] in TERMINAL and decimal(entry["qty"]) == 0:
            trade.update(status="REJECTED",closed_at=self.clock(),note="No entry fill")
            self._trade_save(trade)
            return trade
        if raw and raw.get("attached_order_id"):
            trade["bracket_id"] = raw["attached_order_id"]
        if decimal(entry["qty"]) <= 0:
            trade["status"] = "ENTRY_PENDING"
            self._trade_save(trade)
            return trade
        if decimal(entry["qty"]) > decimal(trade["plan"]["base_size"]):
            raise BrokerError("Entry fill exceeds the planned quantity")
        if not trade.get("bracket_id"):
            children = [o for o in self.adapter.list_orders(trade["product_id"],trade["created_at"])
                        if o.get("originating_order_id") == trade["entry"]["order_id"] and o.get("side")=="SELL"]
            if len(children)==1:
                trade["bracket_id"] = children[0]["order_id"]
            else:
                trade.update(status="NEEDS_ATTENTION",note="Filled entry has no uniquely confirmed protective order. Check Coinbase now.")
                self._trade_save(trade)
                return trade
        bracket = self.adapter.order(trade["bracket_id"])
        trade["bracket_fill"] = self._parse_order(bracket,trade,"SELL")
        if bracket.get("originating_order_id") not in (None,"",trade["entry"]["order_id"]):
            raise BrokerError("Protective order is attached to a different originating order")
        if trade["bracket_fill"]["status"] in ACTIVE and not trade.get("closing"):
            config = bracket.get("order_configuration",{}).get("trigger_bracket_gtc",{})
            if (decimal(config.get("stop_trigger_price")) != decimal(trade["plan"]["stop"]) or
                decimal(config.get("limit_price")) != decimal(trade["plan"]["target"])):
                raise BrokerError("The exchange protective prices do not match the accepted plan")
        for record in trade["exits"]:
            self._refresh_record(trade,record,"SELL")
        sold_qty, sold_value, sold_fees = self._sold(trade)
        entry_qty = decimal(entry["qty"])
        if sold_qty > entry_qty:
            raise BrokerError("Reconciled exits exceed the bot's purchased quantity")
        trade["remaining"] = number(entry_qty-sold_qty)
        if sold_qty == entry_qty:
            fills = [entry,trade["bracket_fill"]]+[r["snapshot"] for r in trade["exits"]]
            if not all(f["status"] in TERMINAL and (decimal(f["qty"])==0 or f["settled"]) for f in fills):
                trade["status"] = "SETTLING"
            else:
                trade.update(status="CLOSED",closed_at=self.clock(),
                    pnl=number(sold_value-sold_fees-decimal(entry["value"])-decimal(entry["fees"])))
                self._state_save(close_trade_id=None)
            self._trade_save(trade)
            return trade
        # Partial bracket fills disable its other side: close the residual after cancellation.
        if sold_qty > 0 or trade["bracket_fill"]["status"] in TERMINAL:
            trade["closing"] = True
            trade.setdefault("exit_reason","partial_fill_or_protection_ended")
        if self.state.get("close_trade_id") == trade["id"]:
            trade["closing"], trade["exit_reason"] = True, "manual"
        if self.clock()-trade["created_at"] >= trade["plan"]["signal"]["params"].get("time_stop_hours",12)*3600:
            trade["closing"], trade["exit_reason"] = True, "time"
        if entry["status"] in TERMINAL and entry_qty != decimal(trade["plan"]["base_size"]):
            trade["closing"], trade["exit_reason"] = True, "unexpected_partial_entry"
        trade["status"] = "EXIT_PENDING" if trade.get("closing") else "OPEN"
        self._trade_save(trade)
        return trade

    def _cancel_known(self, trade, order_id, field):
        self._mutations_allowed()
        previous = trade.get(field,0)
        if self.clock()-previous < 30:
            return
        trade[field] = self.clock()
        self._trade_save(trade)
        self.adapter.cancel(order_id)
        # Never interpret cancel acceptance as permission to send a replacement sell.

    def _manage(self, trade):
        if trade["status"] in DONE or trade["status"] in ("UNKNOWN","NEEDS_ATTENTION","SETTLING"):
            return
        entry = trade.get("entry_fill",{})
        if entry.get("status") in ACTIVE:
            if self.clock()-trade["created_at"]>90:
                self._cancel_known(trade,trade["entry"]["order_id"],"entry_cancel_at")
            return
        if not trade.get("closing"):
            return
        if trade["bracket_fill"]["status"] not in TERMINAL:
            self._cancel_known(trade,trade["bracket_id"],"bracket_cancel_at")
            return
        if trade["exits"] and trade["exits"][-1].get("snapshot",{}).get("status") not in TERMINAL:
            return
        if len(trade["exits"]) >= 5:
            trade.update(status="NEEDS_ATTENTION",note="Repeated incomplete exit fills require review on Coinbase")
            self._trade_save(trade)
            return
        snapshot = self._account_sync()
        self._permissions(snapshot)
        quote = self.adapter.quote(trade["product_id"])
        product = trade["plan"]["product"]
        remaining = decimal(trade["remaining"])
        available = decimal(snapshot["balances"].get(product["base"],{}).get("available","0"))
        if available < remaining:
            self._message("Waiting for Coinbase to release the confirmed residual balance")
            return
        qty = rounded(remaining,product["base_increment"])
        if qty < positive(product["base_min_size"]) or qty*positive(quote["bid"])<positive(product["quote_min_size"]):
            trade.update(status="NEEDS_ATTENTION",note="Residual size is below Coinbase's minimum; resolve the remaining holding on Coinbase")
            self._trade_save(trade)
            return
        payload = {"client_order_id":str(uuid.uuid4()),"product_id":trade["product_id"],"side":"SELL",
                   "order_configuration":{"market_market_ioc":{"base_size":number(qty)}}}
        preview = self.adapter.preview(payload)
        payload["preview_id"] = preview["preview_id"]
        record = {"payload":payload,"created_at":self.clock(),"submission_state":"PREPARED"}
        trade["exits"].append(record)
        self._trade_save(trade)
        self._record_submit(trade,record)

    def _signal_key(self, signal):
        raw = json.dumps({k:signal[k] for k in ("product_id","signal_ts","interval","params")},sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _seen(self, key, live):
        con = db_connect(self.db_path)
        try:
            table = "coinbase_trades" if live else "coinbase_previews"
            return con.execute(f"SELECT 1 FROM {table} WHERE signal_key=?",(key,)).fetchone() is not None
        finally: con.close()

    def _save_preview(self, key, plan, preview):
        report = {"created_at":self.clock(),"plan":plan,"preview":preview,"mode":self.mode}
        con = db_connect(self.db_path)
        try:
            with con:
                con.execute("INSERT OR REPLACE INTO coinbase_previews VALUES(?,?,?)",
                            (key,self.clock(),json.dumps(report,allow_nan=False)))
        finally: con.close()
        self._state_save(last_preview=report)

    def _consider_entries(self, snapshot):
        if self.paused:
            self._message("Coinbase entries are paused; existing orders remain monitored")
            return
        if self.state["risk_day"].get("halted"):
            self._message("The live account's daily entry loss threshold is latched until the next UTC day")
            return
        if not snapshot["simple_fees"]:
            raise BrokerError("Additional account fee pricing requires a supported fee model")
        if self.mode=="live":
            self._permissions(snapshot)
            capital = self._flat_checks(snapshot)
        else:
            capital = min(decimal(snapshot["balances"].get("USD",{}).get("available","0")),Decimal("500"))
        reasons = []
        for pid in ("BTC-USD","ETH-USD","SOL-USD"):
            try:
                signal = self.agent.coinbase_signal(pid)
                key = self._signal_key(signal)
                if self._seen(key,self.mode=="live"):
                    continue
                if self.mode=="live":
                    if self.adapter.list_orders(pid,self.clock()-86400,active_only=True):
                        raise BrokerError("Existing Coinbase orders block a new entry")
                    closed = self._closed_market_trades(pid)
                    last = closed[0] if closed else None
                    delay = cooldown_minutes([decimal(t["pnl"],minimum=Decimal("-1000000")) for t in reversed(closed)],
                                             last.get("plan",{}).get("signal",{}).get("params",{}) if last else signal["params"])
                    if last and self.clock()-last["closed_at"]<delay*60:
                        reasons.append(f"{pid}: market cooldown ({delay:g} minutes after latest close)")
                        continue
                product, book = self.adapter.product(pid), self.adapter.book(pid)
                quote = {"bid":book["bids"][0]["price"],"ask":book["asks"][0]["price"],"ts":book["ts"]}
                plan = entry_plan(signal,product,quote,snapshot,capital,self.agent.settings,str(uuid.uuid4()))
                plan["liquidity"] = book_execution(plan,book,self.agent.settings,self.clock())
                preview = self.adapter.preview(plan["payload"])
                if (decimal(preview["commission_total"])>decimal(plan["fee_cap"]) or
                    decimal(preview["order_total"])>decimal(plan["max_spend"])):
                    raise BrokerError("Coinbase preview costs exceed the tested fee or position budget")
                self._save_preview(key,plan,preview)
                if self.mode=="preview":
                    self._message(f"{pid}: Coinbase preview saved; no order submitted")
                    return
                # A preview is not a reserved fill. Recheck price and signal age.
                book = self.adapter.book(pid)
                fresh = {"bid":book["bids"][0]["price"],"ask":book["asks"][0]["price"],"ts":book["ts"]}
                latest = self.agent.coinbase_signal(pid)
                if self._signal_key(latest)!=key or self.clock()-fresh["ts"]>30:
                    raise BrokerError("The signal changed during preview")
                if positive(fresh["ask"])>decimal(plan["max_entry_price"]) or positive(fresh["bid"])<=decimal(plan["stop"]):
                    raise BrokerError("The executable price moved outside the previewed entry bounds")
                fresh_bid, fresh_ask = positive(fresh["bid"]), positive(fresh["ask"])
                if (fresh_ask-fresh_bid)/((fresh_ask+fresh_bid)/2)>decimal(self.agent.settings["max_spread"]):
                    raise BrokerError("The spread widened beyond the configured limit during preview")
                plan["liquidity"] = book_execution(plan,book,self.agent.settings,self.clock())
                self._mutations_allowed()
                payload = copy.deepcopy(plan["payload"]); payload["preview_id"] = preview["preview_id"]
                trade = {"id":str(uuid.uuid4()),"signal_key":key,"product_id":pid,"created_at":self.clock(),
                         "status":"ENTRY_PENDING","plan":plan,"exits":[],
                         "entry":{"payload":payload,"created_at":self.clock(),"submission_state":"PREPARED"}}
                self._trade_save(trade)
                self._record_submit(trade,trade["entry"])
                self._message(f"{pid}: submitted one protected entry; awaiting exchange confirmation")
                return
            except BrokerError as exc:
                if self._active():
                    raise
                reasons.append(f"{pid}: {exc}")
            except ValueError as exc:
                reasons.append(f"{pid}: {exc}")
        self._message("; ".join(reasons) if reasons else "Waiting for a new qualified signal")

    def tick(self, allow_entries=True):
        snapshot = self._account_sync()
        trade = self._active()
        quote = None
        if trade:
            trade = self._reconcile(trade)
            if trade["status"] not in DONE:
                quote = self.adapter.quote(trade["product_id"])
                self._equity_and_day(trade,quote)
                if self.mode=="live":
                    self._manage(trade)
                self._message(trade.get("note") or f"{trade['product_id']}: {trade['status'].lower()}")
                return
        if self.mode=="live":
            self._flat_checks(snapshot)
        self._equity_and_day()
        if allow_entries and self.mode in ("preview","live"):
            self._consider_entries(snapshot)
        else:
            self._message("Coinbase account, fees and known orders synced")

    def start(self, mode="preview"):
        if mode not in ("sync","preview","live"):
            raise ValueError("Choose sync, preview or live")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise BrokerError("The Coinbase worker is already active")
            if mode=="live" and not self.adapter.allow_live:
                raise BrokerError("Set COINBASE_ALLOW_LIVE=1 locally and restart before arming real orders")
            if mode=="preview" and self._active():
                raise BrokerError("A live position exists; use sync or live monitoring to reconcile it")
            if not self.adapter.configured:
                raise BrokerError("Configure the local Coinbase ECDSA key file first")
            self.lease.acquire()
            self.stop_event.clear()
            self.mode, self.paused = mode, False
            self.runtime.update(running=True,last_error=None,message="Connecting to Coinbase")
            try:
                self.worker = threading.Thread(target=self._run,daemon=True,name="coinbase-runner")
                self.worker.start()
            except Exception:
                self.runtime["running"] = False
                self.lease.release()
                raise

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick(allow_entries=self.mode!="sync")
                except Exception as exc:
                    message = str(exc) if isinstance(exc,(BrokerError,ValueError)) else "Coinbase runner error; entries are blocked until reconciliation succeeds"
                    self._message(message,error=message)
                if self.mode=="sync" or self.stop_event.wait(5):
                    break
        finally:
            with self.lock:
                self.runtime["running"] = False
            self.lease.release()

    def stop(self):
        self.stop_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=2)
        if not self.worker or not self.worker.is_alive():
            self._message("Coinbase runner stopped. Submitted exchange orders remain active.")

    def pause(self, paused=True):
        if not isinstance(paused,bool):
            raise ValueError("paused must be true or false")
        self.paused = paused

    def request_close(self):
        if self.mode!="live" or not self.runtime["running"] or self.stop_event.is_set():
            raise BrokerError("Start live monitoring before requesting a close")
        trade = self._active()
        if not trade:
            raise BrokerError("No active bot-owned Coinbase position exists")
        self._state_save(close_trade_id=trade["id"])
