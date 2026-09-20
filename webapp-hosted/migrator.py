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
def list_box_folder(box, folder_id):
    """Return immediate children of a Box folder as a list of dicts."""
    items = box.folder(folder_id).get_items(limit=1000, fields=["id", "name", "type", "size"])
    out = []
    for item in items:
        out.append({
            "id": item.id,
            "name": item.name,
            "type": item.type,  # 'folder' or 'file'
            "size": getattr(item, "size", None),
        })
    out.sort(key=lambda x: (x["type"] != "folder", x["name"].lower()))
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
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"done_files": sorted(self.done_files),
                       "folder_map": self.folder_map}, f)
        os.replace(tmp, self.path)


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
        for item in list_box_folder(box, box_folder_id):
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
    the common failure where a Box-native Google file has no downloadable
    content (the API returns 404 on /content)."""
    try:
        box_c.file(fid).download_to(buf)
        return
    except Exception as e:  # noqa: BLE001
        msg = str(e)
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
