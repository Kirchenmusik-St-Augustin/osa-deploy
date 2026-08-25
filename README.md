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

`osa-logging` (Loki/Grafana/Fluent-Bit) is **fully retired** (never really
used) — the one remaining log viewer, Dozzle, is now part of `osa-deploy`
itself (`quadlets/logging/`).

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
administrative root tasks (`sudo`), it never runs containers itself.

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
    fresh host. Not needed for *regular* use of this repo: the real
    production host is already hardened via `osa/local-deployment`'s
    near-identical playbook.
  - **`admin`** — for re-runs of `setup_vps.yml` (verification,
    `--check --diff`), needs `sudo`/`become`.
  - **`service`** — for `deploy.yml` (hardcoded as `remote_user: service`,
    no `sudo` needed).
- A vault password for `secrets/<stage>/*.env`/`*.env.j2` (asked
  interactively, no `vault_password_file` in the repo).

## Stages

`inventory/` holds one file per stage this repo can actually deploy to —
`production.ini`, `test.ini`, `qa.ini`. `osa-backend` has a fourth valid
`APP_ENVIRONMENT` value, `development`, but that one is deliberately not an
Ansible target here: local development runs against the Vite dev server
directly on the dev machine, never through this repo. Every command below
takes `-i inventory/<stage>.ini`; there is no default inventory, so a
missing `-i` fails loudly instead of silently targeting the wrong stage.
Only `production` is a real, currently deployed target — `test`/`qa` are
placeholder skeletons (`CHANGEME.example.invalid`) until a dedicated VPS
exists for them. Each
stage's `inventory/<stage>.ini` also carries `backend_domain`,
`frontend_domain`, and `shorturl_domain` — independent variables (backend
and frontend don't have to share a domain, see `osa-backend`'s
`samesite=none`/CSRF-origin-check cross-domain support), rendered into the
Caddyfile and into `secrets/<stage>/*.env.j2` at deploy time. Each stage's
Postgres data also lives under its own path, `~/data/osa/<stage>/postgres`
on that stage's host. See "Local development environment" below for the
one stage this repo deliberately doesn't manage.

## Local development environment

Building on the note above: local development is intentionally hand-set-up
per developer, outside Ansible and outside this repo's stage management —
there is no `inventory/development.ini` and never will be. What *does*
live in this repo is a set of copyable example files under `dev/`
(Quadlets, env files, a Caddy snippet) that this section walks through, so
a fresh dev setup no longer has to be reverse-engineered from a working
machine.

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
Without Koofr access, there is currently no documented way to get a
populated, login-capable dev database (see the note at the end of this
section).

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

### Open gap: no seed data without Koofr access

This documents the environment as it actually works today, not as it
"should" work: a new developer without Koofr credentials currently has no
way to reach a login-capable dev database other than asking for those
credentials. Given this is presently a one-person project, that is a
reasonable state to document and move on from rather than build against
speculatively — a minimal seed-user script would be a small, independent
follow-up if/when a second developer actually needs one, not part of this
change.

## Phase 1 — VPS base configuration (only needed for a fresh setup)

```bash
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root
# If only password login is possible:
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root --ask-pass

# Re-run / verification (root login is disabled afterward):
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u admin --ask-become-pass --check --diff
```

The real production host is already hardened via `osa/local-deployment`'s
near-identical playbook — a first run here should only serve as
verification (`--check --diff`, expect near-zero diff), not a fresh
install.

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

## Maintaining secrets

`secrets/<stage>/caddy.env`, `secrets/<stage>/osa-backend.env.j2`,
`secrets/<stage>/osa-frontend.env.j2` live `ansible-vault`-encrypted in the
repo, never committed in plaintext. The two `.j2` files also contain Jinja2
expressions (`{{ backend_domain }}` etc., filled in from
`inventory/<stage>.ini`) alongside the real secrets — vault encryption and
Jinja2 templating don't conflict: `ansible.builtin.template` decrypts the
vault content first, then renders the Jinja2, so the edit workflow below is
unchanged. `secrets/<stage>/*.example` are the (plaintext-safe) templates:

```bash
cp secrets/production/osa-backend.env.j2.example secrets/production/osa-backend.env.j2
$EDITOR secrets/production/osa-backend.env.j2
ansible-vault encrypt secrets/production/osa-backend.env.j2

# Edit later:
ansible-vault edit secrets/production/osa-backend.env.j2
ansible-vault view secrets/production/osa-backend.env.j2
```

`secrets/<stage>/caddy.env` should reuse `osa-infrastructure`'s
**already-existing** `LOGGING_USER`/`LOGGING_PASSWORD_HASH` values (don't
regenerate) — the Dozzle login doesn't need to change.
`osa-backend.env.j2`'s `KOOFR_USER`/`KOOFR_PASSWORD` are the **same across
every stage** (one shared Koofr account/backup history) — copy verbatim
from `secrets/production/osa-backend.env.j2` rather than generating new
ones, so `scripts/backup_db.py`/`restore_db.py` (see below) always see the
same backup history regardless of stage. `SMTP_*` should stay
blank/commented on non-production stages (no mail sending outside prod).

`secrets/<stage>/osa-backend-pg.env` follows the same `.example` workflow
as the two `.j2` files above — create it fresh per stage with a freshly
generated password, never reuse another stage's:
```bash
cp secrets/production/osa-backend-pg.env.example secrets/production/osa-backend-pg.env
$EDITOR secrets/production/osa-backend-pg.env
ansible-vault encrypt secrets/production/osa-backend-pg.env
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

Starting `osa-backend-pg` against a brand-new, empty data directory (a
fresh VPS setup, or a Postgres major-version bump) needs its image already
present locally *before* the pod starts: Quadlet's pod-exit-policy default
tears the pod down the moment it looks momentarily empty, which can race a
slow first-time image pull and kill it mid-download (hit during the
2026-08-25 PostgreSQL 18 upgrade). Pre-pull explicitly first:

```bash
podman pull docker.io/library/postgres:<target-version>
systemctl --user start osa-backend-pg.service
```

## Retiring the old repos

`osa-deploy` fully replaces `osa/local-deployment` **and**
`osa-infrastructure` **and** `osa-logging` (not additively) — a single,
authoritative deploy repo. All three, plus `osa/osa-einteilung.hochamt.at`
(the original Laravel application), are archived on GitHub (read-only, not
deleted).

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

`osa-logging` (Loki/Grafana/Fluent-Bit) ist **restlos stillgelegt** (nie
wirklich genutzt) — der einzige verbleibende Log-Viewer, Dozzle, ist jetzt
Teil von `osa-deploy` selbst (`quadlets/logging/`).

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
    gebraucht: der reale Produktions-Host ist bereits über `osa/
    local-deployment`s fast identisches Playbook gehärtet.
  - **`admin`** — für Re-Runs von `setup_vps.yml` (Verifikation,
    `--check --diff`), braucht `sudo`/`become`.
  - **`service`** — für `deploy.yml` (fest als `remote_user: service`
    hinterlegt, kein `sudo` nötig).
- Ein Vault-Passwort für `secrets/<stage>/*.env`/`*.env.j2` (wird interaktiv
  abgefragt, kein `vault_password_file` im Repo).

## Stages

`inventory/` enthält eine Datei pro Stage, die dieses Repo tatsächlich
deployen kann — `production.ini`, `test.ini`, `qa.ini`. `osa-backend` kennt
mit `development` einen vierten gültigen `APP_ENVIRONMENT`-Wert, der aber
bewusst kein Ansible-Ziel hier ist: lokale Entwicklung läuft direkt über
den Vite-Dev-Server auf der Dev-Maschine, nie über dieses Repo. Jeder
Befehl unten braucht `-i inventory/<stage>.ini`; es gibt kein
Default-Inventory, ein fehlendes `-i` schlägt also laut fehl, statt still
die falsche Stage zu treffen. Nur `production` ist ein reales, aktuell
deployetes Ziel — `test`/`qa` sind Platzhalter-Skelette
(`CHANGEME.example.invalid`), bis für sie ein eigener VPS existiert. Jede
`inventory/<stage>.ini` trägt außerdem
`backend_domain`, `frontend_domain` und `shorturl_domain` — unabhängige
Variablen (Backend und Frontend müssen sich keine Domain teilen, siehe
`osa-backend`s `samesite=none`/CSRF-Origin-Check-Unterstützung für
Cross-Domain), die zur Deploy-Zeit ins Caddyfile und in
`secrets/<stage>/*.env.j2` einfließen. Auch das Postgres-Datenverzeichnis
jeder Stage liegt unter einem eigenen Pfad, `~/data/osa/<stage>/postgres`
auf dem jeweiligen Host. Siehe "Lokale Entwicklungsumgebung" unten für die
eine Stage, die dieses Repo bewusst nicht verwaltet.

## Lokale Entwicklungsumgebung

Anschließend an die Anmerkung oben: lokale Entwicklung wird bewusst pro
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
Datenbank hoch — anders als Produktions Image hat das `dev`-Build-Target
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

`dev/env/*.example` sind bewusst kleiner als Produktions Vorlagen in
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
  Caddy davor. Ein komplett anderer Mechanismus als Produktions
  Runtime-injiziertes `API_BASE_URL` (siehe `osa-frontend`s README für die
  `VITE_*`-vs.-Runtime-Config-Unterscheidung, hier nicht wiederholt).
- `TEST_DATABASE_URL` ist nur für Dev/Test, genutzt von `osa-backend`s
  Pytest-Suite (`podman exec osa-backend pytest ...`, siehe
  `osa-backend`s README) — in Produktions Env-Vorlage gar nicht enthalten.
- SMTP ist optional. `SMTP_HOST`/`SMTP_PORT` auf ein beliebiges lokales
  SMTP-Capturing-Tool zeigen lassen, oder beide auskommentiert lassen —
  Mailversand schlägt dann einfach still fehl, wie auf jeder anderen
  Non-Production-Stage.

### Offene Lücke: keine Seed-Daten ohne Koofr-Zugang

Das dokumentiert die Umgebung, wie sie heute tatsächlich funktioniert,
nicht wie sie "sein sollte": eine neue Entwickler:in ohne
Koofr-Zugangsdaten hat aktuell keinen Weg zu einer login-fähigen
Dev-Datenbank außer danach zu fragen. Da dies derzeit ein
Ein-Personen-Projekt ist, ist das ein vertretbarer Zustand, den man
dokumentiert und dabei belässt, statt spekulativ dagegen zu bauen — ein
minimales Seed-User-Skript wäre ein kleiner, unabhängiger Folgeschritt,
falls/wenn eine zweite Person das tatsächlich braucht, nicht Teil dieser
Änderung.

## Phase 1 — VPS-Grundkonfiguration (nur bei Neuaufsetzen nötig)

```bash
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root
# Falls nur Passwort-Login moeglich ist:
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root --ask-pass

# Re-Run / Verifikation (root-Login ist danach deaktiviert):
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u admin --ask-become-pass --check --diff
```

Der reale Produktions-Host ist bereits über `osa/local-deployment`s fast
identisches Playbook gehärtet — ein erster Lauf hier sollte nur zur
Verifikation dienen (`--check --diff`, erwartet quasi kein Diff), nicht zur
Neuinstallation.

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

## Secrets pflegen

`secrets/<stage>/caddy.env`, `secrets/<stage>/osa-backend.env.j2`,
`secrets/<stage>/osa-frontend.env.j2` liegen `ansible-vault`-verschlüsselt
im Repo, nie im Klartext committet. Die beiden `.j2`-Dateien enthalten
zusätzlich Jinja2-Ausdrücke (`{{ backend_domain }}` etc., befüllt aus
`inventory/<stage>.ini`) neben den echten Secrets -- Vault-Verschlüsselung
und Jinja2-Templating stehen sich nicht im Weg: `ansible.builtin.template`
entschlüsselt den Vault-Inhalt zuerst, rendert danach das Jinja2, der
Edit-Workflow unten bleibt also unverändert. `secrets/<stage>/*.example`
sind die (Klartext-sicheren) Vorlagen:

```bash
cp secrets/production/osa-backend.env.j2.example secrets/production/osa-backend.env.j2
$EDITOR secrets/production/osa-backend.env.j2
ansible-vault encrypt secrets/production/osa-backend.env.j2

# Später bearbeiten:
ansible-vault edit secrets/production/osa-backend.env.j2
ansible-vault view secrets/production/osa-backend.env.j2
```

`secrets/<stage>/caddy.env` sollte `osa-infrastructure`s **bereits
bestehende** `LOGGING_USER`/`LOGGING_PASSWORD_HASH`-Werte übernehmen (nicht
neu generieren) — der Dozzle-Login muss sich nicht ändern.
`osa-backend.env.j2`s `KOOFR_USER`/`KOOFR_PASSWORD` sind **über alle Stages
hinweg gleich** (ein gemeinsames Koofr-Konto/Backup-Bestand) — verbatim aus
`secrets/production/osa-backend.env.j2` übernehmen statt neu zu generieren,
damit `scripts/backup_db.py`/`restore_db.py` (siehe unten) stageunabhängig
auf denselben Backup-Bestand zugreifen. `SMTP_*` sollte auf Non-Prod-Stages
leer/auskommentiert bleiben (kein Mailversand außerhalb von Prod).

`secrets/<stage>/osa-backend-pg.env` folgt demselben `.example`-Workflow
wie die beiden `.j2`-Dateien oben — pro Stage frisch anlegen, mit einem neu
generierten Passwort, niemals das einer anderen Stage wiederverwenden:
```bash
cp secrets/production/osa-backend-pg.env.example secrets/production/osa-backend-pg.env
$EDITOR secrets/production/osa-backend-pg.env
ansible-vault encrypt secrets/production/osa-backend-pg.env
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

`osa-backend-pg` gegen ein brandneues, leeres Datenverzeichnis zu starten
(frisches VPS-Setup oder ein Postgres-Major-Version-Bump) braucht das
zugehörige Image bereits lokal vorhanden, *bevor* der Pod startet:
Quadlets Pod-Exit-Policy-Default reißt den Pod ab, sobald er kurzzeitig
leer aussieht — das kann einen langsamen Erstmalig-Pull überholen und
mittendrin abwürgen (aufgetreten beim PostgreSQL-18-Upgrade am
25.08.2026). Erst explizit vorab pullen:

```bash
podman pull docker.io/library/postgres:<zielversion>
systemctl --user start osa-backend-pg.service
```

## Ablösung der Alt-Repos

`osa-deploy` ersetzt `osa/local-deployment` **und** `osa-infrastructure`
**und** `osa-logging` vollständig (nicht additiv) — ein einziges,
autoritatives Deploy-Repo. Alle drei, plus `osa/osa-einteilung.hochamt.at`
(die ursprüngliche Laravel-Anwendung), sind auf GitHub archiviert
(read-only, nicht gelöscht).
