# Offline attendance (Stage 1: manual attendance)

This adds an installable offline app for teachers to the existing system.
Teachers can open their prepared classes, mark Present / Absent (also Late and
Excused), save, and the records upload automatically when the server can be
reached again. Nothing existing was removed or renamed.

Stage 2 (offline face recognition) and Stage 3 (offline face enrollment) are
built **except for the neural network itself**, which is not included and
could not be obtained in the build environment. Until a model is installed the
app says so and uses manual attendance. See "Offline face recognition" below.

## How it works

```
Teacher's phone                                    Railway
┌──────────────────────────────┐        ┌───────────────────────────────┐
│ Service worker (app shell)   │        │ Flask (unchanged pages)       │
│ IndexedDB (per teacher):     │        │ /offline-api/ping             │
│   classes, rosters           │ <----- │ /offline-api/bootstrap        │
│   draft + saved sheets       │ -----> │ /offline-api/sync  (1 session │
│   sync status / retry info   │        │   = 1 DB transaction)         │
│ Sync engine + status bar     │        │ PostgreSQL (authoritative)    │
└──────────────────────────────┘        └───────────────────────────────┘
```

* The browser never talks to the database. It only calls the three JSON
  endpoints above, with the normal session cookie and the existing CSRF token.
* `/offline-app/` is a static shell that is identical for every visitor (no
  names, no tokens), so keeping it in the browser cache exposes nothing.
* The service worker caches **only** that shell and its static files. It never
  caches signed-in pages or any API response.

## Teacher instructions

1. **Prepare the phone (needs internet once).** Sign in. The classes you teach
   are downloaded automatically. Open **Offline Attendance** in the menu and
   check your classes show "ready offline". Tap "Refresh classes" any time.
2. **Install it (recommended).** In Chrome choose "Install app" / "Add to Home
   screen". On iPhone use Safari, Share, "Add to Home Screen". Installing also
   protects saved data from being cleared by the browser.
3. **Take attendance (works offline).** Open the class, tap Present or Absent
   for each student (the "..." menu gives Late / Excused). Marks are saved on
   the phone as you tap. Tap **Save attendance**. Students you did not mark are
   saved as Absent (you are asked to confirm).
4. **Syncing is automatic.** When the server can be reached the sheet is sent.
   The status pill (top right) shows what is happening. A sheet is only shown
   as **Synchronized** after the server has confirmed it.
5. **Signing out.** If sheets have not synced yet you are warned. They stay on
   the phone (locked) and upload after you sign in again.

## Connection and sync indicator

| Colour | Meaning | Message |
|---|---|---|
| Green | Server reachable | You're online. Your connection to the server is working. |
| Blue | Server not reachable | You're offline. You can continue working. Your records will synchronize when your connection returns. |
| Orange | Uploading | Synchronizing your records. Please keep the application open until synchronization is complete. |
| Green | Upload confirmed, nothing pending | Synchronization completed successfully. All pending records are saved online. |
| Red | Server reachable but upload failing | Synchronization failed. Your local records are safe. We will retry automatically. |
| Red | Session expired with records waiting | Sign-in required. Your records are safe on this device. Sign in again to synchronize them. |

"Online" means a request to our own server succeeded **and** the server could
reach its database (`/offline-api/ping`). It does not rely on
`navigator.onLine`. Probes: at start, when the browser reports a change, when
the app returns to the foreground, every 45 s while healthy, and with
exponential back-off (5 s up to 60 s) while unreachable. At most one is in
flight and never more than one every 3 s. The tap-to-open panel shows
separate indicators: network, server, local database, pending records, items
needing attention, last successful sync, face-recognition engine, app version.

The green "synchronized" state can never appear while any record is still
pending or failed.

## When synchronization runs

App open, browser "online" event, a probe that flips unreachable to reachable,
app returning to the foreground, right after a sheet is saved, a retry timer
(exponential back-off with jitter, 5 s doubling to 15 min), and the "Sync now"
button. Background Sync is registered where supported, but it only wakes an open
page; **it is never the only trigger** (iOS does not support it).

If the app is closed mid-upload, the sheet is put back in the queue on next
open and re-sent. This is safe because the server is idempotent.

## Server rules

**Never trust the client.** On every upload the server re-checks that the
signed-in account is the teacher of that class/subject (and the assignment is
still active), that each student is active and in that class, and every value.

**Idempotent.** Every session and record has a UUID created on the phone.
`sync_receipts` stores the server's decision per UUID. Re-sending the same data
returns the stored decision and creates nothing. A UUID cannot be reused by
another account.

**Atomic.** One session and all its records are saved in one database
transaction (no half-saved sessions). A retry after a crash is safe.

**One sheet per class, subject and school day.** Two phones for the same
class/subject/day are merged into one server session instead of creating a
duplicate. The date comes from the school timezone in Settings, not the
phone's clock; a date more than one day ahead of the school's today is
rejected.

### Conflict policy (per student)

| Situation | Result |
|---|---|
| Same status already recorded | Duplicate, nothing to review |
| Existing record is only the automatic "absent" written when a session closed, and the teacher marked present/late/excused | Teacher's mark replaces it, with a correction row and an audit entry |
| Existing record is automatic (face / QR / RFID) and the offline mark is manual | Existing kept. **A manual mark never overwrites an automatic one.** Offline mark stored for review |
| Two different explicit marks | First committed mark kept. Later one stored for review |
| Student not in the class any more, or inactive | That record rejected and stored for review; the rest of the session is saved |
| Teacher no longer assigned to the class | Whole session rejected and stored for review |

Nothing is silently discarded. Reviewable items appear in **Sync Conflicts**
(Super Administrator menu) with both versions, the teacher and the device. The
administrator can "Use offline mark" (applied as an audited correction), "Keep
server" or "Dismiss". The phone shows such records as "Conflict, under review"
and they are not counted as pending.

## Offline authorization policy

* A phone is "prepared" for an account after a successful online download.
* Offline use is allowed until **72 hours** after the last time the server
  confirmed the session (`OFFLINE_GRACE_HOURS`). After that the offline app is
  locked until the teacher signs in online. **Unsynced records are never
  deleted** by expiry.
* The server re-validates permissions on every upload. Changes made by an
  administrator while a teacher was offline take effect at sync time (see the
  rejection rules above).
* No password, token or CSRF value is stored on the phone. The server session
  cookie stays in the browser's normal cookie store.
* Data is stored per account. The sync engine only uploads the signed-in
  account's records. If another account's unsynced records are on the phone, a
  warning explains that they must be synced by that teacher.
* Sign-out erases everything for that account if nothing is unsynced. Otherwise
  it deletes the downloaded rosters and keeps only the unsynced sheets (locked).
* Only the minimum is downloaded: the teacher's own active assignments and the
  active students of those classes (id, admission number, name, and a
  true/false "has LBPH face data" flag). No phone numbers, guardians or face
  data.

## Database migration

`database/migrations/0002_offline_sync.sql` is **additive only**:

* `attendance_sessions`: new columns `client_uid`, `source` (default `online`),
  `device_uid`, `synced_at`.
* `attendance_records`: new columns `client_uid`, `source` (default `online`),
  `device_uid`, `client_recorded_at`, `synced_at`.
* New tables: `client_devices`, `sync_receipts`, `sync_conflicts`,
  `face_templates` (empty until Stage 2).
* Partial unique indexes on the new `client_uid` columns (old rows have none).

`database/migrations/0003_face_offline.sql` is also additive: consent and
deactivation columns on `face_templates`, a partial unique index allowing only
one active template per student and engine, and three audit columns on
`attendance_records` (`match_engine`, `match_engine_version`,
`match_template_uid`). It touches no LBPH data.

Nothing is dropped, renamed or rewritten. Existing rows get `source = 'online'`.
It runs through the existing migration runner, which already runs before each
Railway deploy (`railway.toml`). **Back up first** with your existing
`scripts/dbtool.py backup`. To undo the code, redeploy the previous version:
the extra columns and tables are ignored by it and can stay.

## Railway deployment

1. Back up the database (`scripts/dbtool.py backup`).
2. Copy the files listed in the hand-over message into your project and push.
3. Railway redeploys; the pre-deploy migration applies `0002`.
4. Optional variables: `OFFLINE_GRACE_HOURS` (default `72`). Face variables
   (`FACE_OFFLINE_*`) stay unset until a model is installed; with them unset the
   face feature is off and nothing changes for users.
5. No new Python dependency and no new service. HTTPS is already provided by
   Railway (required for service workers and, later, the camera).

After deploying, open the site on a phone, sign in as a teacher, and open
**Offline Attendance**. If the old service worker or an older page is cached,
a "newer version is ready, Reload now" banner appears.

## Updates and versions

`OFFLINE_APP_VERSION` in `routes/offline.py` is the cache version. Change it
whenever any file in the service worker's list changes. A new service worker
installs in the background and waits; the page offers "Reload now" so a teacher
is never refreshed mid-attendance. Saved attendance lives in IndexedDB, which
updates never touch; future schema changes only add stores
(`onupgradeneeded` never recreates or drops existing ones). A phone that is
offline on an older version shows "You are using an older version".

## Offline face recognition (Stage 2) and enrollment (Stage 3)

### What is built and tested
Everything except the model: template storage, encryption, matching rules,
quality checks, camera handling, enrollment, consent, upload, server
validation, conflict handling, admin review, retention, and the screens.

### What is NOT included: the model
Face recognition needs a detector and an embedding network (a few MB). They
could not be downloaded here (no internet) and are not on this machine. **No
face is ever recognized until you install a model.** Nothing is faked: with no
model the status says "The face-recognition model is not installed on the
server." and manual attendance is used. How to install one is in
`static/models/face/README.md` (folder layout, `manifest.json`, the
`engine.js` contract) and the contract at the top of `att-face.js`.

The synthetic "engine" in the tests is a test double that maps an id to a
vector. It proves the plumbing (matching rules, quality gate, sync), **not**
recognition accuracy. Recognition accuracy, speed on your phones, and the
quality thresholds have **not been measured** and must be tested with the
chosen model on your real devices before relying on it.

### Turning it on
1. Put the model package in `static/models/face/<version>/` (see README there).
2. Set Railway variables so they match the manifest exactly:
   `FACE_OFFLINE_ENGINE`, `FACE_OFFLINE_ENGINE_VERSION`,
   `FACE_OFFLINE_EMBEDDING_DIM`. Leave them empty to keep the feature off.
3. Calibrate and set `FACE_OFFLINE_MIN_SIMILARITY` (default 0.60 is only a
   placeholder; it depends on the model), `FACE_OFFLINE_MARGIN` (default 0.05),
   `FACE_OFFLINE_MIN_SAMPLES` (default 5).
   Calibrate by enrolling real students, then measuring how often the right
   student is accepted and, more importantly, how often a wrong one is. Raise
   the threshold until wrong accepts are essentially zero.
4. Increment `OFFLINE_APP_VERSION` and deploy. Migration `0003` runs
   automatically (additive).
5. In the app: Super Administrator opens Offline Attendance, enrolls students;
   teachers tap "Download face data" for their class and "Download model for
   offline use" once while online.

### How a phone decides (Stage 2)
1. Camera frame, then **detection** (engine), then **quality gate** (one face,
   big enough, centred, not too dark/bright/blurry; numbers are starting points
   to calibrate), then optional **liveness** (engine-provided), then
   **embedding** (engine), then **matching** against the class's templates.
2. A student is marked only if the best similarity is at least the threshold
   **and** beats the next different student by the margin, **and** the same
   student is seen on consecutive frames. Otherwise the screen says: no face,
   more than one face, too small/dark/bright/blurry, not recognized (may not be
   enrolled), confidence too low, not certain, already marked, model
   unavailable, camera unavailable. **Uncertain is never marked.**
3. Duplicates are ignored on the phone and again on the server.
4. Manual attendance is always one tap away. A manual tap on a face-marked
   student needs an explicit confirmation, and on the server a manual mark can
   never replace a face record.
5. A review panel lists attempts that were not accepted (counts only, no
   images) and students with no face data.
6. Face records sync with their evidence: engine, version, template id,
   confidence. Nothing is sent per scan; the phone never calls the server to
   recognise a face.

### Enrollment (Stage 3, Super Administrator only, same as the existing pages)
1. Offline Attendance, "Face enrollment" (student list is downloaded while
   online), pick a student.
2. **Consent is required** (guardian consent and "manual attendance remains an
   alternative"), plus who gave it. Without both, the camera does not start.
3. Camera captures several quality-checked samples (switching to another
   person is detected), builds one averaged template, and checks the samples
   are consistent. Only a template (numbers) is kept; **no photograph is
   stored or uploaded**. The camera is switched off as soon as capture ends,
   and whenever the page is hidden or closed.
4. The template is encrypted and saved on the phone, "saved on this device" is
   shown only after that commit, and it uploads automatically when possible.
   Status: waiting, uploading, uploaded, failed (retries), not applied (student
   already enrolled, admin review), rejected.
5. The server allows **one active template per student and engine** (enforced
   by the database). A second enrollment is not applied unless "Replace" was
   chosen; otherwise it goes to **Sync Conflicts** where an administrator can
   "Use offline mark" to replace the old one (the old one is kept as inactive).
6. Existing OpenCV (LBPH) enrollments are untouched and keep working online.
   They cannot be converted; students need a new enrollment for offline use.

### Protecting face data on the phone
* Templates and unsent enrollments are encrypted (AES-GCM) with a key derived
  from a PIN/passphrase (PBKDF2, 310,000 rounds). The key is never stored or
  sent. A wrong PIN is slowed down after 5 tries.
* **Honest limit:** a short PIN can be guessed by someone who copies the phone's
  browser storage. This stops casual access to a lost or shared phone, not a
  determined forensic attacker. Prefer a longer passphrase.
* Forgot the PIN: the face data is erased and downloaded again. Face enrollments
  that were not uploaded yet cannot be recovered; the app warns before erasing.
* Sign-out removes downloaded templates and the student list. Unsent
  enrollments stay (encrypted) until uploaded.
* While the PIN is locked, enrollments cannot upload; the app shows an
  "Unlock" prompt.
* Teachers can download templates only for classes they are assigned to; every
  download and every refused attempt is audit-logged; responses are never
  cached.

### What the server can and cannot verify
It validates the engine/version, template size and shape, consent, role,
class assignment, the similarity floor, and that a claimed match refers to a
real template of that same student. It **cannot** check that a phone really
computed the embedding it claims, so a deliberately modified app could lie.
This is why each face record carries device, account, engine, template and
confidence, why teachers are authenticated, and why conflicts are reviewable.
Presentation-attack (photo/screen) protection exists only as a hook for a
liveness model: **none is included, so a photo of a student may fool the
system**. Do not describe it as spoof-proof.

### Children's data: consent, alternatives, retention
* Consent is mandatory and recorded with each template (who, and that it was
  confirmed). Collecting the guardian's consent properly is the school's
  responsibility; the app only enforces that it was recorded.
* Manual attendance is always available as the alternative.
* **Admin page "Offline Face Data"** lists templates and lets a Super
  Administrator deactivate or permanently delete one (audit-logged). Deleting
  does not touch attendance history or the OpenCV enrollment.
* **Retention command:**
  `flask --app app purge-face-templates --days 365` (preview) and `--apply` to
  delete inactive templates and those of students who left, older than N days.
  Run it on a schedule that matches the school's policy.

### Prepared extension points
Adding a model is a drop-in: nothing else in the app changes. A new model
version is a new folder and new `FACE_OFFLINE_ENGINE_VERSION`; phones refuse a
mismatched model, old templates are no longer served (students re-enroll),
and old templates are kept inactive until retention removes them.

## Tests

```
python -m unittest discover -s tests            # server + real-client-vs-real-server tests (needs Node)
for f in tests/js/*.test.js; do node --test --test-force-exit $f; done   # browser logic + UI smoke tests
```
(Run the Node files one at a time: the UI tests share global state.)

## Known limitations

* **No face model is included, so offline face recognition does not work yet.**
  Accuracy, speed and thresholds are unmeasured. See above.
* **Not verified on a real phone or browser.** The service worker, IndexedDB
  adapter, install prompt, camera, CSS layout and touch behaviour have not run
  in a real browser. Logic is tested with an in-memory store, a simulated DOM
  and a simulated camera.
* **PostgreSQL not run.** Migrations and queries use portable SQL, but only
  SQLite was executed.
* **No liveness / photo-attack protection** is included (hook only).
* A short PIN protects face data only against casual access (see above). Basic
  manual-attendance data on the phone is not encrypted; it depends on the
  phone's lock screen and the offline policy.
* The phone's clock decides whether the 72-hour window has passed; the server
  still checks everything at sync time.
* iPhone Safari may clear web data of sites not opened for about 7 days unless
  installed to the Home Screen. Install the app.
* An account deactivated while offline cannot sync; records stay on the phone
  until an administrator re-enables it.
* Enrollment uploads need the face-data PIN to be unlocked in the open app;
  background sync cannot read them.
* Offline attendance is a new sheet per class/subject/day. It cannot be attached
  to a session started online from the same phone; the two are reconciled by
  the conflict rules.
* Only Super Administrator can enroll (as before). Temporary administrators
  cannot enroll offline.
