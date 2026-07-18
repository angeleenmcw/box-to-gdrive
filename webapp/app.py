"""
Flask web UI for the Box -> Google Shared Drive migration tool.

Run locally:
    pip install flask boxsdk google-api-python-client google-auth google-auth-oauthlib
    python app.py
    open http://127.0.0.1:5000

Auth: server-side JWT for Box (box_config.json) and OAuth for Google
(google_credentials.json, cached to token.json). No per-user login.
"""

import json
import os
import queue
import threading

from flask import Flask, Response, jsonify, render_template, request

import migrator

app = Flask(__name__)

BOX_CONFIG = os.environ.get("BOX_CONFIG", "box_config.json")
GOOGLE_CREDS = os.environ.get("GOOGLE_CREDS", "google_credentials.json")
TOKEN_PATH = "token.json"
CHECKPOINT = "checkpoint.json"
LOG_PATH = "transfer_log.csv"

# Lazily-initialised shared clients.
_box = None
_drive = None
_lock = threading.Lock()


def box_client():
    global _box
    with _lock:
        if _box is None:
            _box = migrator.get_box_client(BOX_CONFIG)
        return _box


def drive_client():
    global _drive
    with _lock:
        if _drive is None:
            _drive = migrator.get_gdrive_service(GOOGLE_CREDS, TOKEN_PATH)
        return _drive


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/box/folder")
def api_box_folder():
    """List immediate children of a Box folder. ?id=0 for root."""
    folder_id = request.args.get("id", "0")
    try:
        items = migrator.list_box_folder(box_client(), folder_id)
        return jsonify({"ok": True, "items": items})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/drive/shared-drives")
def api_shared_drives():
    try:
        drives = migrator.list_shared_drives(drive_client())
        return jsonify({"ok": True, "drives": drives})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/migrate", methods=["POST"])
def api_migrate():
    """
    Kick off a migration and stream progress as Server-Sent Events.

    Body JSON:
      {
        "shared_drive_id": "0A...",
        "dest_folder_id": "optional folder inside the drive",
        "folders": ["boxFolderId", ...],
        "files":   [{"id": "boxFileId", "name": "...", "size": 123}, ...],
        "workers": 4
      }
    """
    payload = request.get_json(force=True)
    shared_drive_id = payload["shared_drive_id"]
    dest_parent = payload.get("dest_folder_id") or shared_drive_id
    folders = payload.get("folders", [])
    files = payload.get("files", [])
    workers = int(payload.get("workers", 4))
    # Combined Drive request ceiling (requests/sec), shared across all workers.
    # Google's per-user write quota is ~200/sec; default to a safe 10/sec.
    rate = float(payload.get("rate", 10))

    events = queue.Queue()

    def progress(evt):
        events.put(evt)

    def worker():
        try:
            box = box_client()
            drive = drive_client()
            ckpt = migrator.Checkpoint(CHECKPOINT)
            log = migrator.TransferLog(LOG_PATH)
            limiter = migrator.RateLimiter(rate=rate, burst=max(rate, workers))
            progress({"type": "scanning"})
            tasks = migrator.expand_selection(
                box, folders, files, dest_parent, shared_drive_id,
                drive, ckpt, progress, limiter=limiter)
            migrator.run_migration(box, TOKEN_PATH, tasks, ckpt, log,
                                   workers, progress, limiter=limiter)
            log.close()
        except Exception as e:  # noqa: BLE001
            progress({"type": "fatal", "error": str(e)})
        finally:
            events.put(None)  # sentinel: stream complete

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            evt = events.get()
            if evt is None:
                break
            yield f"data: {json.dumps(evt)}\n\n"

    return Response(stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    # threaded=True so SSE streaming and API calls can overlap.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
