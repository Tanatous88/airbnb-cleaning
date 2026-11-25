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

/* Copy & paste mode (Settings): the host pastes messages into Airbnb
   themselves, so automated Send / fill-only buttons are hidden. */
let manualMode = true;

function legacyCopy(text) {
  // navigator.clipboard only exists on https or localhost. Opened from a
  // phone at http://<PC's IP>:8321 it's missing, so fall back to the old
  // select-and-copy command, which works anywhere inside a click.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;font-size:16px";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

function copyText(text, quiet = false) {
  // Starts copying immediately (still inside the click), so a window.open
  // right after it isn't treated as a pop-up. Resolves to true on success.
  const done = ok => {
    if (!quiet) toast(ok ? "Copied to clipboard ✓" : "Couldn't copy — select the text and copy it yourself", !ok);
    return ok;
  };
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => done(true), () => done(legacyCopy(text)));
  }
  return Promise.resolve(done(legacyCopy(text)));
}

function openAirbnb(url) {
  // Must be called directly in the click handler, before any await —
  // otherwise Chrome treats the new tab as a blocked pop-up.
  window.open(url || "https://www.airbnb.com/hosting/inbox", "_blank", "noopener");
}

function warnBox(id, text) {
  // Contact details in a Claude draft that the host didn't write (see app/safety.py)
  return `<p id="${id}" class="small" data-warn="${esc(text || "")}" style="${text ? "" : "display:none;"}color:#8a4b00;background:#fff4e0;border-left:3px solid #e0a13a;padding:6px 10px;margin:6px 0">⚠ ${esc(text || "")}</p>`;
}

function okToCopy(warnId) {
  // Synchronous on purpose: runs inside the click, before the Airbnb tab opens.
  const el = document.getElementById(warnId);
  const w = el && el.dataset.warn;
  return !w || confirm(`${w}\n\nCopy it anyway?`);
}

function setWarn(warnId, text) {
  const el = document.getElementById(warnId);
  if (!el) return;
  el.dataset.warn = text || "";
  el.textContent = text ? "⚠ " + text : "";
  el.style.display = text ? "" : "none";
}

function reservationUrl(code) {
  code = (code || "").trim();
  return code ? `https://www.airbnb.com/hosting/reservations/details/${encodeURIComponent(code)}`
              : "https://www.airbnb.com/hosting/inbox";
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
                audit: renderAudit, health: renderHealth, settings: renderSettings };

const RETURNING_BADGE = s => (s.prior_stays && s.prior_stays.length)
  ? ` <span class="badge returning" title="${esc(s.prior_stays.map(p => p.unit_name + " " + p.checkin_date).join(", "))}">returning guest</span>`
  : "";

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
  manualMode = queue.manual_mode !== false;
  const noTemplates = units.filter(u => !u.template_version).map(u => u.name);
  let html = "";

  if (queue.session_expired && !manualMode) {
    html += `<div class="contradiction" style="border-left-color:var(--red);background:#fdecea">
      <b>Airbnb session expired</b> — the send automation can't log in. Click "Browser assist" on any
      draft to open a window and log in to Airbnb again, then retry.</div>`;
  }

  const arrivals = queue.items.filter(i => (i.kind || "arrival") === "arrival");
  const midstays = queue.items.filter(i => i.kind === "midstay");
  const lead = queue.welcome_days_before ?? 1;
  html += `<h2>Welcome messages to send ${arrivals.length ? `<span class="badge ${arrivals.some(i => i.status === "needs_setup") ? "due" : "reviewed"}">${arrivals.filter(i => i.status !== "sent").length} pending</span>` : ""}
    <button class="secondary small" style="float:right" onclick="runQueue(this)">↻ Rebuild queue now</button></h2>`;
  html += `<p class="muted small">For guests arriving ${lead === 0 ? "today" : lead === 1 ? "today or tomorrow" : `in the next ${lead} days`}
    (change it in Settings). Drafts are generated automatically every morning at 7:00 AM Pacific
    (and on app start if the 7 AM run was missed). ${manualMode
      ? "For each one: <b>Copy &amp; open in Airbnb</b>, paste it into the guest's message thread and send it there, then click <b>I sent it</b>."
      : "Nothing sends without your Send click."}
    ${queue.last_run ? `Last run: ${esc(queue.last_run.replace("T", " ").slice(0, 16))} UTC.` : "No run yet today."}</p>`;
  html += arrivals.length ? arrivals.map(renderQueueCard).join("")
                          : `<div class="card muted small">No welcome messages to send — nobody arriving ${lead === 0 ? "today" : lead === 1 ? "today or tomorrow" : "soon"}.</div>`;
  if (midstays.length) {
    html += `<h2>Mid-stay check-ins <span class="badge reviewed">${midstays.filter(i => i.status !== "sent").length} pending</span></h2>
      <p class="muted small">A quick "how's everything?" the day after arrival, for stays of 2+ nights — a chance to fix problems before they become reviews. Edit the wording in Settings.</p>`;
    html += midstays.map(renderQueueCard).join("");
  }

  const reviewsDue = dash.reviews_due || [];
  if (reviewsDue.length) {
    html += `<h2>Reviews due <span class="badge due">${reviewsDue.length}</span></h2>
      <p class="muted small">Airbnb closes the review window 14 days after checkout. Drafts are written automatically the morning after checkout.</p>
      <div class="card"><table><thead><tr><th>Guest</th><th>Unit</th><th>Checked out</th><th>Time left</th><th></th></tr></thead><tbody>`;
    for (const r of reviewsDue) {
      const left = r.days_left <= 0 ? "last day!" : `${r.days_left} day${r.days_left === 1 ? "" : "s"}`;
      html += `<tr><td>${esc(r.guest_name)}</td><td>${esc(r.unit_name)}</td><td>${esc(r.checkout_date)}</td>
        <td><span class="badge ${r.days_left <= 3 ? "due" : "pending"}">${left}</span></td>
        <td>${r.has_drafts ? '<span class="badge reviewed">drafts ready</span> ' : ""}<button class="ghost" onclick="openStay(${r.stay_id})">Open →</button>
          <button class="ghost" onclick="markReviewPostedFromDashboard(${r.stay_id}, this)" title="Already posted it on Airbnb">✓ Posted</button></td></tr>`;
    }
    html += `</tbody></table></div>`;
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
        <td>${esc(s.guest_name)}${RETURNING_BADGE(s)}</td>
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

const PERSONALIZATION_SOURCE = {
  scraped: { icon: "📖", text: "Personalized from the live Airbnb thread", color: "var(--green)" },
  stored:  { icon: "📝", text: "Personalized from the stored booking message", color: "var(--muted)" },
  none:    { icon: "📄", text: "No guest context found — plain template used", color: "var(--muted)" },
};

function renderQueueCard(q) {
  const badge = { ready: "reviewed", needs_setup: "due", sent: "sent", failed: "due" }[q.status] || "pending";
  const label = { ready: "ready", needs_setup: "needs setup", sent: "sent ✓", failed: "failed" }[q.status] || q.status;
  const editable = q.status !== "sent";
  const src = PERSONALIZATION_SOURCE[q.personalization_source];
  return `<div class="card">
    <div class="row" style="justify-content:space-between">
      <b>${q.kind === "midstay" ? "Check-in note · " : ""}${esc(q.unit_name)} — ${esc(q.guest_name)}
        ${q.kind !== "midstay" && q.arrival_date ? `<span class="muted small" style="font-weight:normal">· ${arrivalLabel(q.arrival_date)}</span>` : ""}</b>
      <span class="badge ${badge}">${label}</span>
    </div>
    ${src ? `<p class="small" style="color:${src.color};margin:2px 0 6px">${src.icon} ${src.text}</p>` : ""}
    ${q.error ? `<p class="small" style="color:var(--red);margin:6px 0">${esc(q.error)}</p>` : ""}
    ${q.status !== "sent" ? warnBox(`q-warn-${q.id}`, q.warning) : ""}
    ${q.status === "needs_setup"
      ? `<p class="muted small">Fix it (door code / guest name on the stay page; Wi-Fi on the unit's Amenities). Saving the stay redrafts this card.</p>
         <button class="secondary" onclick="openStay(${q.stay_id})">Open stay →</button>`
      : `<textarea id="q-msg-${q.id}" class="tall" ${editable ? "" : "readonly"}>${esc(q.message)}</textarea>
         <div class="row" style="margin-top:8px">
           ${q.status === "sent"
             ? `<span class="muted small">Sent ${esc((q.sent_at || "").replace("T", " ").slice(0, 16))} UTC — logged.</span>
                <button class="ghost" onclick="undoQueueSent(${q.id}, this)">Undo</button>`
             : `<button onclick="copyAndOpenQueue(${q.id}, '${esc(q.airbnb_url || "")}', this)">📋 Copy &amp; open in Airbnb</button>
                <button class="secondary" onclick="markQueueSent(${q.id}, this)">✓ I sent it</button>
                <button class="ghost" onclick="okToCopy('q-warn-${q.id}') && copyText($('#q-msg-${q.id}').value)">Copy only</button>
                <button class="ghost" onclick="saveQueueEdit(${q.id}, this)">Save edit</button>
                ${manualMode ? "" : `
                <button class="secondary" onclick="sendQueueItem(${q.id}, this)">${q.status === "failed" ? "Retry auto-send" : "Auto-send"}</button>
                <button class="ghost" onclick="queueAssist(${q.id})">Browser assist (fill only)</button>`}`}
         </div>`}
  </div>`;
}

function arrivalLabel(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const day = new Date(y, m - 1, d);
  const now = new Date(); now.setHours(0, 0, 0, 0);
  const diff = Math.round((day - now) / 86400000);
  const when = day.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  return diff === 0 ? `arrives <b>today</b> (${when})` : diff === 1 ? `arrives tomorrow, ${when}` : `arrives ${when}`;
}

function copyAndOpenQueue(id, url, btn) {
  if (!okToCopy(`q-warn-${id}`)) return;
  const text = $(`#q-msg-${id}`).value;
  copyText(text, true).then(ok => {
    toast(ok ? "Copied ✓ — paste it into the guest's message thread and send. Then click \"I sent it\"."
             : "Couldn't copy — select the text and copy it yourself", !ok);
  });
  openAirbnb(url);
  // keep any edits made on the card (and re-check them)
  api(`/api/queue/${id}`, { method: "PUT", body: { message: text } })
    .then(r => setWarn(`q-warn-${id}`, r && r.warning)).catch(() => {});
}

async function markQueueSent(id, btn) {
  busy(btn, true);
  try {
    await api(`/api/queue/${id}/mark-sent`, { method: "POST", body: { message: $(`#q-msg-${id}`).value } });
    toast("Marked as sent ✓");
    renderDashboard();
  } catch (e) { toast(e.message, true); busy(btn, false); }
}

async function undoQueueSent(id, btn) {
  busy(btn, true);
  try {
    await api(`/api/queue/${id}/undo-sent`, { method: "POST" });
    toast("Back to ready");
    renderDashboard();
  } catch (e) { toast(e.message, true); busy(btn, false); }
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
    const r = await api(`/api/queue/${id}`, { method: "PUT", body: { message: $(`#q-msg-${id}`).value } });
    setWarn(`q-warn-${id}`, r && r.warning);
    toast(r && r.warning ? "Edit saved — it still has something that isn't in your template" : "Edit saved ✓", !!(r && r.warning));
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

let showAllStays = false;

async function renderStays() {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const [allStays, units] = await Promise.all([api("/api/stays"), api("/api/units")]);
  const unitOpts = units.map(u => `<option value="${u.id}">${esc(u.name)}</option>`).join("");

  const pastCutoff = new Date();
  pastCutoff.setDate(pastCutoff.getDate() - 14);
  const futureCutoff = new Date();
  futureCutoff.setDate(futureCutoff.getDate() + 14);
  const stays = showAllStays ? allStays
    : allStays.filter(s =>
        (!s.checkout_date || new Date(s.checkout_date) >= pastCutoff) &&
        (!s.checkin_date || new Date(s.checkin_date) <= futureCutoff));
  const hiddenCount = allStays.length - stays.length;

  let html = `<h2>Stays</h2>`;
  if (hiddenCount > 0 || showAllStays) {
    html += `<p class="muted small">${showAllStays
      ? `Showing all ${allStays.length} stays. <a href="#" onclick="showAllStays=false;renderStays();return false;">Hide stays more than 2 weeks away</a>`
      : `Hiding ${hiddenCount} stay(s) more than 2 weeks in the past or future. <a href="#" onclick="showAllStays=true;renderStays();return false;">Show all</a>`
    }</p>`;
  }
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

let currentStayId = null;
let currentStayCode = "";

async function openStay(stayId) {
  currentStayId = stayId;
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const stays = await api("/api/stays");
  const s = stays.find(x => x.id === stayId);
  if (!s) { toast("Stay not found", true); return renderStays(); }
  currentStayCode = s.confirmation_code || "";
  const reviews = await api(`/api/stays/${stayId}/reviews`);

  main.innerHTML = `
  <button class="ghost" onclick="renderStays()">← All stays</button>
  <h2>${esc(s.guest_name)} · ${esc(s.unit_name)} ${STATUS_BADGE(s.status)} ${s.greeting_due ? '<span class="badge due">Greeting due</span>' : ""}${RETURNING_BADGE(s)}</h2>
  <p class="muted small">${esc(s.checkin_date)} → ${esc(s.checkout_date)}${s.confirmation_code ? " · " + esc(s.confirmation_code) : ""}</p>

  <div class="card">
    <div class="grid2">
      <div><label>Guest name (auto-filled from Airbnb's calendar feed or message thread)</label>
        <input id="stay-guest-name" value="${esc(s.guest_name === "Guest (see Airbnb)" ? "" : s.guest_name)}" placeholder="e.g. Jason Luft"></div>
      <div><label>Door code / guest phone last 4 (auto-filled from the Airbnb calendar feed when available)</label>
        <input id="stay-phone4" value="${esc(s.phone_last4 || "")}" placeholder="e.g. 7412" maxlength="8"></div>
    </div>
    <div class="grid2">
      <div><label>Airbnb confirmation code</label>
        <input id="stay-conf" value="${esc(s.confirmation_code || "")}" placeholder="HMABCDEFGH"></div>
    </div>
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
      <button onclick="genReviews(${s.id}, this)">${reviews.length ? "Regenerate reviews" : "Generate Review (3 humor levels)"}</button>
      ${s.review_posted_at
        ? `<span class="badge sent">review posted ${esc(s.review_posted_at.slice(0, 10))}</span>
           <button class="ghost" onclick="markReviewPosted(${s.id}, false, this)">Undo</button>`
        : `<button class="secondary" onclick="markReviewPosted(${s.id}, true, this)">✓ Mark review posted</button>`}
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
    ${warnBox("draft-warn", extra.warning)}
    <div class="row" style="margin-top:10px">
      <button onclick="approveAndCopy(${s.id}, this)">📋 Copy &amp; open in Airbnb</button>
      <button class="secondary" onclick="markSent(${s.id}, this)">✓ I sent it</button>
      ${manualMode ? "" : `<button class="ghost" onclick="assistFill(${s.id}, this)">🤖 Browser assist (fills, never sends)</button>`}
    </div>`;
}

async function draftGreeting(stayId, btn) {
  busy(btn, true);
  try {
    const r = await api(`/api/stays/${stayId}/draft`, { method: "POST" });
    $("#draft-area").innerHTML = renderDraftArea({ id: stayId, draft_greeting: r.draft, merge_values: r.merge_values },
      { draft: r.draft, template: r.template, merge_values: r.merge_values, warning: r.warning });
    if (r.missing_values.length) {
      const amenityFields = r.missing_values.filter(v => v !== "guest_first_name");
      const parts = [];
      if (r.missing_values.includes("guest_first_name"))
        parts.push("guest's name is unknown — fill in the Guest name field above");
      if (amenityFields.length)
        parts.push(`${amenityFields.join(", ")} — fill them in on the unit's Amenities form`);
      toast(`Heads up: ${parts.join("; ")}.`, true);
    }
    else toast("Draft ready — review it below ✓");
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function approveAndCopy(stayId, btn) {
  const content = $("#draft-text").value;
  if (!content.trim()) return toast("The draft is empty", true);
  if (!okToCopy("draft-warn")) return;
  // Copy and open the tab first, while still inside the click — doing it
  // after a server round-trip made Chrome block the new tab as a pop-up.
  const copied = copyText(content, true);
  openAirbnb(reservationUrl(currentStayCode));
  busy(btn, true);
  try {
    await api(`/api/stays/${stayId}/approve`, { method: "POST", body: { content } });
    const ok = await copied;
    toast(ok ? "Copied ✓ — paste it into the guest's message thread and send. Then click \"I sent it\"."
             : "Couldn't copy — select the text and copy it yourself", !ok);
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
    const body = {
      booking_message: $("#stay-booking-msg").value, guest_messages: $("#stay-guest-msgs").value,
      phone_last4: $("#stay-phone4").value.trim(), confirmation_code: $("#stay-conf").value.trim() };
    const name = $("#stay-guest-name").value.trim();
    if (name) body.guest_name = name;
    const r = await api(`/api/stays/${stayId}`, { method: "PUT", body });
    const re = (r && r.redrafted) || [];
    if (!re.length) toast("Saved ✓");
    else if (re.includes("needs_setup")) toast("Saved — today's card still needs something; check it on the Dashboard.", true);
    else toast("Saved ✓ Today's card on the Dashboard was redrafted and is ready.");
    openStay(stayId);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

const HUMOR_LABELS = { subtle: "Subtle", medium: "Medium", full_whimsy: "Full whimsy" };

function renderReview(r) {
  return `<div class="card" style="margin-bottom:10px">
    <div class="row" style="justify-content:space-between">
      <b>${HUMOR_LABELS[r.humor_level] || r.humor_level}</b>
      <span>
        <button class="secondary" onclick="okToCopy('rev-warn-${r.humor_level}') && copyAndOpenReview('rev-${r.humor_level}')" title="Copies this review and opens the reservation — click Leave a review there and paste it">📋 Copy &amp; open in Airbnb</button>
        <button class="ghost" onclick="okToCopy('rev-warn-${r.humor_level}') && copyText(document.getElementById('rev-${r.humor_level}').value)">Copy only</button>
        ${manualMode ? "" : `<button class="ghost" onclick="reviewAssist(document.getElementById('rev-${r.humor_level}').value, this)" title="Opens Airbnb's review form and fills this text in. You pick the stars and press Submit.">Fill in Airbnb (auto)</button>`}
      </span>
    </div>
    ${warnBox(`rev-warn-${r.humor_level}`, r.warning)}
    <textarea id="rev-${r.humor_level}">${esc(r.content)}</textarea>
  </div>`;
}

function copyAndOpenReview(textareaId) {
  copyText(document.getElementById(textareaId).value, true).then(ok => {
    toast(ok ? "Copied ✓ — on the reservation, click Leave a review and paste it. Then click \"Mark review posted\"."
             : "Couldn't copy — select the text and copy it yourself", !ok);
  });
  openAirbnb(reservationUrl(currentStayCode));
}

async function markReviewPostedFromDashboard(stayId, btn) {
  busy(btn, true);
  try {
    await api(`/api/stays/${stayId}/review-posted`, { method: "POST", body: { posted: true } });
    toast("Marked as posted ✓");
    renderDashboard();
  } catch (e) { toast(e.message, true); busy(btn, false); }
}

async function markReviewPosted(stayId, posted, btn) {
  busy(btn, true);
  try {
    await api(`/api/stays/${stayId}/review-posted`, { method: "POST", body: { posted } });
    toast(posted ? "Marked as posted ✓ — it's off the Reviews due list" : "Unmarked");
    openStay(stayId);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function reviewAssist(text, btn) {
  const stayId = currentStayId;
  await copyText(text);
  busy(btn, true);
  try {
    const r = await api(`/api/stays/${stayId}/review-assist`, { method: "POST", body: { content: text } });
    toast(r.detail);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

function renderContextBanner(source, note) {
  const src = PERSONALIZATION_SOURCE[source];
  let html = "";
  if (src) html += `<p class="small" style="color:${src.color};margin:0 0 8px">${src.icon} ${src.text}</p>`;
  if (note) html += `<p class="small" style="color:var(--red);margin:0 0 8px">${esc(note)}</p>`;
  return html;
}

async function genReviews(stayId, btn) {
  busy(btn, true);
  try {
    const r = await api(`/api/stays/${stayId}/reviews`, { method: "POST" });
    $("#reviews-area").innerHTML = renderContextBanner(r.personalization_source, r.note) +
      r.variants.map(renderReview).join("");
    if (r.personalization_source === "scraped") {
      toast("3 review variants ready, flavored from the live Airbnb thread ✓");
    } else if (r.personalization_source === "none") {
      toast("3 review variants ready — no guest messages found, so these are warm but generic. "
           + "Paste something on the stay page for more flavor.", true);
    } else {
      toast("3 review variants ready — edit & copy the one you like ✓");
    }
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

/* ---------------- Health ---------------- */

function fmtTime(iso) {
  return iso ? esc(iso.replace("T", " ").slice(0, 16)) + " UTC" : "never";
}

async function renderHealth() {
  main.innerHTML = `<div class="muted">Loading…</div>`;
  const h = await api("/api/health");
  const ok = (good, yes, no) => good ? `<span class="health-ok">✓ ${yes}</span>` : `<span class="health-bad">✗ ${no}</span>`;
  let html = `<h2>Health</h2>`;

  html += `<div class="card"><h3>App</h3>
    <p>Running since ${fmtTime(h.app.started_at)}.</p>`;
  const changes = h.app.uncommitted_changes;
  if (changes === null) {
    html += `<p class="muted small">Couldn't check for uncommitted code changes (git not available).</p>`;
  } else if (changes.length) {
    html += `<p class="health-bad">⚠ ${changes.length} file(s) in the app folder were changed but never committed —
      possibly another Claude session editing here. A <code>git pull</code> will refuse to run until they're committed or discarded.</p>
      <pre class="mono small">${changes.map(esc).join("\n")}</pre>`;
  } else {
    html += `<p>${ok(true, "No uncommitted code changes")}</p>`;
  }
  html += `<p>${ok(!h.airbnb_session_expired, "Airbnb login OK", "Airbnb login expired — names and guest messages can't be read until you log in again")}
    <button class="secondary small" onclick="airbnbLoginWindow(this)" title="Opens Airbnb on the AI PC in the app's own browser so you can log in">Open Airbnb login window</button></p></div>`;

  const sum = h.calendar.summary;
  html += `<div class="card"><h3>Calendar sync</h3>
    <p>Last sync: ${fmtTime(h.calendar.last_sync)}${sum ? ` — ${sum.added} added, ${sum.updated} updated, ${sum.unchanged} unchanged` : ""}.
      <button class="secondary small" onclick="healthSync(this)">↻ Sync now</button></p>
    <p>${ok(h.calendar.hosting_feed_set, "Guest-name calendar (Hosting Schedules) connected", "Guest-name calendar not set — names will often be missing (Settings)")}</p>
    ${sum && sum.errors && sum.errors.length
      ? `<p class="health-bad">Errors from the last sync:</p><ul>${sum.errors.map(e => `<li>${esc(e)}</li>`).join("")}</ul>`
      : `<p>${ok(true, "No sync errors")}</p>`}
    <p class="muted small">Morning queue last ran ${fmtTime(h.queue.last_run)} · reviews last auto-drafted ${fmtTime(h.reviews.last_auto_run)}${
      h.summary_email && h.summary_email.enabled ? ` · summary email last sent ${fmtTime(h.summary_email.last_sent)}` : ""}</p>
    ${h.summary_email && h.summary_email.last_error ? `<p class="health-bad">Summary email failed: ${esc(h.summary_email.last_error)}</p>` : ""}
    ${h.home_assistant && h.home_assistant.enabled ? `<p class="muted small">Home Assistant alert last sent ${fmtTime(h.home_assistant.last_sent)}</p>` : ""}
    ${h.home_assistant && h.home_assistant.last_error ? `<p class="health-bad">Home Assistant alert failed: ${esc(h.home_assistant.last_error)}</p>` : ""}</div>`;

  html += `<div class="card"><h3>Units</h3><table><thead><tr><th>Unit</th><th>Airbnb calendar feed</th><th>Template</th><th>Door codes from Airbnb</th></tr></thead><tbody>`;
  for (const u of h.units) {
    const pct = u.stays_with_code ? Math.round(100 * (u.stays_with_code - u.phone_missing) / u.stays_with_code) : null;
    html += `<tr><td><b>${esc(u.name)}</b></td>
      <td>${ok(u.feed_set, "set", "missing — add it on the unit's page")}</td>
      <td>${u.template_version ? "v" + u.template_version : '<span class="health-bad">none</span>'}</td>
      <td>${pct === null ? '<span class="muted">no stays yet</span>' : `${pct}% of ${u.stays_with_code} stays`}${u.door_code_fallback ? ' <span class="badge sent" title="Missing codes fall back to the Airbnb app">fallback on</span>' : ""}</td></tr>`;
  }
  html += `</tbody></table></div>`;

  html += `<div class="card"><h3>Needs attention (next 14 days)</h3>`;
  if (!h.attention.length) {
    html += `<p>${ok(true, "Every upcoming stay has what its greeting needs")}</p>`;
  } else {
    html += `<table><thead><tr><th>Check-in</th><th>Guest</th><th>Unit</th><th>Problem</th><th></th></tr></thead><tbody>`;
    for (const a of h.attention) {
      html += `<tr><td>${esc(a.checkin_date)}</td><td>${esc(a.guest_name)}</td><td>${esc(a.unit_name)}</td>
        <td>${a.problems.map(esc).join("<br>")}</td>
        <td><button class="ghost" onclick="openStay(${a.stay_id})">Fix →</button></td></tr>`;
    }
    html += `</tbody></table><p class="muted small">Names are also filled in automatically from the Airbnb message thread when the morning queue runs.</p>`;
  }
  html += `</div>`;

  const dupes = h.duplicates.stays + h.duplicates.queue;
  html += `<div class="card"><h3>Duplicates</h3>
    <p>${ok(dupes === 0, "No duplicate stays or queue cards", `${h.duplicates.stays} duplicated reservation(s), ${h.duplicates.queue} duplicated queue card(s)`)}
    ${dupes ? `<button onclick="healthMerge(this)">Merge duplicates</button>` : ""}</p>
    <p>${ok(h.safeguards.stays_unique && h.safeguards.queue_unique, "Duplicate safeguards active", "Duplicate safeguards off until duplicates are merged")}</p></div>`;

  main.innerHTML = html;
}

async function airbnbLoginWindow(btn) {
  busy(btn, true);
  try {
    const r = await api("/api/airbnb/login-window", { method: "POST" });
    toast(r.detail);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function healthSync(btn) {
  busy(btn, true);
  try {
    const r = await api("/api/health/sync", { method: "POST" });
    toast(`Synced: ${r.added} added, ${r.updated} updated${r.errors.length ? ` — ${r.errors.length} error(s)` : ""}`, r.errors.length > 0);
    renderHealth();
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function healthMerge(btn) {
  if (!confirm("Merge duplicate stays and queue cards? The best copy of each is kept.")) return;
  busy(btn, true);
  try {
    const r = await api("/api/health/merge-duplicates", { method: "POST" });
    toast(`Merged: removed ${r.stays_removed} duplicate stay(s), ${r.queue_removed} duplicate card(s) ✓`);
    renderHealth();
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
      <div><label>Model for template building &amp; reviews <span class="muted">(click-triggered, quality matters)</span></label>
        <input id="set-model" value="${esc(s.anthropic_model)}"></div>
    </div>
    <label>Model for the automated daily draft queue <span class="muted">(runs unattended every morning — this is where API cost actually accumulates; a cheaper/faster model is the recommended way to cut cost without losing template-quality)</span></label>
    <input id="set-queue-model" value="${esc(s.queue_model)}" placeholder="claude-haiku-4-5">
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
    <h3>How you send messages</h3>
    <label><input type="checkbox" id="set-manual-mode" ${s.manual_mode ? "checked" : ""}>
      Copy &amp; paste mode — I paste messages and reviews into Airbnb myself
      <span class="muted">(hides Auto-send, Browser assist and the Airbnb-login banner; the Copy &amp; open and I sent it buttons stay)</span></label>
  </div>
  <div class="card">
    <h3>Morning summary email</h3>
    <p class="muted small">After the 7 AM run, an email listing the welcome messages, check-in notes and reviews
      waiting for you, with a link to the dashboard. Sent from your own email account; only on days with something to do.</p>
    ${s.smtp_configured ? "" : `<p class="small" style="color:var(--red)">Email login not set up yet: add SMTP_USER and SMTP_PASSWORD
      (a Gmail app password) to the .env file on the AI PC, then restart the app. See .env.example.</p>`}
    <label><input type="checkbox" id="set-summary-on" ${s.summary_email_enabled ? "checked" : ""}> Send me the morning summary</label>
    <label>Send to</label>
    <input id="set-summary-to" type="email" value="${esc(s.summary_email_to || "")}" placeholder="you@example.com">
    <label>Dashboard link in the email <span class="muted">(your Tailscale Serve address, e.g. https://ai-pc.&lt;tailnet&gt;.ts.net:8443/ — blank uses ${esc(s.dashboard_link_auto || "this PC's address")})</span></label>
    <input id="set-dashboard-link" value="${esc(s.dashboard_link || "")}" placeholder="${esc(s.dashboard_link_auto || "")}">
    <div class="row" style="margin-top:8px"><button class="secondary small" onclick="sendSummaryNow(this)">Send a test now</button>
      ${s.summary_email_last_sent ? `<span class="muted small">Last sent ${esc(s.summary_email_last_sent.replace("T", " ").slice(0, 16))} UTC</span>` : ""}</div>
    ${s.summary_email_last_error ? `<p class="small" style="color:var(--red)">Last attempt failed: ${esc(s.summary_email_last_error)}</p>` : ""}
  </div>
  <div class="card">
    <h3>Home Assistant</h3>
    <p class="muted small">Sends the morning alert ("2 welcome messages, 1 review due") to your Home Assistant,
      which pushes it to your phone. Tapping it opens the dashboard (the Dashboard link above). Only on days with something to do.</p>
    <label>Home Assistant address <span class="muted">(as the AI PC reaches it, e.g. http://homeassistant.local:8123)</span></label>
    <input id="set-ha-url" value="${esc(s.ha_url || "")}" placeholder="http://homeassistant.local:8123">
    <details style="margin-top:8px"><summary class="small"><b>Set up the automation in Home Assistant</b> (once)</summary>
      <ol class="small">
        <li>In Home Assistant: <b>Settings → Automations &amp; scenes → Create automation → Create new automation</b>,
          then the ⋮ menu → <b>Edit in YAML</b>.</li>
        <li>Replace everything with the text below (<b>Copy</b> button), and change <code>mobile_app_YOUR_PHONE</code>
          to your phone — start typing <code>notify.mobile_app</code> in a new action to see its exact name.</li>
        <li>Save. Back here, click <b>Send a test to Home Assistant</b>.</li>
      </ol>
      <pre class="mono small" id="ha-yaml" style="white-space:pre-wrap">${esc(haAutomationYaml(s.ha_webhook_id))}</pre>
      <button class="ghost small" onclick="copyText($('#ha-yaml').textContent)">Copy</button>
      <p class="muted small">The webhook id in it works like a password for this alert — keep it to your Home Assistant.</p>
    </details>
    <div class="row" style="margin-top:8px"><button class="secondary small" onclick="sendHaTest(this)">Send a test to Home Assistant</button>
      ${s.ha_last_sent ? `<span class="muted small">Last sent ${esc(s.ha_last_sent.replace("T", " ").slice(0, 16))} UTC</span>` : ""}</div>
    ${s.ha_last_error ? `<p class="small" style="color:var(--red)">Last attempt failed: ${esc(s.ha_last_error)}</p>` : ""}
  </div>
  <div class="card">
    <h3>Weekend guide link</h3>
    <p class="muted small">Added to welcome messages for stays over a Friday, Saturday or Sunday night. Open it in a browser to check it works before saving.</p>
    <input id="set-guide-url" value="${esc(s.weekend_guide_url || "")}">
  </div>
  <div class="card">
    <h3>Airbnb browser</h3>
    <label><input type="checkbox" id="set-hide-browser" ${s.hide_browser ? "checked" : ""}>
      Keep the morning Airbnb check out of sight <span class="muted">(runs off-screen instead of popping up on the AI PC; if Airbnb logs you out, use Health → Open Airbnb login window)</span></label>
  </div>
  <div class="card">
    <h3>Welcome message timing</h3>
    <label>Draft each guest's welcome message
      <select id="set-welcome-lead" style="width:auto;display:inline-block">
        ${[[0, "on check-in day"], [1, "the day before check-in"], [2, "2 days before check-in"]].map(([v, t]) =>
          `<option value="${v}" ${s.welcome_days_before === v ? "selected" : ""}>${t}</option>`).join("")}
      </select></label>
    <p class="muted small">Guests arriving sooner than that (last-minute bookings, or a message you didn't get to) show up too.</p>
  </div>
  <div class="card">
    <h3>Automatic drafts</h3>
    <label><input type="checkbox" id="set-auto-reviews" ${s.auto_reviews_enabled ? "checked" : ""}>
      Draft guest reviews automatically the morning after checkout <span class="muted">(never posted — they wait on the stay page)</span></label>
    <label><input type="checkbox" id="set-midstay" ${s.midstay_enabled ? "checked" : ""}>
      Queue a mid-stay check-in message the day after arrival (stays of 2+ nights; nothing goes out until you send it)</label>
    <label>Mid-stay message <span class="muted">(merge fields: {{guest_first_name}}, {{unit_name}}, {{host_signature}})</span></label>
    <textarea id="set-midstay-template">${esc(s.midstay_template || "")}</textarea>
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
      anthropic_model: $("#set-model").value, queue_model: $("#set-queue-model").value,
      host_signature: $("#set-sig").value,
      default_checkin_time: $("#set-ci").value, default_checkout_time: $("#set-co").value,
      core_items: core, listing_aliases: aliases,
      hosting_ics_url: $("#set-hosting-ics").value.trim(),
      auto_reviews_enabled: $("#set-auto-reviews").checked,
      manual_mode: $("#set-manual-mode").checked,
      welcome_days_before: Number($("#set-welcome-lead").value),
      summary_email_enabled: $("#set-summary-on").checked,
      summary_email_to: $("#set-summary-to").value.trim(),
      dashboard_link: $("#set-dashboard-link").value.trim(),
      ha_url: $("#set-ha-url").value.trim(),
      weekend_guide_url: $("#set-guide-url").value.trim(),
      hide_browser: $("#set-hide-browser").checked,
      midstay_enabled: $("#set-midstay").checked,
      midstay_template: $("#set-midstay-template").value };
    const key = $("#set-key").value.trim();
    if (key) body.api_key = key;
    await api("/api/settings", { method: "PUT", body });
    manualMode = body.manual_mode;
    toast("Settings saved ✓"); renderSettings();
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function sendSummaryNow(btn) {
  busy(btn, true);
  try {
    await saveSettingsQuietly();
    const r = await api("/api/summary/send-now?to=email", { method: "POST" });
    toast(`Sent ✓ "${r.subject}"`);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

function haAutomationYaml(webhookId) {
  return `alias: Greeting Studio morning alert
description: Welcome messages / reviews waiting in Greeting Studio
triggers:
  - trigger: webhook
    webhook_id: ${webhookId}
    allowed_methods:
      - POST
    local_only: true
actions:
  - action: notify.mobile_app_YOUR_PHONE
    data:
      title: "{{ trigger.json.title }}"
      message: "{{ trigger.json.message }}"
      data:
        url: "{{ trigger.json.link }}"
        clickAction: "{{ trigger.json.link }}"
mode: single`;
}

async function sendHaTest(btn) {
  busy(btn, true);
  try {
    await api("/api/settings", { method: "PUT", body: {
      ha_url: $("#set-ha-url").value.trim(), dashboard_link: $("#set-dashboard-link").value.trim() } });
    const r = await api("/api/summary/send-now?to=ha", { method: "POST" });
    toast(`Sent to Home Assistant ✓ "${r.subject.replace("Greeting Studio: ", "")}" — check your phone`);
  } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
}

async function saveSettingsQuietly() {
  // so "Send a test now" uses the address just typed, without a separate Save click
  await api("/api/settings", { method: "PUT", body: {
    summary_email_enabled: $("#set-summary-on").checked,
    summary_email_to: $("#set-summary-to").value.trim(),
    dashboard_link: $("#set-dashboard-link").value.trim() } });
}

/* ---------------- boot ---------------- */
renderDashboard();
