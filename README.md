# box-to-gdrive

A command-line tool for migrating files and folders from **Box.com** to a **Google Drive Shared Drive**, preserving the folder hierarchy.

Files are streamed directly from Box to Google Drive (no local disk staging), with support for resuming interrupted runs, parallel transfers, a CSV audit log, and automatic retry with exponential backoff on rate limits.

## Features

- **Preserves folder structure** — recreates the Box folder tree inside the destination Shared Drive.
- **Checkpointing / resume** — records completed files in `checkpoint.json`; re-running skips what's already done.
- **Parallel uploads** — configurable worker pool (`--workers`) for faster transfers.
- **CSV transfer log** — every file logged to `transfer_log.csv` with status, size, IDs, and any error.
- **Exponential backoff** — automatically retries transient errors and Drive rate limits (429 / retryable 403 / 5xx).
- **Dry run** — preview exactly what would be copied without transferring anything.

## Requirements

- Python 3.8+
- A Box app (JWT server auth) or a Box developer token
- A Google Cloud OAuth client with the Google Drive API enabled

## Installation

```bash
git clone https://github.com/angeleenmcw/box-to-gdrive.git
cd box-to-gdrive

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

## Setup

### 1. Box credentials

**Option A — JWT app (recommended for server-to-server):**

1. Create a Box app at <https://app.box.com/developers/console>.
2. Choose **Custom App → Server Authentication (JWT)**.
3. Download the config JSON and save it as `box_config.json` in the project folder.
4. In the Box Admin Console, authorize the app under **Apps → Custom Apps Manager**.

**Option B — Developer token (quick testing only, expires after 60 min):**

```bash
export BOX_DEV_TOKEN="your_token"     # Windows: set BOX_DEV_TOKEN=your_token
```

### 2. Google credentials

1. In the Google Cloud Console, enable the **Google Drive API**.
2. Create an **OAuth client ID** of type **Desktop app**.
3. Download it as `google_credentials.json` in the project folder.
4. On first run, a browser window opens to authorize access; the resulting token is cached to `token.json`.

> **Note:** `box_config.json`, `google_credentials.json`, and `token.json` are listed in `.gitignore` and must never be committed.

## Usage

Preview a migration without transferring anything:

```bash
python box_to_gdrive.py \
    --box-folder 0 \
    --shared-drive-id 0AABBccDDeeFFgg \
    --box-config box_config.json \
    --dry-run
```

Run the migration with 8 parallel workers:

```bash
python box_to_gdrive.py \
    --box-folder 0 \
    --shared-drive-id 0AABBccDDeeFFgg \
    --box-config box_config.json \
    --workers 8
```

Resume an interrupted run — just run the same command again. Completed files are skipped automatically via `checkpoint.json`:

```bash
python box_to_gdrive.py --box-folder 0 --shared-drive-id 0AABBcc... --box-config box_config.json
```

Start fresh, ignoring any existing checkpoint:

```bash
python box_to_gdrive.py ... --reset
```

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `--box-folder` | Box folder ID to export (`0` = root) | `0` |
| `--shared-drive-id` | Google Shared Drive ID (destination) — **required** | — |
| `--dest-folder-id` | Folder ID inside the Shared Drive to copy into | Shared Drive root |
| `--box-config` | Path to Box JWT config JSON | — |
| `--box-dev-token` | Use the `BOX_DEV_TOKEN` env var instead of JWT config | off |
| `--google-creds` | Path to Google OAuth credentials JSON | `google_credentials.json` |
| `--workers` | Number of parallel upload workers | `4` |
| `--checkpoint` | Path to the checkpoint file | `checkpoint.json` |
| `--log` | Path to the CSV transfer log | `transfer_log.csv` |
| `--reset` | Delete the existing checkpoint and start fresh | off |
| `--dry-run` | List what would be copied without transferring | off |

## Finding your IDs

- **Box folder ID** — open the folder in Box; the ID is the number at the end of the URL (`app.box.com/folder/123456` → `123456`). The root is `0`.
- **Shared Drive ID** — open the Shared Drive in Google Drive; the ID is the string after `/drive/folders/` in the URL.

## Output files

| File | Purpose |
|------|---------|
| `checkpoint.json` | Tracks completed file IDs and the Box→Drive folder map; enables resume. |
| `transfer_log.csv` | Per-file audit log: timestamp, status, IDs, size, and errors. |

Both are regenerated as needed and are excluded from version control.

## Notes & limitations

- **Box native files** are transferred in their stored format. Box does not offer Google-Docs-style export conversions.
- If a run fails partway through, re-run the same command to retry only the failed/remaining files.
- If you hit repeated `403` rate-limit errors, lower `--workers`. Backoff will handle occasional limits automatically.
- Genuine permission errors (non-rate-limit 403s) fail fast and are recorded in the log rather than retried.
