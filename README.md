# DOI Services

Persistent identifiers and preservation copies for articles published in our OJS journals.

```
https://doi-services.com/1001/3101
                 │    └── article number (= OJS article id, frozen at import)
                 └─────── journal number (assigned here, unique across all OJS installs)
```

Two forms of the same ID:

| Link | Where it's used | Opens |
|---|---|---|
| `https://doi-services.com/1001/3101` | shared, cited, copied from `/admin` | always our page: metadata and the preserved PDF |
| `https://doi-services.com/1001/3101?redirect` | shown on the article page in OJS (written there by the plugin) | the article on OJS if its page answers right now (live check, 4 s timeout, result cached 2 min), otherwise our page |

### When an address changes

- **The whole install moved** (new domain or folder): `/admin` → the install → **Edit** → new address. It checks the new address has the same journals, switches all stored links at once, and re-syncs every article. No ID changes.
- **A journal's path changed** in OJS: nothing to edit. The next sync (nightly, or **Sync now**) recognises the journal by its internal OJS id, keeps its number and re-syncs its articles.

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
| `app/sync.py` `push_identifiers` | VPS | Writes each article's ID back into OJS (the plugin stores it and shows it on the article page) |
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
python -m app.cli push-ids SOURCE_ID             write article IDs into OJS now
python -m app.cli check-health [SOURCE_ID]
```

In production, prefix with `docker compose exec app`.

## Deploy to the VPS (Ubuntu 24.04)

1. **DNS** (hPanel → Domains → doi-services.com → DNS): `A` records for `@` and `www` → the VPS IP. Delete any `AAAA` records (or point them at the VPS's IPv6), otherwise the HTTPS certificate can fail.
2. **Docker**: hPanel → VPS → Docker Manager → Install, or over SSH: `curl -fsSL https://get.docker.com | sh`.
3. **Firewall**: `ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable` (and if the hPanel VPS firewall is on, allow 22, 80, 443 there too).
4. **Code** (the repo is private, so give the server a read-only deploy key):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C "doi-services vps"
   cat ~/.ssh/github_deploy.pub   # GitHub → repo → Settings → Deploy keys → Add (leave write access off)
   printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n' >> ~/.ssh/config
   git clone git@github.com:ZainAshraf005/ojs-integration.git /opt/doi-services && cd /opt/doi-services
   ```
5. **Settings**:
   ```bash
   cp .env.example .env && chmod 600 .env
   sed -i "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=$(openssl rand -hex 32)|; s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
   nano .env   # check PUBLIC_BASE_URL=https://doi-services.com and SITE_DOMAIN=doi-services.com
   grep APP_SECRET_KEY .env   # save this in your password manager
   ```
6. **Start**: `docker compose up -d --build`, then `docker compose logs -f caddy` until the certificate is obtained, and open https://doi-services.com/healthz.
7. **Admin login**: `docker compose exec app python -m app.cli create-user you@example.com`, then add the OJS install at https://doi-services.com/admin.
8. **Database backups** (PDFs are copied off-site by the worker once `S3_*` is set):
   ```bash
   mkdir -p /opt/backups
   echo '30 3 * * * root cd /opt/doi-services && docker compose exec -T db pg_dump -U archive archive | gzip > /opt/backups/db-$(date +\%F).sql.gz && find /opt/backups -name "db-*.sql.gz" -mtime +14 -delete' > /etc/cron.d/doi-services-backup
   ```

**Updating later**: `cd /opt/doi-services && git pull && docker compose up -d --build`.

> Keep `APP_SECRET_KEY` safe (password manager). It encrypts the stored OJS API keys.
>
> OJS API keys are not configured in `.env`: each install's key is entered in `/admin` when adding it (or later with **Edit** on the install's page), stored encrypted in the database, and never shown again.

## Install the OJS plugin (once per OJS install)

The install's page in `/admin` has a **Download plugin** button and these same steps with that install's config block filled in.

1. Download `archiveSync.tar.gz` from `/admin` (or build it: `tar -czf archiveSync.tar.gz -C ojs-plugin archiveSync`).
2. In OJS, log in as admin, open any journal → **Settings → Website → Plugins → Upload a New Plugin**, and upload `archiveSync.tar.gz`. It installs for the whole installation, so one upload covers all journals in that install.
3. In Hostinger File Manager, add the block shown on the install's page in `/admin` (or printed by `plugin-config ID`) to the end of that install's `config.inc.php`:
   ```ini
   [archivesync]
   webhook_url = "https://doi-services.com/hooks/ojs/1"
   webhook_secret = "…"
   ```
   Optionally add `label = "Permanent link"` to change the heading shown above the ID on article pages.
4. Publish (or unpublish and republish) a test article and check **/admin → Sync log** for a `webhook` entry, then (about a minute later) an `ojs-id` entry. The article page in OJS now shows the ID.
5. For articles imported before the plugin was installed or upgraded: click **Sync now** on the install's page (or `python -m app.cli push-ids SOURCE_ID`; the nightly reconcile also does this).

**Upgrading from 1.0:** upload the new `archiveSync.tar.gz` the same way; OJS offers to upgrade it. The config block stays as it is.

### How the ID gets into OJS

After every sync the worker sends the IDs OJS doesn't have yet to `/index.php/{journal}/archivesync/identifiers`, signed with the same secret as the webhooks. The plugin stores each one on the article (every version) as `archiveId` and shows it under the article details on the article page (default theme, or any theme that keeps the `Templates::Article::Details` hook). It is a separate field: OJS's own DOI field is not touched. The archive notes when OJS confirmed it (article page in /admin → "ID in OJS"), and the next sync re-sends it if it ever disappears from OJS.

Without the config block the plugin only shows IDs it already has. If the archive is unreachable, OJS is not affected and the nightly reconcile catches up.

## ID rules

- Journal numbers start at 1001 and are assigned in discovery order. They never change, even if the OJS path or install changes.
- The article number is the OJS submission id at first import. If a different submission ever claims the same number in a journal, it is **not** overwritten; a `conflict` entry appears in the sync log.
- Unpublished or deleted articles are marked `withdrawn` and keep their preserved copy and identifier.
- Author emails and ORCID tokens returned by the OJS API are never stored.
