"""ui_page — the operator console's single self-contained HTML page.

ONE hermetic document (inline CSS + JS, RELATIVE-URL fetches, no external asset, no build
step) served by the stdlib `http.server` at `GET /`. It drives the same-origin JSON API
(`/api/*`) the router in `ui.py` answers: a live `/api/status` poll feeds the pulsing health
beacon, the CONNECTIONS card lists and mutates the synced-repo registry over `/api/repos`,
the DOORS card renders `/api/doors`, the TOKENS card mints/revokes seats over `/api/tokens`,
the LOGS card tails `/api/logs`, and the safe lifecycle controls call `/api/backup` +
`/api/lifecycle`.

Two properties are structural, not stylistic. (1) The page holds NO absolute URL: every
address it shows — the connect line included — arrives as runtime DATA from `/api/doors`,
which is what keeps the document hermetic AND makes the line it displays byte-identical to
what `hive connect` prints. (2) The connections card renders whatever keys the server's
`sync` sub-block carries, in server order, and derives NO verdict from them: it has no field
list of its own, so a new sync field appears here with no edit and an absent field reads as
absent rather than as a confident zero or a health word the server never said.

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
  /* the numeral is a marigold accent; compound with `.row .v` so specificity (0,3,0) wins the
     colour over the generic `.row .v` (0,2,0) — a bare `.seat-count` (0,1,0) would lose to ink. */
  .row .v.seat-count { color: var(--marigold); font-weight: 700; font-variant-numeric: tabular-nums; }

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

  /* one connection: a hairline sub-block per registered repo */
  .conn { border: 1px solid var(--hair); background: var(--paper);
          padding: 10px 14px; margin-top: 12px; }
  .conn .name { font-weight: 700; }
  .conn .row .k { word-break: break-word; }
  /* the daemon's own state — a sibling slot, never a repo (a fleet fact belongs to none) */
  .fleet { border-top: 1px solid var(--hair); margin-top: 20px; padding-top: 12px; }
  .field--more { flex-wrap: wrap; margin-top: 8px; }
  .field--more .input { flex: 1 1 140px; }

  /* the paste-ready registration line: selectable text FIRST, copy button as a courtesy */
  .snippet { margin-top: 14px; padding: 12px; border: 1px dashed var(--ink);
             font-size: 12px; word-break: break-all; user-select: all; }

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
      <button class="btn" id="backup">Backup now</button>
      <button class="btn" id="start">Start</button>
      <button class="btn" id="stop">Stop</button>
      <button class="btn" id="tunnel-toggle">Activate tunnel</button>
    </div>
    <div class="hint">Start brings the stack up loopback-only. Activate tunnel opens the public
      door (token-gated by construction); Deactivate closes it, leaving the daemon running.
      Backup snapshots the store right now; the volume is always preserved.</div>
    <div class="msg" id="server-msg" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="eb-repos">
    <div class="eyebrow" id="eb-repos">Connections</div>
    <div class="field">
      <input class="input" id="repo-url" placeholder="git remote url to connect"
             aria-label="git remote url" autocomplete="off" spellcheck="false">
      <button class="btn" id="add-repo">Connect</button>
    </div>
    <div class="field field--more">
      <input class="input" id="repo-name" placeholder="name (optional)"
             aria-label="registry name" autocomplete="off" spellcheck="false">
      <input class="input" id="repo-branch" placeholder="branch (optional)"
             aria-label="canonical branch" autocomplete="off" spellcheck="false">
      <input class="input" id="repo-token-env" placeholder="token env var NAME (optional)"
             aria-label="name of the env var holding this repo's git token"
             autocomplete="off" spellcheck="false">
    </div>
    <div class="hint">The token field takes the NAME of an environment variable — never a
      token. Disconnecting stops the feed and prunes the mirror; the memories are kept, so
      a repo you reconnect picks them straight back up.</div>
    <div class="msg" id="repo-msg" role="status" aria-live="polite"></div>
    <div class="repo-list" id="repo-list"></div>
    <div class="fleet" id="fleet-block"></div>
    <div class="actions">
      <button class="btn btn--sm" id="refresh-repos">Refresh</button>
    </div>
  </section>

  <section class="card" aria-labelledby="eb-doors">
    <div class="eyebrow" id="eb-doors">Doors</div>
    <div class="row"><span class="k">posture</span><span class="v" id="door-posture">—</span></div>
    <div class="row"><span class="k">address</span><span class="v" id="door-url">—</span></div>
    <div class="snippet" id="door-line">loading…</div>
    <div class="actions">
      <button class="btn btn--sm" id="copy-line">Copy</button>
    </div>
    <div class="hint" id="door-note"></div>
    <div class="msg" id="door-msg" role="status" aria-live="polite"></div>
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

  <section class="card" aria-labelledby="eb-restore">
    <div class="eyebrow" id="eb-restore">Restore</div>
    <div class="field">
      <select class="input" id="backup-pick" aria-label="snapshot to restore"></select>
      <button class="btn" id="do-restore">Restore</button>
    </div>
    <div class="hint">Restore replaces the live store with the selected snapshot — a safety snapshot
      is taken first, so the current store stays recoverable. You are asked to type
      &quot;restore&quot; to confirm.</div>
    <div class="msg" id="restore-msg" role="status" aria-live="polite"></div>
  </section>
</div>

<script>
(function () {
  "use strict";
  var $ = function (sel) { return document.querySelector(sel); };
  var tunnelOn = false;                          // last-known tunnel state (from /api/status)

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
    tunnelOn = !!(s && s.tunnel_on);
    $("#tunnel-state").textContent = tunnelOn ? (s.tunnel_url || "on") : "off (loopback only)";
    $("#tunnel-toggle").textContent = tunnelOn ? "Deactivate tunnel" : "Activate tunnel";
    $("#seat-count").textContent = (s && s.seats != null) ? String(s.seats) : "—";
  }
  async function pollStatus() {
    try { var res = await api("/api/status"); if (res.body) { renderStatus(res.body); } }
    catch (e) { /* transient — the next tick retries */ }
  }

  // ── connections: the synced-repo registry + what the daemon observed about it ──
  function show(v) {
    // Absent reads as ABSENT. Never 0, never a health word — the page invents no value
    // and no verdict for a field the server did not measure.
    if (v === null || v === undefined) { return "not reported"; }
    if (v === true) { return "yes"; }
    if (v === false) { return "no"; }
    return String(v);
  }
  function kv(k, v, cls) {
    var row = document.createElement("div"); row.className = "row";
    var key = document.createElement("span"); key.className = "k"; key.textContent = k;
    var val = document.createElement("span");
    val.className = "v" + (cls ? " " + cls : ""); val.textContent = show(v);
    row.appendChild(key); row.appendChild(val); return row;
  }
  function connCard(repo) {
    var box = document.createElement("div"); box.className = "conn";
    var head = document.createElement("div"); head.className = "row";
    var label = document.createElement("span");
    label.className = "v name"; label.textContent = repo.name;
    var btn = document.createElement("button");
    btn.className = "btn btn--sm"; btn.textContent = "Disconnect";
    btn.addEventListener("click", function () { disconnectRepo(repo.name); });
    head.appendChild(label); head.appendChild(btn); box.appendChild(head);
    // EVERY field the server sent, in SERVER order. The page keeps no field list of its
    // own, so a new one appears here with no edit and none can be silently dropped.
    Object.keys(repo).forEach(function (key) {
      if (key === "name" || key === "sync") { return; }
      box.appendChild(kv(key, repo[key]));
    });
    if (repo.sync) {
      Object.keys(repo.sync).forEach(function (key) {
        box.appendChild(kv("sync." + key, repo.sync[key]));
      });
    } else {
      box.appendChild(kv("sync", null));   // registered, nothing observed yet
    }
    return box;
  }
  function renderFleet(fleet) {
    // Always rendered: the daemon's own state belongs to no repo, and a missing block
    // would itself be an unreadable signal.
    var box = $("#fleet-block"); box.innerHTML = "";
    var head = document.createElement("div");
    head.className = "eyebrow"; head.textContent = "Sync daemon";
    box.appendChild(head);
    var keys = Object.keys(fleet || {});
    if (!keys.length) { box.appendChild(kv("state", null)); return; }
    keys.forEach(function (key) { box.appendChild(kv(key, fleet[key])); });
  }
  function renderRepos(doc) {
    var list = $("#repo-list"); list.innerHTML = "";
    var repos = (doc && doc.repos) || [];
    if (!repos.length) {
      var e = document.createElement("div");
      e.className = "empty"; e.textContent = "no repos connected yet";
      list.appendChild(e);
    } else {
      repos.forEach(function (repo) { list.appendChild(connCard(repo)); });
    }
    renderFleet(doc && doc.fleet);
  }
  async function loadRepos() {
    var res = await api("/api/repos");
    if (!res.ok) {
      var list = $("#repo-list"); list.innerHTML = "";
      var e = document.createElement("div"); e.className = "empty";
      e.textContent = (res.body && res.body.error === "server_down")
        ? "the server is not running — connections are unreadable"
        : "connections could not be read";
      list.appendChild(e); renderFleet(null); return;
    }
    renderRepos(res.body);
  }
  function refused(body, fallback) {
    // a STATED refusal is the operator's to act on; anything else is an upstream failure.
    return (body && body.reason) ? body.reason : reason(body, fallback);
  }
  async function addRepo() {
    var url = $("#repo-url").value.trim();
    if (!url) { flash("#repo-msg", "enter a git remote url first"); return; }
    var payload = { url: url };
    var name = $("#repo-name").value.trim();
    var branch = $("#repo-branch").value.trim();
    var tokenEnv = $("#repo-token-env").value.trim();
    if (name) { payload.name = name; }
    if (branch) { payload.branch = branch; }
    if (tokenEnv) { payload.token_env = tokenEnv; }   // a NAME; the value never travels
    flash("#repo-msg", "connecting…");
    var res = await api("/api/repos", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (res.ok) {
      flash("#repo-msg", "connected — the sync daemon picks it up on its next tick");
      $("#repo-url").value = ""; $("#repo-name").value = "";
      $("#repo-branch").value = ""; $("#repo-token-env").value = "";
    } else {
      flash("#repo-msg", refused(res.body, "could not connect that repo"));
    }
    loadRepos();
  }
  async function disconnectRepo(name) {
    // Typed confirm, mirroring the CLI: NOTHING is sent until the operator types the
    // repo's own name back, so a mistype or a cancel makes no request at all.
    var typed = window.prompt('Disconnect "' + name + '"? It stops feeding and its mirror '
      + 'is pruned next tick; the memories are kept. Type the name to confirm:');
    if (typed !== name) { flash("#repo-msg", "not confirmed — nothing changed"); return; }
    flash("#repo-msg", "disconnecting " + name + "…");
    var res = await api("/api/repos/remove", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name }) });
    flash("#repo-msg", res.ok ? ("disconnected " + name)
      : refused(res.body, "could not disconnect that repo"));
    loadRepos();
  }

  // ── doors: the address + registration line an agent connects through ──
  async function loadDoors() {
    // The line arrives as DATA — the page templates no address of its own, which is what
    // makes it byte-identical to `hive connect` and keeps the document hermetic.
    var res = await api("/api/doors");
    var d = res.ok ? res.body : null;
    if (!d) { flash("#door-msg", "the door could not be read"); return; }
    $("#door-posture").textContent = d.posture
      + (d.token_gated ? " · token-gated" : " · tokenless");
    $("#door-url").textContent = d.url;
    $("#door-line").textContent = d.line;
    $("#door-note").textContent = d.note;
  }
  async function copyLine() {
    try { await navigator.clipboard.writeText($("#door-line").textContent);
          flash("#door-msg", "copied to clipboard"); }
    catch (e) { flash("#door-msg", "select the line above to copy"); }
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
    loadBackups();                                 // reflect the new snapshot in the restore picker
  }
  var VERB = { "up": "starting", "down": "stopping",
               "tunnel-up": "opening the tunnel", "tunnel-down": "closing the tunnel" };
  async function lifecycle(action) {
    // non-blocking: the server validates, dispatches the docker work on a worker, and returns at
    // once; the /api/status poll (the beacon) reflects the outcome. The click never hangs.
    flash("#server-msg", (VERB[action] || "working") + "… watch the beacon");
    var res = await api("/api/lifecycle", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: action }) });
    if (res.status === 409) { flash("#server-msg", "another operation is already in progress"); return; }
    if (res.status === 400 && res.body && res.body.missing) {
      flash("#server-msg", "tunnel needs " + res.body.missing.join(" + ") + " set in .env first"); return;
    }
    if (res.ok && res.body && res.body.status) {
      flash("#server-msg", "dispatched — " + res.body.status + "; the beacon will follow");
    } else {
      flash("#server-msg", reason(res.body, "the action did not complete"));
    }
    pollStatus();
  }
  function tunnelToggle() {
    if (!tunnelOn) {
      // activation exposes the server publicly; the /mcp door stays token-gated by construction.
      if (!window.confirm("This opens a PUBLIC tunnel to your server. The /mcp door stays token-gated. Continue?")) { return; }
      lifecycle("tunnel-up");
    } else {
      lifecycle("tunnel-down");
    }
  }

  // ── logs tail ──
  async function loadLogs() {
    var res = await api("/api/logs");
    var lines = (res.body && res.body.lines) || [];
    $("#logs").textContent = lines.length ? lines.join("\\n") : "no recent log lines";
  }

  // ── restore: list in-volume snapshots + restore behind a typed confirm ──
  function fmtSize(n) {
    if (n == null) { return "?"; }
    if (n < 1024) { return n + " B"; }
    if (n < 1048576) { return (n / 1024).toFixed(1) + " KB"; }
    return (n / 1048576).toFixed(1) + " MB";
  }
  function fmtDate(mtime) {
    if (!mtime) { return "?"; }
    try { return new Date(mtime * 1000).toISOString().slice(0, 16).replace("T", " "); }
    catch (e) { return "?"; }
  }
  async function loadBackups() {
    var res = await api("/api/backups");
    var backups = (res.body && res.body.backups) || [];
    var sel = $("#backup-pick");
    sel.innerHTML = "";
    if (!backups.length) {
      var o = document.createElement("option");
      o.value = ""; o.textContent = "no snapshots yet — take a backup first";
      sel.appendChild(o); sel.disabled = true; $("#do-restore").disabled = true;
      return;
    }
    sel.disabled = false; $("#do-restore").disabled = false;
    backups.forEach(function (b) {
      var o = document.createElement("option");
      o.value = b.name;
      o.textContent = b.name + "  ·  " + fmtDate(b.mtime) + "  ·  " + fmtSize(b.size);
      sel.appendChild(o);
    });
  }
  async function doRestore() {
    var name = $("#backup-pick").value;
    if (!name) { flash("#restore-msg", "no snapshot selected"); return; }
    // typed confirm, mirroring the CLI — a destructive replace must be deliberate.
    var typed = window.prompt('Restore replaces the live store; a safety snapshot is taken first. Type "restore" to confirm:');
    if (typed !== "restore") { flash("#restore-msg", "not confirmed — the store is unchanged"); return; }
    flash("#restore-msg", "restoring " + name + "… a safety snapshot is taken first; watch the beacon");
    var res = await api("/api/restore", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name }) });
    if (res.status === 409) { flash("#restore-msg", "another operation is already in progress"); return; }
    if (res.ok && res.body && res.body.status) {
      flash("#restore-msg", "restore dispatched — the server will stop, swap the store, and restart");
    } else {
      flash("#restore-msg", reason(res.body, "restore did not start"));
    }
    pollStatus();
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("#add-seat").addEventListener("click", addSeat);
    $("#seat-input").addEventListener("keydown", function (e) { if (e.key === "Enter") { addSeat(); } });
    $("#copy-token").addEventListener("click", copyToken);
    $("#backup").addEventListener("click", backupNow);
    $("#start").addEventListener("click", function () { lifecycle("up"); });
    $("#stop").addEventListener("click", function () { lifecycle("down"); });
    $("#tunnel-toggle").addEventListener("click", tunnelToggle);
    $("#refresh-logs").addEventListener("click", loadLogs);
    $("#do-restore").addEventListener("click", doRestore);
    $("#add-repo").addEventListener("click", addRepo);
    $("#repo-url").addEventListener("keydown", function (e) { if (e.key === "Enter") { addRepo(); } });
    $("#refresh-repos").addEventListener("click", loadRepos);
    $("#copy-line").addEventListener("click", copyLine);
    pollStatus(); loadTokens(); loadLogs(); loadBackups(); loadRepos(); loadDoors();
    // The heartbeat polls STATUS only. The connections list refreshes on load, after a
    // connect/disconnect, and on the explicit Refresh — an `exec` per 3s would be a
    // child storm against the daemon for data that changes at tick cadence.
    setInterval(pollStatus, 3000);   // the live heartbeat cadence: 3s (bounded 2-5s)
  });
})();
</script>
</body>
</html>
"""
