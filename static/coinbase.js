"use strict";
(function () {
  const el = function (id) { return document.getElementById(id); };
  const esc = function (x) { return String(x == null ? "—" : x).replace(/[&<>"']/g,function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); };
  const money = function (x) { return x == null || !Number.isFinite(Number(x)) ? "—" : Number(x).toLocaleString("en-US",{style:"currency",currency:"USD"}); };
  const percent = function (x) { return x == null ? "—" : (Number(x)*100).toFixed(3)+"%"; };
  let current = null, busy = false;
  async function request(path,body) {
    const headers = {}, token = sessionStorage.getItem("cryptoAccessToken");
    if (token) headers.Authorization="Bearer "+token;
    const options={headers:headers,credentials:"same-origin"};
    if (body !== undefined) { options.method="POST"; headers["Content-Type"]="application/json"; options.body=JSON.stringify(body); }
    const response=await fetch("/api/coinbase/"+path,options), result=await response.json();
    if (!response.ok) throw new Error(result.error || "Coinbase request failed");
    return result;
  }
  function render(s) {
    current=s;
    const a=s.snapshot;
    el("cbMode").textContent=(s.running ? "RUNNING · " : "STOPPED · ")+s.mode.toUpperCase();
    el("cbMode").className="badge"+(s.running && s.mode==="live" ? " negative" : "");
    el("cbMessage").textContent=s.last_error || s.message;
    el("cbMessage").className="callout"+(s.last_error ? " negative" : "");
    el("cbCash").textContent=a ? money((a.balances.USD || {}).available) : "—";
    el("cbFees").textContent=a ? percent(a.taker_fee_rate)+" / "+percent(a.maker_fee_rate) : "—";
    el("cbPnl").textContent=a ? money(s.realized_pnl) : "—";
    el("cbEquity").textContent=money(s.last_equity);
    el("cbAccountNote").textContent=a ? "Last sync: "+new Date(a.synced_at*1000).toLocaleString()+" · Fee tier: "+a.fee_tier+(s.equity_stale ? " · Marked equity is unavailable or stale" : "") : "No authenticated account data loaded.";
    el("cbPause").textContent=s.paused ? "Resume Coinbase entries" : "Pause Coinbase entries";
    el("cbSync").disabled=!!s.running;
    el("cbPreview").disabled=!!s.running || !s.configured;
    el("cbLive").disabled=!!s.running || !s.live_orders_allowed;
    el("cbStop").disabled=!s.running;
    el("cbApplyFee").disabled=!a;
    el("cbClose").disabled=!s.running || s.mode!=="live" || !s.trades.some(function (t) { return !["CLOSED","REJECTED"].includes(t.status); });
    el("cbDay").textContent=s.risk_day && s.risk_day.date ? "UTC day: "+s.risk_day.date+" · Equity change: "+money(s.risk_day.pnl)+(s.risk_day.halted ? " · New entries blocked for today" : "") : "The live account tracks its own daily loss threshold.";
    if (s.last_preview) {
      const plan=s.last_preview.plan, preview=s.last_preview.preview;
      el("cbPreviewResult").innerHTML="<h3>"+esc(plan.payload.product_id)+" · BUY</h3><p>Quantity: "+esc(plan.base_size)+"<br>Maximum entry price: "+money(plan.max_entry_price)+"<br>Stop trigger: "+money(plan.stop)+" · Target: "+money(plan.target)+"</p><p>Estimated stop risk: "+money(plan.risk_usd)+"<br>Potential target profit after modeled costs: "+money(plan.target_profit_usd)+"<br>Net reward / risk: "+(plan.net_reward_risk == null ? "—" : Number(plan.net_reward_risk).toFixed(2))+"<br>Preview commission: "+money(preview.commission_total)+"<br>Preview total: "+money(preview.order_total)+'</p><p class="footnote">'+esc(new Date(s.last_preview.created_at*1000).toLocaleString())+" · "+esc(s.last_preview.mode)+" mode</p>";
    }
    el("cbTrades").innerHTML=s.trades.length ? s.trades.map(function (t) {
      return "<tr><td><b>"+esc(t.product_id)+"</b><small>"+esc(new Date(t.created_at*1000).toLocaleString())+"</small></td><td>"+esc(t.status)+"</td><td>"+esc(t.entry_fill ? t.entry_fill.qty : "Awaiting fill")+"<small>Remaining: "+esc(t.remaining)+"</small></td><td>"+money(t.pnl)+"</td><td class=\"order-id\">"+esc(t.entry.order_id || "Awaiting acknowledgement")+"</td><td>"+esc(t.note || t.exit_reason || "")+"</td></tr>";
    }).join("") : '<tr><td colspan="6" class="empty">No real Coinbase orders recorded.</td></tr>';
  }
  async function refresh() {
    if (busy) return;
    busy=true;
    try { render(await request("status")); }
    catch (e) { el("cbMessage").textContent=e.message; }
    finally { busy=false; }
  }
  function button(id,fn) {
    el(id).addEventListener("click",async function () {
      el(id).disabled=true;
      try { await fn(); await refresh(); }
      catch (e) { el("cbMessage").textContent=e.message; el("cbMessage").className="callout negative"; }
      finally { if (current) { const message=el("cbMessage").textContent; render(current); el("cbMessage").textContent=message; } else { el(id).disabled=false; } }
    });
  }
  button("cbSync",function () { return request("sync",{}); });
  button("cbPreview",function () { return request("start",{mode:"preview"}); });
  button("cbLive",async function () {
    const phrase=prompt("This starts automatic REAL Coinbase orders using qualified signals, up to $500 of capital and one position at a time. Type ENABLE COINBASE LIVE to continue.");
    if (phrase!=="ENABLE COINBASE LIVE") return;
    await request("start",{mode:"live",confirm:phrase});
  });
  button("cbStop",function () { return request("stop",{}); });
  button("cbPause",function () { return request("pause",{paused:!(current && current.paused)}); });
  button("cbClose",async function () {
    if (!confirm("Close the bot-owned Coinbase position? The app must confirm protective-order cancellation before selling the remaining quantity.")) return;
    await request("close",{});
  });
  button("cbApplyFee",async function () {
    await request("apply-fees",{});
    window.dispatchEvent(new Event("coinbase-fee-applied"));
  });
  async function poll() { if (!document.hidden) await refresh(); setTimeout(poll,5000); }
  poll();
})();
