// State: which Box IDs are selected, and metadata for selected files.
const selectedFolders = new Set();
const selectedFiles = new Map(); // id -> {id, name, size}

const treeEl = document.getElementById("tree");
const driveEl = document.getElementById("drive");
const destEl = document.getElementById("dest");
const workersEl = document.getElementById("workers");
const rateEl = document.getElementById("rate");
const goEl = document.getElementById("go");
const summaryEl = document.getElementById("summary");
const feedEl = document.getElementById("feed");
const countsEl = document.getElementById("counts");
const barFill = document.getElementById("barfill");
const bannerEl = document.getElementById("banner");

// ---------- Box tree ----------
async function fetchFolder(id) {
  const r = await fetch(`/api/box/folder?id=${encodeURIComponent(id)}`);
  const data = await r.json();
  if (!data.ok) throw new Error(data.error || "Failed to load folder");
  return data.items;
}

function makeNode(item) {
  const node = document.createElement("div");
  node.className = "node";

  const row = document.createElement("div");
  row.className = "row " + item.type;

  const twist = document.createElement("span");
  twist.className = "twist" + (item.type === "folder" ? "" : " leaf");
  twist.textContent = item.type === "folder" ? "▸" : "";

  const cb = document.createElement("input");
  cb.type = "checkbox";

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = item.name;

  const kind = document.createElement("span");
  kind.className = "kind";
  if (item.type === "folder") {
    const parts = [];
    if (typeof item.item_count === "number") {
      parts.push(item.item_count + (item.item_count === 1 ? " item" : " items"));
    }
    if (item.size != null && item.size > 0) parts.push(humanSize(item.size));
    kind.textContent = parts.length ? parts.join(" · ") : "folder";
  } else {
    kind.textContent = humanSize(item.size);
  }

  row.append(twist, cb, name, kind);
  node.appendChild(row);

  const children = document.createElement("div");
  children.className = "children";
  children.style.display = "none";
  node.appendChild(children);

  let loaded = false;
  async function toggle() {
    if (item.type !== "folder") return;
    const open = children.style.display === "none";
    children.style.display = open ? "block" : "none";
    twist.textContent = open ? "▾" : "▸";
    if (open && !loaded) {
      loaded = true;
      children.innerHTML = '<div class="loading">Loading…</div>';
      try {
        const items = await fetchFolder(item.id);
        children.innerHTML = "";
        if (items.length === 0) children.innerHTML = '<div class="loading">Empty</div>';
        items.forEach((it) => children.appendChild(makeNode(it)));
        // If parent is checked, cascade visual hint (selection is by-ID, folders copy recursively).
      } catch (e) {
        children.innerHTML = `<div class="loading">Error: ${e.message}</div>`;
      }
    }
  }
  twist.addEventListener("click", toggle);
  name.addEventListener("dblclick", toggle);

  cb.addEventListener("change", () => {
    if (item.type === "folder") {
      cb.checked ? selectedFolders.add(item.id) : selectedFolders.delete(item.id);
    } else {
      cb.checked
        ? selectedFiles.set(item.id, { id: item.id, name: item.name, size: item.size })
        : selectedFiles.delete(item.id);
    }
    refreshSummary();
  });

  return node;
}

async function loadRoot() {
  try {
    const items = await fetchFolder("0");
    treeEl.innerHTML = "";
    if (items.length === 0) treeEl.innerHTML = '<div class="loading">Box root is empty.</div>';
    items.forEach((it) => treeEl.appendChild(makeNode(it)));
  } catch (e) {
    treeEl.innerHTML = `<div class="loading">Could not load Box: ${e.message}</div>`;
  }
}

// ---------- Shared Drives ----------
async function loadDrives() {
  try {
    const r = await fetch("/api/drive/shared-drives");
    const data = await r.json();
    if (!data.ok) throw new Error(data.error);
    driveEl.innerHTML = "";
    if (data.drives.length === 0) {
      driveEl.innerHTML = '<option value="">No Shared Drives found</option>';
      return;
    }
    data.drives.forEach((d) => {
      const o = document.createElement("option");
      o.value = d.id;
      o.textContent = d.name;
      driveEl.appendChild(o);
    });
    updateGoState();
    buildDestTree();                 // populate destination tree for the first drive
  } catch (e) {
    driveEl.innerHTML = `<option value="">Error: ${e.message}</option>`;
  }
}

// ---------- Destination folder tree ----------
const destChosenEl = document.getElementById("dest-chosen");
const destTreeEl = document.getElementById("dest-tree");

function setDestination(id, label) {
  destEl.value = id || "";
  destChosenEl.textContent = label;
  // clear any prior .selected highlight
  destTreeEl.querySelectorAll(".dest-row.selected").forEach(r => r.classList.remove("selected"));
}

async function fetchDestFolders(parent) {
  const driveId = driveEl.value;
  const url = "/api/drive/folders?drive_id=" + encodeURIComponent(driveId) +
              (parent ? "&parent=" + encodeURIComponent(parent) : "");
  const r = await fetch(url);
  const d = await r.json();
  if (!d.ok) throw new Error(d.error || "Failed to load folders");
  return d.folders;
}

function makeDestNode(folder) {
  const node = document.createElement("div");
  const row = document.createElement("div");
  row.className = "dest-row";
  const twist = document.createElement("span");
  twist.className = "dest-twist";
  twist.textContent = "▸";
  const name = document.createElement("span");
  name.className = "dest-name";
  name.textContent = folder.name;
  row.append(twist, name);
  node.appendChild(row);

  const children = document.createElement("div");
  children.className = "dest-children";
  children.style.display = "none";
  node.appendChild(children);

  let loaded = false;
  twist.addEventListener("click", async (e) => {
    e.stopPropagation();
    const open = children.style.display === "none";
    children.style.display = open ? "block" : "none";
    twist.textContent = open ? "▾" : "▸";
    if (open && !loaded) {
      loaded = true;
      children.innerHTML = '<div class="dest-loading">Loading…</div>';
      try {
        const subs = await fetchDestFolders(folder.id);
        children.innerHTML = "";
        if (!subs.length) { twist.className = "dest-twist leaf"; }
        subs.forEach(s => children.appendChild(makeDestNode(s)));
      } catch (err) {
        children.innerHTML = `<div class="dest-loading">Error: ${err.message}</div>`;
      }
    }
  });
  // Clicking the name selects this folder as the destination.
  row.addEventListener("click", () => {
    setDestination(folder.id, folder.name);
    row.classList.add("selected");
  });
  return node;
}

async function buildDestTree() {
  setDestination("", "Drive root");
  destTreeEl.innerHTML = '<div class="dest-loading">Loading folders…</div>';
  // A "Drive root" row at the top so it's easy to pick the default.
  try {
    const folders = await fetchDestFolders(null);
    destTreeEl.innerHTML = "";
    const rootRow = document.createElement("div");
    rootRow.className = "dest-row selected";
    rootRow.innerHTML = '<span class="dest-twist leaf"></span><span class="dest-name">📁 Drive root (top level)</span>';
    rootRow.addEventListener("click", () => {
      setDestination("", "Drive root");
      rootRow.classList.add("selected");
    });
    destTreeEl.appendChild(rootRow);
    folders.forEach(f => destTreeEl.appendChild(makeDestNode(f)));
    if (!folders.length) {
      const none = document.createElement("div");
      none.className = "dest-loading";
      none.textContent = "No sub-folders — files go to Drive root.";
      destTreeEl.appendChild(none);
    }
  } catch (e) {
    destTreeEl.innerHTML = `<div class="dest-loading">Could not load folders: ${e.message}</div>`;
  }
}

// ---------- Selection summary ----------
function refreshSummary() {
  const nf = selectedFolders.size;
  const nfi = selectedFiles.size;
  if (nf === 0 && nfi === 0) {
    summaryEl.innerHTML = "Nothing selected yet. Tick folders or files on the left.";
  } else {
    const parts = [];
    if (nf) parts.push(`<b>${nf}</b> folder${nf > 1 ? "s" : ""} (copied recursively)`);
    if (nfi) parts.push(`<b>${nfi}</b> file${nfi > 1 ? "s" : ""}`);
    summaryEl.innerHTML = "Selected: " + parts.join(" and ") + ".";
  }
  updateGoState();
}

function updateGoState() {
  const hasSel = selectedFolders.size > 0 || selectedFiles.size > 0;
  const hasDrive = driveEl.value && driveEl.value.length > 0;
  goEl.disabled = !(hasSel && hasDrive);
}

driveEl.addEventListener("change", () => { updateGoState(); buildDestTree(); });

// ---------- Migration (background job + polling) ----------
let totalPending = 0;
let lastRenderedCursor = 0;
let pollTimer = null;
let currentJobId = null;
let autoResuming = false;   // guard so we don't launch overlapping resumes

goEl.addEventListener("click", async () => {
  goEl.disabled = true;
  feedEl.innerHTML = "";
  bannerEl.className = "banner";
  barFill.style.width = "0%";
  countsEl.textContent = "";
  totalPending = 0;
  lastRenderedCursor = 0;

  const body = {
    shared_drive_id: driveEl.value,
    dest_folder_id: destEl.value || null,
    folders: [...selectedFolders],
    files: [...selectedFiles.values()],
    workers: parseInt(workersEl.value, 10),
    rate: parseFloat(rateEl.value),
  };

  let start;
  try {
    const resp = await fetch("/api/migrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    start = await resp.json();
  } catch (e) {
    showBanner("error", "Could not start migration: " + e.message);
    goEl.disabled = false;
    return;
  }
  if (!start.ok) {
    showBanner("error", start.error || "Could not start migration.");
    goEl.disabled = false;
    return;
  }

  addLine("scan", "scan", "Migration started. Working…");
  pollProgress(start.job_id);
});

function pollProgress(jobId) {
  currentJobId = jobId;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    let j;
    try {
      const r = await fetch("/api/progress/" + jobId);
      j = await r.json();
    } catch (e) {
      return; // transient network blip; keep polling
    }
    if (!j.found) return;
    renderProgress(j);
    if (j.status === "done" || j.status === "error" ||
        j.status === "interrupted" || j.status === "needs_reconnect") {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 2000);
}

function renderProgress(j) {
  // Update counts and bar.
  if (j.pending) totalPending = j.pending;
  if (j.status === "running" || j.status === "done") {
    const pct = totalPending ? Math.round((j.done / totalPending) * 100) : 0;
    barFill.style.width = pct + "%";
    updateCounts(j.ok, j.fail, j.done);
  }
  // Append any new feed lines we haven't shown yet.
  if (Array.isArray(j.recent) && j.cursor > lastRenderedCursor) {
    const newCount = j.cursor - lastRenderedCursor;
    const fresh = j.recent.slice(-newCount);
    for (const line of fresh) {
      if (line.startsWith("ok:")) addLine("ok", "ok", line.slice(3).trim());
      else if (line.startsWith("FAIL:")) addLine("FAIL", "fail", line.slice(5).trim());
      else addLine("scan", "scan", line.replace(/^scan:\s*/, ""));
    }
    lastRenderedCursor = j.cursor;
  }
  // Terminal states.
  if (j.status === "done") {
    barFill.style.width = "100%";
    showBanner("done",
      `Finished. ${j.ok} copied, ${j.fail} failed, ${j.skipped} skipped.`);
    bannerEl.innerHTML +=
      ' <a href="/api/log" style="color:var(--accent);font-weight:600;">Download migration log (CSV)</a>';
    // If anything failed, list each failure with its reason so it's visible.
    if (j.fail > 0 && Array.isArray(j.failures) && j.failures.length) {
      const box = document.createElement("div");
      box.className = "failures";
      let html = `<div class="failures-head">${j.failures.length} file(s) could not be copied:</div>`;
      for (const f of j.failures) {
        html += `<div class="failrow"><span class="failpath">${escapeHtml(f.path)}</span>` +
                `<span class="failwhy">${escapeHtml(f.error || "unknown error")}</span></div>`;
      }
      box.innerHTML = html;
      feedEl.parentNode.insertBefore(box, feedEl.nextSibling);
    }
    goEl.disabled = false;
  } else if (j.status === "error") {
    showBanner("error", "Migration stopped: " + (j.error || "unknown error"));
    goEl.disabled = false;
  } else if (j.status === "interrupted") {
    // Auto-resume: the server restarted, so kick off a resume by ourselves
    // and keep going until the migration is truly complete. The checkpoint
    // means already-copied files are skipped, so this makes forward progress
    // each cycle without any clicking.
    if (!autoResuming) {
      showBanner("done",
        `Server restarted after ${typeof j.done === "number" ? j.done : "some"} file(s). ` +
        `Auto-resuming…`);
      autoResume();
    }
  } else if (j.status === "needs_reconnect") {
    showReconnect(j);
    goEl.disabled = false;
  }
}

// Automatically resume an interrupted job, retrying with a short back-off if
// the server is still coming back up. Continues until the job reports done,
// a real error, or a token expiry (which needs a manual reconnect).
async function autoResume() {
  autoResuming = true;
  let attempt = 0;
  const tryResume = async () => {
    attempt++;
    try {
      const r = await fetch("/api/resume/" + currentJobId, { method: "POST" });
      // If the server is mid-restart, the request may fail — retry shortly.
      if (!r.ok) { setTimeout(tryResume, 4000); return; }
      const d = await r.json();
      if (!d.ok) {
        // Couldn't resume (e.g. job details lost). Surface it rather than loop.
        autoResuming = false;
        showBanner("error", d.error || "Could not auto-resume — click Migrate to continue.");
        goEl.disabled = false;
        return;
      }
      autoResuming = false;   // resumed successfully; a later restart may set it again
      addLine("scan", "scan", `Auto-resuming (attempt ${attempt})… already-copied files skipped.`);
      pollProgress(currentJobId);
    } catch (e) {
      // Network blip while the instance restarts — wait and retry.
      setTimeout(tryResume, 4000);
    }
  };
  // Give the instance a moment to finish restarting before the first retry.
  setTimeout(tryResume, 3000);
}

// When the Box session expires mid-run, offer reconnect + resume without
// losing progress (the checkpoint skips already-copied files).
function showReconnect(j) {
  bannerEl.className = "banner show error";
  const doneNote = (typeof j.done === "number")
    ? ` ${j.done} file(s) already copied are saved.` : "";
  bannerEl.innerHTML =
    (j.error || "Box session expired.") + doneNote + "<br>";
  const reconnect = document.createElement("a");
  reconnect.className = "btn small";
  reconnect.style.marginTop = "8px";
  reconnect.textContent = "Reconnect Box";
  reconnect.href = "/oauth/box/start";
  reconnect.target = "_blank";           // reconnect in a new tab
  const resume = document.createElement("button");
  resume.className = "btn small";
  resume.style.margin = "8px 0 0 8px";
  resume.textContent = "Resume migration";
  resume.onclick = async () => {
    resume.disabled = true;
    try {
      const r = await fetch("/api/resume/" + currentJobId, { method: "POST" });
      const d = await r.json();
      if (!d.ok) { showBanner("error", d.error || "Could not resume."); return; }
      bannerEl.className = "banner";
      addLine("scan", "scan", "Resuming — already-copied files will be skipped…");
      pollProgress(currentJobId);
    } catch (e) {
      showBanner("error", "Resume failed: " + e.message);
    }
  };
  bannerEl.appendChild(reconnect);
  bannerEl.appendChild(resume);
}

function addLine(tag, tagClass, text, errText) {
  const placeholder = feedEl.querySelector(".empty");
  if (placeholder) placeholder.remove();
  const line = document.createElement("div");
  line.className = "line";
  line.innerHTML =
    `<span class="tag ${tagClass}">${tag}</span>` +
    `<span class="path">${escapeHtml(text)}</span>` +
    (errText ? `<span class="err">— ${escapeHtml(errText)}</span>` : "");
  feedEl.appendChild(line);
  feedEl.scrollTop = feedEl.scrollHeight;
}

function updateCounts(ok, fail, done) {  countsEl.innerHTML =
    `<span class="ok">${ok} ok</span> · ` +
    `<span class="fail">${fail} failed</span> · ${done}/${totalPending}`;
}

function showBanner(kind, msg) {
  bannerEl.className = "banner show " + kind;
  bannerEl.textContent = msg;
}

// ---------- utils ----------
function humanSize(bytes) {
  if (bytes == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- Connections / status ----------
const chipBox = document.getElementById("chip-box");
const chipGoogle = document.getElementById("chip-google");
const drawer = document.getElementById("drawer");
document.getElementById("toggle-creds").addEventListener("click", () =>
  drawer.classList.toggle("open"));

function renderChip(el, label, state) {
  el.className = "chip " + (state.ok ? "ok" : "bad");
  el.innerHTML =
    `<span class="dot"></span>${label}` +
    `<span class="detail">${escapeHtml(state.message || "")}</span>`;
}

function renderCard(name, ok) {
  const stateEl = document.getElementById("state-" + name);
  const btn = document.getElementById("btn-" + name);
  const dc = document.getElementById("dc-" + name);
  stateEl.textContent = ok ? "Connected" : "Not connected";
  stateEl.className = "cc-state" + (ok ? " on" : "");
  btn.style.display = ok ? "none" : "inline-flex";
  dc.style.display = ok ? "inline-block" : "none";
}

async function loadStatus() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();
    renderChip(chipBox, "Box", s.box);
    renderChip(chipGoogle, "Google Drive", s.google);
    renderCard("box", s.box.ok);
    renderCard("google", s.google.ok);
    document.getElementById("server-warn").style.display =
      s.server_configured ? "none" : "block";
    // Open the drawer until both are connected.
    if (!s.box.ok || !s.google.ok) drawer.classList.add("open");
    // Load data for whichever is connected.
    if (s.box.ok) loadRoot();
    if (s.google.ok) loadDrives();
    return s;
  } catch (e) { return null; }
}

async function disconnectProvider(name) {
  await fetch("/oauth/disconnect/" + name, { method: "POST" });
  if (name === "box") {
    treeEl.innerHTML = '<div class="loading">Box disconnected.</div>';
    selectedFolders.clear(); selectedFiles.clear(); refreshSummary();
  }
  if (name === "google") {
    driveEl.innerHTML = '<option value="">Connect Google Drive</option>';
    updateGoState();
  }
  loadStatus();
}
document.getElementById("dc-box").addEventListener("click", () => disconnectProvider("box"));
document.getElementById("dc-google").addEventListener("click", () => disconnectProvider("google"));

// ---------- Verify migration (compare Box vs Drive) ----------
const compareDrawer = document.getElementById("compare-drawer");
document.getElementById("toggle-compare").addEventListener("click", () =>
  compareDrawer.classList.toggle("open"));

document.getElementById("run-compare").addEventListener("click", async () => {
  const out = document.getElementById("compare-result");
  // Exactly one Box folder must be ticked.
  if (selectedFolders.size !== 1) {
    out.innerHTML = '<span style="color:var(--warn)">Tick exactly one Box folder on the left to compare.</span>';
    return;
  }
  if (!driveEl.value) {
    out.innerHTML = '<span style="color:var(--warn)">Pick a Shared Drive first.</span>';
    return;
  }
  const boxFolderId = [...selectedFolders][0];
  const recursive = document.getElementById("compare-recursive").checked;
  out.innerHTML = "Comparing… (this can take a moment for large folders)";
  try {
    const r = await fetch("/api/compare", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        box_folder_id: boxFolderId,
        drive_id: driveEl.value,
        drive_folder_id: destEl.value || driveEl.value,
        recursive,
      }),
    });
    const data = await r.json();
    if (!data.ok) { out.innerHTML = `<span style="color:var(--warn)">${escapeHtml(data.error||"Compare failed")}</span>`; return; }
    renderCompare(out, data);
  } catch (e) {
    out.innerHTML = `<span style="color:var(--warn)">Compare failed: ${escapeHtml(e.message)}</span>`;
  }
});

function renderCompare(out, data) {
  const s = data.summary, d = data.detail;
  const ok = s.missing_in_drive === 0 && s.missing_folders === 0;
  let html = `<div style="font-weight:600;color:${ok?'var(--accent)':'var(--warn)'};margin-bottom:8px;">`
    + (ok ? "✓ All Box files found in Google Drive" : "⚠ Some items are missing in Google Drive")
    + `</div>`;
  html += `<div style="font-family:'IBM Plex Mono',monospace;font-size:12px;margin-bottom:10px;">`
    + `${s.matched} matched · ${s.missing_in_drive} missing in Drive · `
    + `${s.missing_folders} missing folders · ${s.extra_in_drive} extra in Drive</div>`;
  const section = (title, items, color) => {
    if (!items.length) return "";
    let h = `<div style="margin-top:8px;font-weight:600;color:${color}">${title} (${items.length})</div>`;
    h += '<div style="max-height:160px;overflow:auto;font-family:\'IBM Plex Mono\',monospace;font-size:11px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;margin-top:4px;">';
    h += items.map(i => escapeHtml(i.path)).join("<br>");
    h += "</div>";
    return h;
  };
  html += section("Missing in Google Drive", d.missing_in_drive, "var(--warn)");
  html += section("Missing folders", d.missing_folders, "var(--warn)");
  html += section("Extra in Google Drive (not in Box)", d.extra_in_drive, "var(--muted)");
  out.innerHTML = html;
}

document.getElementById("export-manifest").addEventListener("click", () => {
  const out = document.getElementById("compare-result");
  if (selectedFolders.size !== 1) {
    out.innerHTML = '<span style="color:var(--warn)">Tick exactly one Box folder on the left to export its file list.</span>';
    return;
  }
  const boxFolderId = [...selectedFolders][0];
  out.innerHTML = "Building manifest… (large folders take a moment; the download will start automatically)";
  // Navigate to the download endpoint; browser handles the file save.
  window.location.href = "/api/box/manifest?id=" + encodeURIComponent(boxFolderId);
  setTimeout(() => { out.innerHTML = "If the download didn't start, the folder may be very large — give it a moment and try again."; }, 8000);
});

// ---------- boot ----------
loadStatus();