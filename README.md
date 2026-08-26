# osa-deploy

Central operations handbook for **OSA**'s production
(`einteilung.hochamt.at`, Orchester-Einteilung — the scheduling/casting
system for the church musicians of Kirchenmusik St. Augustin): which repo
is for what, how production is built, and how to set it up and deploy it.
This repo itself contains everything operational: Ansible playbooks, Caddy
configuration, Podman Quadlets, and (vault-encrypted) secrets.

## The repos at a glance

| Repo | What | Tech stack | Deployed as |
|---|---|---|---|
| [`osa-backend`](../osa-backend) | Backend: scheduling/casting management for church musicians | Python 3.12, FastAPI, SQLAlchemy, PostgreSQL | `osa-backend` pod |
| [`osa-frontend`](../osa-frontend) | Frontend to `osa-backend`: Vue 3 SPA | Vue 3 (`<script setup>`, TypeScript), Vite, nginx | `osa-frontend` pod |
| `osa-deploy` (this repo) | Ops: Ansible, Caddy, Quadlets, Secrets | Ansible, systemd Quadlets | doesn't run itself — configures the others |

Both app repos build their own container image via CI/CD (GitHub Actions)
and push it to `ghcr.io/kirchenmusik-st-augustin/<repo>:latest`
automatically on every merge to `main`. `osa-deploy` builds **no** images
and clones **no** app repos onto the production host — it only distributes
config and secrets and makes sure the right images are running.

## Architecture: rootless Podman on a VPS

A single VPS carries all of production — by design, not by necessity: for
this scale, it's the leanest operating model there is, with none of the
overhead a cluster would add. Rootless Podman makes that possible without
compromising on isolation: instead of classic root Docker containers,
everything runs **rootless** under a dedicated, unprivileged Linux user
named `service` — a compromised container can't escalate straight to host
root. A second user `admin` exists only for administrative root tasks
(`sudo`), it never runs containers itself.

- **systemd Quadlets** instead of `docker-compose`: every container/pod is
  described as a `.container`/`.pod`/`.volume` file, which `systemd --user`
  translates into a real systemd service automatically.
- **Two pods for the app:** `osa-backend-pod` (the backend container plus
  an `osa-backend-pg` PostgreSQL sidecar) and `osa-frontend-pod` (nginx) —
  the only stack running.
- **Caddy** is the only service listening publicly on port 80/443,
  terminates TLS, and reverse-proxies **path-based** onto the same domain:
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
- **Image sourcing**: all app containers have
  `Image=ghcr.io/.../<name>:latest` + `AutoUpdate=registry`. Podman's own
  `podman-auto-update.timer` (daily) checks, pulls if needed, and restarts
  — entirely without Ansible. `osa-deploy` is only needed to distribute
  config/secrets/quadlets initially or on change, and for an immediate
  deploy (see below).
- **Dozzle** is a read-only log viewer for all running containers,
  reachable at `einteilung.hochamt.at/logging/dozzle` behind basic auth.
- **podman-prune.timer** cleans up unused images/containers weekly.

## Prerequisites

- Ansible installed locally.
- SSH access to the host — which user is needed depends on the
  playbook/moment (same role separation as above):
  - **`root`** — only for the very first run of `setup_vps.yml` on a
    fresh host. Not needed for *regular* use of this repo: the production
    host is already hardened, so a later run only re-verifies that state
    (see Phase 1 below).
  - **`admin`** — for re-runs of `setup_vps.yml` (verification,
    `--check --diff`), needs `sudo`/`become`.
  - **`service`** — for `deploy.yml` (hardcoded as `remote_user: service`,
    no `sudo` needed).
- A vault password for `secrets/<stage>/*.env`/`*.env.j2` (asked
  interactively, no `vault_password_file` in the repo).
- **DNS** for `backend_domain`/`frontend_domain`/`shorturl_domain` (see
  "Stages" below) must already point to the target VPS before `deploy.yml`
  (Phase 2) runs — not needed for Phase 1, which never touches Caddy.
  Caddy provisions a TLS certificate from Let's Encrypt automatically for
  every domain in the Caddyfile (see `config/caddy/Caddyfile.j2` — no
  manual `tls` directive or certificate files anywhere), which means the
  ACME challenge has to actually reach the VPS at that exact domain over
  port 80 to prove ownership. If a domain doesn't resolve yet (or still
  points at an old host), certificate issuance fails outright — the site
  doesn't come up without HTTPS, it doesn't come up at all. Repeated
  failed attempts against the same domain also count against Let's
  Encrypt's own rate limits, so get DNS right *before* the first
  `deploy.yml` run rather than debugging it against the live ACME
  endpoint. Verify with `dig +short <domain>` first.

## Stages

`inventory/` holds one file per stage this repo can actually deploy to —
`production.ini`, `test.ini`, `qa.ini`. Both `osa-backend` and
`osa-frontend` have a fourth valid `APP_ENVIRONMENT` value, `development`,
but that one is deliberately not an Ansible target here: local development
runs against the Vite dev server directly on the dev machine, never through
this repo. Every command below
takes `-i inventory/<stage>.ini`; there is no default inventory, so a
missing `-i` fails loudly instead of silently targeting the wrong stage.
Only `production` is a real, currently deployed target — `test`/`qa` are
placeholder skeletons (`CHANGEME.example.invalid`) until a dedicated VPS
exists for them.

Each stage's `inventory/<stage>.ini` also carries `backend_domain`,
`frontend_domain`, and `shorturl_domain` — independent variables (backend
and frontend don't have to share a domain, see `osa-backend`'s
`samesite=none`/CSRF-origin-check cross-domain support), rendered into the
Caddyfile and into `secrets/<stage>/*.env.j2` at deploy time. Each stage's
Postgres data also lives under its own path, `~/data/osa/<stage>/postgres`
on that stage's host.

**Standing up `test`/`qa` on a fresh VPS:** edit `inventory/test.ini` or
`inventory/qa.ini` directly — the placeholder file is already checked into
the repo, no copy step needed. Replace the `[vps]` entry with the new
host's IP or hostname (whatever `ssh` can already reach), and replace all
three `CHANGEME.example.invalid` values under `[vps:vars]` with that
stage's real `backend_domain`/`frontend_domain`/`shorturl_domain`. Nothing
else needs to change before Phase 1 below can run against it.

See "Local development environment" below for the one stage this repo
deliberately doesn't manage.

## Phase 1 — VPS base configuration (only needed for a fresh setup)

Turns a bare host into a hardened, rootless-Podman-capable one:

- Podman 5.x repo + package install (git, podman, netavark, firewall
  tooling, ...)
- creates the `admin` (sudo) and `service` (rootless Podman) users,
  distributes your local SSH public key to both
- rootless-Podman kernel/storage tuning
- **reboots the host partway through** (finalizes kernel/Podman/sysctl
  changes, deliberately before SSH gets hardened)
- firewall lockdown to SSH/80/443 only
- **disables root SSH login at the end**, and locks `admin` to key-only
  login (password auth disabled for that user specifically)

See `setup_vps.yml`'s own comments for the full task-by-task rationale.

```bash
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root
# If only password login is possible:
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root --ask-pass

# Re-run / verification (root login is disabled afterward):
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u admin --ask-become-pass --check --diff
```

The production host is already hardened — a first run here should only
serve as verification (`--check --diff`, expect near-zero diff), not a
fresh install.

## Maintaining secrets

`secrets/<stage>/caddy.env`, `secrets/<stage>/osa-backend.env.j2`,
`secrets/<stage>/osa-frontend.env.j2` live `ansible-vault`-encrypted in the
repo, never committed in plaintext. `secrets/<stage>/*.example` are the
(plaintext-safe) templates.

The two `.j2` files also contain Jinja2 expressions (`{{ backend_domain }}`
etc., filled in from `inventory/<stage>.ini`) alongside the real secrets —
vault encryption and Jinja2 templating don't conflict:
`ansible.builtin.template` decrypts the vault content first, then renders
the Jinja2, so the edit workflow below is unchanged:

```bash
cp secrets/production/osa-backend.env.j2.example secrets/production/osa-backend.env.j2
$EDITOR secrets/production/osa-backend.env.j2
ansible-vault encrypt secrets/production/osa-backend.env.j2

# Edit later:
ansible-vault edit secrets/production/osa-backend.env.j2
ansible-vault view secrets/production/osa-backend.env.j2
```

`secrets/<stage>/caddy.env`'s `LOGGING_USER`/`LOGGING_PASSWORD_HASH` follow
the same rule as `osa-backend-pg.env` below — generate a fresh password per
stage, never reuse another stage's, so a leaked non-production Dozzle login
can't double as a production one.
`osa-backend.env.j2`'s `KOOFR_USER`/`KOOFR_PASSWORD` are the **same across
every stage** (one shared Koofr account/backup history) — copy verbatim
from `secrets/production/osa-backend.env.j2` rather than generating new
ones, so `scripts/backup_db.py`/`restore_db.py` (see "Disaster recovery"
below) always see the same backup history regardless of stage. `SMTP_*`
should stay blank/commented on non-production stages (no mail sending
outside prod).

`secrets/<stage>/osa-backend-pg.env` follows the same `.example` workflow
as the two `.j2` files above — create it fresh per stage with a freshly
generated password, never reuse another stage's:
```bash
cp secrets/production/osa-backend-pg.env.example secrets/production/osa-backend-pg.env
$EDITOR secrets/production/osa-backend-pg.env
ansible-vault encrypt secrets/production/osa-backend-pg.env
```

## Phase 2 — day-2 ops: keeping secrets/quadlets/timers in sync

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --check --diff   # check first
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass                  # then apply
```

Syncs secrets, quadlet **definitions** (Caddy/Maintenance/Logging/Backend/
Frontend), the Caddyfile itself, and the maintenance timer; starts and
keeps running the entire stack (Caddy + Backend + Frontend + Maintenance +
Dozzle). Builds no images, clones no repos, restores no database.

### Immediate deploy (instead of waiting for the nightly auto-update run)

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-backend
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-frontend
```

Pulls the current `:latest` image and restarts the corresponding pod.

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

### Fresh or empty Postgres data directory

This applies whenever `osa-backend-pg` is about to start against a data
directory that doesn't have a Postgres cluster in it yet, i.e. `initdb`
still has to run — two concrete cases:

- **Standing up a stage for the first time** on a fresh VPS (see "Stages"
  above for `test`/`qa`) — the data directory doesn't exist yet at all.
- **A Postgres major-version bump** on a stage that's already running —
  the old data directory gets moved aside first (a new major version
  can't start against an old one's on-disk format), so the new version
  starts against an empty directory too.

In both cases, the image has to already be present locally *before*
`systemctl --user start osa-backend-pg.service` runs, or the pod can die
mid-startup: Quadlet's pod-exit-policy default tears the pod down the
moment it looks "empty" (no container of `osa-backend-pod` actually
running yet), and a first-time image pull that hasn't finished counts as
exactly that — if a slow pull for `osa-backend-pg` is still in progress
when the exit-policy check runs, it kills the pod mid-download instead of
letting the pull finish, `osa-backend` container included. Pre-pulling the
image explicitly removes that race entirely, since the pod then never
observes an "empty" window:

```bash
podman pull docker.io/library/postgres:<target-version>
systemctl --user start osa-backend-pg.service
```

## Local development environment

As flagged under "Stages" above: local development is intentionally
hand-set-up per developer, outside Ansible and outside this repo's stage
management — there is no `inventory/development.ini` and never will be.
What *does* live in this repo is a set of copyable example files under
`dev/` (Quadlets, env files, a Caddy snippet) that this section walks
through, so a fresh dev setup no longer has to be reverse-engineered from a
working machine.

None of the placeholders below are prescriptive: pick any domain you like
for local development and substitute it for `<your-dev-domain>` everywhere
it appears. Nothing in `dev/` hardcodes one specific domain.

### Directory layout

Unlike production/test/qa, local dev has no Caddy Quadlet (Caddy runs as a
regular host-wide service on the dev machine, shared with whatever else it
proxies, not containerized) and no `logging`/`maintenance` equivalents
(Dozzle and the prune timer are prod-only conveniences). Only backend and
frontend get Quadlets:

| `dev/` path | Copy to | Purpose |
|---|---|---|
| `dev/quadlets/backend/osa-backend-dev.build.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-dev.build` | Builds `localhost/osa-backend-dev:latest` from `osa-backend`'s own `Dockerfile`, `dev` target |
| `dev/quadlets/backend/osa-backend.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend.container` | Runs the backend, bind-mounts the repo, `uvicorn --reload` |
| `dev/quadlets/backend/osa-backend-pg.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-pg.container` | Dev's own PostgreSQL, separate from every Ansible-managed stage's |
| `dev/quadlets/backend/osa-backend.pod.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend.pod` | Publishes backend/Postgres ports, pod-wide settings |
| `dev/quadlets/frontend/osa-frontend.container.example` | `~/.config/containers/systemd/osa/osa-frontend/osa-frontend.container` | Runs `npm run dev`, bind-mounts the repo, no build/pod unit needed |
| `dev/env/osa-backend.env.example` | `~/.env/osa-backend.env` | Backend secrets/config (dev-sized subset, see below) |
| `dev/env/osa-backend-pg.env.example` | `~/.env/osa-backend-pg.env` | Dev Postgres credentials |
| `dev/env/osa-frontend.env.example` | `~/.env/osa-frontend.env` | Frontend build-time config |
| `dev/Caddyfile.dev.example` | (append into your own Caddyfile) | The two vhosts described below |

The target paths above assume both repos are cloned side-by-side under one
parent directory, e.g. `~/projects/osa-fastapi-vue/osa-backend` and
`.../osa-frontend` (the Quadlet files use `%h/projects/...` accordingly) —
adjust `SetWorkingDirectory=`/`Volume=` if your checkout lives elsewhere.
The `osa-backend.container.example`'s `After=` also assumes your host
Caddy runs as a unit named `caddy.service` — adjust if yours is named
differently, or drop that dependency entirely.

Copy each `.example` file to its target path (dropping the `.example`
suffix), `chmod 600` the env files, then reload systemd and start
everything:

```bash
mkdir -p ~/.config/containers/systemd/osa/osa-backend ~/.config/containers/systemd/osa/osa-frontend ~/.env

for f in dev/quadlets/backend/*.example; do
  cp "$f" ~/.config/containers/systemd/osa/osa-backend/"$(basename "${f%.example}")"
done
for f in dev/quadlets/frontend/*.example; do
  cp "$f" ~/.config/containers/systemd/osa/osa-frontend/"$(basename "${f%.example}")"
done
for f in dev/env/*.example; do
  cp "$f" ~/.env/"$(basename "${f%.example}")"
done
chmod 600 ~/.env/osa-backend.env ~/.env/osa-backend-pg.env ~/.env/osa-frontend.env
```

Edit each `~/.env/*.env` file next: fill in `<your-dev-domain>`,
`SECRET_KEY`, `POSTGRES_PASSWORD`, `DATABASE_URL`'s password, and
optionally `GOOGLE_CLIENT_ID`/`KOOFR_USER`/`KOOFR_PASSWORD`. Then:

```bash
systemctl --user daemon-reload
systemctl --user start osa-backend-pg.service osa-backend.service osa-frontend.service
```

### Caddy routing

Local dev reuses production's path-based split, just unauthenticated and
without Dozzle:

```
Your browser
   │  https://<your-dev-domain>
   ▼
Your host's Caddy (not containerized)
   ├─ /api, /api/*     → 127.0.0.1:21000 → osa-backend (uvicorn --reload)
   └─ everything else  → 127.0.0.1:21001 → osa-frontend (Vite dev server)
Your host's Caddy
   └─ go.<your-dev-domain> (optional, short-URL testing)
        └─ /*  → 127.0.0.1:21000 (with /go prefix) → osa-backend
```

Append `dev/Caddyfile.dev.example`'s two vhost blocks to your own Caddyfile
(location depends on how Caddy is installed on your machine — see Caddy's
own docs for the default path on your platform), replace
`<your-dev-domain>`, then reload Caddy. The `go.<your-dev-domain>` vhost is
optional, only needed for local short-URL redirect testing.

### First start: build, migrate, get a working login

Starting the pod builds the dev image and brings up an **empty** Postgres
database — unlike production's image, the `dev` build target has no
`docker-entrypoint.sh`, so migrations do not run automatically:

```bash
podman exec osa-backend alembic upgrade head
```

A freshly migrated database has zero users — there is currently no
seed/superuser-creation script in `osa-backend`. The practical way to get a
working login on a new dev setup is to pull down the latest production
backup — the same mechanism `osa-backend`'s scheduler already runs
automatically every night on any non-production `APP_ENVIRONMENT`,
triggered manually here instead of waiting for that nightly run:

```bash
podman exec -it osa-backend python scripts/restore_db.py
```

(No `--force` needed — that flag only guards `APP_ENVIRONMENT=production`.)
This needs working `KOOFR_USER`/`KOOFR_PASSWORD` in `~/.env/osa-backend.env`
— ask whoever maintains production for these if you don't have them yet.

Finally, `osa-frontend`'s Quadlet does not run `npm install` for you —
`node_modules` lives inside the bind-mounted repo on the host, so it must
be installed once manually before the frontend serves anything:

```bash
podman exec osa-frontend npm install
# or, if Node is also installed directly on the host:
cd osa-frontend && npm install
```

### Env files

`dev/env/*.example` are deliberately smaller than production's templates in
`secrets/production/*.example` — several Tier 2/3 settings that only
matter in production (mail sender identity, session/token lifetimes, Koofr
backup scheduling, `BACKUP_*`) are safe to leave unset on dev; see
`osa-backend`'s README for the full Tier 1/2/3 breakdown. A few dev-only
notes:

- `DATABASE_URL`'s port must match whichever host port
  `osa-backend.pod.example` publishes Postgres on (`5433` by default —
  adjust if `5432` is already bound by another project on your dev host).
- `VITE_API_BASE_URL` is a full absolute URL, not the relative `/api` the
  app otherwise recommends — the Vite dev server itself doesn't do the
  `/api` path split, only your own Caddy in front of it does. A different
  mechanism entirely from production's runtime-injected `API_BASE_URL`
  (see `osa-frontend`'s README for the `VITE_*` vs. runtime-config
  distinction, not repeated here).
- `TEST_DATABASE_URL` is dev/test-only, consumed by `osa-backend`'s pytest
  suite (`podman exec osa-backend pytest ...`, see `osa-backend`'s README)
  — not part of production's env template at all.
- SMTP is optional. Point `SMTP_HOST`/`SMTP_PORT` at any local
  SMTP-capturing tool you like, or leave both commented out — mail sending
  then just fails silently, same as every other non-production stage.

---

# Deutsch

Zentrales Betriebshandbuch für die Produktion von **OSA** (Orchester-Einteilung,
`einteilung.hochamt.at`): welches Repo wofür da ist, wie die Produktion
aufgebaut ist, und wie man sie aufsetzt und deployt. Dieses Repo selbst
enthält alles Betriebliche: Ansible-Playbooks, Caddy-Konfiguration,
Podman-Quadlets und (vault-verschlüsselte) Secrets.

## Die Repos im Überblick

| Repo | Was | Tech-Stack | Wird deployt als |
|---|---|---|---|
| [`osa-backend`](../osa-backend) | Backend: Dienstplan-/Besetzungsverwaltung für Kirchenmusiker | Python 3.12, FastAPI, SQLAlchemy, PostgreSQL | `osa-backend`-Pod |
| [`osa-frontend`](../osa-frontend) | Frontend zu `osa-backend`: Vue-3-SPA | Vue 3 (`<script setup>`, TypeScript), Vite, nginx | `osa-frontend`-Pod |
| `osa-deploy` (dieses Repo) | Betrieb: Ansible, Caddy, Quadlets, Secrets | Ansible, systemd Quadlets | läuft nicht selbst als Service — konfiguriert die anderen |

Beide App-Repos bauen ihr Container-Image selbst per CI/CD (GitHub Actions)
und pushen es bei jedem Merge nach `main` automatisch nach
`ghcr.io/kirchenmusik-st-augustin/<repo>:latest`. `osa-deploy` baut **keine**
Images und klont **keine** App-Repos auf den Produktionshost — es verteilt
nur Config und Secrets und sorgt dafür, dass die richtigen Images laufen.

## Architektur: rootless Podman auf einem VPS

Ein einzelner VPS trägt die gesamte Produktion — bewusst, nicht notgedrungen:
Für diese Größenordnung ist er das schlankeste Betriebsmodell überhaupt,
ganz ohne Cluster-Overhead. Rootless Podman macht das möglich, ohne bei der
Isolation Kompromisse einzugehen: Statt klassischer Root-Docker-Container
läuft alles **rootless** unter einem eigenen, unprivilegierten Linux-User
namens `service` — ein kompromittierter Container kann so nicht direkt auf
Host-Root eskalieren. Ein zweiter User `admin` existiert nur für
administrative Root-Aufgaben (`sudo`), er betreibt selbst keine Container.

- **systemd Quadlets** statt `docker-compose`: jeder Container/Pod wird als
  `.container`/`.pod`/`.volume`-Datei beschrieben, die `systemd --user`
  automatisch in einen echten systemd-Service übersetzt.
- **Zwei Pods für die App:** `osa-backend-pod` (der Backend-Container plus
  ein `osa-backend-pg`-PostgreSQL-Sidecar) und `osa-frontend-pod` (nginx) —
  der einzige laufende Stack.
- **Caddy** ist der einzige Dienst, der öffentlich auf Port 80/443 lauscht,
  terminiert TLS und reverse-proxied **pfadbasiert** auf dieselbe Domain:
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
- **Image-Bezug**: alle App-Container haben `Image=ghcr.io/.../<name>:latest`
  + `AutoUpdate=registry`. Podmans eigener `podman-auto-update.timer`
  (täglich) prüft, pullt bei Bedarf und startet neu — ganz ohne Ansible.
  `osa-deploy` wird nur gebraucht, um Config/Secrets/Quadlets initial bzw.
  bei Änderungen zu verteilen, und für einen Sofort-Deploy (siehe unten).
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
    gebraucht: der Produktions-Host ist bereits gehärtet, ein späterer Lauf
    verifiziert diesen Zustand nur noch (siehe Phase 1 unten).
  - **`admin`** — für Re-Runs von `setup_vps.yml` (Verifikation,
    `--check --diff`), braucht `sudo`/`become`.
  - **`service`** — für `deploy.yml` (fest als `remote_user: service`
    hinterlegt, kein `sudo` nötig).
- Ein Vault-Passwort für `secrets/<stage>/*.env`/`*.env.j2` (wird interaktiv
  abgefragt, kein `vault_password_file` im Repo).
- **DNS** für `backend_domain`/`frontend_domain`/`shorturl_domain` (siehe
  "Stages" unten) muss bereits auf den Ziel-VPS zeigen, bevor `deploy.yml`
  (Phase 2) läuft — für Phase 1 nicht nötig, die rührt Caddy gar nicht an.
  Caddy besorgt sich für jede Domain im Caddyfile automatisch ein
  TLS-Zertifikat von Let's Encrypt (siehe `config/caddy/Caddyfile.j2` —
  nirgends eine manuelle `tls`-Direktive oder Zertifikatsdateien), das
  heißt die ACME-Challenge muss den VPS tatsächlich unter genau dieser
  Domain über Port 80 erreichen können, um die Inhaberschaft zu beweisen.
  Löst eine Domain noch nicht auf (oder zeigt noch auf einen alten Host),
  scheitert die Zertifikatserstellung komplett — die Seite kommt dann
  nicht nur ohne HTTPS hoch, sie kommt gar nicht hoch. Wiederholte
  fehlgeschlagene Versuche gegen dieselbe Domain zehren außerdem an Let's
  Encrypts eigenen Rate-Limits, daher DNS lieber *vor* dem ersten
  `deploy.yml`-Lauf richtig setzen, statt live gegen den
  Produktions-ACME-Endpunkt zu debuggen. Vorab mit `dig +short <domain>`
  prüfen.

## Stages

`inventory/` enthält eine Datei pro Stage, die dieses Repo tatsächlich
deployen kann — `production.ini`, `test.ini`, `qa.ini`. Sowohl
`osa-backend` als auch `osa-frontend` kennen mit `development` einen
vierten gültigen `APP_ENVIRONMENT`-Wert, der aber bewusst kein Ansible-Ziel
hier ist: lokale Entwicklung läuft direkt über den Vite-Dev-Server auf der
Dev-Maschine, nie über dieses Repo. Jeder
Befehl unten braucht `-i inventory/<stage>.ini`; es gibt kein
Default-Inventory, ein fehlendes `-i` schlägt also laut fehl, statt still
die falsche Stage zu treffen. Nur `production` ist ein reales, aktuell
deployetes Ziel — `test`/`qa` sind Platzhalter-Skelette
(`CHANGEME.example.invalid`), bis für sie ein eigener VPS existiert.

Jede `inventory/<stage>.ini` trägt außerdem `backend_domain`,
`frontend_domain` und `shorturl_domain` — unabhängige Variablen (Backend
und Frontend müssen sich keine Domain teilen, siehe `osa-backend`s
`samesite=none`/CSRF-Origin-Check-Unterstützung für Cross-Domain), die zur
Deploy-Zeit ins Caddyfile und in `secrets/<stage>/*.env.j2` einfließen.
Auch das Postgres-Datenverzeichnis jeder Stage liegt unter einem eigenen
Pfad, `~/data/osa/<stage>/postgres` auf dem jeweiligen Host.

**`test`/`qa` auf einem frischen VPS aufsetzen:** `inventory/test.ini` bzw.
`inventory/qa.ini` direkt editieren — die Platzhalterdatei liegt bereits im
Repo, kein Kopierschritt nötig. Den `[vps]`-Eintrag durch den neuen Host
ersetzen (IP oder Hostname, alles, was `ssh` bereits erreicht), und alle
drei `CHANGEME.example.invalid`-Werte unter `[vps:vars]` durch die echten
`backend_domain`/`frontend_domain`/`shorturl_domain` dieser Stage ersetzen.
Mehr muss nicht angepasst werden, bevor Phase 1 unten dagegen laufen kann.

Siehe "Lokale Entwicklungsumgebung" unten für die eine Stage, die dieses
Repo bewusst nicht verwaltet.

## Phase 1 — VPS-Grundkonfiguration (nur bei Neuaufsetzen nötig)

Macht aus einem nackten Host ein gehärtetes, rootless-Podman-fähiges
System:

- Podman-5-Repo + Paketinstallation (git, podman, netavark,
  Firewall-Tooling, ...)
- legt die User `admin` (sudo) und `service` (rootless Podman) an,
  verteilt den eigenen lokalen SSH-Public-Key an beide
- rootless-Podman-Kernel-/Storage-Tuning
- **startet den Host mittendrin neu** (finalisiert Kernel-/Podman-/
  Sysctl-Änderungen, bewusst vor der SSH-Härtung)
- Firewall-Lockdown auf nur noch SSH/80/443
- **deaktiviert root-SSH-Login am Ende** und sperrt `admin` speziell auf
  Key-only (Passwort-Login für diesen User gezielt deaktiviert)

Die vollständige Task-für-Task-Begründung steht in `setup_vps.yml`s
eigenen Kommentaren.

```bash
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root
# Falls nur Passwort-Login moeglich ist:
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root --ask-pass

# Re-Run / Verifikation (root-Login ist danach deaktiviert):
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u admin --ask-become-pass --check --diff
```

Der Produktions-Host ist bereits gehärtet — ein erster Lauf hier sollte nur
zur Verifikation dienen (`--check --diff`, erwartet quasi kein Diff), nicht
zur Neuinstallation.

## Secrets pflegen

`secrets/<stage>/caddy.env`, `secrets/<stage>/osa-backend.env.j2`,
`secrets/<stage>/osa-frontend.env.j2` liegen `ansible-vault`-verschlüsselt
im Repo, nie im Klartext committet. `secrets/<stage>/*.example` sind die
(Klartext-sicheren) Vorlagen.

Die beiden `.j2`-Dateien enthalten zusätzlich Jinja2-Ausdrücke
(`{{ backend_domain }}` etc., befüllt aus `inventory/<stage>.ini`) neben
den echten Secrets -- Vault-Verschlüsselung und Jinja2-Templating stehen
sich nicht im Weg: `ansible.builtin.template` entschlüsselt den
Vault-Inhalt zuerst, rendert danach das Jinja2, der Edit-Workflow unten
bleibt also unverändert:

```bash
cp secrets/production/osa-backend.env.j2.example secrets/production/osa-backend.env.j2
$EDITOR secrets/production/osa-backend.env.j2
ansible-vault encrypt secrets/production/osa-backend.env.j2

# Später bearbeiten:
ansible-vault edit secrets/production/osa-backend.env.j2
ansible-vault view secrets/production/osa-backend.env.j2
```

`secrets/<stage>/caddy.env`s `LOGGING_USER`/`LOGGING_PASSWORD_HASH` folgen
derselben Regel wie `osa-backend-pg.env` unten — pro Stage ein frisches
Passwort generieren, niemals das einer anderen Stage wiederverwenden, damit
ein geleakter Nicht-Produktions-Dozzle-Login nicht auch als
Produktions-Login funktioniert.
`osa-backend.env.j2`s `KOOFR_USER`/`KOOFR_PASSWORD` sind **über alle Stages
hinweg gleich** (ein gemeinsames Koofr-Konto/Backup-Bestand) — verbatim aus
`secrets/production/osa-backend.env.j2` übernehmen statt neu zu generieren,
damit `scripts/backup_db.py`/`restore_db.py` (siehe "Disaster Recovery"
unten) stageunabhängig auf denselben Backup-Bestand zugreifen. `SMTP_*`
sollte auf Non-Prod-Stages leer/auskommentiert bleiben (kein Mailversand
außerhalb von Prod).

`secrets/<stage>/osa-backend-pg.env` folgt demselben `.example`-Workflow
wie die beiden `.j2`-Dateien oben — pro Stage frisch anlegen, mit einem neu
generierten Passwort, niemals das einer anderen Stage wiederverwenden:
```bash
cp secrets/production/osa-backend-pg.env.example secrets/production/osa-backend-pg.env
$EDITOR secrets/production/osa-backend-pg.env
ansible-vault encrypt secrets/production/osa-backend-pg.env
```

## Phase 2 — Tag-2-Betrieb: Secrets/Quadlets/Timer synchron halten

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --check --diff   # erst pruefen
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass                  # dann anwenden
```

Synct Secrets, Quadlet-**Definitionen** (Caddy/Maintenance/Logging/Backend/
Frontend), die Caddyfile selbst und den Maintenance-Timer; startet und hält
den kompletten Stack am Laufen (Caddy + Backend + Frontend + Maintenance +
Dozzle). Baut keine Images, klont keine Repos, restored keine Datenbank.

### Sofort-Deploy (statt auf den nächtlichen Auto-Update-Lauf zu warten)

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-backend
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-frontend
```

Pullt das aktuelle `:latest`-Image und startet den zugehörigen Pod neu.

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

### Frisches oder leeres Postgres-Datenverzeichnis

Das betrifft jeden Fall, in dem `osa-backend-pg` gegen ein Datenverzeichnis
startet, das noch keinen Postgres-Cluster enthält, `initdb` also erst noch
laufen muss — konkret zwei Situationen:

- **Eine Stage wird erstmalig aufgesetzt**, auf einem frischen VPS (siehe
  "Stages" oben für `test`/`qa`) — das Datenverzeichnis existiert dort
  noch gar nicht.
- **Ein Postgres-Major-Version-Bump** auf einer bereits laufenden Stage —
  das alte Datenverzeichnis wird vorher beiseitegeschoben (eine neue
  Major-Version kann nicht gegen das On-Disk-Format einer alten starten),
  die neue Version startet also ebenfalls gegen ein leeres Verzeichnis.

In beiden Fällen muss das Image bereits lokal vorhanden sein, *bevor*
`systemctl --user start osa-backend-pg.service` läuft, sonst kann der Pod
mitten im Start sterben: Quadlets Pod-Exit-Policy-Default reißt den Pod ab,
sobald er "leer" aussieht (noch kein Container von `osa-backend-pod`
tatsächlich läuft) — und ein noch nicht abgeschlossener Erstmalig-Pull
zählt genau als das: Läuft ein langsamer Pull für `osa-backend-pg` noch,
während die Exit-Policy-Prüfung greift, wird der Pod mittendrin
abgewürgt statt den Pull fertig abzuwarten — inklusive des
`osa-backend`-Containers. Das Image explizit vorab zu pullen entschärft
dieses Rennen komplett, weil der Pod dann nie in diesen "leeren" Zustand
gerät:

```bash
podman pull docker.io/library/postgres:<zielversion>
systemctl --user start osa-backend-pg.service
```

## Lokale Entwicklungsumgebung

Wie schon unter "Stages" oben angemerkt: lokale Entwicklung wird bewusst pro
Entwickler:in von Hand aufgesetzt, außerhalb von Ansible und außerhalb der
Stage-Verwaltung dieses Repos — es gibt kein `inventory/development.ini`
und wird auch keins geben. Was in diesem Repo aber liegt, ist eine Reihe
kopierbarer Beispieldateien unter `dev/` (Quadlets, Env-Dateien, ein
Caddy-Snippet), die dieser Abschnitt durchgeht, damit ein frisches
Dev-Setup nicht mehr von einer laufenden Maschine zurückentwickelt werden
muss.

Keiner der Platzhalter unten ist verbindlich vorgegeben: wählt für lokale
Entwicklung eine beliebige Domain und ersetzt `<your-dev-domain>` überall,
wo sie auftaucht. Nichts unter `dev/` hardcoded eine bestimmte Domain.

### Verzeichnisstruktur

Anders als Produktion/Test/QA hat lokale Entwicklung kein Caddy-Quadlet
(Caddy läuft als gewöhnlicher, host-weiter Dienst auf der Dev-Maschine,
gemeinsam genutzt mit allem anderen, was sie sonst noch proxyt, nicht
containerisiert) und keine `logging`-/`maintenance`-Entsprechung (Dozzle
und der Prune-Timer sind reine Prod-Annehmlichkeiten). Nur Backend und
Frontend bekommen Quadlets:

| `dev/`-Pfad | Kopieren nach | Zweck |
|---|---|---|
| `dev/quadlets/backend/osa-backend-dev.build.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-dev.build` | Baut `localhost/osa-backend-dev:latest` aus `osa-backend`s eigenem `Dockerfile`, Target `dev` |
| `dev/quadlets/backend/osa-backend.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend.container` | Startet das Backend, mountet das Repo, `uvicorn --reload` |
| `dev/quadlets/backend/osa-backend-pg.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-pg.container` | Eigenes Dev-PostgreSQL, getrennt von jeder Ansible-verwalteten Stage |
| `dev/quadlets/backend/osa-backend.pod.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend.pod` | Veröffentlicht Backend-/Postgres-Ports, Pod-weite Einstellungen |
| `dev/quadlets/frontend/osa-frontend.container.example` | `~/.config/containers/systemd/osa/osa-frontend/osa-frontend.container` | Startet `npm run dev`, mountet das Repo, keine Build-/Pod-Unit nötig |
| `dev/env/osa-backend.env.example` | `~/.env/osa-backend.env` | Backend-Secrets/-Config (dev-großer Teilumfang, siehe unten) |
| `dev/env/osa-backend-pg.env.example` | `~/.env/osa-backend-pg.env` | Dev-Postgres-Zugangsdaten |
| `dev/env/osa-frontend.env.example` | `~/.env/osa-frontend.env` | Frontend-Build-Zeit-Config |
| `dev/Caddyfile.dev.example` | (in die eigene Caddyfile einfügen) | Die beiden unten beschriebenen Vhosts |

Die Zielpfade oben setzen voraus, dass beide Repos nebeneinander unter
einem gemeinsamen Elternordner liegen, z. B.
`~/projects/osa-fastapi-vue/osa-backend` und `.../osa-frontend` (die
Quadlet-Dateien nutzen entsprechend `%h/projects/...`) —
`SetWorkingDirectory=`/`Volume=` anpassen, falls euer Checkout woanders
liegt. `osa-backend.container.example`s `After=` setzt außerdem voraus,
dass euer Host-Caddy als Unit namens `caddy.service` läuft — anpassen,
falls eure anders heißt, oder die Abhängigkeit ganz weglassen.

Jede `.example`-Datei an ihren Zielpfad kopieren (Endung `.example`
weglassen), die Env-Dateien `chmod 600`, dann systemd neu laden und alles
starten:

```bash
mkdir -p ~/.config/containers/systemd/osa/osa-backend ~/.config/containers/systemd/osa/osa-frontend ~/.env

for f in dev/quadlets/backend/*.example; do
  cp "$f" ~/.config/containers/systemd/osa/osa-backend/"$(basename "${f%.example}")"
done
for f in dev/quadlets/frontend/*.example; do
  cp "$f" ~/.config/containers/systemd/osa/osa-frontend/"$(basename "${f%.example}")"
done
for f in dev/env/*.example; do
  cp "$f" ~/.env/"$(basename "${f%.example}")"
done
chmod 600 ~/.env/osa-backend.env ~/.env/osa-backend-pg.env ~/.env/osa-frontend.env
```

Danach jede `~/.env/*.env`-Datei bearbeiten: `<your-dev-domain>`,
`SECRET_KEY`, `POSTGRES_PASSWORD`, das Passwort in `DATABASE_URL` sowie
optional `GOOGLE_CLIENT_ID`/`KOOFR_USER`/`KOOFR_PASSWORD` eintragen. Dann:

```bash
systemctl --user daemon-reload
systemctl --user start osa-backend-pg.service osa-backend.service osa-frontend.service
```

### Caddy-Routing

Lokale Entwicklung nutzt denselben pfadbasierten Split wie Produktion, nur
unauthentifiziert und ohne Dozzle:

```
Euer Browser
   │  https://<your-dev-domain>
   ▼
Euer Host-Caddy (nicht containerisiert)
   ├─ /api, /api/*     → 127.0.0.1:21000 → osa-backend (uvicorn --reload)
   └─ alles andere     → 127.0.0.1:21001 → osa-frontend (Vite-Dev-Server)
Euer Host-Caddy
   └─ go.<your-dev-domain> (optional, zum Testen von Kurz-URLs)
        └─ /*  → 127.0.0.1:21000 (mit /go-Präfix) → osa-backend
```

Die beiden Vhost-Blöcke aus `dev/Caddyfile.dev.example` in die eigene
Caddyfile einfügen (Speicherort abhängig davon, wie Caddy auf eurer
Maschine installiert ist — siehe Caddys eigene Doku für den Default-Pfad
auf eurer Plattform), `<your-dev-domain>` ersetzen, dann Caddy neu laden.
Der `go.<your-dev-domain>`-Vhost ist optional, nur nötig, um Kurz-URL-
Redirects lokal zu testen.

### Erster Start: Build, Migration, funktionierender Login

Der Pod-Start baut das Dev-Image und bringt eine **leere** Postgres-
Datenbank hoch — anders als das Produktions-Image hat das `dev`-Build-Target
kein `docker-entrypoint.sh`, Migrationen laufen also nicht automatisch:

```bash
podman exec osa-backend alembic upgrade head
```

Eine frisch migrierte Datenbank hat null User — es gibt aktuell kein
Seed-/Superuser-Anlage-Skript in `osa-backend`. Der praktikable Weg zu
einem funktionierenden Login auf einem neuen Dev-Setup ist, den letzten
Produktions-Backup herunterzuladen — derselbe Mechanismus, den
`osa-backend`s Scheduler ohnehin jede Nacht automatisch auf jedem
Non-Production-`APP_ENVIRONMENT` ausführt, hier manuell ausgelöst statt auf
diesen nächtlichen Lauf zu warten:

```bash
podman exec -it osa-backend python scripts/restore_db.py
```

(Kein `--force` nötig — das Flag schützt nur `APP_ENVIRONMENT=production`.)
Dafür werden funktionierende `KOOFR_USER`/`KOOFR_PASSWORD` in
`~/.env/osa-backend.env` gebraucht — wer immer Produktion betreut, danach
fragen, falls noch nicht vorhanden. Ohne Koofr-Zugang gibt es aktuell
keinen dokumentierten Weg zu einer befüllten, login-fähigen Dev-Datenbank
(siehe Anmerkung am Ende dieses Abschnitts).

Zuletzt: `osa-frontend`s Quadlet führt kein `npm install` für euch aus —
`node_modules` liegt innerhalb des gemounteten Repos auf dem Host, muss
also einmalig manuell installiert werden, bevor das Frontend überhaupt
etwas ausliefert:

```bash
podman exec osa-frontend npm install
# oder, falls Node auch direkt auf dem Host installiert ist:
cd osa-frontend && npm install
```

### Env-Dateien

`dev/env/*.example` sind bewusst kleiner als die Produktions-Vorlagen in
`secrets/production/*.example` — mehrere Tier-2/3-Einstellungen, die nur
in Produktion relevant sind (Mail-Absenderidentität, Session-/Token-
Laufzeiten, Koofr-Backup-Zeitplan, `BACKUP_*`), können auf Dev unbelegt
bleiben; die vollständige Tier-1/2/3-Aufschlüsselung steht in
`osa-backend`s README. Ein paar Dev-spezifische Hinweise:

- Der Port in `DATABASE_URL` muss zu dem Host-Port passen, den
  `osa-backend.pod.example` für Postgres veröffentlicht (`5433` per
  Default — anpassen, falls `5432` auf eurem Dev-Host schon von einem
  anderen Projekt belegt ist).
- `VITE_API_BASE_URL` ist eine vollständige absolute URL, nicht das
  relative `/api`, das die App sonst empfiehlt — der Vite-Dev-Server
  selbst macht den `/api`-Pfad-Split nicht, das übernimmt nur das eigene
  Caddy davor. Ein komplett anderer Mechanismus als das Runtime-injizierte
  `API_BASE_URL` in Produktion (siehe `osa-frontend`s README für die
  `VITE_*`-vs.-Runtime-Config-Unterscheidung, hier nicht wiederholt).
- `TEST_DATABASE_URL` ist nur für Dev/Test, genutzt von `osa-backend`s
  Pytest-Suite (`podman exec osa-backend pytest ...`, siehe
  `osa-backend`s README) — in der Produktions-Env-Vorlage gar nicht enthalten.
- SMTP ist optional. `SMTP_HOST`/`SMTP_PORT` auf ein beliebiges lokales
  SMTP-Capturing-Tool zeigen lassen, oder beide auskommentiert lassen —
  Mailversand schlägt dann einfach still fehl, wie auf jeder anderen
  Non-Production-Stage.
