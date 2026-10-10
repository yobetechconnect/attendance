# Deploying to Railway (step by step)

You need: a GitHub (or GitLab) account, a Railway account, and this project.
You do **not** need to install PostgreSQL on your computer.

Railway services used: **2** — the web app and one PostgreSQL database — plus one **Volume**
(disk) attached to the web app for face-recognition files. Check Railway's current pricing for
your plan; the Volume and database are billed by usage.

## 1. Put the code in Git
```
git init && git add . && git commit -m "Attendance system"
git branch -M main
git remote add origin <your repo URL> && git push -u origin main
```
`.gitignore` already keeps `.env`, databases and `storage/` (face data) out of Git. Never commit those.

## 2. Create the project on Railway
1. Railway dashboard → **New Project → Deploy from GitHub repo** → pick the repository.
2. In the same project: **+ New → Database → Add PostgreSQL**.
3. The first deploy will fail until the variables below exist. That is expected.

## 3. Variables (web service → *Variables* tab)
| Variable | Value |
|---|---|
| `DATABASE_URL` | click **Add Reference** → choose the Postgres service's `DATABASE_URL` |
| `SECRET_KEY` | 32+ random characters: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `FLASK_ENV` | `production` |
| `DATA_DIR` | `/data` (the Volume mount path, step 4) |
| `TZ` | `Africa/Lagos` |
| `SETUP_TOKEN` | a one-time random string for creating the Super Administrator (step 6) |

Optional: `FACE_SAMPLES_REQUIRED` (default 20), `FACE_RECOGNITION_THRESHOLD` (default 65), `DB_POOL_MAX` (default 8), `SESSION_HOURS` (default 12).

The app refuses to start in production if `SECRET_KEY` is weak or `DATABASE_URL` is missing.

## 4. Persistent storage for face recognition (Volume)
Web service → **Settings → Volumes → Add Volume**, mount path `/data`. Without it, enrolled faces are
stored on temporary disk and **are erased on every redeploy** (the Super Administrator dashboard shows a warning
until `DATA_DIR` is set). Size: each enrolled student uses roughly 0.5–1 MB of samples plus a shared model file;
start with a few GB and watch usage.

## 5. Deploy
`railway.toml` (already in the repo) tells Railway to
* run `python -m flask --app app db-migrate` before each release (applies *pending* migrations only — never deletes data),
* start with `gunicorn 'app:create_app()'` (2 workers × 4 threads),
* health-check `/healthz`.

Push to `main` → Railway builds and deploys. Open the generated URL (Settings → Networking → *Generate Domain*; it is HTTPS).
If your Railway start command does not expand `$PORT`, set the start command in the dashboard to the same text inside `sh -c "…"`.

## 6. Create the Super Administrator (no default password exists)
Open `https://<your-domain>/auth/setup`, enter `SETUP_TOKEN`, your name, email and a strong password.
That page disappears once the account exists. Then **delete the `SETUP_TOKEN` variable**.
(Alternative: `railway run python -m flask --app app create-super-admin`.)

## 7. Move your existing SQLite data to PostgreSQL (only if you have real records)
On your computer, from the project folder:
```
pip install -r requirements.txt
# Railway → Postgres service → Variables → copy DATABASE_PUBLIC_URL
export DATABASE_URL="<DATABASE_PUBLIC_URL>"          # Windows PowerShell: $env:DATABASE_URL="..."
python scripts/dbtool.py import-sqlite --source database/attendance.db
```
It copies every table with the **same ids** (so face data stays matched), in a single transaction, then prints
source-vs-target row counts. It **refuses to run if the target already has data** and never deletes anything.
Do this *before* creating the Super Administrator in step 6 (your old super admin comes across with the data).
Then copy `storage/face_data/` to the Volume (Railway CLI: `railway ssh`, or simply re-enroll faces).
Any old `school_admin` accounts are reported by the tool: that role no longer has privileges — only the Super Administrator is unrestricted.

## 8. Go-live checklist
- [ ] `/readyz` returns `{"status":"ready"}`
- [ ] Super Administrator can sign in; `/auth/setup` returns 404
- [ ] Temporary Administrators 1 and 2 created; each signs in and is forced to change the password
- [ ] A test student + parent created; parent sees only that child
- [ ] Logo URL saved in Settings and showing on the login page
- [ ] Volume attached, `DATA_DIR=/data`, dashboard warning gone
- [ ] **Face recognition tested on the real Railway URL from the real classroom device** (camera needs HTTPS — Railway provides it). Enroll one student, take attendance, redeploy, confirm the student is still recognised
- [ ] A backup taken **and a restore test completed** (see BACKUP_RESTORE.md)
- [ ] `SETUP_TOKEN` deleted

## Updating later
Push to `main`. Pending migrations run automatically; existing records and the Volume are untouched.
