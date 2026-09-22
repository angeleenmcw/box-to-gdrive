"""
Core Box -> Google Shared Drive migration logic.

Shared by both the CLI (box_to_gdrive.py) and the web UI (app.py).
Handles authentication, folder-tree browsing, and file transfer with
checkpointing, parallel uploads, exponential backoff, and CSV logging.
"""

import csv
import io
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from boxsdk import Client, JWTAuth, OAuth2

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"

_RETRYABLE_403_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "sharingRateLimitExceeded",
    "dailyLimitExceeded",
}


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def get_box_client(config_path=None, use_dev_token=False):
    if use_dev_token:
        token = os.environ.get("BOX_DEV_TOKEN")
        if not token:
            raise RuntimeError("BOX_DEV_TOKEN env var not set.")
        auth = OAuth2(client_id="", client_secret="", access_token=token)
        return Client(auth)
    if not config_path or not os.path.exists(config_path):
        raise RuntimeError("Box JWT config file not found.")
    auth = JWTAuth.from_settings_file(config_path)
    return Client(auth)


def get_gdrive_service(creds_path="google_credentials.json", token_path="token.json"):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GDRIVE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(creds_path):
                raise RuntimeError(f"Google credentials not found at {creds_path}.")
            raise RuntimeError('Interactive Google sign-in is not available in the hosted app; use the OAuth flow.')
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def build_gdrive_service_from_token(token_path="token.json"):
    """Per-thread Drive service (googleapiclient http objects aren't thread-safe)."""
    creds = Credentials.from_authorized_user_file(token_path, GDRIVE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


# --------------------------------------------------------------------------- #
# OAuth-based clients (per-user, for the hosted multi-user app)
#
# These build clients from tokens obtained via the browser OAuth flow, so the
# server never stores users' private keys — only short-lived access/refresh
# tokens held in each user's session.
# --------------------------------------------------------------------------- #
def box_client_from_token(access_token, refresh_token=None,
                          client_id=None, client_secret=None, on_refresh=None):
    """Build a Box client from an OAuth access token. If a refresh token and
    app client_id/secret are supplied, the SDK will refresh automatically and
    call `on_refresh(access, refresh)` so the caller can persist new tokens."""
    auth = OAuth2(
        client_id=client_id or "",
        client_secret=client_secret or "",
        access_token=access_token,
        refresh_token=refresh_token,
        store_tokens=on_refresh,
    )
    return Client(auth)


def gdrive_creds_from_token(token_info, client_id, client_secret):
    """Build google Credentials from stored OAuth token info (a dict with
    access_token / refresh_token). Refreshes in place if expired."""
    creds = Credentials(
        token=token_info.get("access_token"),
        refresh_token=token_info.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=GDRIVE_SCOPES,
    )
    if (not creds.valid) and creds.refresh_token:
        creds.refresh(Request())
    return creds


def gdrive_service_from_creds(creds):
    return build("drive", "v3", credentials=creds)


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #
def _is_retryable(error):
    status = getattr(error.resp, "status", None)
    if status in (429, 500, 502, 503, 504):
        return True
    if status == 403:
        try:
            reasons = json.loads(error.content.decode())["error"]["errors"]
            return any(r.get("reason") in _RETRYABLE_403_REASONS for r in reasons)
        except (ValueError, KeyError, AttributeError):
            return True
    return False


def with_backoff(func, *args, max_retries=7, base=1.0, limiter=None, **kwargs):
    attempt = 0
    while True:
        if limiter is not None:
            limiter.acquire()
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            if not _is_retryable(e) or attempt >= max_retries:
                raise
            time.sleep(base * (2 ** attempt) + random.uniform(0, 1))
            attempt += 1


# --------------------------------------------------------------------------- #
# Token-bucket rate limiter
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Thread-safe token bucket for capping the combined rate of Drive API
    write calls across all workers.

    Google Drive's per-user write quota is roughly 12,000 requests/minute
    (~200/sec). Set `rate` comfortably under that. `burst` lets a short spike
    through without blocking; tokens refill continuously at `rate` per second.

    A single shared instance is passed to every worker, so raising --workers
    no longer raises the request rate past this ceiling — extra workers simply
    wait for tokens instead of hammering the API.
    """

    def __init__(self, rate, burst=None):
        self.rate = float(rate)                       # tokens added per second
        self.capacity = float(burst if burst is not None else rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, tokens=1):
        """Block until `tokens` are available, then consume them."""
        while True:
            with self.lock:
                now = time.monotonic()
                # Refill based on elapsed time.
                self.tokens = min(
                    self.capacity,
                    self.tokens + (now - self.updated) * self.rate,
                )
                self.updated = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                # How long until enough tokens accrue?
                deficit = tokens - self.tokens
                wait = deficit / self.rate
            time.sleep(wait)


# --------------------------------------------------------------------------- #
# Browsing (for the UI)
# --------------------------------------------------------------------------- #
def _folder_item_count(box, folder_id):
    """Fetch a single folder's direct-child count. Box only returns
    item_collection.total_count when a folder is fetched by its own id, not in
    a bulk listing — hence this per-folder call."""
    try:
        f = box.folder(folder_id).get(fields=["item_collection"])
        ic = getattr(f, "item_collection", None)
        if isinstance(ic, dict):
            return ic.get("total_count")
        return getattr(ic, "total_count", None)
    except Exception:  # noqa: BLE001
        return None  # count is best-effort; never break the listing


def _iter_box_folder_basic(box, folder_id):
    """Yield immediate children of a Box folder — id, name, type, size only.
    A generator, so memory stays flat while walking huge folders."""
    items = box.folder(folder_id).get_items(
        limit=1000,
        fields=["id", "name", "type", "size"],
    )
    for item in items:
        yield {"id": item.id, "name": item.name, "type": item.type,
               "size": getattr(item, "size", None)}


def list_box_folder_basic(box, folder_id):
    """List (not generate) a folder's children — id, name, type, size only.
    No per-folder count calls, so it's cheap for the migration scan."""
    out = list(_iter_box_folder_basic(box, folder_id))
    out.sort(key=lambda x: (x["type"] != "folder", x["name"].lower()))
    return out


def list_box_folder(box, folder_id):
    """Browsing view: children plus an accurate `item_count` per sub-folder
    (an extra per-folder Box call, run in parallel). Used by the tree UI, NOT
    by the migration scan."""
    out = list_box_folder_basic(box, folder_id)

    # Fetch accurate child-counts for the subfolders, in parallel.
    folder_ids = [e["id"] for e in out if e["type"] == "folder"]
    if folder_ids:
        counts = {}
        with ThreadPoolExecutor(max_workers=min(8, len(folder_ids))) as pool:
            future_to_id = {pool.submit(_folder_item_count, box, fid): fid
                            for fid in folder_ids}
            for fut in as_completed(future_to_id):
                counts[future_to_id[fut]] = fut.result()
        for e in out:
            if e["type"] == "folder":
                e["item_count"] = counts.get(e["id"])

    return out


def list_shared_drives(drive):
    """Return the Shared Drives the authenticated user can access."""
    out = []
    page_token = None
    while True:
        resp = drive.drives().list(pageSize=100, pageToken=page_token,
                                   fields="nextPageToken, drives(id, name)").execute()
        out.extend(resp.get("drives", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def list_drive_folders(drive, shared_drive_id, parent_id):
    """Return the sub-folders directly under parent_id within a Shared Drive,
    so the UI can browse the destination tree. parent_id may be the Shared
    Drive id itself (its root) or any folder id inside it."""
    out = []
    page_token = None
    query = (f"'{parent_id}' in parents and "
             f"mimeType = '{GOOGLE_FOLDER_MIME}' and trashed = false")
    while True:
        resp = drive.files().list(
            q=query,
            corpora="drive",
            driveId=shared_drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=200,
            pageToken=page_token,
            orderBy="name",
            fields="nextPageToken, files(id, name)",
        ).execute()
        for f in resp.get("files", []):
            out.append({"id": f["id"], "name": f["name"]})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


# --------------------------------------------------------------------------- #
# Google Drive write helpers
# --------------------------------------------------------------------------- #
def find_or_create_folder(drive, name, parent_id, shared_drive_id, limiter=None):
    safe_name = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and mimeType = '{GOOGLE_FOLDER_MIME}' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = with_backoff(
        drive.files().list(
            q=query, corpora="drive", driveId=shared_drive_id,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            fields="files(id, name)",
        ).execute,
        limiter=limiter,
    )
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {"name": name, "mimeType": GOOGLE_FOLDER_MIME, "parents": [parent_id]}
    folder = with_backoff(
        drive.files().create(body=metadata, supportsAllDrives=True, fields="id").execute,
        limiter=limiter,
    )
    return folder["id"]


# Office extension -> (source mimetype, target Google mimetype).
# When an entry matches, Drive converts the file to the native Google format
# on upload. Only presentations are enabled by default (per requirements);
# the docx/xlsx rows are here and commented so they're easy to turn on later.
_CONVERT_MAP = {
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
              "application/vnd.google-apps.presentation"),
    ".ppt":  ("application/vnd.ms-powerpoint",
              "application/vnd.google-apps.presentation"),
    # Box stores some Google Slides as .gslide/.gslides but serves real
    # PowerPoint bytes on download, so treat them as .pptx for conversion.
    ".gslides": ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
                 "application/vnd.google-apps.presentation"),
    ".gslide":  ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
                 "application/vnd.google-apps.presentation"),
    # ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    #           "application/vnd.google-apps.document"),
    # ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    #           "application/vnd.google-apps.spreadsheet"),
}


def _conversion_for(name):
    """Return (source_mime, google_mime) if this filename should be converted
    to a native Google format on upload, else None."""
    lower = name.lower()
    for ext, pair in _CONVERT_MAP.items():
        if lower.endswith(ext):
            return pair
    return None


def upload_stream(drive, stream, name, parent_id, limiter=None, convert=True):
    conv = _conversion_for(name) if convert else None
    if conv:
        source_mime, google_mime = conv
        media = MediaIoBaseUpload(stream, mimetype=source_mime, resumable=True)
        # Drop the extension from the name so the Google file isn't "deck.pptx".
        display_name = name.rsplit(".", 1)[0]
        metadata = {"name": display_name, "parents": [parent_id],
                    "mimeType": google_mime}
    else:
        media = MediaIoBaseUpload(stream, mimetype="application/octet-stream",
                                  resumable=True)
        metadata = {"name": name, "parents": [parent_id]}
    request = drive.files().create(
        body=metadata, media_body=media, supportsAllDrives=True, fields="id"
    )
    response = None
    attempt = 0
    while response is None:
        if limiter is not None:
            limiter.acquire()
        try:
            _, response = request.next_chunk()
            attempt = 0
        except HttpError as e:
            if not _is_retryable(e) or attempt >= 7:
                raise
            time.sleep(2 ** attempt + random.uniform(0, 1))
            attempt += 1
    return response["id"], bool(conv)


# --------------------------------------------------------------------------- #
# Checkpoint & log
# --------------------------------------------------------------------------- #
class Checkpoint:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.done_files = set()
        self.folder_map = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self.done_files = set(data.get("done_files", []))
                self.folder_map = data.get("folder_map", {})
            except (json.JSONDecodeError, OSError):
                pass

    def is_done(self, fid):
        return fid in self.done_files

    def mark_done(self, fid):
        with self.lock:
            self.done_files.add(fid)
            self._flush()

    def get_folder(self, fid):
        with self.lock:
            return self.folder_map.get(fid)

    def set_folder(self, fid, gid):
        with self.lock:
            self.folder_map[fid] = gid
            self._flush()

    def _flush(self):
        # Use a unique temp file per write so concurrent or restart-interrupted
        # flushes can't clobber each other's temp file (which caused
        # "No such file or directory: ...ckpt.json.tmp" and a lost checkpoint,
        # making resumes re-copy everything). Also fsync so the data is really
        # on disk before the rename.
        import tempfile
        directory = os.path.dirname(self.path) or "."
        data = {"done_files": sorted(self.done_files),
                "folder_map": self.folder_map}
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ckpt_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                # If replace succeeded the tmp is gone; if it failed, clean up.
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        except OSError:
            # Never let a checkpoint write crash the migration; a missed flush
            # just means a few files might be re-checked on resume, which the
            # per-run dedup and is_done guard handle safely.
            pass


class TransferLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        new = not os.path.exists(path)
        self.fh = open(path, "a", newline="")
        self.writer = csv.writer(self.fh)
        if new:
            self.writer.writerow(["timestamp", "status", "path",
                                  "box_file_id", "box_url",
                                  "gdrive_file_id", "gdrive_url",
                                  "size_bytes", "error"])
            self.fh.flush()

    def record(self, status, fid, path, size="", gid="", error="",
               converted_slides=False):
        box_url = f"https://app.box.com/file/{fid}" if fid else ""
        if gid:
            if converted_slides:
                gdrive_url = f"https://docs.google.com/presentation/d/{gid}/edit"
            else:
                gdrive_url = f"https://drive.google.com/file/d/{gid}/view"
        else:
            gdrive_url = ""
        with self.lock:
            self.writer.writerow([datetime.now(timezone.utc).isoformat(), status,
                                  path, fid, box_url, gid, gdrive_url, size, error])
            self.fh.flush()

    def close(self):
        self.fh.close()


# --------------------------------------------------------------------------- #
# Selection-driven migration (used by the UI)
# --------------------------------------------------------------------------- #
def expand_selection(box, selected_folders, selected_files, dest_parent_id,
                     shared_drive_id, drive, ckpt, progress, limiter=None):
    """
    Turn a UI selection into a flat list of file transfer tasks, creating
    destination folders as needed.

    selected_folders: list of Box folder IDs to copy recursively
    selected_files:   list of {"id", "name", "parent_dest_id"} for standalone files
                      (parent_dest_id is resolved by the caller to dest_parent_id)
    """
    tasks = []

    def walk(box_folder_id, gdrive_parent_id, rel_path):
        for item in _iter_box_folder_basic(box, box_folder_id):
            item_path = f"{rel_path}/{item['name']}" if rel_path else item["name"]
            if item["type"] == "folder":
                cached = ckpt.get_folder(item["id"])
                if cached:
                    new_parent = cached
                else:
                    new_parent = find_or_create_folder(
                        drive, item["name"], gdrive_parent_id, shared_drive_id,
                        limiter=limiter)
                    ckpt.set_folder(item["id"], new_parent)
                progress({"type": "scan", "path": item_path})
                walk(item["id"], new_parent, item_path)
            else:
                tasks.append({"box_file_id": item["id"], "name": item["name"],
                              "path": item_path, "size": item.get("size", ""),
                              "parent_id": gdrive_parent_id})

    # Recurse selected folders (each becomes a top-level folder in the destination).
    for fid in selected_folders:
        info = box.folder(fid).get(fields=["name"])
        name = info.name
        cached = ckpt.get_folder(fid)
        if cached:
            top = cached
        else:
            top = find_or_create_folder(drive, name, dest_parent_id,
                                        shared_drive_id, limiter=limiter)
            ckpt.set_folder(fid, top)
        progress({"type": "scan", "path": name})
        walk(fid, top, name)

    # Standalone selected files go directly into the destination parent.
    for f in selected_files:
        tasks.append({"box_file_id": f["id"], "name": f["name"],
                      "path": f["name"], "size": f.get("size", ""),
                      "parent_id": dest_parent_id})

    return tasks


def _download_box_file(box_c, fid, buf):
    """Download a Box file's content into buf. Raises a descriptive error for
    the common failures: an expired Box session (401), or a Box-native Google
    file that has no downloadable content (404)."""
    try:
        box_c.file(fid).download_to(buf)
        return
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if ("invalid_token" in msg or "401" in msg
                or "expired" in msg.lower() or "invalid_grant" in msg):
            raise RuntimeError(
                "Box session expired mid-transfer. Disconnect and reconnect Box, "
                "then re-run — already-copied files will be skipped."
            ) from e
        # Box returns 404 on /content for Google-format files that have no
        # stored binary (Docs/Sheets/Slides created via Box's Google editor).
        if "404" in msg or "not_found" in msg.lower():
            raise RuntimeError(
                "File has no downloadable content in Box (likely a Google-format "
                "file created in Box's Google editor, which the Box API cannot "
                "export). Open it in Box and use 'Download as' to save an Office "
                "copy, or recreate it directly in Google Drive."
            ) from e
        raise


def transfer_one(box, token_path, task, ckpt, log, limiter=None,
                 drive_factory=None, box_factory=None):
    """Download a file from Box and upload it to Drive.

    Token sources are pluggable:
      - drive_factory(): returns a fresh Drive service (per-thread). Falls back
        to build_gdrive_service_from_token(token_path) when not given.
      - box_factory(): returns a fresh Box client (per-thread). Falls back to
        the shared `box` client when not given.
    """
    fid = task["box_file_id"]
    try:
        drive = drive_factory() if drive_factory else build_gdrive_service_from_token(token_path)
        box_c = box_factory() if box_factory else box
        buf = io.BytesIO()
        _download_box_file(box_c, fid, buf)
        buf.seek(0)
        gid, converted = upload_stream(drive, buf, task["name"], task["parent_id"],
                                       limiter=limiter)
        ckpt.mark_done(fid)
        log.record("ok", fid, task["path"], task["size"], gid,
                   converted_slides=converted)
        return (True, task["path"], None)
    except Exception as e:  # noqa: BLE001
        log.record("error", fid, task["path"], task["size"], error=str(e))
        return (False, task["path"], str(e))


def run_migration(box, token_path, tasks, ckpt, log, workers, progress,
                  limiter=None, drive_factory=None, box_factory=None):
    """Execute transfers in parallel, emitting progress events via `progress`.

    Pass a shared RateLimiter as `limiter` to cap the combined Drive request
    rate regardless of worker count. `drive_factory`/`box_factory` let the
    hosted OAuth app supply per-user, per-thread clients built from session
    tokens instead of on-disk credential files."""
    pending = [t for t in tasks if not ckpt.is_done(t["box_file_id"])]
    skipped = len(tasks) - len(pending)
    progress({"type": "start", "total": len(tasks),
              "pending": len(pending), "skipped": skipped})

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(transfer_one, box, token_path, t, ckpt, log,
                               limiter, drive_factory, box_factory): t
                   for t in pending}
        done = 0
        for fut in as_completed(futures):
            success, path, err = fut.result()
            done += 1
            if success:
                ok += 1
            else:
                fail += 1
            progress({"type": "file", "path": path, "ok": success,
                      "error": err, "done": done, "pending": len(pending),
                      "ok_count": ok, "fail_count": fail})

    progress({"type": "done", "ok": ok, "fail": fail, "skipped": skipped})
    return ok, fail


def stream_migration(box_factory, drive_factory, selected_folders, selected_files,
                     dest_parent_id, shared_drive_id, ckpt, log, workers, progress,
                     limiter=None):
    """Walk the Box tree and transfer files AS THEY ARE DISCOVERED.

    The walk does NOT create Google Drive folders — it only reads Box (one cheap
    call per folder), so scanning a huge, deep tree stays fast and light. Each
    file task carries the chain of (box_folder_id, name) from the selected root
    down to its parent; the destination folder path is created lazily, once per
    folder, the first time a file actually needs it. This keeps the scan from
    stalling on thousands of synchronous Drive folder-creates.
    """
    import queue as _queue

    scan_box = box_factory()

    task_q = _queue.Queue(maxsize=max(workers * 4, 16))
    counters = {"found": 0, "done": 0, "ok": 0, "fail": 0, "skipped": 0}
    clock = threading.Lock()
    DONE = object()

    # Guard against copying the same Box file twice in one run. This happens
    # when a user selects a folder AND a file (or subfolder) inside it: the
    # folder walk reaches the file, and the standalone selection also queues it.
    # We enqueue each Box file id at most once per run.
    queued_ids = set()
    queued_lock = threading.Lock()

    def claim(box_file_id):
        """Return True if this file id hasn't been queued yet this run (and
        mark it), False if it's a duplicate to skip."""
        with queued_lock:
            if box_file_id in queued_ids:
                return False
            queued_ids.add(box_file_id)
            return True

    # Lazy destination-folder resolver, shared across workers. Maps a Box folder
    # id to its created Google Drive folder id; creates the whole ancestor chain
    # on demand, each folder at most once.
    folder_lock = threading.Lock()

    def resolve_dest(drive, chain):
        """chain: list of (box_folder_id, name) from the selected root down to
        the file's immediate parent. Returns the Drive folder id to upload into,
        creating any missing folders along the way. dest_parent_id is the base."""
        parent_gid = dest_parent_id
        for box_fid, name in chain:
            cached = ckpt.get_folder(box_fid)
            if cached:
                parent_gid = cached
                continue
            # Serialize creation so two workers don't double-create the same folder.
            with folder_lock:
                cached = ckpt.get_folder(box_fid)   # re-check inside the lock
                if cached:
                    parent_gid = cached
                    continue
                gid = find_or_create_folder(drive, name, parent_gid,
                                            shared_drive_id, limiter=limiter)
                ckpt.set_folder(box_fid, gid)
                parent_gid = gid
        return parent_gid

    def emit_counts(path=None, success=None, error=None):
        with clock:
            payload = {"type": "file", "path": path, "ok": success,
                       "error": error, "done": counters["done"],
                       "pending": counters["found"],
                       "ok_count": counters["ok"], "fail_count": counters["fail"]}
        progress(payload)

    # --- producer: walk Box only; attach the folder chain to each file task ---
    def walker():
        def walk(box_folder_id, chain, rel_path):
            for item in _iter_box_folder_basic(scan_box, box_folder_id):
                item_path = f"{rel_path}/{item['name']}" if rel_path else item["name"]
                if item["type"] == "folder":
                    # Just recurse — no Drive call here.
                    progress({"type": "scan", "path": item_path})
                    walk(item["id"], chain + [(item["id"], item["name"])], item_path)
                else:
                    if not claim(item["id"]):
                        continue          # already queued via another selection
                    task = {"box_file_id": item["id"], "name": item["name"],
                            "path": item_path, "size": item.get("size", ""),
                            "chain": chain}
                    with clock:
                        counters["found"] += 1
                    task_q.put(task)

        try:
            for fid in selected_folders:
                info = scan_box.folder(fid).get(fields=["name"])
                name = info.name
                progress({"type": "scan", "path": name})
                walk(fid, [(fid, name)], name)
            for f in selected_files:
                if not claim(f["id"]):
                    continue              # file already covered by a selected folder
                task = {"box_file_id": f["id"], "name": f["name"],
                        "path": f["name"], "size": f.get("size", ""),
                        "chain": []}   # empty chain = straight into dest root
                with clock:
                    counters["found"] += 1
                task_q.put(task)
        finally:
            for _ in range(workers):
                task_q.put(DONE)

    # --- consumers: resolve dest folder lazily, then transfer ---
    def consumer():
        drive = drive_factory()
        while True:
            task = task_q.get()
            if task is DONE:
                task_q.task_done()
                return
            if ckpt.is_done(task["box_file_id"]):
                with clock:
                    counters["skipped"] += 1
                task_q.task_done()
                continue
            try:
                parent_id = resolve_dest(drive, task["chain"])
                task["parent_id"] = parent_id
                success, path, err = transfer_one(
                    None, None, task, ckpt, log, limiter, drive_factory, box_factory)
            except Exception as e:  # noqa: BLE001
                success, path, err = False, task["path"], str(e)
                log.record("error", task["box_file_id"], task["path"],
                           task.get("size", ""), error=str(e))
            with clock:
                counters["done"] += 1
                if success:
                    counters["ok"] += 1
                else:
                    counters["fail"] += 1
            emit_counts(path=path, success=success, error=err)
            task_q.task_done()

    progress({"type": "start", "total": 0, "pending": 0, "skipped": 0})

    walk_thread = threading.Thread(target=walker, daemon=True)
    walk_thread.start()

    consumers = [threading.Thread(target=consumer, daemon=True)
                 for _ in range(workers)]
    for t in consumers:
        t.start()

    walk_thread.join()
    for t in consumers:
        t.join()

    progress({"type": "done", "ok": counters["ok"], "fail": counters["fail"],
              "skipped": counters["skipped"]})
    return counters["ok"], counters["fail"]
