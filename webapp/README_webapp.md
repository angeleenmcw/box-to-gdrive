# Box → Shared Drive — Web UI

A local browser interface for the migration tool. Browse your Box folder tree,
tick the folders and files you want, pick a Google Shared Drive as the
destination, and watch transfers stream live. Reuses the same transfer engine
as the CLI (checkpoint, parallel workers, exponential backoff, CSV log).

## Layout

```
webapp/
├── app.py              # Flask server + API endpoints (SSE progress)
├── migrator.py         # Core Box→Drive logic (shared with the CLI)
├── requirements.txt
├── templates/
│   └── index.html      # The UI
└── static/
    └── app.js          # Tree browsing, selection, live progress
```

## Setup

Put your credentials in the `webapp/` folder (same files as the CLI):

- `box_config.json`      — Box JWT app config
- `google_credentials.json` — Google OAuth desktop client

Then:

```bash
cd webapp
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000> in your browser.

On first run a Google sign-in window opens (cached afterward to `token.json`).

## Using it

1. **Left panel** — click a folder's arrow to expand it (lazy-loaded, so large
   Box accounts stay responsive). Tick any folders (copied recursively) or
   individual files.
2. **Right panel** — choose the destination Shared Drive, the number of
   parallel **workers**, and the **Req/sec** ceiling. Workers control
   parallelism; Req/sec is a token-bucket cap on the combined Drive request
   rate across all workers, so you can raise workers for speed without pushing
   past Google's quota (~200 write requests/sec per user). If you see repeated
   rate-limit errors, lower Req/sec; extra workers simply wait for tokens.
3. Click **Migrate selected**. Progress streams file-by-file with a running
   ok / failed / skipped count. A full audit trail is written to
   `transfer_log.csv`, and `checkpoint.json` lets you re-run to resume.

## Notes

- **Box SDK version matters.** This uses the classic `boxsdk` 3.x API
  (`Client`, `JWTAuth`). The newer `box_sdk_gen` (v10+) has a different API and
  is not compatible — `requirements.txt` pins `boxsdk<4` for you.
- Runs on `127.0.0.1` only (local machine), matching a single-user setup.
- Do **not** commit `box_config.json`, `google_credentials.json`, `token.json`,
  `checkpoint.json`, or `transfer_log.csv`. The repo `.gitignore` already
  excludes them.
```
