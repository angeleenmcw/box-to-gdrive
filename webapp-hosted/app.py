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
import queue
import secrets
import threading
import urllib.parse

import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)

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
@app.route("/api/migrate", methods=["POST"])
def api_migrate():
    if not _box_tokens() or not _google_tokens():
        return jsonify({"ok": False, "error": "Connect both Box and Google first."}), 400

    payload = request.get_json(force=True)
    shared_drive_id = payload["shared_drive_id"]
    dest_parent = payload.get("dest_folder_id") or shared_drive_id
    folders = payload.get("folders", [])
    files = payload.get("files", [])
    workers = int(payload.get("workers", 4))
    rate = float(payload.get("rate", 10))

    # Snapshot tokens now, in the request thread, so worker threads don't touch
    # the session object.
    box_tok = dict(_box_tokens())
    google_tok = dict(_google_tokens())

    # Per-session working files (isolate one user's run from another's).
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_hex(8)
        session["sid"] = sid
    ckpt_path = f"/tmp/ckpt_{sid}.json"
    log_path = f"/tmp/log_{sid}.csv"

    def box_factory():
        return migrator.box_client_from_token(
            box_tok["access_token"], box_tok.get("refresh_token"),
            client_id=BOX_CLIENT_ID, client_secret=BOX_CLIENT_SECRET)

    def drive_factory():
        creds = migrator.gdrive_creds_from_token(
            google_tok, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
        return migrator.gdrive_service_from_creds(creds)

    events = queue.Queue()

    def progress(evt):
        events.put(evt)

    def worker():
        try:
            box = box_factory()
            drive = drive_factory()
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
            progress({"type": "fatal", "error": str(e)})
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            evt = events.get()
            if evt is None:
                break
            yield f"data: {json.dumps(evt)}\n\n"

    return Response(stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
