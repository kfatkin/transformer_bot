const $ = (id) => document.getElementById(id);
const money = (n) => n == null ? "—" : Number(n).toLocaleString("en-US", {style:"currency",currency:"USD"});
const number = (n, digits=2) => n == null ? "—" : Number(n).toFixed(digits);
const stamp = (s) => s ? new Date(s).toLocaleString("en-US", {timeZone:"America/New_York",timeZoneName:"short"}) : "—";
const active = new Set(["ACTIVE_CALENDAR","RISK_FREE_CONDOR"]);
let latest;
let initialized = false;
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function toast(text) {
  $("toast").textContent = text;
  $("toast").classList.remove("hidden");
  setTimeout(() => $("toast").classList.add("hidden"), 9000);
}
async function api(url, method, body) {
  const response = await fetch(url, {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
function metric(label, value, detail) {
  const card = node("div", null, "metric");
  card.append(node("span",label),node("strong",value),node("span",detail));
  return card;
}
function card(trade) {
  const el = node("article",null,"trade");
  const head = node("div",null,"trade-head");
  head.append(node("h3","SPX"),node("span",`${trade.quantity} CONTRACT${trade.quantity > 1 ? "S" : ""}`,"pill"));
  el.append(head,node("small",trade.trade_id),node("p",`${trade.front_expiry} → ${trade.back_expiry} · ${number(trade.put_strike,0)}P / ${number(trade.call_strike,0)}C`));
  el.append(node("div",trade.transformed
    ? `RISK-FREE (MODELED): ${money(trade.net_floor_dollars)} net floor`
    : `CALENDAR: ${money(Number(trade.calendar_debit_paid)*trade.quantity*100)} debit at risk`,
    trade.transformed ? "badge free" : "badge"));
  if (trade.settlement_pending) el.append(node("p","Expiration passed · official PM settlement required","warn"));
  const dl = node("dl");
  const values = [
    [trade.transformed ? "Condor close debit" : "Calendar liquidation credit",`${number(trade.mark_price)} pts`],
    ["Unrealized P&L (fees paid)",money(trade.unrealized_pnl_dollars)],
    ["Initial debit",`${number(trade.calendar_debit_paid)} pts`],
    ["Transform credit / target",`${number(trade.current_transform_credit)} / ${number(trade.target_credit)}`],
  ];
  for (const [label,value] of values) { const box=node("div");box.append(node("dt",label),node("dd",value));dl.append(box); }
  el.append(dl);
  const progress=node("progress");
  progress.max=100;
  progress.value=Math.max(0,Math.min(100,Number(trade.current_transform_credit || 0)/Number(trade.target_credit)*100));
  el.append(progress,node("small","Fee-aware target = opening debit + wing width + 8 leg commissions / 100"));
  el.append(node("small",`${trade.quote_stale ? "STALE / UNAVAILABLE" : "LIVE SAMPLE"} · ${stamp(trade.last_quote_at)}`,trade.quote_stale ? "warn" : ""));
  return el;
}
function render(data) {
  latest=data;
  $("connection").textContent=data.connected ? "● IBKR CONNECTED" : "○ IBKR DISCONNECTED";
  $("connection").className=`pill ${data.connected ? "good" : "bad"}`;
  $("clock").textContent=stamp(data.server_time);
  $("market").textContent=data.market_open ? "MARKET OPEN" : "MARKET CLOSED";
  $("error").textContent=data.error ? `Latest operational error: ${data.error}` : "";
  $("error").classList.toggle("hidden",!data.error);
  const m=data.metrics;
  $("metrics").replaceChildren(
    metric("TRADES LOGGED",m.total_trades,`${m.closed_trades} closed · ${m.sessions_elapsed}/${m.entry_window_sessions} sessions`),
    metric("TRANSFORM SUCCESS",`${number(m.transform_success_rate,1)}%`,`${number(m.average_minutes_to_transform,1)} avg. minutes`),
    metric("REALIZED PAPER P&L",money(m.total_pnl),`Win rate ${number(m.win_rate,1)}% · ${m.proxy_settled_trades} proxy settlements`),
    metric("PROFIT FACTOR",m.profit_factor == null ? m.profit_factor_label : number(m.profit_factor),`Best ${money(m.max_winner)} / Worst ${money(m.max_loser)}`),
  );
  if (!initialized) {
    const form=$("config");
    for (const [key,value] of Object.entries(data.config)) {
      const field=form.elements.namedItem(key);
      if (field) { if (field.type === "checkbox") field.checked=value; else field.value=value; }
    }
    initialized=true;
  }
  $("campaign").textContent=!data.config.enabled ? "New entries disabled; monitoring continues." :
    m.entry_window_complete ? "Entry study complete; monitoring remaining trades." : "DST-aware · NYSE holidays skipped";
  const positions=data.trades.filter(t => active.has(t.status));
  $("trades").replaceChildren(...(positions.length ? positions.map(card) : [node("div","No active trades. Waiting for the next valid market entry.","empty")]));
  const pending=positions.filter(t => t.settlement_pending);
  const selected=$("settlement-trades").value;
  $("settlement-trades").replaceChildren(...(pending.length ? pending.map(t => {
    const option=node("option",`${t.trade_id} · ${t.front_expiry}`);
    option.value=t.trade_id;return option;
  }) : [node("option","No pending expirations")]));
  if (!pending.length) $("settlement-trades").firstChild.value="";
  if (pending.some(t => t.trade_id === selected)) $("settlement-trades").value=selected;
  $("history").replaceChildren(...data.trades.filter(t => !active.has(t.status)).reverse().map(t => {
    const row=node("tr");row.append(node("td",t.trade_id),node("td",t.status.replace("CLOSED_","") + (t.settlement_provenance === "ibkr_close_proxy" ? " (IBKR PROXY)" : "")),node("td",money(t.final_pnl_dollars),Number(t.final_pnl_dollars)>=0?"good":"bad"),node("td",`${number(t.roi_pct)}%`));return row;
  }));
  $("advisor-mode").textContent=data.advisor_enabled ? "LLM ENABLED" : "LLM DISABLED";
  $("feed").replaceChildren(...(data.feed.length ? data.feed.map(item => {
    const el=node("div",null,"feed-item");el.append(node("time",`${stamp(item.timestamp)} · ${item.kind} · ${item.model}`),node("p",item.text));return el;
  }) : [node("p",data.advisor_enabled ? "Waiting for an entry or transform evaluation. Check operational audit for provider errors." : "Advisor disabled. Configure a local Ollama or remote provider in .env. Strategy decisions always use deterministic math.","muted")]));
  $("errors").replaceChildren(...(data.errors.length ? data.errors.map(item => node("p",`${stamp(item.logged_at)} · ${item.component}: ${item.message}`,"audit-line")) : [node("p","No operational errors recorded.")]));
}
$("config").addEventListener("submit",async(event) => {
  event.preventDefault();const f=event.target.elements;
  try {
    await api("/api/config","PUT",{symbol:"SPX",quantity:Number(f.quantity.value),wing_width:Number(f.wing_width.value),commission_per_leg:Number(f.commission_per_leg.value),enabled:f.enabled.checked});
    toast("Configuration saved. Existing trade parameters are unchanged.");
  } catch(error) {toast(error.message);}
});
$("entry").addEventListener("click",async() => {
  if (!confirm("Simulate one SPX double calendar using current live market quotes? No broker order will be placed.")) return;
  $("entry").disabled=true;
  try {await api("/api/entry","POST",{});toast("Paper entry recorded.");}
  catch(error) {toast(error.message);}
  finally {$("entry").disabled=false;}
});
$("settlement").addEventListener("submit",async(event) => {
  event.preventDefault();const f=event.target.elements;
  try {
    await api(`/api/trades/${encodeURIComponent(f.trade_id.value)}/settlement`,"POST",{value:f.value.value,source:f.source.value,confirmed_official_spxw_pm:f.confirmed.checked});
    toast("Official settlement recorded.");event.target.reset();
  } catch(error) {toast(error.message);}
});
setInterval(() => {
  if (!latest) return;
  const remaining=Math.max(0,new Date(latest.next_entry)-new Date());
  const days=Math.floor(remaining/86400000);
  $("countdown").textContent=`${days ? `${days}d ` : ""}${String(Math.floor(remaining/3600000)%24).padStart(2,"0")}:${String(Math.floor(remaining/60000)%60).padStart(2,"0")}:${String(Math.floor(remaining/1000)%60).padStart(2,"0")}`;
},1000);
function connect() {
  const socket=new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/stream`);
  socket.onmessage=(event) => render(JSON.parse(event.data));
  socket.onclose=() => {
    $("connection").textContent="DASHBOARD OFFLINE";
    $("connection").className="pill bad";
    setTimeout(connect,3000);
  };
  socket.onerror=() => socket.close();
}
fetch("/api/state").then(response => {if (!response.ok) throw new Error("State request failed");return response.json();}).then(render).catch(error=>toast(error.message));
connect();
