#!/usr/bin/env python3
"""
Box.com -> Google Shared Drive migration tool.

Exports files/folders from Box and uploads them into a Google Drive Shared Drive,
preserving the folder hierarchy. Supports checkpointing (resume), parallel
uploads, and a CSV transfer log.

SETUP
-----
1. Install dependencies:
     pip install boxsdk google-api-python-client google-auth google-auth-oauthlib

2. Box credentials (JWT app is simplest for server-to-server):
     - Create a Box app at https://app.box.com/developers/console
     - Choose "Custom App" -> "Server Authentication (JWT)"
     - Download the config JSON, save as box_config.json
     - In Box Admin Console, authorize the app (Apps > Custom Apps Manager)

   OR use a developer token for quick testing:
     export BOX_DEV_TOKEN="your_token"

3. Google credentials:
     - Create an OAuth client (Desktop) in Google Cloud Console with the
       Google Drive API enabled. Download as google_credentials.json
     - First run opens a browser to authorize; a token is cached to token.json.

USAGE
-----
  # Migrate a Box folder into a Shared Drive root, 8 parallel workers
  python box_to_gdrive.py \
      --box-folder 0 \
      --shared-drive-id 0AABBccDDeeFFgg \
      --box-config box_config.json \
      --workers 8

  # Resume an interrupted run (uses checkpoint.json automatically)
  python box_to_gdrive.py --box-folder 0 --shared-drive-id 0AABBcc... --box-config box_config.json

  # Start fresh, ignoring any existing checkpoint
  python box_to_gdrive.py ... --reset

  # Dry run
  python box_to_gdrive.py ... --dry-run
"""

import argparse
import csv
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ---- Box ----
from boxsdk import Client, JWTAuth, OAuth2

# ---- Google ----
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


# --------------------------------------------------------------------------- #
# Checkpoint  (records which Box file IDs are already done, + folder ID map)
# --------------------------------------------------------------------------- #
class Checkpoint:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.done_files = set()          # Box file IDs successfully uploaded
        self.folder_map = {}             # Box folder ID -> Google Drive folder ID
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self.done_files = set(data.get("done_files", []))
                self.folder_map = data.get("folder_map", {})
                print(f"Loaded checkpoint: {len(self.done_files)} files already done.")
            except (json.JSONDecodeError, OSError):
                print("Checkpoint unreadable; starting fresh.")

    def is_done(self, box_file_id):
        return box_file_id in self.done_files

    def mark_done(self, box_file_id):
        with self.lock:
            self.done_files.add(box_file_id)
            self._flush()

    def get_folder(self, box_folder_id):
        with self.lock:
            return self.folder_map.get(box_folder_id)

    def set_folder(self, box_folder_id, gdrive_id):
        with self.lock:
            self.folder_map[box_folder_id] = gdrive_id
            self._flush()

    def _flush(self):
        # caller already holds the lock
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"done_files": sorted(self.done_files),
                       "folder_map": self.folder_map}, f)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# CSV transfer log
# --------------------------------------------------------------------------- #
class TransferLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        new = not os.path.exists(path)
        self.fh = open(path, "a", newline="")
        self.writer = csv.writer(self.fh)
        if new:
            self.writer.writerow(
                ["timestamp", "status", "box_file_id", "path", "size_bytes",
                 "gdrive_file_id", "error"]
            )
            self.fh.flush()

    def record(self, status, box_file_id, path, size="", gdrive_id="", error=""):
        with self.lock:
            self.writer.writerow(
                [datetime.now(timezone.utc).isoformat(), status, box_file_id,
                 path, size, gdrive_id, error]
            )
            self.fh.flush()

    def close(self):
        self.fh.close()


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def get_box_client(config_path=None, use_dev_token=False):
    if use_dev_token:
        token = os.environ.get("BOX_DEV_TOKEN")
        if not token:
            sys.exit("BOX_DEV_TOKEN env var not set.")
        auth = OAuth2(client_id="", client_secret="", access_token=token)
        return Client(auth)
    if not config_path or not os.path.exists(config_path):
        sys.exit("Box JWT config file not found. Pass --box-config or use --box-dev-token.")
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
                sys.exit(f"Google credentials not found at {creds_path}.")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GDRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def build_gdrive_service_from_token(token_path="token.json"):
    """Cheap per-thread Drive service. googleapiclient's http objects are not
    thread-safe, so each worker builds its own service from the cached token."""
    creds = Credentials.from_authorized_user_file(token_path, GDRIVE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


# --------------------------------------------------------------------------- #
# Google Drive helpers
# --------------------------------------------------------------------------- #
# Google Drive rate-limit / user-rate-limit reasons come back as 403 (and
# occasionally 429). These are retryable with exponential backoff; other 403s
# (e.g. permission denied) are not, so we inspect the reason string.
_RETRYABLE_403_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "sharingRateLimitExceeded",
    "dailyLimitExceeded",
}


def _is_retryable(error):
    status = getattr(error.resp, "status", None)
    if status in (429, 500, 502, 503, 504):
        return True
    if status == 403:
        try:
            reasons = json.loads(error.content.decode())["error"]["errors"]
            return any(r.get("reason") in _RETRYABLE_403_REASONS for r in reasons)
        except (ValueError, KeyError, AttributeError):
            # Can't parse the reason; treat generic 403 as rate limiting to be safe.
            return True
    return False


def with_backoff(func, *args, max_retries=7, base=1.0, limiter=None, **kwargs):
    """Call an API function, retrying retryable errors with exponential backoff
    plus jitter. Raises the last error if retries are exhausted. If a
    RateLimiter is supplied, a token is acquired before each attempt."""
    import random
    attempt = 0
    while True:
        if limiter is not None:
            limiter.acquire()
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            if not _is_retryable(e) or attempt >= max_retries:
                raise
            delay = base * (2 ** attempt) + random.uniform(0, 1)
            time.sleep(delay)
            attempt += 1


class RateLimiter:
    """Thread-safe token bucket capping the combined rate of Drive API calls
    across all workers. Google Drive allows ~200 write requests/sec per user;
    set `rate` below that. `burst` permits a short spike; tokens refill at
    `rate` per second. Raising --workers past this ceiling just makes extra
    workers wait for tokens rather than exceeding the quota."""

    def __init__(self, rate, burst=None):
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None else rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, tokens=1):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                wait = (tokens - self.tokens) / self.rate
            time.sleep(wait)


def find_or_create_folder(drive, name, parent_id, shared_drive_id, dry_run=False,
                          limiter=None):
    safe_name = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and mimeType = '{GOOGLE_FOLDER_MIME}' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = with_backoff(
        drive.files().list(
            q=query,
            corpora="drive",
            driveId=shared_drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id, name)",
        ).execute,
        limiter=limiter,
    )
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    if dry_run:
        return f"(new-folder:{name})"
    metadata = {"name": name, "mimeType": GOOGLE_FOLDER_MIME, "parents": [parent_id]}
    folder = with_backoff(
        drive.files().create(
            body=metadata, supportsAllDrives=True, fields="id"
        ).execute,
        limiter=limiter,
    )
    return folder["id"]


def upload_stream(drive, stream, name, parent_id, limiter=None):
    media = MediaIoBaseUpload(stream, mimetype="application/octet-stream", resumable=True)
    metadata = {"name": name, "parents": [parent_id]}
    request = drive.files().create(
        body=metadata, media_body=media, supportsAllDrives=True, fields="id"
    )
    import random
    response = None
    attempt = 0
    max_retries = 7
    while response is None:
        if limiter is not None:
            limiter.acquire()
        try:
            _, response = request.next_chunk()
            attempt = 0  # reset after any successful chunk
        except HttpError as e:
            if not _is_retryable(e) or attempt >= max_retries:
                raise
            delay = 2 ** attempt + random.uniform(0, 1)
            time.sleep(delay)
            attempt += 1
    return response["id"]


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #
def collect_files(box, box_folder_id, gdrive_parent_id, shared_drive_id,
                  drive, ckpt, rel_path, dry_run, tasks, limiter=None):
    """Recursively create the folder tree (single-threaded, so folder IDs are
    stable) and gather the flat list of file transfer tasks."""
    items = box.folder(box_folder_id).get_items(
        limit=1000, fields=["id", "name", "type", "size"]
    )
    for item in items:
        item_path = f"{rel_path}/{item.name}" if rel_path else item.name
        if item.type == "folder":
            cached = ckpt.get_folder(item.id)
            if cached:
                new_parent = cached
            else:
                new_parent = find_or_create_folder(
                    drive, item.name, gdrive_parent_id, shared_drive_id, dry_run,
                    limiter=limiter
                )
                if not dry_run:
                    ckpt.set_folder(item.id, new_parent)
            print(f"[folder] {item_path}")
            collect_files(box, item.id, new_parent, shared_drive_id,
                          drive, ckpt, item_path, dry_run, tasks, limiter)
        elif item.type == "file":
            tasks.append({
                "box_file_id": item.id,
                "name": item.name,
                "path": item_path,
                "size": getattr(item, "size", ""),
                "parent_id": gdrive_parent_id,
            })


def transfer_one(box, token_path, task, ckpt, log, limiter=None):
    """Runs in a worker thread: download from Box, upload to Drive."""
    box_id = task["box_file_id"]
    try:
        drive = build_gdrive_service_from_token(token_path)
        buffer = io.BytesIO()
        box.file(box_id).download_to(buffer)
        buffer.seek(0)
        gdrive_id = upload_stream(drive, buffer, task["name"], task["parent_id"],
                                  limiter=limiter)
        ckpt.mark_done(box_id)
        log.record("ok", box_id, task["path"], task["size"], gdrive_id)
        return (True, task["path"], None)
    except Exception as e:  # noqa: BLE001  (log everything, keep going)
        log.record("error", box_id, task["path"], task["size"], error=str(e))
        return (False, task["path"], str(e))


def main():
    parser = argparse.ArgumentParser(description="Export Box.com files to a Google Shared Drive.")
    parser.add_argument("--box-folder", default="0",
                        help="Box folder ID to export ('0' = root).")
    parser.add_argument("--shared-drive-id", required=True,
                        help="Google Shared Drive ID (destination).")
    parser.add_argument("--dest-folder-id",
                        help="Folder ID inside the Shared Drive to copy into "
                             "(defaults to the Shared Drive root).")
    parser.add_argument("--box-config", help="Path to Box JWT config JSON.")
    parser.add_argument("--box-dev-token", action="store_true",
                        help="Use BOX_DEV_TOKEN env var instead of JWT config.")
    parser.add_argument("--google-creds", default="google_credentials.json")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel upload workers (default 4).")
    parser.add_argument("--rate", type=float, default=10.0,
                        help="Max combined Drive requests/sec across all workers "
                             "(token bucket; default 10). Lets you raise --workers "
                             "safely. Google's per-user write quota is ~200/sec.")
    parser.add_argument("--checkpoint", default="checkpoint.json")
    parser.add_argument("--log", default="transfer_log.csv")
    parser.add_argument("--reset", action="store_true",
                        help="Ignore/delete existing checkpoint and start fresh.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be copied without transferring.")
    args = parser.parse_args()

    if args.reset and os.path.exists(args.checkpoint):
        os.remove(args.checkpoint)
        print("Checkpoint reset.")

    box = get_box_client(args.box_config, args.box_dev_token)
    drive = get_gdrive_service(args.google_creds)  # also caches token.json
    ckpt = Checkpoint(args.checkpoint)
    log = TransferLog(args.log)

    dest = args.dest_folder_id or args.shared_drive_id
    limiter = RateLimiter(rate=args.rate, burst=max(args.rate, args.workers))
    print(f"Scanning Box folder {args.box_folder} -> Shared Drive {args.shared_drive_id}")
    print(f"Workers: {args.workers} | Rate cap: {args.rate}/sec")
    if args.dry_run:
        print("*** DRY RUN — nothing will be transferred ***")

    # Phase 1: build folder tree + flat task list (single-threaded).
    tasks = []
    collect_files(box, args.box_folder, dest, args.shared_drive_id,
                  drive, ckpt, "", args.dry_run, tasks, limiter)

    pending = [t for t in tasks if not ckpt.is_done(t["box_file_id"])]
    skipped = len(tasks) - len(pending)
    print(f"\n{len(tasks)} files total | {skipped} already done | {len(pending)} to transfer")

    if args.dry_run:
        for t in pending:
            print(f"  [dry-run] {t['path']}")
        log.close()
        print("Done (dry run).")
        return

    # Phase 2: parallel transfers.
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(transfer_one, box, "token.json", t, ckpt, log, limiter): t
            for t in pending
        }
        for i, fut in enumerate(as_completed(futures), 1):
            success, path, err = fut.result()
            if success:
                ok += 1
                print(f"  [{i}/{len(pending)}] ok   {path}")
            else:
                fail += 1
                print(f"  [{i}/{len(pending)}] FAIL {path}  ({err})")

    log.close()
    print(f"\nDone. {ok} succeeded, {fail} failed. Log: {args.log}")
    if fail:
        print("Re-run the same command to retry failed files (checkpoint skips successes).")


if __name__ == "__main__":
    main()
