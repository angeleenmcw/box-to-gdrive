"""
Box -> Google Shared Drive migration — hosted, multi-user OAuth version.

Each visitor connects THEIR OWN Box and Google accounts via a browser OAuth
"Connect" flow. The server holds only short-lived access/refresh tokens in the
signed session cookie — never users' passwords or private keys.

You (the operator) register ONE Box app and ONE Google app, and provide their
client IDs/secrets as environment variables. These identify your application,
not any user.

Required environment variables
------------------------------
  FLASK_SECRET_KEY      long random string for signing sessions
  BOX_CLIENT_ID         from your Box OAuth 2.0 app
  BOX_CLIENT_SECRET
  GOOGLE_CLIENT_ID      from your Google OAuth web client
  GOOGLE_CLIENT_SECRET
  OAUTH_REDIRECT_BASE   public base URL, e.g. https://your-app.onrender.com
                        (used to build the OAuth redirect URIs)

Register these redirect URIs with each provider:
  Box:     {OAUTH_REDIRECT_BASE}/oauth/box/callback
  Google:  {OAUTH_REDIRECT_BASE}/oauth/google/callback

Run locally for testing:
  export FLASK_SECRET_KEY=dev-only-change-me
  export BOX_CLIENT_ID=...  BOX_CLIENT_SECRET=...
  export GOOGLE_CLIENT_ID=...  GOOGLE_CLIENT_SECRET=...
  export OAUTH_REDIRECT_BASE=http://127.0.0.1:5000
  export OAUTHLIB_INSECURE_TRANSPORT=1   # ONLY for http:// local testing
  python app.py
"""

import json
import os
import secrets
import threading
import urllib.parse

import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

import migrator

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

# ---- Operator-provided app credentials (identify THIS app, not any user) ----
BOX_CLIENT_ID = os.environ.get("BOX_CLIENT_ID", "")
BOX_CLIENT_SECRET = os.environ.get("BOX_CLIENT_SECRET", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_BASE = os.environ.get("OAUTH_REDIRECT_BASE", "http://127.0.0.1:5000").rstrip("/")

BOX_AUTH_URL = "https://account.box.com/api/oauth2/authorize"
BOX_TOKEN_URL = "https://api.box.com/oauth2/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive"


# --------------------------------------------------------------------------- #
# Session token helpers
# --------------------------------------------------------------------------- #
def _box_tokens():
    return session.get("box")

def _google_tokens():
    return session.get("google")

def _save_box_tokens(access, refresh):
    session["box"] = {"access_token": access, "refresh_token": refresh}
    session.modified = True

def _save_google_tokens(access, refresh):
    # Google may omit refresh_token on re-consent; keep the previous one.
    prev = session.get("google", {})
    session["google"] = {"access_token": access,
                         "refresh_token": refresh or prev.get("refresh_token")}
    session.modified = True


def box_client_for_session():
    tok = _box_tokens()
    if not tok:
        raise RuntimeError("Box not connected.")
    return migrator.box_client_from_token(
        tok["access_token"], tok.get("refresh_token"),
        client_id=BOX_CLIENT_ID, client_secret=BOX_CLIENT_SECRET,
        on_refresh=_save_box_tokens_threadsafe)


def _save_box_tokens_threadsafe(access, refresh):
    # store_tokens may fire off-request-thread; guard the session write.
    try:
        _save_box_tokens(access, refresh)
    except RuntimeError:
        pass  # outside request context (worker thread) — token still valid in memory


def drive_creds_for_session():
    tok = _google_tokens()
    if not tok:
        raise RuntimeError("Google not connected.")
    return migrator.gdrive_creds_from_token(tok, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/log")
def api_log():
    """Download this session's transfer log (CSV with Box→Drive URL mapping)."""
    sid = session.get("sid")
    if not sid:
        return "No migration has been run in this session yet.", 404
    log_path = f"/tmp/log_{sid}.csv"
    if not os.path.exists(log_path):
        return "No log available yet — run a migration first.", 404
    return send_file(log_path, mimetype="text/csv", as_attachment=True,
                     download_name="box_to_drive_migration_log.csv")


@app.route("/api/status")
def api_status():
    box_ok = _box_tokens() is not None
    google_ok = _google_tokens() is not None
    configured = all([BOX_CLIENT_ID, BOX_CLIENT_SECRET,
                      GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET])
    return jsonify({
        "box": {"ok": box_ok, "message": "Connected" if box_ok else "Not connected"},
        "google": {"ok": google_ok, "message": "Connected" if google_ok else "Not connected"},
        "server_configured": configured,
    })


# --------------------------------------------------------------------------- #
# Box OAuth
# --------------------------------------------------------------------------- #
@app.route("/oauth/box/start")
def box_start():
    state = secrets.token_urlsafe(24)
    session["box_state"] = state
    params = {
        "response_type": "code",
        "client_id": BOX_CLIENT_ID,
        "redirect_uri": f"{REDIRECT_BASE}/oauth/box/callback",
        "state": state,
    }
    return redirect(BOX_AUTH_URL + "?" + urllib.parse.urlencode(params))


@app.route("/oauth/box/callback")
def box_callback():
    if request.args.get("state") != session.get("box_state"):
        return "State mismatch — please try connecting again.", 400
    code = request.args.get("code")
    if not code:
        return "Box authorization was cancelled.", 400
    resp = requests.post(BOX_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": BOX_CLIENT_ID,
        "client_secret": BOX_CLIENT_SECRET,
        "redirect_uri": f"{REDIRECT_BASE}/oauth/box/callback",
    }, timeout=30)
    if resp.status_code != 200:
        return f"Box token exchange failed: {resp.text}", 400
    data = resp.json()
    _save_box_tokens(data["access_token"], data.get("refresh_token"))
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Google OAuth
# --------------------------------------------------------------------------- #
@app.route("/oauth/google/start")
def google_start():
    state = secrets.token_urlsafe(24)
    session["google_state"] = state
    params = {
        "response_type": "code",
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": f"{REDIRECT_BASE}/oauth/google/callback",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",       # request a refresh token
        "prompt": "consent",
        "state": state,
    }
    return redirect(GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params))


@app.route("/oauth/google/callback")
def google_callback():
    if request.args.get("state") != session.get("google_state"):
        return "State mismatch — please try connecting again.", 400
    code = request.args.get("code")
    if not code:
        return "Google authorization was cancelled.", 400
    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": f"{REDIRECT_BASE}/oauth/google/callback",
    }, timeout=30)
    if resp.status_code != 200:
        return f"Google token exchange failed: {resp.text}", 400
    data = resp.json()
    _save_google_tokens(data["access_token"], data.get("refresh_token"))
    return redirect(url_for("index"))


@app.route("/oauth/disconnect/<provider>", methods=["POST"])
def disconnect(provider):
    if provider in ("box", "google"):
        session.pop(provider, None)
        session.modified = True
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


# --------------------------------------------------------------------------- #
# Data endpoints (require the relevant connection)
# --------------------------------------------------------------------------- #
@app.route("/api/box/folder")
def api_box_folder():
    folder_id = request.args.get("id", "0")
    try:
        items = migrator.list_box_folder(box_client_for_session(), folder_id)
        return jsonify({"ok": True, "items": items})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/drive/shared-drives")
def api_shared_drives():
    try:
        creds = drive_creds_for_session()
        drive = migrator.gdrive_service_from_creds(creds)
        return jsonify({"ok": True, "drives": migrator.list_shared_drives(drive)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


# --------------------------------------------------------------------------- #
# Migration (SSE) — builds per-thread clients from the session's tokens
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Background jobs
#
# Instead of doing all the work while holding one long HTTP connection open
# (which hosts time out), we start the migration in a background thread and let
# the browser poll a lightweight status endpoint. Each poll is a fast request,
# so nothing ever hits a connection-duration limit, and the job runs to
# completion server-side regardless of the browser.
# --------------------------------------------------------------------------- #
_jobs = {}          # job_id -> state dict
_jobs_lock = threading.Lock()

# Job state is also written to disk so a progress poll still works after the
# server process restarts (Render recycles the instance). Without this, a
# restart wipes the in-memory job and every poll 404s, which looks like a
# frozen migration. On disk, the poll returns the last known state instead.
def _job_path(job_id):
    return f"/tmp/job_{job_id}.json"


def _persist_job(job_id):
    """Write the current in-memory job state to disk. Caller holds _jobs_lock."""
    j = _jobs.get(job_id)
    if j is None:
        return
    try:
        tmp = _job_path(job_id) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(j, f)
        os.replace(tmp, _job_path(job_id))
    except OSError:
        pass  # disk hiccup shouldn't crash the migration


def _new_job():
    job_id = secrets.token_hex(8)
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "starting",          # starting|scanning|running|done|error|interrupted
            "total": 0, "pending": 0, "skipped": 0,
            "done": 0, "ok": 0, "fail": 0,
            "recent": [],                  # last handful of file events
            "failures": [],                # {path, error} for every failed file
            "error": None,
            "cursor": 0,                   # monotonically increasing event count
        }
        _persist_job(job_id)
    return job_id


def _update_job(job_id, **changes):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j:
            j.update(changes)
            _persist_job(job_id)


def _push_recent(job_id, line):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j:
            j["cursor"] += 1
            j["recent"].append(line)
            # keep only the last 40 lines to bound memory
            j["recent"] = j["recent"][-40:]
            _persist_job(job_id)


def _launch_migration(job_id, params, box_tok, google_tok, sid):
    """Start (or resume) a migration in a background thread. Reuses the
    checkpoint at ckpt_path, so resuming skips already-copied files."""
    shared_drive_id = params["shared_drive_id"]
    dest_parent = params.get("dest_folder_id") or shared_drive_id
    folders = params.get("folders", [])
    files = params.get("files", [])
    workers = int(params.get("workers", 4))
    rate = float(params.get("rate", 10))
    ckpt_path = f"/tmp/ckpt_{sid}.json"
    log_path = f"/tmp/log_{sid}.csv"

    box_token_state = {
        "access_token": box_tok["access_token"],
        "refresh_token": box_tok.get("refresh_token"),
    }
    box_token_lock = threading.Lock()

    def _on_box_refresh(access, refresh):
        with box_token_lock:
            box_token_state["access_token"] = access
            if refresh:
                box_token_state["refresh_token"] = refresh

    def box_factory():
        with box_token_lock:
            at = box_token_state["access_token"]
            rt = box_token_state["refresh_token"]
        return migrator.box_client_from_token(
            at, rt, client_id=BOX_CLIENT_ID, client_secret=BOX_CLIENT_SECRET,
            on_refresh=_on_box_refresh)

    def drive_factory():
        creds = migrator.gdrive_creds_from_token(
            google_tok, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
        return migrator.gdrive_service_from_creds(creds)

    def _is_auth_error(msg):
        return ("invalid_token" in msg or "invalid_grant" in msg
                or "expired" in msg.lower() or "401" in msg)

    def progress(evt):
        t = evt.get("type")
        if t == "scanning":
            _update_job(job_id, status="scanning")
        elif t == "scan":
            _push_recent(job_id, f"scan: {evt.get('path','')}")
        elif t == "start":
            _update_job(job_id, status="running", total=evt["total"],
                        pending=evt["pending"], skipped=evt["skipped"])
        elif t == "file":
            _update_job(job_id, done=evt["done"], ok=evt["ok_count"],
                        fail=evt["fail_count"])
            tag = "ok" if evt["ok"] else "FAIL"
            extra = "" if evt["ok"] else f"  ({evt.get('error','')})"
            _push_recent(job_id, f"{tag}: {evt.get('path','')}{extra}")
            if not evt["ok"]:
                with _jobs_lock:
                    jj = _jobs.get(job_id)
                    if jj is not None:
                        jj.setdefault("failures", []).append(
                            {"path": evt.get("path", ""), "error": evt.get("error", "")})
                        _persist_job(job_id)
        elif t == "done":
            _update_job(job_id, status="done", ok=evt["ok"],
                        fail=evt["fail"], skipped=evt["skipped"])
        elif t == "fatal":
            _update_job(job_id, status="error", error=evt.get("error"))

    def worker():
        try:
            box = box_factory()
            drive = drive_factory()
            # Pre-flight: verify the Box token works. If it's expired, pause the
            # job (needs_reconnect) instead of failing, so the user can click
            # Reconnect Box and resume from the checkpoint.
            try:
                box.user(user_id="me").get(fields=["id"])
            except Exception as e:  # noqa: BLE001
                if _is_auth_error(str(e)):
                    _update_job(job_id, status="needs_reconnect",
                                error="Box session expired. Click Reconnect Box, "
                                      "then Resume — already-copied files are skipped.")
                    return
                raise
            ckpt = migrator.Checkpoint(ckpt_path)
            log = migrator.TransferLog(log_path)
            limiter = migrator.RateLimiter(rate=rate, burst=max(rate, workers))
            progress({"type": "scanning"})
            tasks = migrator.expand_selection(
                box, folders, files, dest_parent, shared_drive_id,
                drive, ckpt, progress, limiter=limiter)
            migrator.run_migration(
                box, None, tasks, ckpt, log, workers, progress,
                limiter=limiter, drive_factory=drive_factory,
                box_factory=box_factory)
            log.close()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if _is_auth_error(msg):
                _update_job(job_id, status="needs_reconnect",
                            error="Box session expired mid-migration. Click "
                                  "Reconnect Box, then Resume — already-copied "
                                  "files are skipped.")
            else:
                _update_job(job_id, status="error", error=msg)

    threading.Thread(target=worker, daemon=True).start()


@app.route("/api/migrate", methods=["POST"])
def api_migrate():
    if not _box_tokens() or not _google_tokens():
        return jsonify({"ok": False, "error": "Connect both Box and Google first."}), 400

    payload = request.get_json(force=True)
    params = {
        "shared_drive_id": payload["shared_drive_id"],
        "dest_folder_id": payload.get("dest_folder_id"),
        "folders": payload.get("folders", []),
        "files": payload.get("files", []),
        "workers": int(payload.get("workers", 4)),
        "rate": float(payload.get("rate", 10)),
    }

    box_tok = dict(_box_tokens())
    google_tok = dict(_google_tokens())

    sid = session.get("sid")
    if not sid:
        sid = secrets.token_hex(8)
        session["sid"] = sid

    job_id = _new_job()
    # Store params + sid on the job so a reconnect can resume it.
    _update_job(job_id, params=params, sid=sid)

    _launch_migration(job_id, params, box_tok, google_tok, sid)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/resume/<job_id>", methods=["POST"])
def api_resume(job_id):
    """Resume a paused (needs_reconnect) job using the current session's fresh
    Box + Google tokens. The checkpoint skips everything already copied."""
    if not _box_tokens() or not _google_tokens():
        return jsonify({"ok": False, "error": "Connect both Box and Google first."}), 400

    # Load the job (from memory or disk) to recover its params + sid.
    with _jobs_lock:
        j = _jobs.get(job_id)
        params = j.get("params") if j else None
        sid = j.get("sid") if j else None
    if params is None:
        try:
            with open(_job_path(job_id)) as f:
                disk = json.load(f)
            params = disk.get("params")
            sid = disk.get("sid")
            with _jobs_lock:
                _jobs[job_id] = disk
        except (OSError, ValueError):
            params = None
    if not params or not sid:
        return jsonify({"ok": False, "error": "Cannot resume — original job details missing."}), 400

    box_tok = dict(_box_tokens())
    google_tok = dict(_google_tokens())
    _update_job(job_id, status="starting", error=None)
    _launch_migration(job_id, params, box_tok, google_tok, sid)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j:
            snapshot = dict(j)
            snapshot["recent"] = list(j["recent"])
            snapshot["failures"] = list(j.get("failures", []))
            snapshot["found"] = True
            return jsonify(snapshot)

    # Not in memory — the process may have restarted mid-run. Read the last
    # state we persisted to disk so the UI can show a recoverable message
    # instead of a 404 that spins forever.
    try:
        with open(_job_path(job_id)) as f:
            snapshot = json.load(f)
    except (OSError, ValueError):
        return jsonify({"found": False, "error": "Unknown job"}), 404

    # If it was still running when we lost it, the restart interrupted it.
    if snapshot.get("status") in ("starting", "scanning", "running"):
        snapshot["status"] = "interrupted"
        snapshot["error"] = ("The server restarted mid-migration, so it was "
                             "interrupted. Files already copied are saved — click "
                             "Migrate again to resume where it left off.")
    snapshot["found"] = True
    return jsonify(snapshot)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
