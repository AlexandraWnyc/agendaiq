# AgendaIQ v6 — Deploy Guide (Render.com demo)

Goal: stand up AgendaIQ as a live, shared URL your team can open without installing anything. Everything below takes about an hour the first time.

---

## 1. Prerequisites

- A **GitHub account** (free)
- A **Render.com account** (free to sign up; this deployment costs about $7/mo)
- Your **Anthropic API key** (`sk-ant-…`)
- Optional: an **S3 or Backblaze B2** bucket for off-site nightly backups

---

## 2. Push the code to GitHub

From the `oca-tool` folder on your machine:

```bash
git init
git add .
git commit -m "AgendaIQ v6 — initial deploy"
git branch -M main
# create a private repo at github.com/<you>/agendaiq, then:
git remote add origin https://github.com/<you>/agendaiq.git
git push -u origin main
```

The `.gitignore` already excludes secrets and local state (DB, pdf_cache, exports). The shipped `oca_config.json` is also excluded — set SMTP creds via the Settings page in the deployed app instead.

---

## 3. Deploy on Render

1. Render dashboard → **New → Blueprint**
2. Connect your GitHub repo. Render reads `render.yaml` and proposes:
   - `agendaiq` (web service) on the Starter plan with a 1 GB persistent disk mounted at `/data`
   - `agendaiq-backup` (nightly cron) sharing the same disk
3. Render will prompt for the `sync: false` env vars. Fill in:
   - **ANTHROPIC_API_KEY** — your key
   - **APP_PASSWORD** — any shared password for the demo (e.g. `oca-demo-2026`). Anyone who knows it can open the site. Remove this env var to turn off the password gate.
   - *Backup cron only* — leave blank for now if you're not doing backups yet:
     - `BACKUP_BUCKET`, `BACKUP_ENDPOINT_URL` (Backblaze only), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`
4. Click **Apply**. First build takes 3-5 min.
5. When it says "Live," open the URL Render gives you (e.g. `https://agendaiq.onrender.com`). Browser asks for the password → enter `APP_PASSWORD`. Site loads.

---

## 4. One-time setup inside the app

- Settings → **Who am I?** → pick your name (each person does this once per browser)
- Settings → **Team Members** → add everyone who'll use the tool
- Settings → **Email Notifications** → paste Gmail SMTP app password if you want reminders
- Home → **Welcome strip → Analyze an agenda** → run your first pipeline

---

## 5. Where the data lives

Everything persistent sits on the mounted Render Disk at `/data`:

```
/data/
├── oca_agenda.db          ← the source of truth (SQLite)
├── oca_agenda.db-wal      ← write-ahead log
├── oca_config.json        ← SMTP / team config
├── pdf_cache/             ← downloaded Legistar PDFs
├── output/                ← pipeline output
└── exports/               ← Word / Excel exports
```

This disk survives redeploys, crashes, and restarts. It does **not** survive deleting the Render service itself — that's what the backup cron is for.

---

## 6. Nightly off-site backups (recommended)

The cron service in `render.yaml` runs `backup_db.py` every night at 03:00 ET. It takes a consistent SQLite snapshot, gzips it, uploads to your S3/B2 bucket, and prunes old copies (keeps 30 daily + 12 monthly).

Cheap setup with Backblaze B2 (~$0.05/mo for this data volume):

1. Create a B2 bucket, e.g. `agendaiq-backups`
2. Create an Application Key scoped to that bucket
3. In Render → `agendaiq-backup` → Environment:
   - `BACKUP_BUCKET=agendaiq-backups`
   - `BACKUP_ENDPOINT_URL=https://s3.us-east-005.backblazeb2.com` (replace with your bucket's endpoint)
   - `AWS_ACCESS_KEY_ID=<keyID>`
   - `AWS_SECRET_ACCESS_KEY=<applicationKey>`
   - `AWS_DEFAULT_REGION=us-east-005`
4. Trigger a manual run from the Render dashboard to verify the first backup lands.

S3 works identically — omit `BACKUP_ENDPOINT_URL`.

**Restore drill (do this once before going live):** download a recent `.db.gz`, gunzip, scp to your laptop, open with any SQLite viewer to confirm readable.

---

## 7. Demo → production transition

When the county approves it:

1. Replace `APP_PASSWORD` with real auth (Flask-Login + Microsoft SSO via `msal`). The existing "Acting as…" dropdown becomes a read-only badge of the logged-in user. `changed_by` on audit rows starts coming from the server session instead of the request body.
2. Move SQLite → managed Postgres (Render offers `$7/mo` Starter Postgres). `db.py` connection swap only — schema is identical via SQLAlchemy or psycopg. Built-in daily backups + point-in-time recovery come free.
3. Put the site behind your county's domain (Render supports custom domains + free Let's Encrypt TLS).

Everything that's per-user today (My Items filter, assignment, Acting-as picker) already queries by name, so multi-user behavior is "free" once auth is in — no data migration needed.

---

## 8. Local development still works

Running locally is unchanged:

```bash
pip install -r requirements.txt
python app_v6.py
```

`DATA_DIR` defaults to the source folder, so your existing `oca_agenda.db`, `pdf_cache/`, etc. are untouched. `APP_PASSWORD` is unset locally → no password prompt.

---

## 9. Rough monthly cost (demo)

| Item | Cost |
|---|---|
| Render Web Starter (512MB RAM, 1 GB disk) | $7 |
| Render Cron (free on any paid service) | $0 |
| Backblaze B2 storage (< 1 GB) | ~$0.05 |
| **Total** | **~$7.05/mo** |

Bump to Render Standard ($25/mo) if you outgrow 512 MB RAM — typical trigger is running the Analyze pipeline on a 50-item agenda with PDFs.
