# osa-deploy

Central operations handbook for **OSA**'s production
(`einteilung.hochamt.at`, Orchester-Einteilung — the scheduling/casting
system for the church musicians of Kirchenmusik St. Augustin): which repo
is for what, how production is built, and how to set it up, deploy it, and
switch between Legacy and the new stack. This repo itself contains
everything operational: Ansible playbooks, Caddy configuration, Podman
Quadlets, and (vault-encrypted) secrets.

## The repos at a glance

| Repo | What | Tech stack | Deployed as |
|---|---|---|---|
| [`osa-backend`](../osa-backend) | Backend: scheduling/casting management for church musicians | Python 3.12, FastAPI, SQLAlchemy, SQLite (Phase 1) | `osa-backend` pod |
| [`osa-frontend`](../osa-frontend) | Frontend to `osa-backend`: Vue 3 SPA | Vue 3 (`<script setup>`, TypeScript), Vite, nginx | `osa-frontend` pod |
| `osa-deploy` (this repo) | Ops: Ansible, Caddy, Quadlets, Secrets | Ansible, systemd Quadlets | doesn't run itself — configures the others |

`osa-logging` (Loki/Grafana/Fluent-Bit) is **fully retired** by this
cutover (never really used, see below) — the one remaining log viewer,
Dozzle, is now part of `osa-deploy` itself (`quadlets/logging/`).

Both app repos build their own container image via CI/CD (GitHub Actions)
and push it to `ghcr.io/kirchenmusik-st-augustin/<repo>:latest`
automatically on every merge to `main`. `osa-deploy` builds **no** images
and clones **no** app repos onto the production host — it only distributes
config and secrets and makes sure the right images are running.

## Architecture: rootless Podman on a VPS

A single VPS carries all of production. Instead of classic root Docker
containers, everything runs **rootless** under a dedicated, unprivileged
Linux user named `service` — a compromised container can't escalate
straight to host root. A second user `admin` exists only for
administrative root tasks (`sudo`), it never runs containers itself (same
security boundary as the sister project `vb-fastapi-vue`/`vb-deploy`).

- **systemd Quadlets** instead of `docker-compose`: every container/pod is
  described as a `.container`/`.pod`/`.volume` file, which `systemd --user`
  translates into a real systemd service automatically.
- **Two pods for the app:** `osa-backend-pod` (currently just the backend
  container — structured to grow an `osa-backend-pg` sidecar in Phase 2,
  without baking in any Postgres assumptions now) and `osa-frontend-pod`
  (nginx). Legacy keeps running as its own, unchanged pod
  (`einteilung.hochamt.at-pod`, from `osa/osa-einteilung.hochamt.at`).
- **A shared SQLite file instead of data migration:** Phase 1 (see
  CLAUDE.md section 3) means `osa-backend`'s schema is structurally
  identical to Legacy's. Both pods mount the same host path
  (`~/data/osa/einteilung.hochamt.at/sqlite`) — the cutover is a pure app
  swap with no data migration. **Neither may ever run writing at the same
  time** — `playbooks/flip.yml` enforces that structurally (see below),
  not just by documentation.
- **Caddy** is the only service listening publicly on port 80/443,
  terminates TLS, and reverse-proxies **path-based** (not per subdomain
  like in the sister project) onto the same domain:
  ```
  Internet
     │  :80 / :443
     ▼
  Caddy (host network)
     └─ einteilung.hochamt.at
          ├─ /logging/dozzle*  → 127.0.0.1:8081 → Dozzle (basic auth)
          ├─ /go/*             → redirect → go.hochamt.at
          ├─ /api, /api/*      → 127.0.0.1:21000 → osa-backend-pod (prefix stripped)
          └─ everything else   → 127.0.0.1:21001 → osa-frontend-pod
     └─ go.hochamt.at (short-URL service)
          └─ /*                → 127.0.0.1:21000 (with /go prefix) → osa-backend-pod
  ```
  Before the first forward flip, `einteilung.hochamt.at` instead proxies
  everything to port 8080 (Legacy) — see `config/caddy/Caddyfile.legacy`,
  a frozen snapshot of that state for the backward flip.
- **Image sourcing**: all app containers have
  `Image=ghcr.io/.../<name>:latest` + `AutoUpdate=registry`. Podman's own
  `podman-auto-update.timer` (daily) checks, pulls if needed, and restarts
  — entirely without Ansible. `osa-deploy` is only needed to distribute
  config/secrets/quadlets initially or on change, and for the flip itself.
- **Dozzle** is a read-only log viewer for all running containers,
  reachable at `einteilung.hochamt.at/logging/dozzle` behind basic auth.
- **podman-prune.timer** cleans up unused images/containers weekly.

## Prerequisites

- Ansible installed locally.
- SSH access to the host — which user is needed depends on the
  playbook/moment (same role separation as above):
  - **`root`** — only for the very first run of `setup_vps.yml` on a
    fresh host. Not needed for *regular* use of this repo: the real
    production host is already hardened via `osa/local-deployment`'s
    near-identical playbook.
  - **`admin`** — for re-runs of `setup_vps.yml` (verification,
    `--check --diff`), needs `sudo`/`become`.
  - **`service`** — for `deploy.yml` and `flip.yml` (hardcoded as
    `remote_user: service`, no `sudo` needed).
- A vault password for `secrets/*.env` (asked interactively, no
  `vault_password_file` in the repo).

## Phase 1 — VPS base configuration (only needed for a fresh setup)

```bash
ansible-playbook -i inventory/hosts.ini playbooks/setup_vps.yml -u root
# If only password login is possible:
ansible-playbook -i inventory/hosts.ini playbooks/setup_vps.yml -u root --ask-pass

# Re-run / verification (root login is disabled afterward):
ansible-playbook -i inventory/hosts.ini playbooks/setup_vps.yml -u admin --ask-become-pass --check --diff
```

The real production host is already hardened via `osa/local-deployment`'s
near-identical playbook — a first run here should only serve as
verification (`--check --diff`, expect near-zero diff), not a fresh
install.

## Phase 2 — day-2 ops: keeping secrets/quadlets/timers in sync

```bash
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --check --diff   # check first
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass                  # then apply
```

Syncs secrets, quadlet **definitions** (Caddy/Maintenance/Logging/Backend/
Frontend) and the maintenance timer, starts Frontend+Maintenance+Dozzle
(stateless, safe at any time). **Never touches which stack is currently
active, and never starts/restarts Caddy** — that's exclusively
`playbooks/flip.yml`'s job (see below). Builds no images, clones no repos,
restores no database.

### Immediate deploy (instead of waiting for the nightly auto-update run)

```bash
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-backend
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-frontend
```

Pulls the current `:latest` image and restarts the corresponding pod.
**Only meaningful if that stack is currently the active one according to
the last `flip.yml` run** — `--tags deploy-backend` while `target=legacy`
is active would bring up `osa-backend-pod` alongside Legacy.

## Forward/backward flip: `playbooks/flip.yml`

**One single, symmetric playbook for both directions** — no separate
cutover and rollback script. As long as Phase 1 holds (both stacks share
the same SQLite file, before the Postgres migration), switching in either
direction is technically identical and repeatable any number of times.
This reversibility ends structurally with Phase 2 — after that,
`target=legacy` is no longer a simple reversal, it would need a real
back-migration.

```bash
# Forward (Legacy -> new stack):
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=new --check --diff
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=new

# Backward (new stack -> Legacy), any time, as often as needed:
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=legacy --check --diff
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=legacy
```

Internally, identical for both directions (only `target` decides
source/destination):
1. Stop the other stack (Legacy pod resp. `osa-backend-pod`).
2. **Only then** start the target stack — structurally enforces that
   neither ever writes to the shared SQLite file at the same time.
3. Roll out the matching Caddyfile variant (`config/caddy/Caddyfile`
   resp. `Caddyfile.legacy`).
4. Restart `caddy.service` — the actual traffic switch.

No `--ask-vault-pass` needed — `flip.yml` never touches `secrets/*.env`,
only pod start/stop + the Caddyfile.

**Every command in this repo/README is only ever described by Claude, never
executed by it — carrying it out is exclusively manual.**

## The complete forward flip — step-by-step runbook

Considerably simpler than a full VPS reinstall, because the DB migration
deliberately runs AFTER the cutover (user decision): no ETL step, no
empty DB, no DNS change.

### One-time preparation (before the very first forward flip)

1. `osa-backend`'s backup capability merged, images built. Rehearse the
   backup/restore CLI once from a dev environment against real
   `KOOFR_USER`/`KOOFR_PASSWORD` + a local copy of the SQLite file (cleans
   itself up via the 28-day retention).
2. `config/caddy/Caddyfile.legacy` is already checked in as a snapshot of
   the state BEFORE this whole endeavor (see above) — nothing further to
   do, except never touching it again.

### Forward flip (Legacy → new stack)

1. Announce a maintenance window.
2. **Final pre-cutover backup via Legacy's own, still-running mechanism**
   (belt and suspenders, freshest possible snapshot):
   ```bash
   ssh service@<prod-host>
   podman exec einteilung.hochamt.at-fpm php artisan osa:schedule:backup-prod-database
   ```
3. Host sanity check: `setup_vps.yml --check --diff` — expect ~no diff.
4. **Manually remove `osa-infrastructure`'s AND `osa-logging`'s previously
   deployed files** (both repos are fully replaced by this one):
   ```bash
   rm -rf ~/.config/containers/systemd/osa/infrastructure/{caddy,maintenance}
   rm -rf ~/.config/containers/systemd/osa/logging
   systemctl --user daemon-reload
   ```
   Caddy and Dozzle keep running throughout (only the quadlet ownership
   changes), the FLG stack containers (Fluent-Bit/Loki/Grafana) are
   permanently stopped by this step. From here on, never run
   `osa-infrastructure/build.sh` or `osa-logging/build.sh` again.
5. `deploy.yml --check --diff` then for real — distributes secrets/
   quadlet definitions/timer, starts Frontend+Maintenance+Dozzle.
   `osa-backend` deliberately does not exist as a running container yet
   at this point. Verify directly: `curl 127.0.0.1:21001/`,
   `curl 127.0.0.1:21001/config.js` (real values, not a placeholder
   literal).
6. `flip.yml -e target=new --check --diff`, then for real.
7. Verify directly: `podman logs osa-backend` (scheduler starts cleanly),
   `curl 127.0.0.1:21000/`.
8. Verify externally:
   - `curl -I https://einteilung.hochamt.at/` → 200, new frontend.
   - `curl -I https://einteilung.hochamt.at/api/` → FastAPI JSON.
   - `curl -I https://go.hochamt.at/...` → redirect via the new backend.
   - A real browser login + one booking view.
   - `curl -I https://einteilung.hochamt.at/logging/dozzle` → still
     basic-auth protected.
   - `systemctl --user list-units 'caddy*' 'osa-*'` → all active/healthy.
9. Close the maintenance window once step 8 is fully green.

### Backward flip (new stack → Legacy) — possible any time, not a special case

As long as Phase 1 holds, the backward flip is exactly the same,
pre-tested `flip.yml` mechanism in reverse — no freehand procedure:

```bash
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=legacy --check --diff
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=legacy
```

Can be repeated any number of times (e.g. flip back → fix a bug → flip
forward again with `target=new`), as long as no Postgres migration
happened in between. Verification mirrors the above, just against
Legacy's own endpoints.

**Recommended minimum time on `target=new` before the Legacy pod and old
repos are finally decommissioned:** ≥1 week, at least one full service
cycle — after that the forward flip counts as final, not because a
backward flip would become technically harder, but because decommissioning
is next.

### Final decommissioning (after sufficient time on `target=new`)

Only now is the reversibility above deliberately given up: archive
`osa/local-deployment` + `osa-infrastructure` + `osa-logging`, remove the
Legacy pod, prune images. Keep the pre-cutover backup permanently (it
otherwise doesn't survive Koofr's 28-day sweep) — it remains the last
reachable Legacy data state, in case it's ever needed again.

## Postgres cutover

Structural 1:1 SQLite → PostgreSQL migration (`osa-backend`'s
`app/db/models/` unchanged, no schema redesign — see that repo's own
migration plan for the full rationale). Measured ETL runtime against the
real ~619 MB dev SQLite file: **32 seconds** for 163k rows across 30
tables — that's the actual downtime budget below, steps 5-9, not counting
verification.

**Why the `development` → `main` merge stays coupled to this runbook, not
done ahead of time:** a push to `main` triggers `osa-backend`'s CI to build
and push a new `:latest` image to `ghcr.io`. `osa-backend.container` runs
with `AutoUpdate=registry`, and Podman's own `podman-auto-update.timer`
(daily) would pull and restart it **on its own**, before the Postgres
sidecar below exists — hence merging only as step 3, immediately followed
by a manual pull in step 6 (not waiting for the daily timer).

1. Announce a maintenance window.
2. **Final backup via the current, still-running mechanism** (SQLite):
   ```bash
   ssh service@<prod-host>
   podman exec osa-backend python scripts/backup_db.py
   ```
3. Merge `osa-backend`'s `development` → `main` PR on GitHub (own
   account, web UI) — triggers the image build above. Wait for the
   `release-image` job to finish (GitHub Actions tab) before continuing.
4. Deploy the new quadlet + secret (still no downtime — the existing
   `osa-backend` container keeps running against SQLite throughout):
   ```bash
   # secrets/osa-backend-pg.env created+vault-encrypted locally first,
   # see "Maintaining secrets" below.
   ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --check --diff
   ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml
   ```
   Verify: `systemctl --user status osa-backend-pg`, wait for
   `pg_isready`-green in its healthcheck.
5. **Stop the app container — downtime starts here:**
   ```bash
   systemctl --user stop osa-backend.service
   ```
6. Pull the already-built image manually (don't wait for the daily
   timer), then run the ETL once:
   ```bash
   podman pull ghcr.io/kirchenmusik-st-augustin/osa-backend:latest
   podman run --rm --pod osa-backend-pod \
     --env-file ~/.env/osa-backend.env \
     --env-file ~/.env/osa-backend-pg.env \
     -e DATABASE_URL=postgresql://osa:<password>@127.0.0.1:5432/osa \
     -v ~/data/osa/einteilung.hochamt.at/sqlite:/database:Z \
     ghcr.io/kirchenmusik-st-augustin/osa-backend:latest \
     python scripts/migration_archive/sqlite2pg.py
   ```
7. **Hard verification, not "looked fine":** `SELECT COUNT(*)` on both
   sides for all 30 migrated tables (the ETL script's own log output
   already lists per-table row counts — diff that against
   `sqlite3 database/database.sqlite "SELECT COUNT(*) FROM <table>"` from
   step 2's backup, or a fresh read of the live file before step 5).
   Confirm `migrations`/`queue_jobs`/`queue_failed_jobs` are intentionally
   absent from Postgres (dead Laravel-only tables, not a copy bug — see
   the migration plan).
8. Point the app at the new database:
   ```bash
   ansible-vault edit secrets/osa-backend.env   # DATABASE_URL -> postgresql://...
   ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml
   ```
9. Restart the app — `alembic upgrade head` runs automatically on
   container start, should be a no-op here (schema already exists):
   ```bash
   systemctl --user start osa-backend.service
   ```
10. Verify like the forward-flip runbook above: `podman logs osa-backend`,
    `curl 127.0.0.1:21000/`, external checks, a real browser login + one
    booking view.
11. **Trigger the backup job once manually** and confirm it actually
    produces a Koofr backup via the new `pg_dump` path (don't wait for the
    next scheduled `03:00` run):
    ```bash
    podman exec osa-backend python scripts/backup_db.py
    ```
12. Close the maintenance window once step 10+11 are fully green.
13. **Deliberately keep the old SQLite file**
    (`~/data/osa/einteilung.hochamt.at/sqlite`) — last cold fallback,
    rollback path is `DATABASE_URL` back to `sqlite:///…` + pod restart,
    until a deliberate cleanup decision is made (≥1 week bake time, same
    convention as the Legacy decommissioning above).

## Maintaining secrets

`secrets/caddy.env`, `secrets/osa-backend.env`, `secrets/osa-frontend.env`
live `ansible-vault`-encrypted in the repo, never committed in plaintext.
`secrets/*.env.example` are the (plaintext-safe) templates for them:

```bash
cp secrets/osa-backend.env.example secrets/osa-backend.env
$EDITOR secrets/osa-backend.env
ansible-vault encrypt secrets/osa-backend.env

# Edit later:
ansible-vault edit secrets/osa-backend.env
ansible-vault view secrets/osa-backend.env
```

`secrets/caddy.env` should reuse Legacy's/`osa-infrastructure`'s
**already-existing** `LOGGING_USER`/`LOGGING_PASSWORD_HASH` values (don't
regenerate) — the Dozzle login stays unaffected by the cutover.
`secrets/osa-backend.env`'s `KOOFR_USER`/`KOOFR_PASSWORD` should reuse
Legacy's existing Koofr account credentials, so `scripts/backup_db.py`/
`restore_db.py` (see below) access the same backup history as
`osa-einteilung.hochamt.at/tools/restore-koofr-backup.sh` -- though that
script only recognizes backups created before 2026-08-13's stage-prefixed
filename change (deliberate, accepted break, see `backup_service.py`'s
module docstring).

`secrets/osa-backend-pg.env` (Postgres cutover, see above) has no
`.example` template (mirrors the sister project's `vb-api-pg.env`) —
create it fresh with a freshly generated password, never reuse the dev
one:
```bash
cat > secrets/osa-backend-pg.env <<'EOF'
POSTGRES_USER=osa
POSTGRES_PASSWORD=<paste output of: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
POSTGRES_DB=osa
EOF
ansible-vault encrypt secrets/osa-backend-pg.env
```

## Disaster recovery / database restore

Doesn't run through this repo, but through scripts in `osa-backend` — run
on the target system itself, via SSH onto the production host:

```bash
ssh service@<prod-host>

# See available backups:
podman exec osa-backend python scripts/backup_db.py --list

# Run the restore (auto-selects the latest without --backup-name):
podman exec -it osa-backend python scripts/restore_db.py --force
```

`--force` is mandatory because `restore_db.py` refuses a restore under
`APP_ENVIRONMENT=production` by default (protection against accidentally
overwriting the live database). Always verify afterward that real data
actually landed, not just that the command exited cleanly.

A manual backup before risky changes:
`podman exec osa-backend python scripts/backup_db.py`. Full docs for both
scripts (all parameters incl. `--dry-run`/`--cleanup`) in
[`osa-backend`'s README](../osa-backend/README.md).

## Retiring the old repos

`osa-deploy` fully replaces `osa/local-deployment` **and**
`osa-infrastructure` **and** `osa-logging` (not additively) — a single,
authoritative deploy repo. All three are archived once there has been
sufficient time on the new stack (see the runbook above, "Final
decommissioning").

---

# Deutsch

Zentrales Betriebshandbuch für die Produktion von **OSA** (Orchester-Einteilung,
`einteilung.hochamt.at`): welches Repo wofür da ist, wie die Produktion
aufgebaut ist, und wie man sie aufsetzt, deployt und zwischen Legacy und dem
neuen Stack wechselt. Dieses Repo selbst enthält alles Betriebliche:
Ansible-Playbooks, Caddy-Konfiguration, Podman-Quadlets und
(vault-verschlüsselte) Secrets.

## Die Repos im Überblick

| Repo | Was | Tech-Stack | Wird deployt als |
|---|---|---|---|
| [`osa-backend`](../osa-backend) | Backend: Dienstplan-/Besetzungsverwaltung für Kirchenmusiker | Python 3.12, FastAPI, SQLAlchemy, SQLite (Phase 1) | `osa-backend`-Pod |
| [`osa-frontend`](../osa-frontend) | Frontend zu `osa-backend`: Vue-3-SPA | Vue 3 (`<script setup>`, TypeScript), Vite, nginx | `osa-frontend`-Pod |
| `osa-deploy` (dieses Repo) | Betrieb: Ansible, Caddy, Quadlets, Secrets | Ansible, systemd Quadlets | läuft nicht selbst als Service — konfiguriert die anderen |

`osa-logging` (Loki/Grafana/Fluent-Bit) wird durch diesen Cutover **restlos
stillgelegt** (nie wirklich genutzt, siehe unten) — der einzige verbleibende
Log-Viewer, Dozzle, ist jetzt Teil von `osa-deploy` selbst
(`quadlets/logging/`).

Beide App-Repos bauen ihr Container-Image selbst per CI/CD (GitHub Actions)
und pushen es bei jedem Merge nach `main` automatisch nach
`ghcr.io/kirchenmusik-st-augustin/<repo>:latest`. `osa-deploy` baut **keine**
Images und klont **keine** App-Repos auf den Produktionshost — es verteilt
nur Config und Secrets und sorgt dafür, dass die richtigen Images laufen.

## Architektur: rootless Podman auf einem VPS

Ein einzelner VPS trägt die gesamte Produktion. Statt klassischer Root-Docker-
Container läuft alles **rootless** unter einem eigenen, unprivilegierten
Linux-User namens `service` — ein kompromittierter Container kann so nicht
direkt auf Host-Root eskalieren. Ein zweiter User `admin` existiert nur für
administrative Root-Aufgaben (`sudo`), er betreibt selbst keine Container
(gleiche Sicherheitsgrenze wie im Schwesterprojekt `vb-fastapi-vue`/
`vb-deploy`).

- **systemd Quadlets** statt `docker-compose`: jeder Container/Pod wird als
  `.container`/`.pod`/`.volume`-Datei beschrieben, die `systemd --user`
  automatisch in einen echten systemd-Service übersetzt.
- **Zwei Pods für die App:** `osa-backend-pod` (aktuell nur der Backend-
  Container — strukturiert, um in Phase 2 einen `osa-backend-pg`-Sidecar
  aufzunehmen, ohne jetzt schon Postgres-Annahmen zu backen) und
  `osa-frontend-pod` (nginx). Legacy läuft weiterhin als eigener,
  unveränderter Pod (`einteilung.hochamt.at-pod`, aus `osa/
  osa-einteilung.hochamt.at`).
- **Geteilte SQLite-Datei statt Datenmigration:** Phase 1 (siehe CLAUDE.md
  Abschnitt 3) bedeutet, `osa-backend`s Schema ist strukturgleich zu
  Legacys. Beide Pods mounten denselben Host-Pfad
  (`~/data/osa/einteilung.hochamt.at/sqlite`) — der Cutover ist ein reiner
  App-Tausch ohne Datenmigration. **Beide dürfen dabei nie gleichzeitig
  schreibend laufen** — das erzwingt `playbooks/flip.yml` strukturell
  (siehe unten), nicht nur durch Dokumentation.
- **Caddy** ist der einzige Dienst, der öffentlich auf Port 80/443 lauscht,
  terminiert TLS und reverse-proxied **pfadbasiert** (nicht pro Subdomain
  wie im Schwesterprojekt) auf dieselbe Domain:
  ```
  Internet
     │  :80 / :443
     ▼
  Caddy (Host-Netzwerk)
     └─ einteilung.hochamt.at
          ├─ /logging/dozzle*  → 127.0.0.1:8081 → Dozzle (Basic-Auth)
          ├─ /go/*             → redirect → go.hochamt.at
          ├─ /api, /api/*      → 127.0.0.1:21000 → osa-backend-pod (Präfix gestrippt)
          └─ alles andere      → 127.0.0.1:21001 → osa-frontend-pod
     └─ go.hochamt.at (Kurz-URL-Dienst)
          └─ /*                → 127.0.0.1:21000 (mit /go-Präfix) → osa-backend-pod
  ```
  Vor dem ersten Hinstieg proxied `einteilung.hochamt.at` stattdessen alles
  auf Port 8080 (Legacy) — siehe `config/caddy/Caddyfile.legacy`, ein
  eingefrorener Snapshot dieses Zustands für den Rückstieg.
- **Image-Bezug**: alle App-Container haben `Image=ghcr.io/.../<name>:latest`
  + `AutoUpdate=registry`. Podmans eigener `podman-auto-update.timer`
  (täglich) prüft, pullt bei Bedarf und startet neu — ganz ohne Ansible.
  `osa-deploy` wird nur gebraucht, um Config/Secrets/Quadlets initial bzw.
  bei Änderungen zu verteilen, und für den Hin-/Rückstieg selbst.
- **Dozzle** ist ein schreibgeschützter Log-Viewer für alle laufenden
  Container, erreichbar über `einteilung.hochamt.at/logging/dozzle` hinter
  Basic-Auth.
- **podman-prune.timer** räumt wöchentlich ungenutzte Images/Container auf.

## Voraussetzungen

- Ansible lokal installiert.
- SSH-Zugriff auf den Host — welcher User gebraucht wird, hängt vom
  Playbook/Zeitpunkt ab (gleiche Rollentrennung wie oben):
  - **`root`** — nur für den allerersten Lauf von `setup_vps.yml` auf einem
    frischen Host. Wird für die *reguläre* Nutzung dieses Repos nicht
    gebraucht: der reale Produktions-Host ist bereits über `osa/
    local-deployment`s fast identisches Playbook gehärtet.
  - **`admin`** — für Re-Runs von `setup_vps.yml` (Verifikation,
    `--check --diff`), braucht `sudo`/`become`.
  - **`service`** — für `deploy.yml` und `flip.yml` (fest als `remote_user:
    service` hinterlegt, kein `sudo` nötig).
- Ein Vault-Passwort für `secrets/*.env` (wird interaktiv abgefragt, kein
  `vault_password_file` im Repo).

## Phase 1 — VPS-Grundkonfiguration (nur bei Neuaufsetzen nötig)

```bash
ansible-playbook -i inventory/hosts.ini playbooks/setup_vps.yml -u root
# Falls nur Passwort-Login moeglich ist:
ansible-playbook -i inventory/hosts.ini playbooks/setup_vps.yml -u root --ask-pass

# Re-Run / Verifikation (root-Login ist danach deaktiviert):
ansible-playbook -i inventory/hosts.ini playbooks/setup_vps.yml -u admin --ask-become-pass --check --diff
```

Der reale Produktions-Host ist bereits über `osa/local-deployment`s fast
identisches Playbook gehärtet — ein erster Lauf hier sollte nur zur
Verifikation dienen (`--check --diff`, erwartet quasi kein Diff), nicht zur
Neuinstallation.

## Phase 2 — Tag-2-Betrieb: Secrets/Quadlets/Timer synchron halten

```bash
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --check --diff   # erst pruefen
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass                  # dann anwenden
```

Synct Secrets, Quadlet-**Definitionen** (Caddy/Maintenance/Logging/Backend/
Frontend) und den Maintenance-Timer, startet Frontend+Maintenance+Dozzle
(zustandslos, jederzeit unbedenklich). **Rührt nie an, welcher Stack gerade
aktiv ist, und startet/restartet Caddy nie** — das ist ausschließlich
`playbooks/flip.yml`s Aufgabe (siehe unten). Baut keine Images, klont keine
Repos, restored keine Datenbank.

### Sofort-Deploy (statt auf den nächtlichen Auto-Update-Lauf zu warten)

```bash
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-backend
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-frontend
```

Pullt das aktuelle `:latest`-Image und startet den zugehörigen Pod neu.
**Nur sinnvoll, wenn der jeweilige Stack laut dem letzten `flip.yml`-Lauf
gerade der aktive ist** — `--tags deploy-backend`, während `target=legacy`
aktiv ist, würde `osa-backend-pod` parallel zu Legacy hochziehen.

## Hin-/Rückstieg: `playbooks/flip.yml`

**Ein einziges, symmetrisches Playbook für beide Richtungen** — kein
separates Cutover- und Rollback-Skript. Solange Phase 1 gilt (beide Stacks
teilen dieselbe SQLite-Datei, vor der Postgres-Migration), ist der Wechsel
in beide Richtungen technisch identisch und beliebig oft wiederholbar. Diese
Reversibilität endet strukturell mit Phase 2 — danach ist `target=legacy`
keine einfache Umkehrung mehr, sondern bräuchte eine echte Rückmigration.

```bash
# Hinstieg (Legacy -> neuer Stack):
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=new --check --diff
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=new

# Rückstieg (neuer Stack -> Legacy), jederzeit, beliebig oft:
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=legacy --check --diff
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=legacy
```

Intern, für beide Richtungen identisch (nur `target` bestimmt Quelle/Ziel):
1. Den jeweils anderen Stack stoppen (Legacy-Pod bzw. `osa-backend-pod`).
2. **Erst danach** den Zielstack starten — erzwingt strukturell, dass beide
   nie gleichzeitig gegen die geteilte SQLite-Datei schreiben.
3. Die passende Caddyfile-Variante (`config/caddy/Caddyfile` bzw.
   `Caddyfile.legacy`) einspielen.
4. `caddy.service` neu starten — der eigentliche Traffic-Switch.

Kein `--ask-vault-pass` nötig — `flip.yml` fasst nie `secrets/*.env` an,
nur Pod-Start/-Stopp + Caddyfile.

**Jeder Befehl in diesem Repo/dieser README wird von Claude nur beschrieben,
nie selbst ausgeführt — durchgeführt wird das ausschließlich manuell.**

## Der komplette Hinstieg — Schritt-für-Schritt-Runbook

Deutlich einfacher als ein volles VPS-Neuaufsetzen, weil die DB-Migration
bewusst NACH dem Cutover läuft (User-Entscheidung): kein ETL-Schritt, keine
leere DB, kein DNS-Wechsel.

### Einmalige Vorbereitung (vor dem allerersten Hinstieg)

1. `osa-backend`s Backup-Fähigkeit gemerged, Images gebaut. Backup/Restore-
   CLI einmal von einer Dev-Umgebung gegen echte `KOOFR_USER`/
   `KOOFR_PASSWORD` + eine lokale Kopie der SQLite-Datei geprobt (räumt
   sich über die 28-Tage-Retention selbst auf).
2. `config/caddy/Caddyfile.legacy` ist bereits als Snapshot des Stands VOR
   diesem Vorhaben eingecheckt (siehe oben) — nichts weiter zu tun, außer
   ihn nie wieder zu verändern.

### Hinstieg (Legacy → neuer Stack)

1. Wartungsfenster ankündigen.
2. **Letzter Pre-Cutover-Backup über Legacys eigenen, noch laufenden
   Mechanismus** (Gürtel+Hosenträger, frischest möglicher Snapshot):
   ```bash
   ssh service@<prod-host>
   podman exec einteilung.hochamt.at-fpm php artisan osa:schedule:backup-prod-database
   ```
3. Host-Sanity-Check: `setup_vps.yml --check --diff` — erwartet ~kein Diff.
4. **`osa-infrastructure`s UND `osa-logging`s bisher deployte Dateien
   manuell entfernen** (beide Repos werden durch dieses hier vollständig
   abgelöst):
   ```bash
   rm -rf ~/.config/containers/systemd/osa/infrastructure/{caddy,maintenance}
   rm -rf ~/.config/containers/systemd/osa/logging
   systemctl --user daemon-reload
   ```
   Caddy und Dozzle laufen dabei durchgehend weiter (nur die
   Quadlet-Eigentümerschaft wechselt), die FLG-Stack-Container
   (Fluent-Bit/Loki/Grafana) werden mit diesem Schritt endgültig gestoppt.
   Ab hier `osa-infrastructure/build.sh` und `osa-logging/build.sh` nie
   wieder ausführen.
5. `deploy.yml --check --diff` dann scharf — verteilt Secrets/Quadlet-
   Definitionen/Timer, startet Frontend+Maintenance+Dozzle. `osa-backend`
   existiert zu diesem Zeitpunkt bewusst noch nicht als laufender
   Container. Direkt verifizieren: `curl 127.0.0.1:21001/`,
   `curl 127.0.0.1:21001/config.js` (echte Werte, kein Platzhalter-Literal).
6. `flip.yml -e target=new --check --diff`, dann scharf.
7. Direkt verifizieren: `podman logs osa-backend` (Scheduler startet
   fehlerfrei), `curl 127.0.0.1:21000/`.
8. Extern verifizieren:
   - `curl -I https://einteilung.hochamt.at/` → 200, neues Frontend.
   - `curl -I https://einteilung.hochamt.at/api/` → FastAPI JSON.
   - `curl -I https://go.hochamt.at/...` → Redirect über den neuen Backend.
   - Ein echter Browser-Login + eine Buchungsansicht.
   - `curl -I https://einteilung.hochamt.at/logging/dozzle` → weiterhin
     Basic-Auth-geschützt.
   - `systemctl --user list-units 'caddy*' 'osa-*'` → alle aktiv/healthy.
9. Wartungsfenster schließen, sobald Schritt 8 vollständig grün ist.

### Rückstieg (neuer Stack → Legacy) — jederzeit möglich, kein Sonderfall

Solange Phase 1 gilt, ist der Rückstieg genau derselbe, vorab getestete
`flip.yml`-Mechanismus in Gegenrichtung — kein Freihand-Vorgehen:

```bash
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=legacy --check --diff
ansible-playbook -i inventory/hosts.ini playbooks/flip.yml -e target=legacy
```

Kann beliebig oft wiederholt werden (z. B. Rückstieg → Bug fixen → erneuter
Hinstieg mit `target=new`), solange keine Postgres-Migration dazwischen
lag. Verifikation spiegelbildlich zu oben, nur gegen Legacys eigene
Endpunkte.

**Empfohlene Mindest-Standzeit auf `target=new`, bevor Legacy-Pod und
Alt-Repos endgültig entsorgt werden:** ≥1 Woche, mindestens ein voller
Dienst-Zyklus — danach gilt der Hinstieg als endgültig, nicht weil ein
Rückstieg technisch schwerer würde, sondern weil ab dann die Entsorgung
ansteht.

### Endgültige Entsorgung (nach hinreichender Standzeit auf `target=new`)

Erst jetzt wird die Reversibilität oben bewusst aufgegeben:
`osa/local-deployment` + `osa-infrastructure` + `osa-logging` archivieren,
Legacy-Pod entfernen, Images prunen. Den Pre-Cutover-Backup dauerhaft
aufheben (überlebt sonst Koofrs 28-Tage-Sweep nicht) — er bleibt der letzte
greifbare Legacy-Datenstand, falls je wieder gebraucht.

## Postgres-Cutover

Struktureller 1:1-SQLite→PostgreSQL-Übertrag (`osa-backend`s
`app/db/models/` unverändert, kein Schema-Redesign — vollständige
Begründung im Migrationsplan des Repos). Gemessene ETL-Laufzeit gegen die
reale ~619-MB-Dev-SQLite-Datei: **32 Sekunden** für 163.000 Zeilen über 30
Tabellen — das ist das tatsächliche Downtime-Budget unten, Schritte 5-9,
Verifikation nicht mitgerechnet.

**Warum der `development`→`main`-Merge zeitlich an dieses Runbook gekoppelt
bleibt, nicht vorab passiert:** Ein Push nach `main` löst `osa-backend`s
CI aus, die ein neues `:latest`-Image nach `ghcr.io` baut+pusht.
`osa-backend.container` läuft mit `AutoUpdate=registry`, und Podmans
eigener `podman-auto-update.timer` (täglich) würde es **von selbst**
pullen und neu starten, bevor der Postgres-Sidecar unten existiert — daher
der Merge erst als Schritt 3, direkt gefolgt von einem manuellen Pull in
Schritt 6 (nicht auf den täglichen Timer warten).

1. Wartungsfenster ankündigen.
2. **Finales Backup über den aktuell laufenden Mechanismus** (SQLite):
   ```bash
   ssh service@<prod-host>
   podman exec osa-backend python scripts/backup_db.py
   ```
3. `osa-backend`s `development`→`main`-PR auf GitHub mergen (eigener
   Account, Web-UI) — löst den obigen Image-Build aus. Auf den
   `release-image`-Job warten (GitHub-Actions-Tab), bevor es weitergeht.
4. Neuen Quadlet + Secret ausrollen (immer noch keine Downtime — der
   bestehende `osa-backend`-Container läuft währenddessen unverändert
   gegen SQLite weiter):
   ```bash
   # secrets/osa-backend-pg.env vorher lokal angelegt+vault-verschlüsselt,
   # siehe "Secrets pflegen" unten.
   ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --check --diff
   ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml
   ```
   Verifizieren: `systemctl --user status osa-backend-pg`, auf grünen
   `pg_isready`-Healthcheck warten.
5. **App-Container stoppen — ab hier beginnt die Downtime:**
   ```bash
   systemctl --user stop osa-backend.service
   ```
6. Das bereits gebaute Image manuell pullen (nicht auf den täglichen
   Timer warten), dann das ETL einmalig laufen lassen:
   ```bash
   podman pull ghcr.io/kirchenmusik-st-augustin/osa-backend:latest
   podman run --rm --pod osa-backend-pod \
     --env-file ~/.env/osa-backend.env \
     --env-file ~/.env/osa-backend-pg.env \
     -e DATABASE_URL=postgresql://osa:<password>@127.0.0.1:5432/osa \
     -v ~/data/osa/einteilung.hochamt.at/sqlite:/database:Z \
     ghcr.io/kirchenmusik-st-augustin/osa-backend:latest \
     python scripts/migration_archive/sqlite2pg.py
   ```
7. **Harte Verifikation, kein "sah gut aus":** `SELECT COUNT(*)` auf
   beiden Seiten für alle 30 migrierten Tabellen (das ETL-Skript listet
   die Zeilenzahlen pro Tabelle bereits selbst im Log — dagegen
   `sqlite3 database/database.sqlite "SELECT COUNT(*) FROM <table>"` aus
   dem Backup von Schritt 2 gegenchecken, bzw. einen frischen Read der
   Live-Datei vor Schritt 5). Bestätigen, dass `migrations`/`queue_jobs`/
   `queue_failed_jobs` bewusst NICHT in Postgres existieren (toter
   Laravel-Ballast, kein Kopierfehler — siehe Migrationsplan).
8. Die App auf die neue Datenbank umstellen:
   ```bash
   ansible-vault edit secrets/osa-backend.env   # DATABASE_URL -> postgresql://...
   ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml
   ```
9. App neu starten — `alembic upgrade head` läuft automatisch beim
   Container-Start, sollte hier ein No-Op sein (Schema existiert schon):
   ```bash
   systemctl --user start osa-backend.service
   ```
10. Verifizieren wie im Hinstieg-Runbook oben: `podman logs osa-backend`,
    `curl 127.0.0.1:21000/`, externe Checks, ein echter Browser-Login +
    eine Buchungsansicht.
11. **Backup-Job einmal manuell antriggern** und bestätigen, dass er über
    den neuen `pg_dump`-Pfad tatsächlich ein Koofr-Backup erzeugt (nicht
    auf den nächsten planmäßigen `03:00`-Lauf warten):
    ```bash
    podman exec osa-backend python scripts/backup_db.py
    ```
12. Wartungsfenster schließen, sobald Schritt 10+11 vollständig grün sind.
13. Die alte SQLite-Datei **bewusst behalten**
    (`~/data/osa/einteilung.hochamt.at/sqlite`) — letzte kalte
    Rückfallebene, Rollback-Pfad ist `DATABASE_URL` zurück auf
    `sqlite:///…` + Pod-Neustart, bis eine bewusste Aufräum-Entscheidung
    fällt (≥1 Woche Bewährungsfrist, dieselbe Konvention wie bei der
    Legacy-Entsorgung oben).

## Secrets pflegen

`secrets/caddy.env`, `secrets/osa-backend.env`, `secrets/osa-frontend.env`
liegen `ansible-vault`-verschlüsselt im Repo, nie im Klartext committet.
`secrets/*.env.example` sind die (Klartext-sicheren) Vorlagen dafür:

```bash
cp secrets/osa-backend.env.example secrets/osa-backend.env
$EDITOR secrets/osa-backend.env
ansible-vault encrypt secrets/osa-backend.env

# Später bearbeiten:
ansible-vault edit secrets/osa-backend.env
ansible-vault view secrets/osa-backend.env
```

`secrets/caddy.env` sollte Legacys/`osa-infrastructure`s **bereits
bestehende** `LOGGING_USER`/`LOGGING_PASSWORD_HASH`-Werte übernehmen (nicht
neu generieren) — der Dozzle-Login bleibt vom Cutover unberührt.
`secrets/osa-backend.env`s `KOOFR_USER`/`KOOFR_PASSWORD` sollten Legacys
bestehende Koofr-Kontodaten übernehmen, damit `scripts/backup_db.py`/
`restore_db.py` (siehe unten) auf denselben Backup-Bestand zugreifen wie
`osa-einteilung.hochamt.at/tools/restore-koofr-backup.sh` -- dieses Skript
erkennt allerdings nur Backups von vor der Stage-Präfix-Namensumstellung
vom 13.08.2026 (bewusster, akzeptierter Bruch, siehe Modul-Docstring von
`backup_service.py`).

`secrets/osa-backend-pg.env` (Postgres-Cutover, siehe oben) hat keine
`.example`-Vorlage (analog zu `vb-api-pg.env` im Schwesterprojekt) — frisch
anlegen, mit einem neu generierten Passwort, niemals das Dev-Passwort
wiederverwenden:
```bash
cat > secrets/osa-backend-pg.env <<'EOF'
POSTGRES_USER=osa
POSTGRES_PASSWORD=<Ausgabe von: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
POSTGRES_DB=osa
EOF
ansible-vault encrypt secrets/osa-backend-pg.env
```

## Disaster Recovery / Datenbank-Restore

Läuft nicht über dieses Repo, sondern über Skripte in `osa-backend` — auf
dem Zielsystem selbst ausführen, per SSH auf den Produktions-Host:

```bash
ssh service@<prod-host>

# Verfuegbare Backups ansehen:
podman exec osa-backend python scripts/backup_db.py --list

# Restore ausfuehren (nimmt ohne --backup-name automatisch das neueste):
podman exec -it osa-backend python scripts/restore_db.py --force
```

`--force` ist zwingend, weil `restore_db.py` einen Restore bei
`APP_ENVIRONMENT=production` standardmäßig verweigert (Schutz vor
versehentlichem Überschreiben der Live-Datenbank). Danach immer
verifizieren, dass wirklich echte Daten da sind, nicht nur, dass der Befehl
fehlerfrei durchlief.

Ein manueller Backup vor riskanten Änderungen:
`podman exec osa-backend python scripts/backup_db.py`. Volle Doku zu
beiden Skripten (alle Parameter inkl. `--dry-run`/`--cleanup`) in
[`osa-backend`s README](../osa-backend/README.md).

## Ablösung der Alt-Repos

`osa-deploy` ersetzt `osa/local-deployment` **und** `osa-infrastructure`
**und** `osa-logging` vollständig (nicht additiv) — ein einziges,
autoritatives Deploy-Repo. Alle drei werden nach hinreichender Standzeit
auf dem neuen Stack archiviert (siehe Runbook oben, "Endgültige
Entsorgung").
