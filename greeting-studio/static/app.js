/* Guest Greeting & Review Studio — vanilla JS single-page frontend */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const main = $("#main");

/* ---------------- helpers ---------------- */

async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json" }, ...options };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), isError ? 7000 : 3500);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function openModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal-backdrop").classList.remove("hidden");
}
function closeModal() { $("#modal-backdrop").classList.add("hidden"); }
$("#modal-backdrop").addEventListener("click", e => {
  if (e.target.id === "modal-backdrop") closeModal();
});

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard ✓");
  } catch (e) {
    toast("Clipboard blocked — select and copy manually", true);
  }
}

function busy(btn, on) {
  if (!btn) return;
  btn.disabled = on;
  if (on) { btn.dataset.label = btn.textContent; btn.textContent = "Working…"; }
  else if (btn.dataset.label) btn.textContent = btn.dataset.label;
}

const STATUS_BADGE = s => `<span class="badge ${esc(s)}">${esc(s)}</span>`;

/* ---------------- tabs ---------------- */

const views = { dashboard: renderDashboard, units: renderUnits, stays: renderStays,
                audit: renderAudit, settings: renderSettings };

$$("#tabs button").forEach(btn => btn.addEventListener("click", () => showTab(btn.dataset.tab)));

function showTab(name) {
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  views[name]();
}

/* ---------------- Dashboard ---------------- */

async function renderDashboard() {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const [dash, units, queue] = await Promise.all([
    api("/api/dashboard"), api("/api/units"), api("/api/queue")]);
  const noTemplates = units.filter(u => !u.template_version).map(u => u.name);
  let html = "";

  if (queue.session_expired) {
    html += `<div class="contradiction" style="border-left-color:var(--red);background:#fdecea">
      <b>Airbnb session expired</b> — the send automation can't log in. Click "Browser assist" on any
      draft to open a window and log in to Airbnb again, then retry.</div>`;
  }

  html += `<h2>Today's Arrivals ${queue.items.length ? `<span class="badge ${queue.items.some(i => i.status === "needs_setup") ? "due" : "reviewed"}">${queue.items.filter(i => i.status !== "sent").length} pending</span>` : ""}
    <button class="secondary small" style="float:right" onclick="runQueue(this)">↻ Rebuild queue now</button></h2>`;
  html += `<p class="muted small">Drafts are generated automatically every morning at 7:00 AM Pacific
    (and on app start if the 7 AM run was missed). Nothing sends without your Send click.
    ${queue.last_run ? `Last run: ${esc(queue.last_run.replace("T", " ").slice(0, 16))} UTC.` : "No run yet today."}</p>`;
  if (!queue.items.length) {
    html += `<div class="card muted small">No check-ins today.</div>`;
  } else {
    html += queue.items.map(renderQueueCard).join("");
  }

  html += `<h2>Next 7 days ${dash.due_count ? `<span class="badge due">⚠ ${dash.due_count} greeting${dash.due_count > 1 ? "s" : ""} due</span>` : ""}</h2>`;
  if (!dash.stays.length) {
    html += `<div class="card empty-state"><div class="big">🌤</div>
      No check-ins in the next 7 days.<br><span class="small">Add stays on the Stays tab (manual, CSV, or Airbnb .ics import).</span></div>`;
  } else {
    html += `<div class="card"><table><thead><tr>
      <th>Check-in</th><th>Guest</th><th>Unit</th><th>Status</th><th></th></tr></thead><tbody>`;
    for (const s of dash.stays) {
      const due = s.greeting_due
        ? `<span class="badge due">Greeting due${s.days_until_checkin === 0 ? " — today!" : " — tomorrow"}</span> `
        : "";
      html += `<tr>
        <td>${esc(s.checkin_date)}<div class="small muted">${s.days_until_checkin === 0 ? "today" : s.days_until_checkin === 1 ? "tomorrow" : "in " + s.days_until_checkin + " days"}</div></td>
        <td>${esc(s.guest_name)}</td>
        <td>${esc(s.unit_name)}</td>
        <td>${due}${STATUS_BADGE(s.status)}</td>
        <td><button class="ghost" onclick="openStay(${s.id})">Open →</button></td></tr>`;
    }
    html += `</tbody></table></div>`;
  }
  if (noTemplates.length) {
    html += `<div class="card muted small">Units without a template yet: <b>${noTemplates.map(esc).join(", ")}</b> — build them on the Units tab so drafting works.</div>`;
  }
  main.innerHTML = html;
}

function renderQueueCard(q) {
  const badge = { ready: "reviewed", needs_setup: "due", sent: "sent", failed: "due" }[q.status] || "pending";
  const label = { ready: "ready", needs_setup: "needs setup", sent: "sent ✓", failed: "failed" }[q.status] || q.status;
  const editable = q.status !== "sent";
  return `<div class="card">
    <div class="row" style="justify-content:space-between">
      <b>${esc(q.unit_name)} — ${esc(q.guest_name)}</b>
      <span class="badge ${badge}">${label}</span>
    </div>
    ${q.error ? `<p class="small" style="color:var(--red);margin:6px 0">${esc(q.error)}</p>` : ""}
    ${q.status === "needs_setup"
      ? `<p class="muted small">Fix the setup above, then click Rebuild queue now.</p>`
      : `<textarea id="q-msg-${q.id}" class="tall" ${editable ? "" : "readonly"}>${esc(q.message)}</textarea>
         <div class="row" style="margin-top:8px">
           ${q.status === "sent"
             ? `<span class="muted small">Sent ${esc((q.sent_at || "").replace("T", " ").slice(0, 16))} UTC — logged.</span>`
             : `<button onclick="sendQueueItem(${q.id}, this)">${q.status === "failed" ? "Retry send" : "Send"}</button>
                <button class="secondary" onclick="saveQueueEdit(${q.id}, this)">Save edit</button>
                <button class="secondary" onclick="queueAssist(${q.id})">Browser assist (fill only)</button>`}
         </div>`}
  </div>`;
}

async function runQueue(btn) {
  busy(btn, true);
  try {
    const r = await api("/api/queue/run?force=true", { method: "POST" });
    const sync = r.sync ? ` (calendar sync: +${r.sync.added} new, ${r.sync.updated} updated)` : "";
    toast(`Queue rebuilt: ${r.queued} drafted, ${r.needs_setup} need setup${sync} ✓`);
    if (r.errors && r.errors.length) setTimeout(() => toast(r.errors[0], true), 3600);
    renderDashboard();
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function saveQueueEdit(id, btn) {
  busy(btn, true);
  try {
    await api(`/api/queue/${id}`, { method: "PUT", body: { message: $(`#q-msg-${id}`).value } });
    toast("Edit saved ✓");
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function sendQueueItem(id, btn) {
  if (!confirm("Send this message to the guest's Airbnb thread now?")) return;
  busy(btn, true);
  try {
    const r = await api(`/api/queue/${id}/send`, { method: "POST",
      body: { message: $(`#q-msg-${id}`).value } });
    toast(r.detail || "Sent ✓");
    renderDashboard();
  } catch (e) {
    toast(e.message, true);
    renderDashboard();  // show the failed status + retry button
  } finally { busy(btn, false); }
}

async function queueAssist(id) {
  const queue = await api("/api/queue");
  const q = queue.items.find(x => x.id === id);
  if (!q) return;
  await copyText($(`#q-msg-${id}`) ? $(`#q-msg-${id}`).value : q.message);
  try {
    const r = await api(`/api/stays/${q.stay_id}/assist`, { method: "POST",
      body: { content: $(`#q-msg-${id}`).value } });
    toast(r.detail);
  } catch (e) { toast(e.message, true); }
}

/* ---------------- Units & Templates ---------------- */

async function renderUnits() {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const units = await api("/api/units");
  let html = `<h2>Units</h2><div class="card"><table><thead><tr>
    <th>Unit</th><th>Listing ID</th><th>History</th><th>Template</th><th></th></tr></thead><tbody>`;
  for (const u of units) {
    html += `<tr><td><b>${esc(u.name)}</b>${u.listing_title && u.listing_title !== u.name ? `<div class="small muted">"${esc(u.listing_title)}"</div>` : ""}</td>
      <td class="small muted">${esc(u.airbnb_listing_id || "—")}</td>
      <td class="small">${u.message_count} msgs / ${u.greeting_count} greetings</td>
      <td class="small">${u.template_version ? `v${u.template_version} <span class="muted">(${esc(u.template_source)})</span>` : `<span class="muted">none</span>`}</td>
      <td><button class="ghost" onclick="openUnit(${u.id})">Open →</button></td></tr>`;
  }
  html += `</tbody></table></div>
  <h2>Add Unit</h2>
  <div class="card">
    <div class="grid2">
      <div><label>Name *</label><input id="new-unit-name" placeholder="The Treehouse"></div>
      <div><label>Airbnb listing ID</label><input id="new-unit-lid" placeholder="1234567890123456789"></div>
      <div><label>Airbnb listing title (if different)</label><input id="new-unit-title"></div>
      <div><label>Profile / character notes</label><input id="new-unit-profile" placeholder="What makes this unit special"></div>
    </div>
    <div style="margin-top:10px"><button onclick="addUnit(this)">Add Unit</button></div>
  </div>`;
  main.innerHTML = html;
}

async function addUnit(btn) {
  busy(btn, true);
  try {
    await api("/api/units", { method: "POST", body: {
      name: $("#new-unit-name").value, airbnb_listing_id: $("#new-unit-lid").value,
      listing_title: $("#new-unit-title").value, profile: $("#new-unit-profile").value } });
    toast("Unit added ✓"); renderUnits();
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function openUnit(unitId) {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const [units, tpl, versions, messages] = await Promise.all([
    api("/api/units"), api(`/api/units/${unitId}/template`),
    api(`/api/units/${unitId}/template/versions`), api(`/api/units/${unitId}/messages`)]);
  const unit = units.find(u => u.id === unitId);
  const donors = units.filter(u => u.id !== unitId && u.template_version);
  const am = unit.amenities || {};

  main.innerHTML = `
  <button class="ghost" onclick="renderUnits()">← All units</button>
  <h2>${esc(unit.name)} ${unit.listing_title && unit.listing_title !== unit.name ? `<span class="muted small">"${esc(unit.listing_title)}"</span>` : ""}</h2>

  <div class="grid2">
    <div class="card">
      <h3>Import message history</h3>
      <p class="muted small">Paste a thread (prefix lines with <code>Host:</code> / <code>Guest:</code>), or CSV (<code>sender,message</code>), or JSON. Host greetings are auto-detected — you can fix any message below.</p>
      <label>Guest name (optional, helps parsing & reviews)</label><input id="imp-guest">
      <label>Stay dates (optional, e.g. "Aug 1–4 2026")</label><input id="imp-dates">
      <label>Thread</label><textarea id="imp-text" class="tall mono" placeholder="Host: Hi Sarah! Welcome..."></textarea>
      <div style="margin-top:8px"><button onclick="doImport(${unitId}, this)">Import</button></div>
    </div>
    <div class="card">
      <h3>Base template ${tpl ? `<span class="muted small">v${tpl.version} · ${esc(tpl.source)}</span>` : ""}</h3>
      ${tpl ? `<textarea id="tpl-content" class="tall">${esc(tpl.content)}</textarea>
        <div class="row" style="margin-top:8px">
          <button onclick="saveTemplate(${unitId}, this)">Save new version</button>
          <button class="secondary" onclick="synthesize(${unitId}, this)">Re-synthesize from history</button>
        </div>`
      : `<p class="muted small">No template yet.</p>
        <div class="row">
          <button onclick="synthesize(${unitId}, this)">Synthesize from history</button>
        </div>`}
      <div class="row" style="margin-top:8px">
        ${donors.length ? `<select id="donor-select" style="width:auto">${donors.map(d => `<option value="${d.id}">${esc(d.name)} (v${d.template_version})</option>`).join("")}</select>
        <button class="secondary" onclick="bootstrap(${unitId}, this)">Bootstrap from donor unit</button>` : `<span class="muted small">No donor units with templates yet (needed for bootstrapping a new unit).</span>`}
      </div>
      ${versions.length > 1 ? `<h3>Version history</h3>` + versions.map(v =>
        `<div class="row small" style="justify-content:space-between;border-bottom:1px solid var(--border);padding:4px 0">
           <span>v${v.version} · ${esc(v.source)} · ${esc(v.note)} <span class="muted">${esc(v.created_at.slice(0, 10))}</span></span>
           <button class="ghost" onclick='previewVersion(${JSON.stringify(JSON.stringify({ v: v.version, c: v.content }))})'>view</button>
         </div>`).join("") : ""}
    </div>
  </div>

  <div class="card">
    <h3>Amenities & operational details <span class="muted small">(used as merge-field values when drafting)</span></h3>
    <div class="grid2">
      <div><label>Door code</label><input id="am-door_code" value="${esc(am.door_code || "")}"></div>
      <div><label>WiFi network</label><input id="am-wifi_network" value="${esc(am.wifi_network || "")}"></div>
      <div><label>WiFi password</label><input id="am-wifi_password" value="${esc(am.wifi_password || "")}"></div>
      <div><label>Check-in time (blank = default)</label><input id="am-checkin_time" value="${esc(am.checkin_time || "")}"></div>
      <div><label>Checkout time (blank = default)</label><input id="am-checkout_time" value="${esc(am.checkout_time || "")}"></div>
      <div><label>Parking notes</label><input id="am-parking" value="${esc(am.parking || "")}"></div>
    </div>
    <label>House quirks (thermostat, plumbing of an older home, etc.)</label>
    <textarea id="am-quirks">${esc(am.quirks || "")}</textarea>
    <label>Unit profile / character</label>
    <textarea id="unit-profile">${esc(unit.profile || "")}</textarea>
    <label>Airbnb listing description (paste to enrich bootstrapping)</label>
    <textarea id="unit-listing-desc">${esc(unit.listing_description || "")}</textarea>
    <label>Airbnb calendar feed URL (auto-syncs stays every morning; from Airbnb → Calendar → Connect to another website)</label>
    <input id="unit-ics-url" value="${esc(unit.ics_url || "")}" placeholder="https://www.airbnb.com/calendar/ical/....ics?t=...">

    <div style="margin-top:8px"><button onclick="saveUnitDetails(${unitId}, this)">Save details</button></div>
  </div>

  <div class="card">
    <h3 class="row" style="justify-content:space-between">Imported messages (${messages.length})
      ${messages.length ? `<button class="secondary small" onclick="clearMessages(${unitId})">Delete ALL imported messages</button>` : ""}</h3>
    <p class="muted small">✅ greeting = will feed template synthesis. Click a message's buttons to reclassify or delete.</p>
    <div id="msg-list">${messages.map(renderMsg).join("") || `<span class="muted small">Nothing imported yet.</span>`}</div>
  </div>`;
}

function renderMsg(m) {
  return `<div class="msg ${m.sender}" id="msg-${m.id}">
    <div class="meta">
      <b>${m.sender === "host" ? "Host" : "Guest"}</b>
      ${m.is_greeting ? `<span class="tag">✅ greeting</span>` : ""}
      ${m.guest_name ? `<span>· ${esc(m.guest_name)}</span>` : ""}
      ${m.stay_dates ? `<span>· ${esc(m.stay_dates)}</span>` : ""}
      <span style="margin-left:auto">
        <button class="ghost small" onclick="flipSender(${m.id}, '${m.sender === "host" ? "guest" : "host"}')">↔ ${m.sender === "host" ? "guest" : "host"}</button>
        <button class="ghost small" onclick="flipGreeting(${m.id}, ${m.is_greeting ? "false" : "true"})">${m.is_greeting ? "✕ not greeting" : "✓ mark greeting"}</button>
        <button class="ghost small" onclick="deleteMessage(${m.id})">🗑</button>
      </span>
    </div>${esc(m.body)}</div>`;
}

async function doImport(unitId, btn) {
  busy(btn, true);
  try {
    const r = await api(`/api/units/${unitId}/imports`, { method: "POST", body: {
      raw_text: $("#imp-text").value, format: "auto",
      guest_name: $("#imp-guest").value, stay_dates: $("#imp-dates").value } });
    toast(`Imported ${r.messages} messages (${r.host_messages} host / ${r.guest_messages} guest, ${r.greetings_detected} greetings detected) ✓`);
    openUnit(unitId);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function flipSender(id, sender) {
  await api(`/api/messages/${id}`, { method: "PUT", body: { sender } });
  const unitId = currentUnitIdFromDom(); if (unitId) openUnit(unitId);
}
async function flipGreeting(id, isGreeting) {
  await api(`/api/messages/${id}`, { method: "PUT", body: { is_greeting: isGreeting } });
  const unitId = currentUnitIdFromDom(); if (unitId) openUnit(unitId);
}
async function deleteMessage(id) {
  await api(`/api/messages/${id}`, { method: "DELETE" });
  const unitId = currentUnitIdFromDom(); if (unitId) openUnit(unitId);
}

async function clearMessages(unitId) {
  if (!confirm("Delete ALL imported messages for this unit? Templates and their version history are kept.")) return;
  await api(`/api/units/${unitId}/messages`, { method: "DELETE" });
  toast("Imported messages cleared ✓");
  openUnit(unitId);
}

function currentUnitIdFromDom() {
  const btn = $("button[onclick^='doImport(']");
  if (!btn) return null;
  return parseInt(btn.getAttribute("onclick").match(/doImport\((\d+)/)[1], 10);
}

async function synthesize(unitId, btn) {
  busy(btn, true);
  try {
    const r = await api(`/api/units/${unitId}/synthesize`, { method: "POST" });
    toast(`Template v${r.version} synthesized ✓`);
    openUnit(unitId);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function bootstrap(unitId, btn) {
  busy(btn, true);
  try {
    const donor = parseInt($("#donor-select").value, 10);
    const r = await api(`/api/units/${unitId}/bootstrap`, { method: "POST", body: { donor_unit_id: donor } });
    toast(`Template v${r.version} bootstrapped ✓ — review & fill any [PLACEHOLDERS]`);
    openUnit(unitId);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function saveTemplate(unitId, btn) {
  busy(btn, true);
  try {
    const r = await api(`/api/units/${unitId}/template`, { method: "POST",
      body: { content: $("#tpl-content").value, note: "Manual edit" } });
    toast(`Saved as v${r.version} ✓`); openUnit(unitId);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function saveUnitDetails(unitId, btn) {
  busy(btn, true);
  try {
    const amenities = {};
    for (const key of ["door_code", "wifi_network", "wifi_password", "checkin_time", "checkout_time", "parking"]) {
      const v = $(`#am-${key}`).value.trim();
      if (v) amenities[key] = v;
    }
    const quirks = $("#am-quirks").value.trim();
    if (quirks) amenities.quirks = quirks;
    await api(`/api/units/${unitId}`, { method: "PUT", body: {
      amenities, profile: $("#unit-profile").value, listing_description: $("#unit-listing-desc").value,
      ics_url: $("#unit-ics-url").value.trim() } });
    toast("Details saved ✓");
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

function previewVersion(payload) {
  const { v, c } = JSON.parse(payload);
  openModal(`<h3>Template v${v}</h3><pre class="template">${esc(c)}</pre>
    <div class="row" style="margin-top:10px"><button class="secondary" onclick="closeModal()">Close</button></div>`);
}

/* ---------------- Stays ---------------- */

async function renderStays() {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const [stays, units] = await Promise.all([api("/api/stays"), api("/api/units")]);
  const unitOpts = units.map(u => `<option value="${u.id}">${esc(u.name)}</option>`).join("");
  let html = `<h2>Stays</h2>`;
  html += stays.length ? `<div class="card"><table><thead><tr>
      <th>Check-in</th><th>Checkout</th><th>Guest</th><th>Unit</th><th>Status</th><th></th></tr></thead><tbody>` +
    stays.map(s => `<tr>
      <td>${esc(s.checkin_date)} ${s.greeting_due ? '<span class="badge due">due</span>' : ""}</td>
      <td>${esc(s.checkout_date)}</td><td>${esc(s.guest_name)}</td><td>${esc(s.unit_name)}</td>
      <td>${STATUS_BADGE(s.status)}</td>
      <td><button class="ghost" onclick="openStay(${s.id})">Open →</button>
          <button class="ghost" onclick="deleteStay(${s.id})">🗑</button></td></tr>`).join("") +
    `</tbody></table></div>`
    : `<div class="card empty-state"><div class="big">🧳</div>No stays yet — add one below or import.</div>`;

  html += `<div class="grid2">
    <div class="card"><h3>Add stay manually</h3>
      <label>Unit</label><select id="stay-unit">${unitOpts}</select>
      <label>Guest name</label><input id="stay-guest">
      <div class="grid2">
        <div><label>Check-in date</label><input id="stay-ci" type="date"></div>
        <div><label>Checkout date</label><input id="stay-co" type="date"></div>
      </div>
      <label>Airbnb confirmation code (optional, for deep link)</label><input id="stay-code" placeholder="HMABCDEFGH">
      <label>Guest's booking message</label><textarea id="stay-msg" placeholder="Paste what they wrote when they booked"></textarea>
      <div style="margin-top:8px"><button onclick="addStay(this)">Add Stay</button></div>
    </div>
    <div class="card"><h3>Import stays</h3>
      <p class="muted small">Paste an Airbnb calendar export (<b>.ics</b> — the easiest path from Google Calendar) or a CSV with <code>guest_name,checkin_date,checkout_date[,booking_message]</code>.</p>
      <label>Unit these reservations belong to</label><select id="import-unit">${unitOpts}</select>
      <label>Kind</label><select id="import-kind"><option value="ics">.ics calendar</option><option value="csv">CSV</option></select>
      <label>Content (or choose a file)</label>
      <input type="file" id="import-file" accept=".ics,.csv,.txt" style="margin-bottom:6px">
      <textarea id="import-content" class="mono" placeholder="BEGIN:VCALENDAR..."></textarea>
      <div style="margin-top:8px"><button onclick="importStays(this)">Import</button></div>
    </div>
  </div>`;
  main.innerHTML = html;
  $("#import-file").addEventListener("change", async e => {
    const f = e.target.files[0];
    if (f) $("#import-content").value = await f.text();
  });
}

async function addStay(btn) {
  busy(btn, true);
  try {
    await api("/api/stays", { method: "POST", body: {
      unit_id: parseInt($("#stay-unit").value, 10), guest_name: $("#stay-guest").value,
      checkin_date: $("#stay-ci").value, checkout_date: $("#stay-co").value,
      booking_message: $("#stay-msg").value, confirmation_code: $("#stay-code").value } });
    toast("Stay added ✓"); renderStays();
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function importStays(btn) {
  busy(btn, true);
  try {
    const r = await api("/api/stays/import", { method: "POST", body: {
      unit_id: parseInt($("#import-unit").value, 10),
      kind: $("#import-kind").value, content: $("#import-content").value } });
    toast(`Imported ${r.added} stay(s), skipped ${r.skipped_duplicates} duplicate(s) ✓`);
    renderStays();
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function deleteStay(id) {
  if (!confirm("Delete this stay?")) return;
  await api(`/api/stays/${id}`, { method: "DELETE" });
  renderStays();
}

async function openStay(stayId) {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const stays = await api("/api/stays");
  const s = stays.find(x => x.id === stayId);
  if (!s) { toast("Stay not found", true); return renderStays(); }
  const reviews = await api(`/api/stays/${stayId}/reviews`);

  main.innerHTML = `
  <button class="ghost" onclick="renderStays()">← All stays</button>
  <h2>${esc(s.guest_name)} · ${esc(s.unit_name)} ${STATUS_BADGE(s.status)} ${s.greeting_due ? '<span class="badge due">Greeting due</span>' : ""}</h2>
  <p class="muted small">${esc(s.checkin_date)} → ${esc(s.checkout_date)}${s.confirmation_code ? " · " + esc(s.confirmation_code) : ""}</p>

  <div class="card">
    <h3>Booking message from guest</h3>
    <textarea id="stay-booking-msg">${esc(s.booking_message || "")}</textarea>
    <h3>Guest messages during the stay <span class="muted small">(paste here — used for the review generator)</span></h3>
    <textarea id="stay-guest-msgs">${esc(s.guest_messages || "")}</textarea>
    <div style="margin-top:8px"><button class="secondary" onclick="saveStayMsgs(${s.id}, this)">Save messages</button></div>
  </div>

  <div class="card">
    <h3>Check-in greeting</h3>
    <div class="row" style="margin-bottom:10px">
      <button onclick="draftGreeting(${s.id}, this)">${s.draft_greeting ? "Re-draft with Claude" : "Draft Greeting"}</button>
    </div>
    <div id="draft-area">${s.draft_greeting ? renderDraftArea(s) : `<span class="muted small">No draft yet. The draft uses the unit template + stay details + the guest's booking message.</span>`}</div>
  </div>

  <div class="card">
    <h3>Guest review (post-checkout)</h3>
    <div class="row" style="margin-bottom:10px">
      <button onclick="genReviews(${s.id}, this)">Generate Review (3 humor levels)</button>
    </div>
    <div id="reviews-area">${reviews.length ? reviews.map(renderReview).join("") : `<span class="muted small">No review variants yet.</span>`}</div>
  </div>`;
}

function renderDraftArea(s, extra = {}) {
  const mv = extra.merge_values || s.merge_values || {};
  const tplHtml = extra.template
    ? `<div><h3>Template used</h3><pre class="template">${esc(extra.template)}</pre></div>` : "";
  const mvRows = Object.entries(mv).map(([k, v]) =>
    `<tr><td class="mono small">{{${esc(k)}}}</td><td class="${v === "[MISSING]" ? "missing" : ""}">${esc(v)}</td></tr>`).join("");
  return `
    <div class="side-by-side">
      <div><h3>Draft (editable — nothing is sent without you)</h3>
        <textarea id="draft-text" class="tall">${esc(extra.draft || s.draft_greeting)}</textarea></div>
      ${tplHtml || `<div><h3>Merge-field values used</h3>
        <table class="merge-table">${mvRows || "<tr><td class='muted'>—</td></tr>"}</table>
        <p class="muted small">Verify the door code, WiFi, and dates before approving. [MISSING] = fill in the unit's amenities.</p></div>`}
    </div>
    ${extra.template ? `<h3>Merge-field values used</h3><table class="merge-table">${mvRows}</table>` : ""}
    <div class="row" style="margin-top:10px">
      <button onclick="approveAndCopy(${s.id}, this)">✓ Approve → Copy + open Airbnb</button>
      <button class="secondary" onclick="assistFill(${s.id}, this)">🤖 Browser assist (fills, never sends)</button>
      <button class="secondary" onclick="markSent(${s.id}, this)">Mark as sent &amp; log</button>
    </div>`;
}

async function draftGreeting(stayId, btn) {
  busy(btn, true);
  try {
    const r = await api(`/api/stays/${stayId}/draft`, { method: "POST" });
    $("#draft-area").innerHTML = renderDraftArea({ id: stayId, draft_greeting: r.draft, merge_values: r.merge_values },
      { draft: r.draft, template: r.template, merge_values: r.merge_values });
    if (r.missing_values.length) toast(`Heads up: missing values → ${r.missing_values.join(", ")}. Fill the unit's amenities.`, true);
    else toast("Draft ready — review it below ✓");
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function approveAndCopy(stayId, btn) {
  busy(btn, true);
  try {
    const content = $("#draft-text").value;
    const r = await api(`/api/stays/${stayId}/approve`, { method: "POST", body: { content } });
    await copyText(content);
    const url = r.links.reservation || r.links.inbox;
    window.open(url, "_blank");
    toast("Approved ✓ Copied to clipboard — paste into the Airbnb thread and hit send.");
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function assistFill(stayId, btn) {
  busy(btn, true);
  try {
    const content = $("#draft-text").value;
    await copyText(content);
    const r = await api(`/api/stays/${stayId}/assist`, { method: "POST", body: { content } });
    toast(r.detail);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function markSent(stayId, btn) {
  busy(btn, true);
  try {
    await api(`/api/stays/${stayId}/mark-sent`, { method: "POST", body: { content: $("#draft-text").value } });
    toast("Logged as sent ✓"); openStay(stayId);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function saveStayMsgs(stayId, btn) {
  busy(btn, true);
  try {
    await api(`/api/stays/${stayId}`, { method: "PUT", body: {
      booking_message: $("#stay-booking-msg").value, guest_messages: $("#stay-guest-msgs").value } });
    toast("Saved ✓");
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

const HUMOR_LABELS = { subtle: "Subtle", medium: "Medium", full_whimsy: "Full whimsy" };

function renderReview(r) {
  return `<div class="card" style="margin-bottom:10px">
    <div class="row" style="justify-content:space-between">
      <b>${HUMOR_LABELS[r.humor_level] || r.humor_level}</b>
      <button class="ghost" onclick="copyText(document.getElementById('rev-${r.humor_level}').value)">Copy</button>
    </div>
    <textarea id="rev-${r.humor_level}">${esc(r.content)}</textarea>
  </div>`;
}

async function genReviews(stayId, btn) {
  busy(btn, true);
  try {
    const r = await api(`/api/stays/${stayId}/reviews`, { method: "POST" });
    $("#reviews-area").innerHTML = r.variants.map(renderReview).join("");
    toast("3 review variants ready — edit & copy the one you like ✓");
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

/* ---------------- Audit ---------------- */

async function renderAudit() {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const a = await api("/api/audit");
  let html = `<h2>Template consistency audit</h2>
    <p class="muted small">🟢 covered with merge fields · 🟡 present but hardcoded / missing merge field (stale-info risk) · 🔴 missing. Click a cell for details &amp; a Claude-drafted fix.</p>`;
  html += `<div class="card"><table class="matrix"><thead><tr><th>Unit</th>` +
    a.core_items.map(i => `<th class="small">${esc(i.label)}</th>`).join("") + `</tr></thead><tbody>`;
  for (const row of a.matrix) {
    html += `<tr><td><b>${esc(row.unit_name)}</b>${row.has_template ? "" : ' <span class="muted small">(no template)</span>'}</td>`;
    for (const item of a.core_items) {
      const cell = row.items[item.key];
      html += `<td class="cell" title="${esc(cell.detail)}"
        onclick='auditCell(${row.unit_id}, ${JSON.stringify(JSON.stringify({ unit: row.unit_name, key: item.key, label: item.label, status: cell.status, detail: cell.detail }))})'>
        <span class="dot ${cell.status}"></span></td>`;
    }
    html += `</tr>`;
  }
  html += `</tbody></table></div>`;
  if (a.contradictions.length) {
    html += `<h2>Contradictions</h2>` + a.contradictions.map(c =>
      `<div class="contradiction"><b>${esc(c.type)}</b>: ${esc(c.detail)}</div>`).join("");
  }
  main.innerHTML = html;
}

async function auditCell(unitId, payload) {
  const info = JSON.parse(payload);
  const tpl = await api(`/api/units/${unitId}/template`);
  let tplHtml = "(no template)";
  if (tpl) {
    // highlight lines matching the item's keywords
    const audit = await api("/api/audit");
    const item = audit.core_items.find(i => i.key === info.key);
    const kws = (item && item.keywords) || [];
    tplHtml = esc(tpl.content).split("\n").map(line => {
      const hit = kws.some(k => line.toLowerCase().includes(k));
      return hit ? `<mark>${line}</mark>` : line;
    }).join("\n");
  }
  openModal(`<h3>${esc(info.unit)} — ${esc(info.label)}</h3>
    <p><span class="dot ${info.status}"></span> ${esc(info.detail) || "Looks good."}</p>
    <pre class="template">${tplHtml}</pre>
    <div class="row" style="margin-top:10px">
      ${info.status !== "green" && tpl ? `<button onclick='auditSuggest(${unitId}, ${JSON.stringify(JSON.stringify(info))}, this)'>Draft a fix with Claude</button>` : ""}
      <button class="secondary" onclick="closeModal()">Close</button>
    </div>
    <div id="fix-area"></div>`);
}

async function auditSuggest(unitId, payload, btn) {
  const info = JSON.parse(payload);
  busy(btn, true);
  try {
    const r = await api("/api/audit/fix", { method: "POST", body: {
      unit_id: unitId, item_key: info.key, finding: info.detail } });
    $("#fix-area").innerHTML = `<h3>Suggested fix</h3>
      <textarea class="tall" id="fix-text">${esc(r.suggestion)}</textarea>
      <div class="row" style="margin-top:8px">
        <button onclick="copyText($('#fix-text') ? document.getElementById('fix-text').value : '')">Copy</button>
        <span class="muted small">Paste into the unit's template editor and save.</span>
      </div>`;
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

/* ---------------- Settings ---------------- */

async function renderSettings() {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const s = await api("/api/settings");
  main.innerHTML = `<h2>Settings</h2>
  <div class="card">
    <h3>Anthropic API</h3>
    <p class="muted small">Key status: ${s.api_key_set ? "✅ configured" : "❌ not set — Claude features will fail"} (loaded from <code>.env</code>; you can also paste one here for this session)</p>
    <div class="grid2">
      <div><label>API key (session override)</label><input id="set-key" type="password" placeholder="sk-ant-..."></div>
      <div><label>Model</label><input id="set-model" value="${esc(s.anthropic_model)}"></div>
    </div>
  </div>
  <div class="card">
    <h3>Host defaults</h3>
    <div class="grid2">
      <div><label>Host signature</label><input id="set-sig" value="${esc(s.host_signature)}"></div>
      <div></div>
      <div><label>Default check-in time</label><input id="set-ci" value="${esc(s.default_checkin_time)}"></div>
      <div><label>Default checkout time</label><input id="set-co" value="${esc(s.default_checkout_time)}"></div>
    </div>
  </div>
  <div class="card">
    <h3>Calendar auto-sync</h3>
    <p class="muted small">Per-unit Airbnb feeds are set on each unit's page. Optionally add your
      "Airbnb Hosting Schedules" Google calendar's <b>secret iCal address</b> here — it supplies guest
      names and party details the raw Airbnb feeds omit (Google Calendar → Settings → that calendar →
      Integrate calendar → Secret address in iCal format).
      ${s.calendar_last_sync ? `Last sync: ${esc(s.calendar_last_sync.replace("T", " ").slice(0, 16))} UTC.` : ""}</p>
    <label>Hosting Schedules secret iCal URL</label>
    <input id="set-hosting-ics" value="${esc(s.hosting_ics_url || "")}" placeholder="https://calendar.google.com/calendar/ical/.../basic.ics">
    <label>Listing name → unit aliases (JSON; matches the "Listing:" text in calendar events)</label>
    <textarea id="set-aliases" class="mono">${esc(JSON.stringify(s.listing_aliases, null, 2))}</textarea>
  </div>
  <div class="card">
    <h3>Core information checklist <span class="muted small">(drives the audit matrix — JSON, editable)</span></h3>
    <textarea id="set-core" class="tall mono">${esc(JSON.stringify(s.core_items, null, 2))}</textarea>
  </div>
  <button onclick="saveSettings(this)">Save settings</button>`;
}

async function saveSettings(btn) {
  busy(btn, true);
  try {
    let core, aliases;
    try { core = JSON.parse($("#set-core").value); }
    catch (e) { throw new Error("Core items JSON is invalid: " + e.message); }
    try { aliases = JSON.parse($("#set-aliases").value); }
    catch (e) { throw new Error("Listing aliases JSON is invalid: " + e.message); }
    const body = {
      anthropic_model: $("#set-model").value, host_signature: $("#set-sig").value,
      default_checkin_time: $("#set-ci").value, default_checkout_time: $("#set-co").value,
      core_items: core, listing_aliases: aliases,
      hosting_ics_url: $("#set-hosting-ics").value.trim() };
    const key = $("#set-key").value.trim();
    if (key) body.api_key = key;
    await api("/api/settings", { method: "PUT", body });
    toast("Settings saved ✓"); renderSettings();
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

/* ---------------- boot ---------------- */
renderDashboard();
