"""Public Coinbase data. REST candles drive signals; tickers only mark positions.

Ticker messages may batch trades, so last_size is never used as candle volume.
No credentials or order submission are supported.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

try:
    import websocket
except ImportError:
    websocket = None

REST = "https://api.exchange.coinbase.com"
WS = "wss://ws-feed.exchange.coinbase.com"
GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
STABLE_BASES = {"USDC", "USDT", "DAI", "PYUSD", "EURC", "USDG", "GUSD", "PAX", "TUSD", "USDP", "FDUSD", "USDS", "RLUSD"}
PREFERRED = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD", "LINK-USD", "AVAX-USD", "LTC-USD", "BCH-USD"]


def timestamp(value):
    if not value:
        raise ValueError("Market update has no timestamp")
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class CoinbaseClient:
    def __init__(self, timeout=8):
        self.timeout = timeout
        self._rate_lock = threading.Lock()
        self._next_request = 0.0

    def _get(self, path, params=None):
        url = REST + path + ("?" + urlencode(params) if params else "")
        for attempt in range(3):
            with self._rate_lock:
                delay = max(0.0, self._next_request - time.monotonic())
                self._next_request = max(time.monotonic(), self._next_request) + .26
            if delay:
                time.sleep(delay)
            try:
                req = Request(url, headers={"User-Agent": "CryptO-Lab/7.0", "Accept": "application/json"})
                with urlopen(req, timeout=self.timeout) as response:
                    return json.load(response)
            except HTTPError as exc:
                if attempt == 2 or (exc.code != 429 and exc.code < 500):
                    raise
                time.sleep(1 + attempt)

    def products(self):
        return self._get("/products")

    def stats(self, pid):
        return self._get(f"/products/{quote(pid, safe='')}/stats")

    def ticker(self, pid):
        raw = self._get(f"/products/{quote(pid, safe='')}/ticker")
        return {"product_id": pid, "price": float(raw["price"]),
                "best_bid": float(raw.get("bid") or 0), "best_ask": float(raw.get("ask") or 0),
                "ts": timestamp(raw.get("time")), "source": "rest"}

    def candles(self, pid, interval="5m", limit=300, end_ms=None):
        """Paginate within the 300-bucket API limit; omit open candles."""
        step = GRANULARITY[interval]
        now = int(time.time() * 1000) if end_ms is None else int(end_ms)
        cutoff = now // (step * 1000) * step * 1000
        end = cutoff // 1000
        result = {}
        limit = max(1, min(1500, int(limit)))
        for _ in range((limit + 299) // 300 + 2):
            if len(result) >= limit:
                break
            start = end - min(300, limit - len(result)) * step
            iso = lambda n: datetime.fromtimestamp(n, timezone.utc).isoformat()
            data = self._get(f"/products/{quote(pid, safe='')}/candles", {
                "granularity": step, "start": iso(start), "end": iso(end)})
            if not isinstance(data, list):
                raise ValueError(f"Invalid candle response for {pid}")
            for row in data:
                ts = int(row[0]) * 1000
                if ts < start * 1000 or ts >= end * 1000 or ts >= cutoff or ts % (step * 1000):
                    continue
                low, high, op, close, volume = map(float, (row[1], row[2], row[3], row[4], row[5]))
                if not all(math.isfinite(v) for v in (op, high, low, close, volume)):
                    continue
                if low <= 0 or volume < 0 or low > min(op, close) or high < max(op, close):
                    continue
                result[ts] = {"ts": ts, "open": op, "high": high, "low": low, "close": close,
                              "volume": volume, "quote_volume": volume * close, "trades": 0}
            if not data:
                break
            end = start
        return [result[t] for t in sorted(result)][-limit:]

    def discover_top_usd(self, limit=30, stop_event=None):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        eligible = []
        for p in self.products():
            pid = p.get("id", "")
            if (p.get("quote_currency") == "USD" and p.get("base_currency") not in STABLE_BASES
                    and p.get("status") == "online" and not p.get("trading_disabled")
                    and not p.get("cancel_only") and not p.get("auction_mode") and pid.endswith("-USD")):
                eligible.append(pid)
        preferred = [p for p in PREFERRED if p in eligible]
        candidates = (preferred + sorted(p for p in eligible if p not in preferred))[:80]

        def volume(pid):
            if stop_event and stop_event.is_set():
                return pid, 0
            try:
                s = self.stats(pid)
                return pid, float(s.get("last") or 0) * float(s.get("volume") or 0)
            except Exception:
                return pid, 0

        with ThreadPoolExecutor(max_workers=4) as pool:
            ranked = [f.result() for f in as_completed([pool.submit(volume, p) for p in candidates])]
        ranked.sort(key=lambda x: (-x[1], x[0]))
        selected = [pid for pid, vol in ranked if math.isfinite(vol) and vol > 0][:limit]
        if not selected and not (stop_event and stop_event.is_set()):
            raise RuntimeError("Coinbase returned no active USD markets with usable volume data")
        return selected


class CoinbaseTickerStream:
    def __init__(self, products, on_tick, on_status=None):
        self.products = list(products)
        self.on_tick = on_tick
        self.on_status = on_status or (lambda *_: None)
        self._stop = threading.Event()
        self.thread = None
        self.ws = None
        self.last_message_at = None
        self._last_trade = {}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="coinbase-stream")
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.ws:
            self.ws.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def _message(self, ws, raw):
        self.last_message_at = time.time()
        try:
            m = json.loads(raw)
            if m.get("type") == "error":
                self.on_status("error", str(m.get("message", "Coinbase rejected the subscription")))
                return
            if m.get("type") != "ticker":
                return
            pid = m.get("product_id")
            price = float(m.get("price") or 0)
            ts = timestamp(m.get("time"))
            if pid not in self.products or not math.isfinite(price) or price <= 0 or ts > time.time() * 1000 + 5000:
                return
            trade_id = m.get("trade_id")
            if trade_id is not None:
                trade_id = int(trade_id)
                if trade_id <= self._last_trade.get(pid, -1):
                    return
                self._last_trade[pid] = trade_id
            tick = {"product_id": pid, "ts": ts, "price": price,
                    "best_bid": float(m.get("best_bid") or 0),
                    "best_ask": float(m.get("best_ask") or 0), "source": "websocket"}
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.on_status("warning", f"Invalid market update: {exc}")
            return
        try:
            self.on_tick(tick)
        except Exception as exc:
            self.on_status("error", f"Could not process market update: {exc}")

    def _run(self):
        if websocket is None:
            self.on_status("error", "Install websocket-client to enable the live price feed")
            return
        backoff = 2
        while not self._stop.is_set():
            opened = time.monotonic()
            try:
                self.on_status("connecting", "Connecting to Coinbase")

                def on_open(ws):
                    ws.send(json.dumps({"type": "subscribe", "product_ids": self.products,
                                        "channels": ["ticker", "heartbeat"]}))
                    self.on_status("live", "Coinbase connected; waiting for fresh market prices")

                self.ws = websocket.WebSocketApp(WS, on_open=on_open, on_message=self._message,
                    on_error=lambda ws, err: self.on_status("warning", f"Coinbase stream: {err}"),
                    on_close=lambda ws, code, msg: None)
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self.on_status("warning", f"Stream reconnect: {exc}")
            if self._stop.is_set():
                break
            self.on_status("reconnecting", "Connection lost; entries wait for fresh data")
            if time.monotonic() - opened > 60:
                backoff = 2
            if self._stop.wait(backoff):
                break
            backoff = min(30, backoff * 1.5)
        self.on_status("stopped", "Market stream stopped")
