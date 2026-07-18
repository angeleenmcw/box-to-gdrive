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


def _validate_box_config(data):
    """Check that an uploaded Box JWT config has the required key material.
    Returns (ok, message)."""
    try:
        cfg = json.loads(data)
    except (ValueError, TypeError):
        return False, "File is not valid JSON."
    settings = cfg.get("boxAppSettings")
    if not isinstance(settings, dict):
        return False, "Missing 'boxAppSettings'. This doesn't look like a Box JWT config."
    if not settings.get("clientID") or not settings.get("clientSecret"):
        return False, "Missing clientID / clientSecret in boxAppSettings."
    app_auth = settings.get("appAuth") or {}
    if not app_auth.get("privateKey"):
        return False, ("Missing the private key (boxAppSettings.appAuth.privateKey). "
                       "When creating the app, click 'Generate a Public/Private Keypair' "
                       "and download that config — it embeds the key.")
    if not app_auth.get("publicKeyID"):
        return False, "Missing publicKeyID in appAuth."
    if not cfg.get("enterpriseID"):
        return False, "Missing enterpriseID. Use the config downloaded from the app's Configuration tab."
    return True, "Config looks complete."


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Report which services are connected, so the UI can show/hide the
    credential panel."""
    box_ok, box_msg = False, ""
    google_ok, google_msg = False, ""

    # Box: file present + valid + client initialises.
    if os.path.exists(BOX_CONFIG):
        with open(BOX_CONFIG) as f:
            ok, msg = _validate_box_config(f.read())
        if not ok:
            box_msg = msg
        else:
            try:
                box_client()
                box_ok = True
                box_msg = "Connected"
            except Exception as e:  # noqa: BLE001
                box_msg = str(e)
    else:
        box_msg = "No box_config.json uploaded yet."

    # Google: token cached or credentials present.
    if os.path.exists(TOKEN_PATH):
        google_ok, google_msg = True, "Connected"
    elif os.path.exists(GOOGLE_CREDS):
        google_msg = "Credentials present — sign-in needed."
    else:
        google_msg = "No google_credentials.json present."

    return jsonify({"box": {"ok": box_ok, "message": box_msg},
                    "google": {"ok": google_ok, "message": google_msg}})


@app.route("/api/box/upload-config", methods=["POST"])
def api_box_upload_config():
    """Accept a Box JWT config file from the UI, validate it, save it, and
    reset the cached client so the next call uses the new credentials."""
    global _box
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file received."}), 400
    raw = request.files["file"].read().decode("utf-8", errors="replace")
    ok, msg = _validate_box_config(raw)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    # Save and reset the client.
    with open(BOX_CONFIG, "w") as f:
        f.write(raw)
    with _lock:
        _box = None
    # Try to actually connect and read the root, to surface auth/authorization errors.
    try:
        migrator.list_box_folder(box_client(), "0")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error":
                        f"Config saved, but connecting to Box failed: {e}. "
                        "If this mentions authorization, an admin must approve the "
                        "app in the Box Admin Console (Apps > Custom Apps Manager)."}), 400
    return jsonify({"ok": True, "message": "Box connected."})


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
