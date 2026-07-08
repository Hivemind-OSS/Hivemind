"""ui_page — the operator console's single self-contained HTML page.

ONE hermetic document (inline CSS + JS, RELATIVE-URL fetches, no external asset, no build
step) served by the stdlib `http.server` at `GET /`. It drives the same-origin JSON API
(`/api/*`) the router in `ui.py` answers: a live `/api/status` poll feeds the pulsing health
beacon, the TOKENS card mints/revokes seats over `/api/tokens`, the LOGS card tails
`/api/logs`, and the safe lifecycle controls call `/api/backup` + `/api/lifecycle`.

Visual system (the operator brief): white paper / off-white cards / ink text / soft labels /
hairline borders, with marigold as the ONLY accent — sourced from a single `--marigold` token
and applied to exactly the health beacon, the primary button, the seat-count numeral, and the
keyboard focus ring. Health is the beacon's shape/fill + pulse, never a red state. The pulse is
the page's ONLY animation (one `@keyframes`, gated off under `prefers-reduced-motion`).
"""
from __future__ import annotations

PAGE_HTML: str = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hivemind · operator console</title>
<style>
  /* tokens: the whole palette + type system in one place */
  :root {
    --paper:    #FFFFFF;   /* page */
    --card:     #FAFAF7;   /* card fill */
    --ink:      #0A0A0A;   /* primary text */
    --soft:     #6B6B6B;   /* labels / secondary text */
    --hair:     #EAEAEA;   /* 1px hairlines */
    --marigold: #FFC400;   /* the ONLY accent — beacon(healthy) + primary button + seat numeral + focus ring */
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: var(--mono);              /* console vernacular: mono for all data/labels/logs */
    font-size: 13px;
    line-height: 1.5;
    padding: 32px 16px 64px;
  }
  .wrap { max-width: 600px; margin: 0 auto; }

  /* masthead: heavy-sans wordmark + live beacon over a 2px marigold rule */
  .masthead { display: flex; align-items: center; gap: 12px; padding-bottom: 12px; }
  .wordmark { font-family: var(--sans); font-weight: 800; font-size: 20px; letter-spacing: -0.01em; }
  .rule { height: 2px; background: var(--marigold); border: 0; margin: 0 0 8px; }
  .subtitle { font-family: var(--mono); font-size: 11px; letter-spacing: 0.16em;
              text-transform: uppercase; color: var(--soft); margin-bottom: 24px; }

  /* the beacon — the heartbeat of the hive. Hollow ink ring by default; solid marigold + pulse
     when healthy. Health is shape/fill, never colour-coded red/green. */
  .beacon { width: 12px; height: 12px; border-radius: 50%;
            border: 2px solid var(--soft); background: transparent; flex: none; }
  .beacon.is-healthy {
    border-color: var(--marigold); background: var(--marigold);
    animation: pulse 2.4s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1);    opacity: 1;    }
    50%      { transform: scale(1.28); opacity: 0.55; }
  }
  @media (prefers-reduced-motion: reduce) {
    .beacon.is-healthy { animation: none; }             /* motion-safe: no pulse */
  }

  /* stacked hairline cards: SERVER / TOKENS / LOGS */
  .card { border: 1px solid var(--hair); background: var(--card);
          padding: 20px; margin-bottom: 16px; }
  .eyebrow { font-family: var(--mono); font-size: 11px; letter-spacing: 0.16em;
             text-transform: uppercase; color: var(--soft); margin-bottom: 16px; }

  .row { display: flex; justify-content: space-between; align-items: baseline;
         gap: 12px; padding: 7px 0; border-bottom: 1px solid var(--hair); }
  .row:last-of-type { border-bottom: 0; }
  .row .k { color: var(--soft); }
  .row .v { color: var(--ink); text-align: right; word-break: break-word; }
  .seat-count { color: var(--marigold); font-weight: 700; font-variant-numeric: tabular-nums; }

  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
  .hint { font-size: 11px; color: var(--soft); margin-top: 12px; line-height: 1.55; }
  .msg  { font-size: 12px; color: var(--soft); margin-top: 10px; min-height: 1em; word-break: break-word; }

  /* controls */
  .btn { font-family: var(--mono); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
         padding: 9px 14px; border: 1px solid var(--ink); background: var(--paper);
         color: var(--ink); cursor: pointer; }
  .btn:hover { background: var(--ink); color: var(--paper); }
  .btn--primary { background: var(--marigold); border-color: var(--marigold);
                  color: var(--ink); font-weight: 700; }
  .btn--primary:hover { background: var(--ink); border-color: var(--ink); color: var(--paper); }
  .btn--sm { padding: 5px 10px; font-size: 11px; }
  .input { font-family: var(--mono); font-size: 13px; padding: 9px 11px; width: 100%;
           border: 1px solid var(--hair); background: var(--paper); color: var(--ink); }
  .input::placeholder { color: var(--soft); }

  /* a visible marigold keyboard-focus ring on EVERY control */
  :focus-visible { outline: 2px solid var(--marigold); outline-offset: 2px; }

  .field { display: flex; gap: 8px; }
  .field .input { flex: 1 1 auto; min-width: 0; }

  /* the once-shown minted token */
  .mint { display: none; margin-top: 16px; padding: 14px; border: 1px dashed var(--ink); }
  .mint.show { display: block; }
  .mint .cap { font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase;
               color: var(--soft); margin-bottom: 8px; }
  .mint .tok { font-size: 13px; word-break: break-all; user-select: all; margin-bottom: 10px; }

  .seat-list { margin-top: 8px; }
  .empty { font-size: 12px; color: var(--soft); padding: 7px 0; }

  .logs { font-family: var(--mono); font-size: 12px; line-height: 1.5; color: var(--ink);
          white-space: pre-wrap; word-break: break-word; margin: 0;
          max-height: 340px; overflow-y: auto; }

  @media (max-width: 380px) {
    body { padding: 24px 12px 48px; }
    .field { flex-direction: column; }        /* stack input + button — no horizontal overflow */
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="masthead">
      <span class="wordmark">hivemind</span>
      <span class="beacon" id="beacon" role="img" aria-label="server health"></span>
    </div>
    <hr class="rule">
    <div class="subtitle">operator console · 127.0.0.1</div>
  </header>

  <section class="card" aria-labelledby="eb-server">
    <div class="eyebrow" id="eb-server">Server</div>
    <div class="row"><span class="k">state</span><span class="v" id="server-state">checking…</span></div>
    <div class="row"><span class="k">tunnel</span><span class="v" id="tunnel-state">—</span></div>
    <div class="row"><span class="k">seats</span><span class="v seat-count" id="seat-count">—</span></div>
    <div class="actions">
      <button class="btn btn--primary" id="backup">Backup now</button>
      <button class="btn" id="start">Start</button>
      <button class="btn" id="stop">Stop</button>
    </div>
    <div class="hint">Start brings the stack up loopback-only — it never opens a public tunnel.
      Backup snapshots the store right now; the volume is always preserved.</div>
    <div class="msg" id="server-msg" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="eb-tokens">
    <div class="eyebrow" id="eb-tokens">Tokens</div>
    <div class="field">
      <input class="input" id="seat-input" placeholder="new seat, e.g. alice-laptop"
             aria-label="new seat name" autocomplete="off" spellcheck="false">
      <button class="btn btn--primary" id="add-seat">Add a seat</button>
    </div>
    <div class="hint">Mint one token per seat — never share a token across agents.</div>
    <div class="mint" id="mint">
      <div class="cap">shown once — copy it now</div>
      <div class="tok" id="mint-token"></div>
      <button class="btn btn--sm" id="copy-token">Copy</button>
      <span class="msg" id="mint-msg" role="status" aria-live="polite"></span>
    </div>
    <div class="msg" id="token-msg" role="status" aria-live="polite"></div>
    <div class="seat-list" id="seat-list"></div>
  </section>

  <section class="card" aria-labelledby="eb-logs">
    <div class="eyebrow" id="eb-logs">Logs</div>
    <pre class="logs" id="logs">loading…</pre>
    <div class="actions">
      <button class="btn" id="refresh-logs">Refresh</button>
    </div>
  </section>
</div>

<script>
(function () {
  "use strict";
  var $ = function (sel) { return document.querySelector(sel); };

  async function api(path, opts) {
    // relative URL — same-origin only; the browser sends Origin, the server allowlists it.
    var r = await fetch(path, opts || {});
    var text = await r.text();
    var body = null;
    try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
    return { ok: r.ok, status: r.status, body: body };
  }
  function reason(body, fallback) {
    return (body && body.error) ? body.error : fallback;
  }
  function flash(sel, msg) { var el = $(sel); if (el) { el.textContent = msg; } }

  // ── live status → the beacon heartbeat (the only animation; CSS-driven) ──
  function renderStatus(s) {
    var healthy = !!(s && s.healthy === true);
    $("#beacon").classList.toggle("is-healthy", healthy);   // JS only toggles the class
    var up = !!(s && s.server === "up");
    $("#server-state").textContent = up ? (healthy ? "up · healthy" : "up · unhealthy") : "down";
    $("#tunnel-state").textContent = (s && s.tunnel_on)
      ? (s.tunnel_url || "on") : "off (loopback only)";
    $("#seat-count").textContent = (s && s.seats != null) ? String(s.seats) : "—";
  }
  async function pollStatus() {
    try { var res = await api("/api/status"); if (res.body) { renderStatus(res.body); } }
    catch (e) { /* transient — the next tick retries */ }
  }

  // ── tokens: list / mint (shown once) / revoke ──
  async function loadTokens() {
    var res = await api("/api/tokens");
    var seats = (res.body && res.body.seats) || [];
    var list = $("#seat-list");
    list.innerHTML = "";
    if (!seats.length) {
      var e = document.createElement("div");
      e.className = "empty"; e.textContent = "no seats provisioned yet";
      list.appendChild(e); return;
    }
    seats.forEach(function (seat) {
      var row = document.createElement("div"); row.className = "row";
      var label = document.createElement("span"); label.className = "v"; label.textContent = seat;
      var btn = document.createElement("button");
      btn.className = "btn btn--sm"; btn.textContent = "Revoke";
      btn.addEventListener("click", function () { revokeSeat(seat); });
      row.appendChild(label); row.appendChild(btn); list.appendChild(row);
    });
  }
  async function addSeat() {
    var input = $("#seat-input"); var seat = input.value.trim();
    if (!seat) { flash("#token-msg", "enter a seat name first"); return; }
    flash("#token-msg", "minting…");
    var res = await api("/api/tokens", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ seat: seat }) });
    if (res.ok && res.body && res.body.token) {
      // the plaintext appears HERE and nowhere else; a reload re-lists labels only, so it is gone.
      $("#mint-token").textContent = res.body.token;
      $("#mint").classList.add("show");
      flash("#mint-msg", ""); flash("#token-msg", "minted seat " + res.body.seat);
      input.value = ""; loadTokens();
    } else {
      flash("#token-msg", reason(res.body, "could not add that seat"));
    }
  }
  async function revokeSeat(seat) {
    var res = await api("/api/tokens/revoke", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ seat: seat }) });
    if (res.ok && res.body && res.body.revoked) {
      $("#mint").classList.remove("show");
      flash("#token-msg", "revoked seat " + seat); loadTokens();
    } else {
      flash("#token-msg", reason(res.body, "could not revoke that seat"));
    }
  }
  async function copyToken() {
    var tok = $("#mint-token").textContent;
    try { await navigator.clipboard.writeText(tok); flash("#mint-msg", "copied to clipboard"); }
    catch (e) { flash("#mint-msg", "select the token above to copy"); }
  }

  // ── safe lifecycle: backup now / start (loopback) / stop (volume preserved) ──
  async function backupNow() {
    flash("#server-msg", "snapshotting the store…");
    var res = await api("/api/backup", { method: "POST" });
    flash("#server-msg", (res.ok && res.body && res.body.path)
      ? ("snapshot saved: " + res.body.path) : reason(res.body, "backup did not complete"));
  }
  async function lifecycle(action) {
    flash("#server-msg", action === "up" ? "starting the stack…" : "stopping the stack…");
    var res = await api("/api/lifecycle", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: action }) });
    if (res.ok && res.body && res.body.ok) {
      flash("#server-msg", action === "up" ? "stack started (loopback-only)" : "stack stopped");
    } else {
      flash("#server-msg", reason(res.body, "the action did not complete"));
    }
    pollStatus();
  }

  // ── logs tail ──
  async function loadLogs() {
    var res = await api("/api/logs");
    var lines = (res.body && res.body.lines) || [];
    $("#logs").textContent = lines.length ? lines.join("\\n") : "no recent log lines";
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("#add-seat").addEventListener("click", addSeat);
    $("#seat-input").addEventListener("keydown", function (e) { if (e.key === "Enter") { addSeat(); } });
    $("#copy-token").addEventListener("click", copyToken);
    $("#backup").addEventListener("click", backupNow);
    $("#start").addEventListener("click", function () { lifecycle("up"); });
    $("#stop").addEventListener("click", function () { lifecycle("down"); });
    $("#refresh-logs").addEventListener("click", loadLogs);
    pollStatus(); loadTokens(); loadLogs();
    setInterval(pollStatus, 3000);   // the live heartbeat cadence: 3s (bounded 2-5s)
  });
})();
</script>
</body>
</html>
"""
