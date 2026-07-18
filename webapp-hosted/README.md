# Box → Shared Drive Migration — hosted (OAuth) version

A web app where each visitor connects **their own** Box and Google accounts with
"Connect" buttons, then migrates selected Box folders/files into a Google Shared
Drive. Transfers stream live, resume via checkpoint, run in parallel, and are
rate-limited.

## Why this design is safe to host

Users authorize through Box's and Google's own sign-in pages (OAuth 2.0
authorization-code flow). Your server **never receives users' passwords or
private keys** — only short-lived access/refresh tokens, held per-user in the
signed session cookie. Each user only ever touches their own accounts.

You register the app **once** and provide its client IDs/secrets as environment
variables. These identify *your application*, not any user.

## One-time setup (operator)

### 1. Register a Box OAuth app
- Box Developer Console → Create Custom App → **User Authentication (OAuth 2.0)**
- Add the redirect URI: `{YOUR_URL}/oauth/box/callback`
- Note the **Client ID** and **Client Secret**

### 2. Register a Google OAuth web client
- Google Cloud Console → enable the **Google Drive API**
- Create Credentials → **OAuth client ID** → type **Web application**
- Add the redirect URI: `{YOUR_URL}/oauth/google/callback`
- Note the **Client ID** and **Client Secret**
- On the OAuth consent screen, add the scope `.../auth/drive` and add test users
  (or publish the app) so others can sign in

### 3. Set environment variables
```
FLASK_SECRET_KEY      long random string (e.g. `python -c "import secrets;print(secrets.token_hex(32))"`)
BOX_CLIENT_ID
BOX_CLIENT_SECRET
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
OAUTH_REDIRECT_BASE   your public base URL, e.g. https://your-app.onrender.com
```

## Deploy

The included `Procfile` runs the app under gunicorn. Works on Render, Railway,
Fly.io, or any host that runs Python web apps.

**Render example:** New → Web Service → point at your repo's `webapp-hosted`
folder → Build `pip install -r requirements.txt` → Start command comes from the
Procfile → add the environment variables above → deploy. Then set
`OAUTH_REDIRECT_BASE` to the URL Render gives you and add the two callback URLs
to your Box and Google apps.

## Run locally (for testing)

```bash
pip install -r requirements.txt
export FLASK_SECRET_KEY=dev-change-me
export BOX_CLIENT_ID=...  BOX_CLIENT_SECRET=...
export GOOGLE_CLIENT_ID=...  GOOGLE_CLIENT_SECRET=...
export OAUTH_REDIRECT_BASE=http://127.0.0.1:5000
export OAUTHLIB_INSECURE_TRANSPORT=1   # ONLY for http:// local testing
python app.py
```
Register `http://127.0.0.1:5000/oauth/box/callback` and
`http://127.0.0.1:5000/oauth/google/callback` as redirect URIs while testing.

## Notes & limitations

- **Sessions are cookie-based.** Fine for personal/small-team use. For heavier
  multi-user load, move token storage to a server-side session store (Redis).
- **Checkpoints/logs are per-session** files under `/tmp`. On hosts with
  ephemeral disks these reset on redeploy; that only affects resume, not
  correctness.
- **Box app scope:** the OAuth app must have "Read all files" (and write isn't
  needed on the Box side). Google needs the Drive scope.
- This is the multi-user counterpart to the local single-user tool in `webapp/`.
