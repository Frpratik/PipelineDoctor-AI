// "Data sources" tab: connect a database, scan it, choose tables to watch, see how they are doing.
// Uses api(), post(), esc(), num(), day(), tag() and state from app.js.

const src = {
  connections: [],
  watched: [],
  connectionId: null,   // the connection whose tables are shown in the scan list
  tables: [],
  editing: null,        // "schema.table" whose watch form is open
  timer: null,
};

const $s = (id) => document.getElementById(id);

function notice(kind, html) {
  const box = $s("src-message");
  box.className = "notice " + kind;
  box.innerHTML = html;
  box.hidden = !html;
}

// ---------- switching tabs ----------

function showView(view) {
  document.body.dataset.view = view;
  $s("view-pipeline").hidden = view !== "pipeline";
  $s("view-sources").hidden = view !== "sources";
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  clearInterval(src.timer);
  if (view === "sources") {
    refreshSources();
    src.timer = setInterval(refreshSources, 4000);   // keep the watched tables fresh while this tab is open
  }
}

// ---------- connections ----------

function renderConnections() {
  $s("connections").innerHTML = src.connections.length
    ? `<tr><th>name</th><th>database</th><th>user</th><th>access</th><th></th></tr>` + src.connections.map((c) => `<tr>
        <td>${esc(c.name)}</td><td>${esc(c.host)}:${c.port} / ${esc(c.database)}</td><td>${esc(c.username)}</td>
        <td>${c.read_only ? `<span class="tag ok">read-only</span>` : `<span class="tag warn" title="${esc(c.warnings.join(" "))}">can write</span>`}</td>
        <td><button data-scan="${c.connection_id}">Scan tables</button></td></tr>`).join("")
    : `<tr><td class="empty">No database connected yet.</td></tr>`;
}

async function connect(form) {
  const data = Object.fromEntries(new FormData(form));
  data.port = Number(data.port);
  $s("connect-btn").disabled = true;
  notice("", "connecting...");
  try {
    const result = await post("/connections", data);
    form.password.value = "";
    if (result.read_only) {
      notice("ok", `Connected as <b>${esc(result.user)}</b>. This user cannot write, which is what we want.`);
    } else {
      notice("warn", `<p><b>Connected, but this user is not read-only:</b></p>` + result.warnings.map((w) => `<p>${esc(w)}</p>`).join("") +
        `<p>PipelineDoctor only opens read-only sessions, but a SELECT-only user is the real safeguard.</p>`);
    }
    await refreshSources();
    await scan(result.connection_id);
  } catch (e) {
    notice("bad", esc(e.message));
  }
  $s("connect-btn").disabled = false;
}

// ---------- scanning and choosing tables ----------

function renderScan() {
  $s("scan-section").hidden = !src.tables.length && src.connectionId === null;
  const watchedKeys = new Set(src.watched.map((w) => `${w.schema_name}.${w.table_name}`));
  $s("scan-table").innerHTML = `<tr><th>table</th><th class="num">rows</th><th>latest row</th><th>time column</th><th>fit</th><th></th></tr>` +
    src.tables.map((t) => {
      const key = `${t.schema}.${t.table}`;
      const fit = !t.watchable ? `<span class="tag">no time column</span>` : t.score >= 5 ? `<span class="tag ok">active</span>` : `<span class="tag warn">not recent</span>`;
      const button = watchedKeys.has(key) ? `<span class="faint">watching</span>`
        : t.watchable ? `<button data-edit="${esc(key)}">Watch...</button>` : "";
      return `<tr><td>${esc(key)}</td><td class="num">${num(t.rows)}</td><td class="dim">${esc((t.latest || "-").slice(0, 16))}</td>
        <td>${esc(t.suggested_time_column || "-")}</td><td>${fit}</td><td>${button}</td></tr>` + (src.editing === key ? editorRow(t) : "");
    }).join("");
}

function editorRow(t) {
  const timeTypes = t.columns.filter((c) => c.type.startsWith("timestamp") || c.type === "date");
  const options = (list, selected, blank) => (blank ? `<option value="">none</option>` : "") +
    list.map((c) => `<option value="${esc(c.name)}" ${c.name === selected ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const excluded = new Set(t.suggested_excluded);
  return `<tr class="editor-row"><td colspan="6"><div class="editor">
    <label>Time column (when a row was added)<select id="ed-time">${options(timeTypes, t.suggested_time_column)}</select></label>
    <label>Key column (to find duplicates)<select id="ed-key">${options(t.columns, t.suggested_key_column, true)}</select></label>
    <div class="excl"><span>Never read these columns (personal data suggested):</span>
      ${t.columns.map((c) => `<label><input type="checkbox" class="ed-excl" value="${esc(c.name)}" ${excluded.has(c.name) ? "checked" : ""}>${esc(c.name)}</label>`).join("")}
    </div>
    <button class="primary" data-start="${esc(`${t.schema}.${t.table}`)}">Start watching</button>
  </div></td></tr>`;
}

async function scan(connectionId) {
  src.connectionId = connectionId;
  src.editing = null;
  $s("scan-section").hidden = false;
  $s("scan-table").innerHTML = `<tr><td class="empty">scanning...</td></tr>`;
  try {
    src.tables = await post(`/connections/${connectionId}/scan`);
    const name = (src.connections.find((c) => c.connection_id === connectionId) || {}).name || "";
    $s("scan-title").innerHTML = `Tables in ${esc(name)} <span class="hint">best candidates first: tables that keep receiving new rows</span>`;
    renderScan();
  } catch (e) {
    $s("scan-table").innerHTML = `<tr><td class="empty bad">${esc(e.message)}</td></tr>`;
  }
}

async function startWatching(key) {
  const t = src.tables.find((x) => `${x.schema}.${x.table}` === key);
  const body = {
    schema_name: t.schema, table: t.table,
    time_column: $s("ed-time").value, key_column: $s("ed-key").value || null,
    excluded_columns: [...document.querySelectorAll(".ed-excl:checked")].map((i) => i.value),
  };
  try {
    const { job_id } = await post(`/connections/${src.connectionId}/watch`, body);
    src.editing = null;
    renderScan();   // close the form now (a background refresh may already have redrawn the list)
    notice("", `Watching ${esc(key)}. Learning what a normal day looks like (job ${job_id})...`);
    follow(job_id);
    await refreshSources();
  } catch (e) {
    notice("bad", esc(e.message));
  }
}

// Follow a background job and show its progress in the message box.
async function follow(jobId) {
  for (let i = 0; i < 120; i++) {
    try {
      const job = await api(`/jobs/${jobId}`);
      if (job.status === "done") { notice("ok", esc(job.progress)); break; }
      if (job.status === "failed") { notice("bad", esc(job.error)); break; }
      notice("", esc(job.progress || job.status));
    } catch (e) { /* not visible yet */ }
    await new Promise((r) => setTimeout(r, 1000));
  }
  refreshSources();
}

// ---------- watched tables ----------

function renderWatched() {
  $s("watched").innerHTML = src.watched.length
    ? `<tr><th>table</th><th>connection</th><th>status</th><th>last check</th><th>days learned</th><th></th></tr>` + src.watched.map((w) => {
      const cells = (w.checks || []).map((c) => `<span class="cell ${c.passed ? "pass" : "fail"}" title="${esc(c.name)}: ${c.passed ? "passed" : "FAILED"}"></span>`).join("");
      return `<tr class="click" data-watch="${w.watch_id}"><td>${esc(w.schema_name)}.${esc(w.table_name)}</td><td>${esc(w.connection_name)}</td>
        <td>${tag(w.status)} <span class="strip">${cells}</span></td><td class="dim">${esc(day(w.last_checked_day) || "-")}</td>
        <td>${w.profiled_days}</td><td><button data-check="${w.watch_id}">Check now</button></td></tr>`;
    }).join("")
    : `<tr><td class="empty">Nothing watched yet. Connect a database, scan it, and choose a table.</td></tr>`;
}

async function openWatch(id) {
  state.openIncident = null;   // the drawer now shows a watched table, not an incident
  $s("drawer").hidden = false;
  $s("drawer-title").textContent = `watched table ${id}`;
  const d = await api(`/watch/${id}`);
  const w = d.watch;
  $s("drawer-title").textContent = `${w.schema_name}.${w.table_name}  /  ${w.connection_name}`;
  const recent = d.days.slice(-30);
  const peak = Math.max(1, ...recent.map((x) => x.row_count));
  $s("drawer-body").innerHTML = `
    <div class="block"><dl class="kv"><dt>time column</dt><dd>${esc(w.time_column)}</dd><dt>key column</dt><dd>${esc(w.key_column || "none")}</dd>
      <dt>never read</dt><dd>${esc((w.excluded_columns || []).join(", ") || "nothing excluded")}</dd></dl></div>
    <div class="block"><h3>Rows per day <span class="faint">last ${recent.length} days, latest in blue</span></h3>
      <div class="bars">${recent.map((x, i) => `<i class="${i === recent.length - 1 ? "last" : ""}" style="height:${(100 * x.row_count / peak).toFixed(0)}%" title="${day(x.day)}: ${num(x.row_count)} rows"></i>`).join("")}</div></div>
    <div class="block"><h3>Latest check ${d.checks.length ? `<span class="faint">${day(d.checks[0].day)}</span>` : ""}</h3>
      ${d.checks.length ? `<table>${d.checks.map((c) => `<tr><td><span class="${c.passed ? "ok" : "bad"}">${c.passed ? "PASS" : "FAIL"}</span></td><td><code>${esc(c.check_name)}</code></td><td class="dim">${esc(c.details)}</td></tr>`).join("")}</table>`
        : `<p class="dim">No check yet: still learning (needs ${5} healthy days).</p>`}</div>
    <div class="block"><h3>Alerts</h3>${d.alerts.length ? d.alerts.map((a) => `<p><span class="tag ${a.status === "open" ? "bad" : ""}">${esc(a.status)}</span> ${esc(a.summary)}</p>`).join("") : `<p class="dim">No alerts.</p>`}</div>`;
}

async function refreshSources() {
  try {
    const [connections, watched] = await Promise.all([api("/connections"), api("/watch")]);
    // Redraw only what changed: rebuilding a table under the mouse can swallow a click or wipe a half-filled form.
    if (JSON.stringify(connections) !== JSON.stringify(src.connections)) { src.connections = connections; renderConnections(); }
    if (JSON.stringify(watched) !== JSON.stringify(src.watched)) {
      src.watched = watched;
      renderWatched();
      if (src.connectionId !== null) renderScan();   // the "watching" markers depend on the watched list
    }
  } catch (e) { /* the pipeline tab shows API problems; stay quiet here */ }
}

// ---------- events ----------

document.querySelector(".tabs").addEventListener("click", (e) => { if (e.target.dataset.view) showView(e.target.dataset.view); });
$s("connect-form").addEventListener("submit", (e) => { e.preventDefault(); connect(e.target); });
$s("demo-fill").addEventListener("click", () => {
  const f = $s("connect-form");
  Object.entries({ name: "demo shop", host: "postgres", port: "5432", database: "shop_demo", user: "demo_reader", password: "demo_reader_pw" })
    .forEach(([k, v]) => { f[k].value = v; });
});

$s("view-sources").addEventListener("click", async (e) => {
  const t = e.target;
  if (t.dataset.scan) return scan(Number(t.dataset.scan));
  if (t.dataset.edit) { src.editing = src.editing === t.dataset.edit ? null : t.dataset.edit; return renderScan(); }
  if (t.dataset.start) return startWatching(t.dataset.start);
  if (t.dataset.check) {
    t.disabled = true;
    try { const { job_id } = await post(`/watch/${t.dataset.check}/check`); notice("", "Checking..."); follow(job_id); }
    catch (err) { notice("bad", esc(err.message)); t.disabled = false; }
    return;
  }
  const row = t.closest("tr[data-watch]");
  if (row) openWatch(Number(row.dataset.watch));
});

showView("pipeline");
