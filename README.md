# SCH Archive

Persistent identifiers and preservation copies for articles published in our OJS journals.

```
https://sch.org/1001/3101
                 │    └── article number (= OJS article id, frozen at import)
                 └─────── journal number (assigned here, unique across all OJS installs)
```

The link redirects to the article on OJS while OJS is healthy, and shows our own page with the metadata and the preserved PDF when it isn't. Add `?archive` to always see the preserved copy.

## How it works

```
OJS (Hostinger) ──① signed webhook──► FastAPI /hooks/ojs/{source} ──► Redis queue ──► arq worker
      ▲                                                                                 │
      └──────────────② REST API + public PDF download ──────────────────────────────────┘
                                                                                         ▼
                                              PostgreSQL (metadata)  +  data/files/ab/cd/<sha256>.pdf
                                                                                         │ ③ optional
                                                                                         ▼
                                                                          Backblaze B2 (off-site copy)
```

| Piece | Where | What it does |
|---|---|---|
| `ojs-plugin/archiveSync` | each OJS install | On publish/unpublish, POSTs a small HMAC-signed notification |
| `app/routes/hooks.py` | VPS | Verifies the signature, queues a sync |
| `app/sync.py` | VPS | Fetches metadata and galleys, assigns IDs, stores files (idempotent) |
| `app/worker/main.py` | VPS | Background jobs and schedules (see below) |
| `app/routes/public.py` | VPS | Resolver, landing pages, search, file downloads |
| `app/routes/admin.py` | VPS | Dashboard at `/admin`: OJS installs → journals → articles (copy ID, open PDF), add installs, sync buttons, activity log |

Scheduled jobs: **health checks** every 10 min, **reconcile** nightly at 02:00 (catches missed webhooks; the first one on a source is the backfill), **checksum audit** weekly, **off-site replication** hourly if configured.

## Local development

```bash
cp .env.example .env              # set APP_SECRET_KEY (openssl rand -hex 32)
docker compose up -d db redis
uv sync
uv run alembic upgrade head
uv run python -m app.cli create-user you@example.com  # your dashboard login
uv run uvicorn app.main:app --reload                   # terminal 1: http://localhost:8000/admin
uv run arq app.worker.main.WorkerSettings              # terminal 2: background worker (imports, sync, health)
uv run pytest
```

Then in the dashboard: **+ Add OJS install** → name, URL, API key. The import starts by itself.

## CLI

```
python -m app.cli create-user EMAIL              admin login (also resets a password)
python -m app.cli add-source NAME BASE_URL       register an OJS install; prints the plugin config
python -m app.cli list-sources
python -m app.cli plugin-config SOURCE_ID        print the config.inc.php block again
python -m app.cli backfill SOURCE_ID [--journal PATH]   import now with progress
python -m app.cli queue-backfill SOURCE_ID       let the worker do it
python -m app.cli sync-article SOURCE_ID PATH ID
python -m app.cli check-health [SOURCE_ID]
```

In production, prefix with `docker compose exec app`.

## Deploy to the VPS (Ubuntu 24.04)

1. **DNS**: in hPanel, add an `A` record for `sch.org` (or a subdomain) pointing to the VPS IP.
2. **Server basics** (as root):
   ```bash
   apt update && apt upgrade -y
   curl -fsSL https://get.docker.com | sh
   ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
   ```
3. **App**:
   ```bash
   git clone <repo> /opt/sch-archive && cd /opt/sch-archive
   cp .env.example .env && nano .env    # APP_SECRET_KEY, PUBLIC_BASE_URL, SITE_DOMAIN, POSTGRES_PASSWORD
   docker compose up -d --build
   docker compose exec app python -m app.cli create-user you@example.com
   docker compose exec app python -m app.cli add-source "Main OJS" https://studentsconsultancyhome.com/journal
   docker compose exec app python -m app.cli queue-backfill 1
   ```
   Caddy obtains the HTTPS certificate automatically once DNS points to the server.
4. **Backups**: nightly `pg_dump` plus the `files` volume to B2. Set the `S3_*` variables to have the worker replicate every PDF automatically.

> Keep `APP_SECRET_KEY` safe (password manager). It encrypts the stored OJS API keys.

## Install the OJS plugin (once per OJS install)

1. Build the package: `tar -czf archiveSync.tar.gz -C ojs-plugin archiveSync`
2. In OJS, log in as admin, open any journal → **Settings → Website → Plugins → Upload a New Plugin**, and upload `archiveSync.tar.gz`. It installs for the whole installation, so one upload covers all journals in that install.
3. In Hostinger File Manager, add the block printed by `add-source` (or `plugin-config ID`) to the end of that install's `config.inc.php`:
   ```ini
   [archivesync]
   webhook_url = "https://sch.org/hooks/ojs/1"
   webhook_secret = "…"
   ```
4. Publish (or unpublish and republish) a test article and check **/admin → Sync log** for a `webhook` entry.

Without the config block the plugin does nothing. If the archive is unreachable, OJS is not affected and the nightly reconcile catches up.

## ID rules

- Journal numbers start at 1001 and are assigned in discovery order. They never change, even if the OJS path or install changes.
- The article number is the OJS submission id at first import. If a different submission ever claims the same number in a journal, it is **not** overwritten; a `conflict` entry appears in the sync log.
- Unpublished or deleted articles are marked `withdrawn` and keep their preserved copy and identifier.
- Author emails and ORCID tokens returned by the OJS API are never stored.
