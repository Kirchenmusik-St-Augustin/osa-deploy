# osa-deploy

[Deutsch](README.de.md) · [Hand-over sheet](HANDOVER.md)

Operations repository for **OSA** (`einteilung.hochamt.at`, the scheduling and
casting system of Kirchenmusik St. Augustin): Ansible playbooks, Caddy
configuration, Podman Quadlets and the `./osa` command that runs them. It
contains **no secrets and no installation data**. The inventory, the
vault-encrypted secrets, the operator key and the access note of every stage
live in the private site repository `osa-access`, which sits next to this
repository (`../osa-access/sites/<stage>/`).

## 1. Where to start

| Situation | Go to |
|---|---|
| The site is down or misbehaves | [3. When something is down](#3-when-something-is-down) |
| A new server, or moving to another host | [4. New build or migration](#4-new-build-or-migration) |
| Deploy a change, pin or roll back an image | [5. The ./osa command](#5-the-osa-command) |
| Set up a development machine | [6. Local development](#6-local-development) |
| Take over operations from someone else | [HANDOVER.md](HANDOVER.md) |
| Which secret lives where | [7.1 Secrets](#71-secrets) |

## 2. Architecture

### 2.1 The repositories

| Repository | What | Deployed as |
|---|---|---|
| [`osa-backend`](https://github.com/Kirchenmusik-St-Augustin/osa-backend) | FastAPI, SQLAlchemy, PostgreSQL, arq worker | `osa-backend` pod |
| [`osa-frontend`](https://github.com/Kirchenmusik-St-Augustin/osa-frontend) | Vue 3 single page application served by nginx | `osa-frontend` pod |
| [`osa-deploy`](https://github.com/Kirchenmusik-St-Augustin/osa-deploy) (this one) | playbooks, templates, Quadlets, the `./osa` command | configures the others |
| `osa-access` (private) | inventory, vault-encrypted secrets, operator key, access note | read by `./osa` |

The CI of both application repositories builds an image on every merge to
`main` and pushes it to `ghcr.io/kirchenmusik-st-augustin/<repo>` as `latest`
and as the full commit SHA. This repository builds no image and clones no
application repository onto the host; it distributes configuration and
secrets and makes sure the right images run.

### 2.2 Runtime layout

One VPS carries a stage. Everything runs **rootless** under the unprivileged
user `service`; the user `admin` exists only for root tasks (`sudo`) and never
runs containers. Containers are described as systemd Quadlets
(`.container`, `.pod`, `.volume`), which `systemd --user` turns into services.

- `osa-backend-pod`: the backend, an `osa-backend-pg` PostgreSQL sidecar, an
  `osa-backend-valkey` queue sidecar and the `osa-backend-worker` (arq jobs:
  mail, backup, downsync, housekeeping).
- `osa-frontend-pod`: nginx with the single page application.
- Caddy is the only public listener (80/443), terminates TLS and proxies by
  path:

  ```
  Internet :80/:443
     ▼
  Caddy (host network)
     └─ einteilung.hochamt.at
          ├─ /logging/dozzle*  → 127.0.0.1:8081 → Dozzle (basic auth)
          ├─ /go/*             → redirect → go.hochamt.at
          ├─ /api, /api/*      → 127.0.0.1:21000 → osa-backend-pod (prefix stripped)
          └─ everything else   → 127.0.0.1:21001 → osa-frontend-pod
     └─ go.hochamt.at (short URLs) → 127.0.0.1:21000 (with /go prefix)
  ```
- The application containers use `Image=…:latest` and `AutoUpdate=registry`:
  Podman's own `podman-auto-update.timer` pulls and restarts them daily,
  without Ansible; the first `./osa deploy` of a stage enables that timer (the
  package ships it switched off). `./osa deploy` is needed to distribute configuration, for
  an immediate deploy and to pin a release.
- Dozzle is a read-only log viewer behind basic auth. The weekly
  `podman-prune.timer` runs `podman system prune -a -f` on the host.

### 2.3 Container hardening

All containers run with `ReadOnly=true`, `NoNewPrivileges=true` and
`DropCapability=all`, with a small `tmpfs` where a process must write. Images
that would otherwise drop privileges internally run directly as their own user
(`User=101:101` for nginx, `User=999:999` for PostgreSQL). Memory and process
limits are set per container. The comments in `templates/` and `quadlets/`
carry the numbers and their derivation. The shutdown timeouts of the backend
and the worker are layered on purpose: `StopTimeout=` (Podman) and
`TimeoutStopSec=` (systemd) exceed the graceful-shutdown window of gunicorn
and arq, so a restart never cuts off a request or a running backup. CI of
`osa-backend` builds the production image and runs both containers under
these restrictions before a release.

### 2.4 Stages and the site repository

`./osa` knows the stages `production`, `qa` and `test` (the value of
`app_environment`). A stage is a directory of the site repository:

```
osa-access/sites/<stage>/
  inventory.ini          host, domains, optional image pins
  operator_key.vault     SSH key of the operator (vault-encrypted)
  operator_keys.pub      public key(s) installed for admin and service
  secrets/               vault-encrypted environment files
```

`inventory.ini` carries `app_environment`, `backend_domain`,
`frontend_domain`, `shorturl_domain` and `acme_email`. The domains are
independent (backend and frontend may share one, as on production, or not) and
are rendered into the Caddyfile and into the `*.env.j2` files at deploy time.
Each stage keeps its database under `~/data/osa/<stage>/postgres` on its
host. `development` is deliberately not a stage here: local development runs
on the developer's machine (chapter 6).

Only `production` is deployed today. `test` and `qa` are created the same way
when a server exists for them (section 4.5).

## 3. When something is down

### 3.1 Find out what is wrong

```bash
./osa ssh production                      # shell as the service user
podman ps -a                              # containers and their health
systemctl --user list-units --failed
journalctl --user -u osa-backend-pod -n 100
```

The log viewer is at `https://einteilung.hochamt.at/logging/dozzle` (basic
auth, the login is in `secrets/caddy.env`). The units are `caddy.service`,
`osa-backend-pod.service`, `osa-frontend-pod.service`,
`logging-dozzle.service` and `podman-prune.timer`.

### 3.2 Restart

Restart the **pod service**, never a single container: a container is bound to
its pod, so restarting it alone stops the pod and nothing brings it back.

```bash
systemctl --user restart osa-backend-pod.service    # backend, worker, PostgreSQL, Valkey
systemctl --user restart osa-frontend-pod.service
systemctl --user restart caddy.service
```

### 3.3 Roll back an image

See section 5.3: pin the previous release and deploy.

### 3.4 Restore the database

The scripts live in `osa-backend` and run on the target system:

```bash
./osa ssh production
podman exec osa-backend-worker python scripts/backup_db.py --list      # available backups
podman exec -it osa-backend-worker python scripts/restore_db.py --force
```

Run the scripts in `osa-backend-worker`, not in `osa-backend`: only the worker
receives the Koofr credentials (7.1), the web container stops with
`KOOFR_USER is not set`. `--force` is mandatory under
`APP_ENVIRONMENT=production`. The script names
the target database and the backup and asks for the database name before it
replaces anything. Afterwards check that real data is there, not only that the
command exited cleanly. A restore wipes the `public` schema and terminates
every other session of the database; PostgreSQL does not let `osa_app`
terminate a session of the bootstrap role, so keep no such session open on the
application database (use the `postgres` database for administration).

**Restart the backend pod after every restore** (`systemctl --user restart
osa-backend-pod.service`, see 3.2). The web container was already running when
the restore replaced the schema, so its connections and schema cache still
belong to the database as it was before. If the dump is older than the Alembic
head of the running image, the restart also runs the pending migrations on
the restored data: the web container is the only one that migrates. Then check
`alembic_version` and the row counts of a few large tables.

Every non-production stage restores the latest production backup by itself
every night (job `downsync`, one hour after the production backup), so `test`
and `qa` stay close to production. A manual backup before a risky change:
`podman exec osa-backend-worker python scripts/backup_db.py`. All parameters are in
the README of `osa-backend`.

**Empty data directory.** When `osa-backend-pg` starts against a directory
without a cluster (a new stage, or after a PostgreSQL major-version bump that
moved the old directory aside), the database comes up migrated but empty. Pull
the image first (`podman pull docker.io/library/postgres:<version>`, the
version is in `templates/osa-backend-pg.container.j2`) and start the service
in the same sitting: Quadlet tears a pod down when it looks empty, and an
unfinished pull counts as empty. The weekly prune removes images that no
container uses, so a pre-pulled image must be started within the week or the
prune timer stopped for the gap. The role `osa_app` lives in the data
directory too: the backend cannot log in until the next `./osa deploy` has set
it up. Then restore data with `podman exec -it osa-backend-worker python
scripts/restore_db.py` (no `--force` on non-production stages). On production
use `--force` with the backup taken right before the bump.

## 4. New build or migration

### 4.1 Prerequisites

- On the workstation: `git`, `ssh`, `ansible` (the full package: the playbooks use the collections
  `ansible.posix` and `community.general`), `sshpass` (for the first run
  against a password login) and Python 3.12 or newer.
- The repositories `osa-deploy` and `osa-access`, cloned side by side. The
  vault password (see [HANDOVER.md](HANDOVER.md)).
- A server with Debian 13 or another release with **Podman 5 or newer** from
  the distribution's own repository. Podman 5.0 is a hard requirement (the
  Quadlet `.pod` type exists from 5.0). `setup_vps.yml` aborts before any
  destructive step if the version is lower.

  | Distribution | Native Podman |
  |---|---|
  | Debian 13 | 5.4.2 |
  | Ubuntu 26.04 LTS | 5.7.0 |
  | Rocky / AlmaLinux / CentOS Stream 10 | 5.6.0+ |
  | Fedora (current) | 5.8.x |
  | openSUSE Leap 16.0 | 5.4.2 |

  Too old: Debian 12, Ubuntu 24.04, RHEL-family 9.x, openSUSE Leap 15.x.
- **DNS** of `backend_domain`, `frontend_domain` and `shorturl_domain` must
  point at the server before the first `./osa deploy`. Caddy requests the
  certificates from Let's Encrypt by itself and needs port 80 to reach the
  server; a domain that does not resolve yet makes certificate issuance fail
  and counts against the rate limits of Let's Encrypt. Check with
  `dig +short <domain>`.

### 4.2 Create the stage

```bash
./osa init production        # asks for a NEW vault password (20 characters or more)
$EDITOR ../osa-access/sites/production/inventory.ini
```

`init` copies the template from `site-template/`, generates the operator key
pair, encrypts the private key with the vault password and stores the public
key in `operator_keys.pub`. For a migration use a different directory name
and name the environment: `./osa init production-new --environment production`.

Then create the secret files from their templates (section 7.1 explains each
value):

```bash
cd ../osa-access/sites/production/secrets
for f in caddy.env osa-backend.env.j2 osa-backend-worker.env osa-backend-pg.env \
         osa-backend-valkey.env osa-frontend.env.j2; do cp $f.example $f; done
$EDITOR *.env *.env.j2                                    # fill in the values
cd ../../../../osa-deploy
./osa vault production encrypt secrets/caddy.env secrets/osa-backend.env.j2 \
    secrets/osa-backend-worker.env secrets/osa-backend-pg.env \
    secrets/osa-backend-valkey.env secrets/osa-frontend.env.j2
```

### 4.3 Prepare the server (once)

```bash
./osa setup production -u root --ask-pass -e full_upgrade=true      # first run
./osa setup production -u admin --ask-become-pass --check --diff    # later: verification
```

The playbook installs the packages from the distribution, checks the Podman
version, creates `admin` (sudo) and `service` (rootless Podman), installs the
keys from `operator_keys.pub` for both, tunes rootless Podman, **reboots
partway through**, locks the firewall down to SSH/80/443 (SSH rate-limited)
and hardens SSH at the end (key-only login, no root login, three attempts,
no X11). It fails if a file below `/etc/ssh/sshd_config.d/` overrides these
settings. Daily unattended security updates are switched on. Connect once by
hand (`ssh service@<host>`) and verify the host fingerprint over an
independent channel before the first run: host key checking is strict.

A server that was set up before the operator key existed needs the key once
by hand, because `./osa` offers only that key: append the line of
`operator_keys.pub` to `~/.ssh/authorized_keys` of both `admin` and `service`
(through an existing login), then check with `./osa ssh <stage>`.

The first run generates the sudo password of `admin` and prints it twice: right
after it is set and again in a box at the end of the run. Write it into the
password manager and into the access note (`./osa vault <stage> edit
../../credentials.md`) at once; it exists nowhere else, and every later
`./osa setup` run needs it as the BECOME password.

`./osa setup` installs exactly the keys of `operator_keys.pub` for `admin` and
`service` and removes every other key from their `authorized_keys`. Run it
only after the operator key has been checked with `./osa ssh <stage>`, and
never keep a key on the server that is missing from `operator_keys.pub`.

### 4.4 First deploy

```bash
./osa check production        # dry run with diff
./osa deploy production
```

Then pull the PostgreSQL image as described in 3.4 if the data directory is
new, restore the data (3.4) and run the check in 5.4.

### 4.5 Test and QA stages

A stage is **permanent** (the server keeps serving as `test` or `qa`) or a
**one-off smoke test** (a server that only validates the tooling and is torn
down). Both are a directory in `osa-access`; for a one-off test do the work on
a throwaway branch of `osa-access` that is never merged, so the real host
and the encrypted files of the trial never enter the main history. Non-production
stages use the **read-only** Koofr account in `osa-backend-worker.env`
(section 7.1) and keep `SMTP_*` commented out in `osa-backend.env.j2`.

## 5. The ./osa command

### 5.1 Commands

| Command | Does |
|---|---|
| `./osa init <stage> [--environment E]` | create a stage from the template with a new operator key |
| `./osa setup <stage> [ansible options]` | `setup_vps.yml` |
| `./osa check <stage> [ansible options]` | `deploy.yml --check --diff` |
| `./osa deploy <stage> [ansible options]` | `deploy.yml` |
| `./osa ssh <stage> [ssh options]` | shell as `service` with the operator key |
| `./osa vault <stage> encrypt\|decrypt\|edit\|view FILE…` | work on files relative to the stage directory |
| `./osa vault <stage> rekey` | re-encrypt every vault file of the stage under a new password |

`--sites-dir DIR` (before the command) points at another site directory. The
command asks for the vault password, unpacks the password file and the
operator key into a private directory in `/dev/shm` for the duration of the
run and removes both afterwards. Everything after the stage name goes to
`ansible-playbook` unchanged.

### 5.2 Day-2 deploy

`./osa check production` and `./osa deploy production` sync the secrets, the
Quadlet definitions, the Caddyfile and the native prune units and keep the
whole stack running. They build no image, clone no repository and restore no
database. A changed secret file restarts exactly the units that read it:
`caddy.env` restarts Caddy; `osa-backend.env`, `osa-backend-worker.env`,
`osa-backend-pg.env` and `osa-backend-valkey.env` restart the backend pod;
`osa-frontend.env` restarts the frontend pod. A new `POSTGRES_PASSWORD` alone
is not enough: PostgreSQL applies it only to an empty data directory, so an
existing database also needs `ALTER ROLE`.

**Immediate deploy** instead of waiting for the nightly auto-update:

```bash
./osa deploy production --tags deploy-backend
./osa deploy production --tags deploy-frontend
```

This pulls the image tag the Quadlet uses and restarts the pod only when the
running container does not use that image yet. Add `-e force_restart=true` to
restart regardless.

### 5.3 Pin a release (hold back or roll back)

The image tag comes from two optional inventory variables and defaults to
`latest`:

| Variable | Containers | Value |
|---|---|---|
| `osa_backend_image_tag` | `osa-backend` and `osa-backend-worker` together | full commit SHA of a release |
| `osa_frontend_image_tag` | `osa-frontend` | full commit SHA of a release |

Backend and worker share a variable on purpose: a worker must never run
against a schema the web container (the only one that migrates) has not
migrated. The SHA is the tag CI pushes next to `latest`; to roll back take the
merge commit before the faulty one.

```bash
./osa check production -e osa_backend_image_tag=<full-commit-sha>
./osa deploy production -e osa_backend_image_tag=<full-commit-sha>
```

A pin given with `-e` holds for this run only. For a lasting pin add the
variable under `[vps:vars]` in `inventory.ini` and remove it once the fix is
released (`grep image_tag ../osa-access/sites/*/inventory.ini` shows active
pins). The nightly auto-update does not override a pin. A pin does not undo
database migrations a newer release already applied.

### 5.4 Check after a deploy

Containers healthy (`podman ps`), no errors in the logs of the pods, and four
clicks in the browser: log in, save a cast, create a performance, send a
message. After a database restore the backend pod must have been restarted
first (3.4).

## 6. Local development

Local development is hand-set-up per developer, outside Ansible: there is no
development stage. The directory `dev/` holds copyable examples. Pick any
domain and substitute it for `<your-dev-domain>`; `<your-shorturl-domain>` is
a second, independent placeholder.

| `dev/` path | Copy to | Purpose |
|---|---|---|
| `quadlets/backend/osa-backend-dev.build.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-dev.build` | builds the `dev` target of the backend `Dockerfile` |
| `quadlets/backend/osa-backend.container.example` | same folder | backend with the repository bind-mounted, `uvicorn --reload` |
| `quadlets/backend/osa-backend-pg.container.example` | same folder | the developer's own PostgreSQL |
| `postgres-init/10-app-role.sh` | mounted in place | creates the role `osa_app` on an empty data directory |
| `quadlets/backend/osa-backend-valkey.container.example` | same folder | Valkey for the worker |
| `quadlets/backend/osa-backend-worker.container.example` | same folder | arq worker with `--watch` |
| `quadlets/backend/osa-backend.pod.example` | same folder | pod ports and settings |
| `quadlets/frontend/osa-frontend.container.example` | `…/osa/osa-frontend/osa-frontend.container` | `npm run dev` with the repository bind-mounted |
| `env/*.env.example` | `~/.env/<name>.env` | backend, PostgreSQL, Valkey and frontend environment |
| `Caddyfile.dev.example` | append to your own Caddyfile | the two vhosts below |

The paths assume `osa-backend` and `osa-frontend` next to each other under one
parent (`%h/projects/osa-fastapi-vue/…`); adjust `Volume=` and
`SetWorkingDirectory=` otherwise. Copy, then:

```bash
install -d -m 700 ~/.env
mkdir -p ~/.config/containers/systemd/osa/osa-backend ~/.config/containers/systemd/osa/osa-frontend \
         ~/data/osa-backend/postgres ~/data/osa-backend/valkey
for f in dev/quadlets/backend/*.example; do cp "$f" ~/.config/containers/systemd/osa/osa-backend/"$(basename "${f%.example}")"; done
for f in dev/quadlets/frontend/*.example; do cp "$f" ~/.config/containers/systemd/osa/osa-frontend/"$(basename "${f%.example}")"; done
for f in dev/env/*.example; do cp "$f" ~/.env/"$(basename "${f%.example}")"; done
chmod 600 ~/.env/*.env
```

Podman does not create missing bind-mount directories, hence the `mkdir`.
Fill in the placeholders of the env files (`SECRET_KEY`, `POSTGRES_PASSWORD`,
`OSA_APP_PASSWORD`, the same `VALKEY_PASSWORD` in two files) and start:

```bash
systemctl --user daemon-reload
systemctl --user start osa-backend-pg.service osa-backend-valkey.service \
    osa-backend-worker.service osa-backend.service osa-frontend.service
podman exec osa-backend alembic upgrade head       # the dev image does not migrate by itself
podman exec -it osa-backend python scripts/restore_db.py   # real data; needs the read-only Koofr login
podman exec osa-frontend npm install                       # once
```

There is no user seed script; the restore of the latest production backup is
the way to a working login. The nightly downsync also overwrites the dev
database with production's schema, so run `alembic upgrade head` again after
04:00 when a branch has newer migrations. The pytest database is a separate
database: `podman exec osa-backend-pg psql -U osa -d osa -c "CREATE DATABASE
osa_test OWNER osa_app;"`.

**Caddy routing.** Dev reuses production's path split without Dozzle:
`/api` and `/api/*` go to `127.0.0.1:21000` (backend), everything else to
`127.0.0.1:21001` (Vite). Append the two vhosts of `Caddyfile.dev.example` to
the Caddy of the machine and replace the placeholders. `<your-allowed-cidrs>`
lists the networks allowed to reach the dev stage; everyone else gets `403`.
Do not leave it open: the database is a production snapshot and Vite serves
source files. The short-URL vhost is optional and may stay public (it only
redirects).

**Dev env notes.** The database URLs use port `5432` (the pod's shared
network namespace); the host port `5433` is a commented-out option for manual
`psql`. `VITE_API_BASE_URL` is a full absolute URL because only your Caddy does
the `/api` split. `VITE_DEV_ALLOWED_HOSTS` lists the host names Vite accepts
besides localhost (comma-separated); without your public dev domain Vite
answers `403`. SMTP is optional; unset, mail sending fails silently. The dev
PostgreSQL has the two roles described in 7.1; an existing dev database is
switched to `osa_app` once with `config/postgres/split-app-role.sql`:

```bash
OSA_APP_PASSWORD='<password>' podman exec -i -e OSA_APP_PASSWORD osa-backend-pg \
    sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -v ON_ERROR_STOP=1' \
    < config/postgres/split-app-role.sql
```

Restart backend and worker afterwards.

## 7. Appendices

### 7.1 Secrets

All files below are `ansible-vault` encrypted and live in
`osa-access/sites/<stage>/secrets/`; the matching `*.example` files in
`site-template/secrets/` are the templates. `.env.j2` files also contain Jinja2
expressions (`{{ backend_domain }}` and so on, filled in from `inventory.ini`);
vault encryption and templating do not conflict because the template module
decrypts first. Edit later with `./osa vault <stage> edit secrets/<file>`.

| File | Contents |
|---|---|
| `caddy.env` | `LOGGING_USER`, `LOGGING_PASSWORD_HASH` (bcrypt) of the log viewer |
| `osa-backend.env.j2` | web and worker environment: database URL, Valkey password, `SECRET_KEY`, mail, schedule |
| `osa-backend-worker.env` | only the worker: Koofr credentials and backup target |
| `osa-backend-pg.env` | bootstrap role of PostgreSQL |
| `osa-backend-valkey.env` | `VALKEY_PASSWORD` |
| `osa-frontend.env.j2` | runtime configuration of the frontend |

Generate a fresh password for every stage and never reuse one of another
stage: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. The
Valkey password must be identical in `osa-backend.env.j2` and
`osa-backend-valkey.env`. The Caddy hash is made interactively so the password
never reaches the shell history: `podman run -it --rm
docker.io/library/caddy:2.11 caddy hash-password --algorithm bcrypt
--bcrypt-cost 14`. The log viewer holds the Podman API socket, so its login is
as sensitive as the stage's secrets. The optional inventory variable
`logging_allowed_cidrs=["203.0.113.0/24"]` answers clients outside the list
with `403` before any credential is examined.

**Database roles.** `POSTGRES_USER` (`osa`) is the cluster bootstrap role:
always a superuser, for administration only. The backend and the worker
connect as `osa_app`, a role without superuser rights that owns the
application database. Nobody creates it by hand: every `./osa deploy` applies
`config/postgres/split-app-role.sql` inside the database container, after the
stack is up and before the pod restart, sets the password from `DATABASE_URL`
and moves the ownership of everything in the `public` schema. `DATABASE_URL`
must name `osa_app` with a password of at least 16 characters from
`A-Za-z0-9._~-` (it is used in the URL unescaped), different from
`POSTGRES_PASSWORD`; anything else stops the deploy, also under `--check`.
Switching a database that was created with the bootstrap role as owner:
(1) take a backup and make sure no bootstrap-role session is open on the
application database, (2) make sure the running image restores with
`--no-owner --no-acl`, (3) `./osa vault <stage> edit secrets/osa-backend.env.j2`
and set `DATABASE_URL`, (4) `./osa check`, then `./osa deploy`, (5) verify
with `select usename, count(*) from pg_stat_activity where datname =
current_database() and pid <> pg_backend_pid() group by 1` that only
`osa_app` connects. A manual way back is the `DATABASE_URL` line in
`~/.env/osa-backend.env` on the host plus a pod restart.

**Koofr accounts.** `osa-backend-worker.env` holds the WebDAV credentials. Only
the worker container receives them; the web container never does.
Production uses the account that owns the backup folder (it writes the
nightly dumps and prunes old ones). `test` and `qa` use a second account that
can only **read** the folder, because they only download the latest production
backup. Never put the production account into the file of another stage.
Setting up the read-only account: create a second Koofr account, share the
backup folder with it read-only, check in that account that listing and
downloading work, that uploading and deleting are refused, and under which
WebDAV path the folder appears (that path is `KOOFR_BACKUP_PATH` of the
stage). Rotating a password is an edit of this file followed by
`./osa deploy`.

**Mail.** `SMTP_*` stays commented out on non-production stages (no mail
outside production). `SMTP_PORT` is typed as a number, so a blank value fails
the start; only an absent key counts as unset.

### 7.2 Pitfalls

- A `.env` file with literal quotes around a value keeps them; the process
  then sees the quotes.
- `systemctl --user restart` of a single container unit stops its pod and
  does not restart it; restart the pod unit (3.2).
- A fresh directory belongs to the calling user's UID, but the PostgreSQL
  image runs as UID 999 in its own namespace; `deploy.yml` fixes ownership
  through `podman unshare` only when it differs, so a deploy never walks a
  live database.
- Unmanaged files in `~/.env` stop a deploy and are listed for review: orphaned
  secret files would keep stale credentials alive.
- The changed hash of a vaulted environment file restarts the pod it belongs
  to once, also when only a comment changed.
- Port 2020 on the host is used by the Caddy health probe; the Caddy admin API
  is off.
- Every container start waits for `podman-user-wait-network-online.service`,
  which polls the system `network-online.target` but never starts it. On an
  image that brings the network up with ifupdown nothing makes that target
  active, the wait unit fails after its 90 s timeout and each start (also
  after a reboot) is delayed by that time. `setup_vps.yml` enables
  `ifupdown-wait-online.service` on such hosts and lets `multi-user.target`
  request the target at every boot (a drop-in below
  `/etc/systemd/system/multi-user.target.d/`). On a host set up earlier the
  symptom is `podman-user-wait-network-online` in
  `systemctl --user list-units --failed` and a slow deploy.

### 7.3 Access and hand-over

Everything a successor needs is described in [HANDOVER.md](HANDOVER.md). The
vault password is never stored in any repository; to change it run
`./osa vault <stage> rekey` for every stage and re-encrypt `credentials.md`
of `osa-access` by hand with `ansible-vault rekey`.
