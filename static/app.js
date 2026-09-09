"use strict";
(function () {
  const $ = function (id) { return document.getElementById(id); };
  let token = sessionStorage.getItem("cryptoAccessToken") || "";
  let state = null, settingsLoaded = false, busy = false, noticeTimer = null;
  const percentFields = new Set(["fee_rate","slippage_rate","risk_per_trade","max_total_risk","daily_loss_limit","max_notional_fraction","max_spread"]);
  const escape = function (value) { return String(value == null ? "—" : value).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); };
  const finite = function (v) { return typeof v === "number" && Number.isFinite(v); };
  const money = function (v) { return finite(v) ? new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",maximumFractionDigits:2}).format(v) : "—"; };
  const num = function (v, d) { return finite(v) ? v.toFixed(d == null ? 2 : d) : "—"; };
  const pct = function (v) { return finite(v) ? num(v) + "%" : "—"; };
  const price = function (v) { return finite(v) ? "$" + v.toLocaleString("en-US",{maximumFractionDigits:v < 1 ? 6 : 2}) : "—"; };
  const tone = function (v) { return finite(v) && v !== 0 ? (v > 0 ? "positive" : "negative") : ""; };
  const family = function (v) { return String(v || "").replace(/_simple$/,"").replace(/_/g," "); };
  const date = function (v, ms) { return v ? new Date(ms ? v : v * 1000).toLocaleString() : "—"; };
  const emptyRow = function (cols, text) { return '<tr><td class="empty" colspan="' + cols + '">' + escape(text) + "</td></tr>"; };
  function notice(message, error) {
    clearTimeout(noticeTimer);
    $("notice").textContent = message; $("notice").className = "notice" + (error ? " error" : ""); $("notice").hidden = false;
    if (!error) noticeTimer = setTimeout(function () { $("notice").hidden = true; }, 6500);
  }
  async function api(path, body, asBlob) {
    const options = {headers:{},credentials:"same-origin"};
    if (token) options.headers.Authorization = "Bearer " + token;
    if (body !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    const response = await fetch(path, options);
    if (!response.ok) {
      if (response.status === 401) $("accessPanel").hidden = false;
      let msg = "Request failed (" + response.status + ")";
      try { msg = (await response.json()).error || msg; } catch (_) {}
      throw new Error(msg);
    }
    return asBlob ? response.blob() : response.json();
  }
  async function download(path, filename) {
    const blob = await api(path, undefined, true), url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = filename; a.click(); setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }
  function bind(id, action) {
    $(id).addEventListener("click", async function () {
      const button = $(id); button.disabled = true;
      try { await action(); } catch (e) { notice(e.message, true); }
      finally { button.disabled = false; }
    });
  }
  function renderState(s) {
    state = s;
    const p = s.portfolio, r = s.runtime, day = s.risk_day || {};
    $("equity").textContent = money(p.equity);
    $("equityNote").textContent = p.equity_stale ? "Stale quote: marked equity may be outdated" : "Starting balance · " + money(p.starting_balance);
    $("return").textContent = pct(p.return_pct); $("return").className = tone(p.return_pct);
    $("realized").textContent = "Realized P&L · " + money(p.realized_pnl);
    $("openRisk").textContent = money(p.open_risk_usd); $("exposure").textContent = "Position value · " + money(p.gross_exposure_usd);
    $("profiles").textContent = Object.keys(s.active_profiles || {}).length;
    $("runtimeBadge").textContent = r.running ? (s.settings.entries_paused ? "Entries paused" : "Paper trader running") : (r.stream_status === "stopping" ? "Stopping" : "Stopped");
    $("runtimeBadge").className = "badge" + (r.running ? " positive" : "");
    $("startBtn").disabled = r.running; $("stopBtn").disabled = !r.running && r.stream_status !== "stopping";
    $("pauseBtn").textContent = s.settings.entries_paused ? "Resume entries" : "Pause entries";
    $("streamState").textContent = r.last_error ? "Data error: " + r.last_error : (r.stream_message || "Paper trader is stopped.");
    $("bootstrapState").textContent = r.running && !r.bootstrapped ? "Loading market history: " + r.bootstrap_done + " / " + r.bootstrap_total + " markets" : (r.last_tick_age_seconds != null ? "Latest quote received " + num(r.last_tick_age_seconds,0) + " seconds ago" : "");
    $("dayPnl").textContent = money(day.pnl); $("dayPnl").className = tone(day.pnl);
    $("dayNote").textContent = day.date ? day.date + " · Change in marked equity since today's baseline" : "Start the paper trader to begin tracking today.";
    $("goalFill").style.width = Math.max(0, Math.min(100, (day.pnl || 0) / 15 * 100)) + "%";
    $("entryState").textContent = day.halted ? "Daily loss threshold reached. New entries are blocked until the next UTC day." : (s.settings.entries_paused ? "New entries paused. Existing positions remain monitored while running." : (s.settings.validated_only ? "Only strategies with a current passing historical profile can enter." : "Experiment mode: unvalidated paper trades use one quarter of configured risk."));
    $("positions").innerHTML = s.positions.length ? s.positions.map(function (p) {
      return "<tr><td><b>" + escape(p.product_id) + "</b><small>" + escape(family(p.family)) + "</small></td><td>" + price(p.entry) + "</td><td>" + price(p.stop) + "<small>" + price(p.target) + "</small></td><td>" + money(p.risk_usd) + '</td><td class="' + tone(p.unrealized_pnl) + '">' + money(p.unrealized_pnl) + (p.price_stale ? "<small>Stale quote</small>" : "") + '</td><td><button class="small" data-close="' + escape(p.product_id) + '">Close</button></td></tr>';
    }).join("") : emptyRow(6,"No open paper positions.");
    $("marketsTable").innerHTML = s.coins.length ? s.coins.map(function (c) {
      return "<tr><td><b>" + escape(c.product_id) + "</b></td><td>" + price(c.price) + '</td><td class="' + (c.price_stale ? "negative" : "") + '">' + (c.quote_age_seconds == null ? "No quote" : num(c.quote_age_seconds,0) + "s") + "</td><td>" + ["5m","15m","1h","4h"].map(function (iv) { return c.bar_counts[iv] || 0; }).join(" / ") + "</td><td>" + escape(c.regime) + "<small>" + escape(c.structure) + "</small></td><td>" + escape(c.last_decision || c.readiness) + "</td></tr>";
    }).join("") : emptyRow(6,"Start the paper trader to load markets.");
    if (!settingsLoaded) {
      const form = $("settingsForm");
      Object.keys(s.settings).forEach(function (k) {
        const input = form.elements.namedItem(k);
        if (!input) return;
        if (input.type === "checkbox") input.checked = s.settings[k];
        else input.value = percentFields.has(k) ? Number((s.settings[k] * 100).toFixed(6)) : s.settings[k];
      });
      settingsLoaded = true;
    }
    $("researchCosts").textContent = "Current assumptions: " + s.settings.decision_interval + " candles · " + pct(s.settings.fee_rate * 100) + " fee per side · " + pct(s.settings.slippage_rate * 100) + " slippage per fill + 0.05% assumed half-spread. Stress test multiplies costs by 1.5.";
  }
  function drawCurve(points) {
    if (points.length < 2) { $("balanceChart").innerHTML = '<p class="empty">Your balance history will appear after a paper trade closes.</p>'; return; }
    const balances = points.map(function (p) { return p.balance; }), lo = Math.min.apply(null,balances), hi = Math.max.apply(null,balances);
    const spread = Math.max(hi - lo, 2), bottom = lo - spread * .1, top = hi + spread * .1;
    const coords = points.map(function (p,i) { return (55 + i / (points.length - 1) * 535).toFixed(2) + "," + (175 - (p.balance - bottom) / (top - bottom) * 155).toFixed(2); }).join(" ");
    $("balanceChart").innerHTML = '<svg viewBox="0 0 620 205" role="img" aria-label="Realized paper balance after each closed trade"><line x1="55" y1="175" x2="590" y2="175" stroke="#263342"/><line x1="55" y1="20" x2="590" y2="20" stroke="#263342"/><text x="0" y="27">' + escape(money(top)) + '</text><text x="0" y="180">' + escape(money(bottom)) + '</text><polyline fill="none" stroke="#56d6bc" stroke-width="2.5" points="' + coords + '"/><text x="55" y="201">Start</text><text x="535" y="201">Latest</text></svg>';
  }
  function renderAnalytics(a) {
    $("tradeCount").textContent = a.closed_trades + " closed trades";
    $("winRate").textContent = pct(a.win_rate); $("profitFactor").textContent = num(a.profit_factor); $("profitFactor").title = a.profit_factor_note || "";
    $("expectancy").textContent = num(a.expectancy_r); $("drawdown").textContent = pct(a.max_closed_drawdown_pct);
    drawCurve(a.equity_curve);
    $("strategyResults").innerHTML = a.strategies.length ? '<div class="table-wrap"><table><thead><tr><th>Strategy</th><th>Trades</th><th>Net P&L</th><th>Mean net R</th></tr></thead><tbody>' + a.strategies.map(function (x) {
      return "<tr><td>" + escape(family(x.family)) + "</td><td>" + x.trades + '</td><td class="' + tone(x.net_pnl) + '">' + money(x.net_pnl) + "</td><td>" + num(x.expectancy_r) + "</td></tr>";
    }).join("") + "</tbody></table></div>" : '<p class="empty">No closed trades to summarize.</p>';
  }
  function renderJournal(rows) {
    $("journalTable").innerHTML = rows.length ? rows.map(function (t) {
      return "<tr><td><b>" + escape(t.product_id) + "</b><small>" + escape(date(t.opened_at)) + "</small></td><td>" + escape(family(t.family)) + "</td><td>" + escape(t.status) + '</td><td class="' + tone(t.pnl) + '">' + money(t.pnl) + "</td><td>" + num(t.result_r) + "</td><td>" + escape(t.exit_reason) + "</td></tr>";
    }).join("") : emptyRow(6,"No paper trades recorded.");
  }
  function renderCandidateSelection(selection) {
    if (!selection || !Array.isArray(selection.candidates)) return "";
    const labels = {failed_training:"Failed training", not_shortlisted:"Outside top five",
      rejected_validation:"Failed validation", survived_validation:"Passed; lower rank", selected:"Selected for holdout"};
    const rows = selection.candidates.map(function (c) {
      const p=c.params || {};
      return '<tr><td>'+escape(family(p.family))+'<small>Candidate '+escape(c.candidate_id)+
        ' · Stop '+num(p.stop_atr)+' ATR · Target '+num(p.rr2)+'× · Volume z ≥ '+num(p.volume_z_min)+
        '</small></td><td>'+money(c.training && c.training.net_pnl)+'</td><td>'+money(c.validation && c.validation.net_pnl)+
        '</td><td>'+money(c.validation_stressed && c.validation_stressed.net_pnl)+'</td><td>'+escape(labels[c.selection_status] || c.selection_status)+'</td></tr>';
    }).join("");
    return '<details><summary>Inspect all '+selection.candidates.length+' candidates</summary><p class="footnote">'+escape(selection.scope)+
      ' Ranking uses net R with a sample-size penalty, subject to trade-count and drawdown screens.</p><div class="table-wrap"><table><thead><tr><th>Candidate</th><th>Training net P&amp;L</th><th>Validation net P&amp;L</th><th>Validation at 1.5× costs</th><th>Selection outcome</th></tr></thead><tbody>'+rows+'</tbody></table></div></details>';
  }
  function renderResearch(r) {
    $("researchMessage").textContent = r.message || r.status;
    const running = ["running","training","downloading"].includes(r.status);
    $("researchRun").disabled = running; $("researchCancel").disabled = !running;
    $("researchProgress").value = r.status === "complete" ? 1 : ((r.completed || 0) / (r.total || 1));
    if (!r.results || !r.results.length) {
      $("researchResults").innerHTML = '<p class="empty">' + (running ? "Research in progress. Completed market reports will appear here." : "No completed results. A valid outcome is that every strategy is rejected.") + "</p>"; return;
    }
    $("researchResults").innerHTML = r.results.map(function (x) {
      const h = x.holdout || {}, stress = x.holdout_stressed || {}, d = x.daily_goal || {}, ci = x.holdout_expectancy_interval || {};
      const stale = !!r.active_engine_version && x.engine_version !== r.active_engine_version;
      const c=x.upgrade_comparison;
      const comparison=c ? '<div class="callout">V9 filters versus the same rule without them: net P&amp;L difference <b>'+money(c.net_pnl_difference)+'</b>, at stressed costs <b>'+money(c.stress_net_pnl_difference)+'</b>; trade difference '+num(c.trade_count_difference)+'.<p class="footnote">'+escape(c.scope)+'</p></div>' : "";
      const candidates=renderCandidateSelection(x.candidate_selection);
      return '<article class="panel"><div class="panel-title"><h2>' + escape(x.symbol) + ' <span class="muted">' + escape(x.interval) + '</span></h2><span class="badge ' + (x.validated && !stale ? "positive" : "negative") + '">' + (stale ? "OLDER ENGINE · RERUN RESEARCH" : (x.validated ? "PASSES HISTORICAL GATE" : "NOT QUALIFIED")) + '</span></div><p class="muted">' + escape(x.selected_params ? family(x.selected_params.family) : "No strategy survived selection") + '</p><div class="result-metrics"><div>Holdout net return<strong class="' + tone(h.return_pct) + '">' + pct(h.return_pct) + '</strong></div><div>Holdout trades<strong>' + (h.trades == null ? "0" : h.trades) + '</strong></div><div>At 1.5× costs<strong>' + pct(stress.return_pct) + '</strong></div><div>Mean realized / day<strong>' + money(d.mean_net_per_day) + '</strong></div></div><p class="muted">Days reaching $10: ' + (d.days_at_least_10 || 0) + " / " + (d.calendar_days || 0) + " · Days reaching $15: " + (d.days_at_least_15 || 0) + " · Losing days: " + (d.losing_days || 0) + '</p><p class="footnote">Cash benchmark: 0% · Buy and hold after costs: ' + pct(x.buy_hold_return_pct) + " · Positive walk-forward windows: " + x.profitable_folds + "/3 · Historical mean R interval: " + num(ci.lower_r) + " to " + num(ci.upper_r) + '</p><ul class="result-reasons">' + x.rejection_reasons.map(function (reason) { return "<li>" + escape(reason) + "</li>"; }).join("") + '</ul>' + candidates + comparison + '<p class="footnote">' + escape(x.scope) + " " + escape(x.warning) + "</p></article>";
    }).join("");
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const responses = await Promise.allSettled([
        api("/api/continuous/status"), api("/api/continuous/analytics"),
        api("/api/continuous/trades?limit=100"), api("/api/continuous/activity?limit=20"), api("/api/research/status")
      ]);
      const renderers = [renderState,renderAnalytics,renderJournal,function (rows) {
        $("activity").innerHTML = rows.length ? rows.map(function (a) { return '<div class="activity-row">' + escape(a.message) + "<small>" + escape(date(a.ts)) + "</small></div>"; }).join("") : '<p class="empty">No activity yet.</p>';
      },renderResearch];
      let firstError = null;
      responses.forEach(function (response,i) { if (response.status === "fulfilled") renderers[i](response.value); else if (!firstError) firstError = response.reason; });
      if (firstError) throw firstError;
    } catch (e) { notice(e.message, true); } finally { busy = false; }
  }
  window.addEventListener("coinbase-fee-applied",function () { settingsLoaded=false; notice("Coinbase fee applied. Rerun research with the updated fee."); refresh(); });
  document.querySelectorAll("[data-tab]").forEach(function (button) {
    button.addEventListener("click",function () {
      document.querySelectorAll("[data-tab]").forEach(function (b) { b.classList.toggle("active", b === button); });
      document.querySelectorAll(".tab-panel").forEach(function (panel) { panel.hidden = panel.id !== button.dataset.tab; });
    });
  });
  bind("startBtn",async function () { await api("/api/continuous/start",{}); await refresh(); });
  bind("stopBtn",async function () { await api("/api/continuous/stop",{}); await refresh(); });
  bind("pauseBtn",async function () { await api("/api/continuous/pause",{paused:!(state && state.settings.entries_paused)}); await refresh(); });
  bind("refreshRankings",async function () {
    const rows = await api("/api/continuous/opportunities");
    $("rankings").innerHTML = rows.length ? rows.map(function (x) { return '<div class="rank-row"><div><b>' + escape(x.product_id) + "</b><small>" + escape(family(x.family)) + " · " + escape(x.regime) + " · " + escape(date(x.signal_ts,true)) + '</small></div><span class="score">' + num(x.score,1) + "</span></div>"; }).join("") : '<p class="empty">No qualifying setups in the current snapshot.</p>';
  });
  $("positions").addEventListener("click",async function (event) {
    const button = event.target.closest("[data-close]"); if (!button) return;
    button.disabled = true;
    try { const result = await api("/api/continuous/close",{product_id:button.dataset.close}); notice("Paper position closed. Net P&L: " + money(result.pnl)); await refresh(); }
    catch (e) { notice(e.message,true); } finally { button.disabled = false; }
  });
  $("researchForm").addEventListener("submit",async function (event) {
    event.preventDefault(); $("researchRun").disabled = true;
    try { await api("/api/research/start",{symbols:$("researchSymbols").value.split(/[,\s]+/).filter(Boolean),days:Number($("researchDays").value)}); await refresh(); }
    catch (e) { notice(e.message,true); $("researchRun").disabled = false; }
  });
  bind("researchCancel",async function () { await api("/api/research/cancel",{}); notice("Cancellation requested."); });
  bind("researchExport",function () { return download("/api/research/export","research-results.json"); });
  bind("tradeExport",function () { return download("/api/continuous/export","paper-trades.csv"); });
  bind("backupBtn",function () { return download("/api/continuous/backup","crypto-account-backup.sqlite3"); });
  bind("resetBtn",async function () {
    if (prompt("Type RESET to clear the paper account journal and return to $500. A database backup is created first.") !== "RESET") return;
    await api("/api/continuous/reset",{confirm:"RESET",keep_memory:$("keepMemory").checked}); notice("Paper account reset. A backup was saved before the reset."); await refresh();
  });
  $("settingsForm").addEventListener("submit",async function (event) {
    event.preventDefault();
    const patch = {}, button = event.submitter; if (button) button.disabled = true;
    Array.from(event.target.elements).forEach(function (input) {
      if (!input.name) return;
      patch[input.name] = input.type === "checkbox" ? input.checked : (input.name === "decision_interval" ? input.value : Number(input.value) / (percentFields.has(input.name) ? 100 : 1));
    });
    try { await api("/api/continuous/settings",patch); settingsLoaded = false; notice("Settings saved."); await refresh(); }
    catch (e) { notice(e.message,true); } finally { if (button) button.disabled = false; }
  });
  $("accessForm").addEventListener("submit",async function (event) {
    event.preventDefault(); token = $("accessToken").value; sessionStorage.setItem("cryptoAccessToken",token); $("accessPanel").hidden = true; $("notice").hidden = true; await refresh();
  });
  async function poll() { if (!document.hidden) await refresh(); setTimeout(poll,5000); }
  poll();
})();
