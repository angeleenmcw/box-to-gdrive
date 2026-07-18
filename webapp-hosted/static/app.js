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
  kind.textContent = item.type === "folder" ? "folder" : humanSize(item.size);

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
  } catch (e) {
    driveEl.innerHTML = `<option value="">Error: ${e.message}</option>`;
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

driveEl.addEventListener("change", updateGoState);

// ---------- Migration (SSE) ----------
goEl.addEventListener("click", async () => {
  goEl.disabled = true;
  feedEl.innerHTML = "";
  bannerEl.className = "banner";
  barFill.style.width = "0%";
  countsEl.textContent = "";

  const body = {
    shared_drive_id: driveEl.value,
    dest_folder_id: destEl.value || null,
    folders: [...selectedFolders],
    files: [...selectedFiles.values()],
    workers: parseInt(workersEl.value, 10),
    rate: parseFloat(rateEl.value),
  };

  const resp = await fetch("/api/migrate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 2);
      if (chunk.startsWith("data:")) handleEvent(JSON.parse(chunk.slice(5).trim()));
    }
  }
});

function addLine(tag, tagClass, text, errText) {
  // Clear the placeholder on first real line.
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

let totalPending = 0;

function handleEvent(evt) {
  switch (evt.type) {
    case "scanning":
      addLine("scan", "scan", "Scanning Box selection…");
      break;
    case "scan":
      addLine("scan", "scan", evt.path);
      break;
    case "start":
      totalPending = evt.pending;
      addLine("info", "scan",
        `${evt.total} files found · ${evt.skipped} already done · ${evt.pending} to copy`);
      updateCounts(0, 0, 0);
      break;
    case "file": {
      const tag = evt.ok ? "ok" : "FAIL";
      addLine(tag, evt.ok ? "ok" : "fail", evt.path, evt.ok ? null : evt.error);
      const pct = totalPending ? Math.round((evt.done / totalPending) * 100) : 100;
      barFill.style.width = pct + "%";
      updateCounts(evt.ok_count, evt.fail_count, evt.done);
      break;
    }
    case "done":
      barFill.style.width = "100%";
      showBanner("done",
        `Finished. ${evt.ok} copied, ${evt.fail} failed, ${evt.skipped} skipped.`);
      goEl.disabled = false;
      break;
    case "fatal":
      showBanner("error", "Migration stopped: " + evt.error);
      goEl.disabled = false;
      break;
  }
}

function updateCounts(ok, fail, done) {
  countsEl.innerHTML =
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

// ---------- boot ----------
loadStatus();
