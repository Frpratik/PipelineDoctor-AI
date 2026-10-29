// PipelineDoctor watch screen. Plain JavaScript, no framework: poll /api/overview, redraw, log what changed.

const CHECKS = ["schema_change", "null_rate", "duplicate_keys", "row_count_change", "value_distribution", "date_range", "join_fanout"];
const FAST_MS = 1000;   // poll quickly while a background job is running
const SLOW_MS = 4000;   // otherwise just keep watching

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => (n == null ? "-" : Number(n).toLocaleString("en-US"));
const day = (s) => String(s ?? "").slice(0, 10);
const clock = () => new Date().toTimeString().slice(0, 8);

const state = {
  prev: null,            // the previous overview, so we can see what changed
  scans: 0,
  lastScan: null,
  events: [],            // newest first
  openIncident: null,
  open: new Set(["sec:checks", "sec:investigation", "sec:fix"]),   // drawer sections the user has expanded
  timer: null,
};

const TAG = {   // status -> colour
  open: "warn", investigating: "info", verified: "ok", unverified: "warn", fix_proposed: "warn", fix_applied: "warn",
  resolved: "ok", fix_rejected: "bad", success: "ok", failed: "bad", running: "info", queued: "", done: "ok",
  pending: "warn", applied: "ok", approved: "ok", rejected: "bad", alert: "bad", ok: "ok", "not checked": "",
};
const tag = (status) => `<span class="tag ${TAG[status] ?? ""}">${esc(status)}</span>`;

async function api(path, options) {
  const res = await fetch("/api" + path, options);
  if (!res.ok) {
    let message = res.statusText;
    try { message = (await res.json()).detail || message; } catch (e) { /* not JSON */ }
    throw new Error(message);
  }
  return res.json();
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
});

// ---------- event log ----------

function log(text, cls = "") {
  state.events.unshift({ t: clock(), text, cls });
  state.events.length = Math.min(state.events.length, 120);
  $("events").innerHTML = state.events.map((e) => `<div class="${e.cls}"><time>${e.t}</time>${esc(e.text)}</div>`).join("");
}

// Compare two overviews, write what changed to the log, and return the set of tables whose size changed.
function diff(a, b) {
  const changedTables = new Set();
  if (!a) {
    log(`connected to ${b.database.name}, watching ${b.tables.length} tables`, "ok");
    return changedTables;
  }
  const before = Object.fromEntries(a.tables.map((t) => [t.name, t.rows]));
  b.tables.forEach((t) => {
    const d = t.rows - (before[t.name] ?? 0);
    if (d) { changedTables.add(t.name); log(`${t.name} ${d > 0 ? "+" : ""}${num(d)} rows`); }
  });

  const oldRuns = Object.fromEntries(a.runs.map((r) => [r.run_id, r]));
  b.runs.slice().reverse().forEach((r) => {
    const old = oldRuns[r.run_id];
    if (!old) log(`run ${r.run_id} (${day(r.run_date)}) ${r.status}: ${num(r.rows_loaded)} of ${num(r.rows_in)} rows loaded`, r.status === "failed" ? "bad" : "");
    if (r.checks && !(old && old.checks)) {
      const failed = r.checks.filter((c) => !c.passed).map((c) => c.name);
      log(failed.length ? `run ${r.run_id} checks FAILED: ${failed.join(", ")}` : `run ${r.run_id} all checks passed`, failed.length ? "bad" : "ok");
    }
  });

  const oldIncidents = Object.fromEntries(a.incidents.map((i) => [i.incident_id, i]));
  b.incidents.slice().reverse().forEach((i) => {
    const old = oldIncidents[i.incident_id];
    if (!old) log(`incident ${i.incident_id} opened on run ${i.run_id}`, "warn");
    else if (old.status !== i.status) log(`incident ${i.incident_id}: ${old.status} -> ${i.status}`, i.status === "resolved" ? "ok" : "");
  });

  const oldJobs = Object.fromEntries(a.jobs.map((j) => [j.job_id, j]));
  b.jobs.slice().reverse().forEach((j) => {
    const old = oldJobs[j.job_id];
    if (!old) log(`job ${j.job_id} ${j.job_type} ${j.status}`);
    else if (old.status !== j.status) {
      const cls = j.status === "failed" ? "bad" : j.status === "done" ? "ok" : "";
      log(`job ${j.job_id} ${j.job_type} ${j.status}` + (j.error ? `: ${j.error.slice(0, 80)}` : ""), cls);
    }
  });
  return changedTables;
}

// ---------- rendering the watch screen ----------

function strip(checks) {
  const byName = Object.fromEntries((checks || []).map((c) => [c.name, c.passed]));
  return `<span class="strip">` + CHECKS.map((name) => {
    const result = byName[name];
    const cls = result === undefined ? "" : result ? "pass" : "fail";
    const label = result === undefined ? "not run" : result ? "passed" : "FAILED";
    return `<span class="cell ${cls}" title="${name}: ${label}"></span>`;
  }).join("") + `</span>`;
}

function render(data, changedTables) {
  $("target").textContent = `${data.database.host} / ${data.database.name}`;

  $("tables").innerHTML = data.tables.map((t) =>
    `<tr><td>${esc(t.name)}</td><td class="num ${changedTables.has(t.name) ? "flash" : ""}">${num(t.rows)}</td></tr>`).join("");

  $("runs").innerHTML = `<tr><th>run</th><th>date</th><th class="num">rows in</th><th class="num">loaded</th><th>status</th><th>checks</th><th>incident</th></tr>` +
    data.runs.map((r) => `<tr class="${r.incident_id ? "click" : ""} ${r.incident_id === state.openIncident ? "sel" : ""}" ${r.incident_id ? `data-incident="${r.incident_id}"` : ""}>
      <td>${r.run_id}</td><td>${day(r.run_date)}</td><td class="num">${num(r.rows_in)}</td><td class="num">${num(r.rows_loaded)}</td>
      <td>${tag(r.status)}</td>
      <td>${strip(r.checks)}${r.checks ? "" : ` <span class="faint">baseline</span>`}</td>
      <td>${r.incident_id ? `#${r.incident_id} ${tag(r.incident_status)}` : `<span class="faint">-</span>`}</td></tr>`).join("");

  $("incidents").innerHTML = data.incidents.length
    ? `<tr><th>id</th><th>run</th><th>date</th><th>status</th><th>summary</th></tr>` + data.incidents.map((i) =>
      `<tr class="click ${i.incident_id === state.openIncident ? "sel" : ""}" data-incident="${i.incident_id}">
        <td>${i.incident_id}</td><td>${i.run_id}</td><td>${day(i.run_date)}</td><td>${tag(i.status)}</td><td>${esc(i.summary)}</td></tr>`).join("")
    : `<tr><td class="empty">No incidents. Every scanned run passed its checks.</td></tr>`;

  $("jobs").innerHTML = data.jobs.length
    ? `<tr><th>job</th><th>type</th><th>incident</th><th>status</th><th>progress</th><th>created</th></tr>` + data.jobs.map((j) =>
      `<tr><td>${j.job_id}</td><td>${esc(j.job_type)}</td><td>${j.incident_id ?? `<span class="faint">-</span>`}</td><td>${tag(j.status)}</td>
        <td class="${j.error ? "bad" : "dim"}" title="${esc(j.error || j.progress)}">${esc((j.error || j.progress || "").slice(0, 70))}</td>
        <td class="dim">${String(j.created_at).slice(11, 19)}</td></tr>`).join("")
    : `<tr><td class="empty">No jobs yet. Use "Run next day" above.</td></tr>`;
}

// ---------- incident detail drawer ----------

function section(key, title, count, body) {
  const k = "sec:" + key;
  return `<div class="block"><details data-key="${k}" ${state.open.has(k) ? "open" : ""}>
    <summary class="sum">${title} <span class="faint">${count ?? ""}</span></summary>${body}</details></div>`;
}

function dryRunHtml(r) {
  if (!r || !r.before) return `<p class="dim">${esc(r?.note || r?.error || "no dry run")}</p>`;
  const row = (label, a, b) => `<tr><td class="dim">${label}</td><td>${esc(a)}</td><td>${esc(b)}</td></tr>`;
  return `<p class="dim">Dry run on a copy of <code>orders</code>. Real data was not touched. Rows affected: ${esc(r.rows_affected.join(", "))}</p>
    <table><tr><th></th><th>before</th><th>after</th></tr>
    ${row("rows", num(r.before.row_count), num(r.after.row_count))}
    ${row("mean amount", num(r.before.amount_mean), num(r.after.amount_mean))}
    ${row("first order", r.before.first_order, r.after.first_order)}
    ${row("last order", r.before.last_order, r.after.last_order)}</table>
    <p class="dim" style="margin-top:10px">Checks re-run on the fixed copy</p>
    ${r.checks_after_fix.map((c) => `<p><span class="${c.passed ? "ok" : "bad"}">${c.passed ? "PASS" : "FAIL"}</span> <code>${esc(c.check_name)}</code> <span class="dim">${esc(c.details)}</span></p>`).join("")}
    <p class="faint">${esc(r.note)}</p>`;
}

function renderDetail(d) {
  const inc = d.incident;
  $("drawer-title").textContent = `incident ${inc.incident_id}  /  run ${inc.run_id}  /  ${day(inc.run_date)}`;

  const overview = `<div class="block"><dl class="kv">
    <dt>status</dt><dd>${tag(inc.status)}</dd><dt>run</dt><dd>${tag(inc.run_status)} ${num(inc.rows_loaded)} of ${num(inc.rows_in)} rows</dd>
    <dt>summary</dt><dd>${esc(inc.summary)}</dd></dl></div>`;

  const checks = section("checks", "Failed and passed checks", `${d.checks.filter((c) => !c.passed).length} failed`,
    `<table>${d.checks.map((c) => `<tr><td><span class="${c.passed ? "ok" : "bad"}">${c.passed ? "PASS" : "FAIL"}</span></td><td><code>${esc(c.check_name)}</code></td><td class="dim">${esc(c.details)}</td></tr>`).join("")}</table>`);

  const verdictFor = Object.fromEntries(d.verdicts.map((v) => [v.attempt, v]));
  const attempts = d.diagnoses.length ? d.diagnoses.map((a) => {
    const g = a.diagnosis, v = verdictFor[a.attempt];
    return `<p><b>Attempt ${a.attempt}</b> <span class="dim">confidence ${g.confidence}</span></p>
      <p>${esc(g.root_cause)}</p>
      <p class="dim">hypotheses: ${g.hypotheses_considered.map(esc).join(" / ")}</p>
      <table><tr><th>claim</th><th>tool or SQL</th><th>result</th></tr>${g.evidence.map((e) =>
        `<tr><td>${esc(e.claim)}</td><td><code>${esc(e.sql_or_tool)}</code></td><td class="dim">${esc(e.result)}</td></tr>`).join("")}</table>
      ${v ? `<p style="margin-top:10px">Verifier: ${v.accepted ? `<span class="ok">accepted</span>` : `<span class="bad">rejected</span>`}
        ${v.problems.length ? `<span class="dim"> / ${v.problems.map(esc).join("; ")}</span>` : ""}</p>
        ${v.feedback ? `<p class="dim">feedback: ${esc(v.feedback)}</p>` : ""}` : `<p class="dim">Verifier has not judged this yet.</p>`}`;
  }).join("<hr>") : `<p class="dim">${inc.status === "investigating" ? "Agents are working on it..." : "Not investigated yet."}</p>`;
  const investigation = section("investigation", "Investigation", `${d.diagnoses.length} attempt(s)`, attempts);

  const fixes = d.fix_proposals.length ? d.fix_proposals.map((f) => `
    <p>${tag(f.status)} <code>${esc(f.fix_type)}</code> <span class="faint">fix ${f.fix_id}</span></p>
    <p>${esc(f.description)}</p><pre>${esc(f.sql_or_steps)}</pre>
    <p class="dim">risk: ${esc(f.risk_notes)}</p>${dryRunHtml(f.dry_run_result)}
    ${f.status === "pending" ? `<div class="actions"><button class="primary" data-act="approve" data-fix="${f.fix_id}">Approve and apply</button>
      <button class="danger" data-act="reject" data-fix="${f.fix_id}">Reject</button></div>` : ""}`).join("<hr>")
    : `<p class="dim">No fix proposed yet.</p>`;
  const fix = section("fix", "Fix proposal", `${d.fix_proposals.length}`, fixes);

  const trace = section("trace", "Agent trace", `${d.trace.length} steps`, d.trace.map((s) => {
    const body = [s.tool_input && "in:  " + s.tool_input, s.tool_output && "out: " + s.tool_output].filter(Boolean).join("\n\n");
    const preview = (s.tool_output || s.tool_input || "").replace(/\s+/g, " ").slice(0, 100);
    return `<div class="step"><span class="who ${esc(s.agent_name)}">${esc(s.agent_name)}</span> #${s.step_no} ${esc(s.action)} ${esc(s.tool_name || "")}
      <span class="faint">${s.duration_ms} ms${s.tokens ? `, ${num(s.tokens)} tok` : ""}</span>
      ${body ? `<details data-key="step:${inc.incident_id}:${s.step_no}" ${state.open.has(`step:${inc.incident_id}:${s.step_no}`) ? "open" : ""}><summary>${esc(preview)}</summary><pre>${esc(body)}</pre></details>` : ""}</div>`;
  }).join(""));

  const drawer = $("drawer");
  const scroll = drawer.scrollTop;   // keep the reader's place when the panel refreshes
  $("drawer-body").innerHTML = overview + checks + investigation + fix + trace;
  drawer.scrollTop = scroll;
}

async function refreshDetail() {
  if (!state.openIncident) return;
  try {
    const detail = await api(`/incidents/${state.openIncident}`);
    const fingerprint = state.openIncident + JSON.stringify(detail);
    if (fingerprint === state.shown) return;   // nothing changed: do not rebuild, or clicks and text selection get lost
    state.shown = fingerprint;
    renderDetail(detail);
  } catch (e) { $("drawer-body").innerHTML = `<div class="block bad">${esc(e.message)}</div>`; }
}

async function openDetail(id) {
  state.openIncident = id;
  state.shown = null;
  $("drawer").hidden = false;
  $("drawer-title").textContent = `incident ${id}`;
  $("drawer-body").innerHTML = `<div class="block dim">loading...</div>`;
  document.querySelectorAll("tr[data-incident]").forEach((tr) => tr.classList.toggle("sel", Number(tr.dataset.incident) === id));
  await refreshDetail();
}

// ---------- polling ----------

function status(live, message) {
  $("scan").className = "scan " + (live ? "live" : "down");
  if (!live) $("scan-text").textContent = "API unreachable: " + message;
}

function tick() {   // the "last scan N seconds ago" text, refreshed every second
  if (!state.lastScan) return;
  const ago = Math.round((Date.now() - state.lastScan) / 1000);
  if ($("scan").classList.contains("live")) $("scan-text").textContent = `scan #${state.scans}  /  ${ago}s ago`;
}

function restartSweep(ms) {
  const sweep = $("sweep");
  sweep.classList.remove("run");
  void sweep.offsetWidth;   // force a reflow so the animation restarts
  sweep.style.setProperty("--scan-ms", ms + "ms");
  sweep.classList.add("run");
}

async function poll() {
  clearTimeout(state.timer);
  try {
    const data = await api("/overview");
    const changed = diff(state.prev, data);
    state.prev = data;
    state.scans += 1;
    state.lastScan = Date.now();
    render(data, changed);
    status(true);
    tick();
    await refreshDetail();
  } catch (e) {
    status(false, e.message);
  }
  const busy = state.prev && state.prev.jobs.some((j) => j.status === "queued" || j.status === "running");
  const ms = busy ? FAST_MS : SLOW_MS;
  restartSweep(ms);
  state.timer = setTimeout(poll, ms);
}

// ---------- events ----------

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fault = $("fault").value.trim();
  $("run-btn").disabled = true;
  try {
    const { job_id } = await post("/pipeline/run", fault ? { fault } : {});
    log(`requested job ${job_id}: run next day` + (fault ? ` with fault ${fault}` : ""));
    poll();
  } catch (err) {
    log("could not start run: " + err.message, "bad");
  }
  setTimeout(() => { $("run-btn").disabled = false; }, 1500);
});

document.body.addEventListener("click", async (e) => {
  const row = e.target.closest("tr[data-incident]");
  if (row) return openDetail(Number(row.dataset.incident));

  const button = e.target.closest("button[data-act]");
  if (!button) return;
  button.disabled = true;
  try {
    const path = `/fixes/${button.dataset.fix}/${button.dataset.act}`;
    const result = await post(path);
    log(button.dataset.act === "approve" ? `fix ${button.dataset.fix} approved, job ${result.job_id} applying it` : `fix ${button.dataset.fix} rejected`,
        button.dataset.act === "approve" ? "ok" : "warn");
    poll();
  } catch (err) {
    log(`${button.dataset.act} failed: ${err.message}`, "bad");
    button.disabled = false;
  }
});

$("drawer-close").addEventListener("click", () => {
  state.openIncident = null;
  $("drawer").hidden = true;
  document.querySelectorAll("tr.sel").forEach((tr) => tr.classList.remove("sel"));
});

// <details> toggles do not bubble, so listen in the capture phase to remember what the reader opened
$("drawer-body").addEventListener("toggle", (e) => {
  const key = e.target.dataset && e.target.dataset.key;
  if (!key) return;
  if (e.target.open) state.open.add(key); else state.open.delete(key);
}, true);

setInterval(tick, 1000);
poll();
