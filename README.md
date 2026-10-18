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
| [`osa-backend`](https://github.com/Kirchenmusik-St-Augustin/osa-backend) | Backend: scheduling/casting management for church musicians | Python 3.14, FastAPI, SQLAlchemy, PostgreSQL | `osa-backend` pod |
| [`osa-frontend`](https://github.com/Kirchenmusik-St-Augustin/osa-frontend) | Frontend to `osa-backend`: Vue 3 SPA | Vue 3 (`<script setup>`, TypeScript), Vite, nginx | `osa-frontend` pod |
| `osa-deploy` (this repo) | Ops: Ansible, Caddy, Quadlets, Secrets | Ansible, systemd Quadlets | doesn't run itself — configures the others |

Both app repos build their own container image via CI/CD (GitHub Actions)
and push it to `ghcr.io/kirchenmusik-st-augustin/<repo>:latest` and
`...:<full commit sha>` automatically on every merge to `main`. `osa-deploy` builds **no** images
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
- **Two pods for the app:** `osa-backend-pod` (the backend container, an
  `osa-backend-pg` PostgreSQL sidecar, an `osa-backend-valkey` queue
  sidecar, and an `osa-backend-worker` container running the arq job
  worker) and `osa-frontend-pod` (nginx) — the only stack running.
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
  config/secrets/quadlets initially or on change, for an immediate
  deploy, and to pin a release (see below).
- **Dozzle** is a read-only log viewer for all running containers,
  reachable at `einteilung.hochamt.at/logging/dozzle` behind basic auth.
- **podman-prune.timer** triggers a native systemd oneshot service
  (`podman system prune -a -f`) weekly — runs directly on the host, no
  container, no Podman-socket mount needed.

### Container hardening: read-only rootfs & graceful shutdown

`osa-backend` and `osa-backend-worker` run with `ReadOnly=true`,
`NoNewPrivileges=true`, and `DropCapability=all` (same pattern as
`caddy`/`logging-dozzle`). The only write either process needs is a
temp file — gunicorn's per-worker heartbeat file, and the downloaded
backup archive that `backup_koofr`/`downsync` write before handing it to
`pg_restore` — covered by an explicit `Mount=type=tmpfs,target=/tmp` (on
top of Podman's own `ReadOnlyTmpfs` default, which already covers it).

Shutdown timeouts are deliberately layered so a restart never cuts off an
in-flight request or background job: gunicorn's `--graceful-timeout` and
arq's `job_completion_wait` (`app/worker/settings.py`) each mirror an
existing ceiling that process already enforces during normal operation
(gunicorn's own `--timeout`, arq's own default `job_timeout`) — a request
or job can never legitimately run longer than that anyway, so the grace
period never needs to allow more. Each Quadlet's `StopTimeout=`
(Podman) and `TimeoutStopSec=` (systemd) build on top of those same
values with a buffer, so neither Podman nor systemd kills the process
before its own graceful shutdown had a real chance to finish. See the
comments in `templates/osa-backend.container.j2` and
`templates/osa-backend-worker.container.j2` for the exact numbers and
their derivation.

The local dev containers (`dev/quadlets/backend/*.example`) keep a lighter
profile than production (the source tree is bind-mounted writable for hot
reload, no memory limits on the application containers), but they are not
throwaway: the nightly `downsync` loads a production snapshot into the dev
database. They therefore still run with `NoNewPrivileges=true`, all
capabilities dropped, the bind-mounted `.git` directory read-only, and a
read-only, capability-restricted database container.

CI gates on this: `container-readonly-smoke-test` in
`osa-backend`'s workflow builds the production image, runs both
containers under the exact same restrictions, and fails the build before
`release-image` if either one fails its health check or logs a
read-only-filesystem violation.

`osa-frontend` runs read-only too, but gets there differently: the nginx
base image runs as root by default and internally `chown`/`setuid`s down
to an unprivileged user for its worker processes, which needs
`CAP_CHOWN`/`CAP_SETUID`/`CAP_SETGID` back under `DropCapability=all`. The
Quadlet instead sets `User=101:101` (nginx's own uid) directly — nginx
detects it's already non-root and skips that internal privilege-drop
entirely, so only `CAP_NET_BIND_SERVICE` (binding port 80 as a
non-root process) needs adding back. Two `Mount=type=tmpfs` exceptions:
`/run` (nginx's pid file, and the runtime `config.js` that
`docker-entrypoint.d/40-generate-runtime-config.sh` generates from
`API_BASE_URL`/`APP_ENVIRONMENT`/`GOOGLE_CLIENT_ID` — see that script and
`nginx.conf`'s `/config.js` location) and `/var/cache/nginx` (nginx
creates its client/proxy/fastcgi/uwsgi/scgi temp dirs here unconditionally
at startup, whether a static-only site ever uses them or not). No
shutdown-timeout tuning needed here, unlike the backend: the base image
already sets `STOPSIGNAL SIGQUIT` (nginx's own graceful-shutdown signal,
verified to still apply through Podman), and a static file server has no
long-running request/job to protect the way the backend's synchronous
backup/restore trigger does.

Every remaining container runs the same three directives too:
`osa-backend-pg` (Postgres) sets `User=999:999` for the same reason as
`osa-frontend` -- the official image's own root-to-postgres privilege
drop needs `CAP_CHOWN` back under `DropCapability=all`, so it runs
directly as its own uid instead (relying on this repo's existing
`podman unshare chown -R 999:999`/`chmod 700` deploy tasks having already
set correct ownership up front). `osa-backend-valkey` needed no
adjustment at all -- its `sh -c` `Exec=` wrapper (see that Quadlet's own
comment) already keeps it off the same internal privilege-drop path. Both
verified the same way: built/pulled the exact image, ran it under the
exact restrictions against a scratch volume, confirmed it works, then
removed one restriction at a time to confirm it fails predictably without
it.

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

Each stage's `inventory/<stage>.ini` also carries `acme_email` (the contact
address Caddy registers with the certificate authority) as well as
`backend_domain`, `frontend_domain`, and `shorturl_domain`. The three domains are independent variables (backend
and frontend don't have to share a domain, see `osa-backend`'s
`samesite=none`/CSRF-origin-check cross-domain support), rendered into the
Caddyfile and into `secrets/<stage>/*.env.j2` at deploy time. Each stage's
Postgres data also lives under its own path, `~/data/osa/<stage>/postgres`
on that stage's host.

**Standing up `test`/`qa` on a fresh VPS:** edit `inventory/test.ini` or
`inventory/qa.ini` directly — the placeholder file is already checked into
the repo, no copy step needed. Replace the `[vps]` entry with the new
host's IP or hostname (whatever `ssh` can already reach), and replace all
three `CHANGEME.example.invalid` domain values and the `acme_email`
placeholder under `[vps:vars]` with that stage's real
`backend_domain`/`frontend_domain`/`shorturl_domain` and a mailbox of
yours -- while
you're in there, also update or remove the file's own top-of-file
"PLACEHOLDER, no real VPS exists yet" comment, or it'll keep claiming that
after you've filled in a real, working stage. Nothing else needs to change
before Phase 1 below can run against it.

See "Local development environment" below for the one stage this repo
deliberately doesn't manage.

## Phase 1 — VPS base configuration (only needed for a fresh setup)

Turns a bare host into a hardened, rootless-Podman-capable one:

- native package install (git, podman, netavark, firewall tooling, ...)
  straight from the distribution's own repository, no third-party
  repository added; a full distribution upgrade runs only with
  `-e full_upgrade=true` (use it on a fresh install), and daily unattended
  security updates are switched on (Debian and RedHat family; a kernel
  update still needs a reboot)
- **checks the installed Podman major version and aborts with a clear
  error if it's too old** (see "Supported distributions" below) — before
  anything else runs
- creates the `admin` (sudo) and `service` (rootless Podman) users,
  distributes your local SSH public key to both
- rootless-Podman kernel/storage tuning
- **reboots the host partway through** (finalizes kernel/Podman/sysctl
  changes, deliberately before SSH gets hardened)
- firewall lockdown to SSH/80/443 only (SSH rate-limited)
- **hardens SSH at the end**: key-only login for every account (password
  and keyboard-interactive login off), root login disabled, at most three
  authentication attempts, no X11 forwarding. The playbook then reads the
  effective `sshd` configuration and fails if a file below
  `/etc/ssh/sshd_config.d/` overrides these settings

See `setup_vps.yml`'s own comments for the full task-by-task rationale.

### Supported distributions

This playbook requires **Podman major version 5 or newer**, installed
from each distribution's own default repository — not a third-party one.
Podman 5.0 is a hard technical requirement, not a preference: Quadlet's
`.pod` unit type (used by every pod in this repo, e.g.
`quadlets/backend/osa-backend.pod`) only exists from Podman 5.0 onward.
`setup_vps.yml`'s version-check task aborts cleanly — before any
destructive step (reboot, firewall, SSH hardening) runs — if the target's
native repository ships anything older, rather than silently patching the
gap with a third-party repository of unknown long-term trustworthiness.

Known-good release branches (native Podman version verified 2026-08-27,
drifts over time — patch versions shown are a snapshot, what actually
matters is the major-version gate the playbook enforces at runtime):

| Distribution | Native Podman |
| --- | --- |
| Debian 13 ("trixie") | 5.4.2 |
| Ubuntu 26.04 LTS | 5.7.0 |
| Rocky Linux / AlmaLinux / CentOS Stream 10 | 5.6.0+ |
| Fedora (current stable) | 5.8.x |
| openSUSE Leap 16.0 | 5.4.2 |

Known-too-old release branches — reinstall a current one instead of
running this playbook against these: Debian 12, Ubuntu 24.04 LTS (native
4.9.3), any RHEL-family 9.x release, openSUSE Leap 15.x.

```bash
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root -e full_upgrade=true
# If only password login is possible:
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root -e full_upgrade=true --ask-pass

# Re-run / verification (root login is disabled afterward):
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u admin --ask-become-pass --check --diff
```

The production host is already hardened — a first run here should only
serve as verification (`--check --diff`, expect near-zero diff), not a
fresh install.

## Maintaining secrets

`secrets/<stage>/caddy.env`, `secrets/<stage>/osa-backend.env.j2`,
`secrets/<stage>/osa-frontend.env.j2`, `secrets/production/koofr.yml` and
`secrets/nonproduction/koofr.yml` live `ansible-vault`-encrypted in the repo,
never committed in plaintext. The matching `*.example` files are the
(plaintext-safe) templates.

Every `secrets/<stage>/` directory carries the same five `.example`
templates (`caddy.env.example`, `osa-backend-pg.env.example`,
`osa-backend-valkey.env.example`, `osa-backend.env.j2.example`,
`osa-frontend.env.j2.example`) regardless of whether that stage is
deployed yet -- they hold no secrets, so there's no reason to withhold
them. The five matching real, vault-encrypted files only get created once
a stage has an actual target in `inventory/<stage>.ini` (not just a
`CHANGEME.example.invalid` placeholder): `production` and any stage
currently being stood up have all five; an unprovisioned stage has none
yet.

The Koofr credentials sit outside the stage directories, in
`secrets/production/koofr.yml` and `secrets/nonproduction/koofr.yml` (the
latter directory is not a stage, it serves `test` and `qa`), each with its
`.example` template.

The two `.j2` files also contain Jinja2 expressions (`{{ backend_domain }}`
etc., filled in from `inventory/<stage>.ini`) alongside the real secrets —
vault encryption and Jinja2 templating don't conflict:
`ansible.builtin.template` decrypts the vault content first, then renders
the Jinja2, so the edit workflow below is unchanged:

```bash
cp secrets/<stage>/osa-backend.env.j2.example secrets/<stage>/osa-backend.env.j2
$EDITOR secrets/<stage>/osa-backend.env.j2
ansible-vault encrypt secrets/<stage>/osa-backend.env.j2

# Edit later:
ansible-vault edit secrets/<stage>/osa-backend.env.j2
ansible-vault view secrets/<stage>/osa-backend.env.j2
```

`secrets/<stage>/caddy.env`'s `LOGGING_USER`/`LOGGING_PASSWORD_HASH` follow
the same rule as `osa-backend-pg.env` below — generate a fresh password per
stage, never reuse another stage's, so a leaked non-production Dozzle login
can't double as a production one. Dozzle holds the Podman API socket, so its
login is as sensitive as the stage's secrets. Access can additionally be
limited to networks with the optional inventory variable
`logging_allowed_cidrs`, a list set in the stage's `[vps:vars]`, for example
`logging_allowed_cidrs=["203.0.113.0/24"]`. A client outside the list gets
`403` before any credential is examined; without the variable the viewer is
reachable from anywhere behind the basic-auth login. Keep the list as small
as possible.
`osa-backend.env.j2`'s `KOOFR_USER`/`KOOFR_PASSWORD` come from two vault
files, so that no host holds more access than it needs:

- `secrets/production/koofr.yml`: the account that owns the backup folder.
  Production writes the nightly dumps with it and prunes old ones.
- `secrets/nonproduction/koofr.yml`: a second account that can only **read**
  the production backup folder. `test` and `qa` use it, because they only
  download the latest production backup (`downsync`); a compromised host
  there must not be able to read, overwrite or delete the backups.

`playbooks/deploy.yml` loads the matching file via `vars_files:` and stops
with a message if it is missing, so every stage's `osa-backend.env.j2` just
references `{{ koofr_user }}`/`{{ koofr_password }}` and nothing needs filling
in per stage. Set the read-only account up once: create a second Koofr
account, share the backup folder with it read-only, and check in that
account that listing and downloading work, that uploading and deleting are
refused, and under which WebDAV path the shared folder appears (that path
goes into `KOOFR_BACKUP_PATH` of the `test`/`qa` `osa-backend.env.j2`). Then:
```bash
cp secrets/nonproduction/koofr.yml.example secrets/nonproduction/koofr.yml
$EDITOR secrets/nonproduction/koofr.yml
ansible-vault encrypt secrets/nonproduction/koofr.yml
```
Rotating a Koofr password is a single edit of the matching file:
```bash
ansible-vault edit secrets/production/koofr.yml      # or secrets/nonproduction/koofr.yml
```
`scripts/backup_db.py`/`restore_db.py` (see "Disaster recovery" below)
see the same backup history regardless of stage (non-production stages
through the read-only share). `SMTP_*` should stay
commented out on non-production stages (no mail sending outside prod) --
`SMTP_PORT` is int-typed, so a blank value fails startup validation; only a
commented-out key counts as unset.

`secrets/<stage>/osa-backend-pg.env` follows the same `.example` workflow
as the two `.j2` files above — create it fresh per stage with a freshly
generated password, never reuse another stage's:
```bash
cp secrets/<stage>/osa-backend-pg.env.example secrets/<stage>/osa-backend-pg.env
$EDITOR secrets/<stage>/osa-backend-pg.env
ansible-vault encrypt secrets/<stage>/osa-backend-pg.env
```

`secrets/<stage>/osa-backend-valkey.env` follows the identical workflow —
its `VALKEY_PASSWORD` must match the value filled into that stage's
`osa-backend.env.j2` (see `templates/osa-backend-valkey.container.j2`):
```bash
cp secrets/<stage>/osa-backend-valkey.env.example secrets/<stage>/osa-backend-valkey.env
$EDITOR secrets/<stage>/osa-backend-valkey.env
ansible-vault encrypt secrets/<stage>/osa-backend-valkey.env
```

### Database roles

Every stage's PostgreSQL has two roles. `POSTGRES_USER` in
`osa-backend-pg.env` (`osa`) is the cluster bootstrap role: always a
superuser, for administration only. The backend and the worker connect as
`osa_app`, a role without superuser rights that owns the application
database, so a flaw in the application cannot reach `COPY ... PROGRAM`,
server-side files or role management.

Nobody creates the role by hand. Every `deploy.yml` run applies
`config/postgres/split-app-role.sql` inside the database container, after
the stack is up and before the pod restart: it creates the role, sets the
password from `DATABASE_URL` in `osa-backend.env.j2` and moves the ownership
of everything in the `public` schema to it. The user in `DATABASE_URL` must
be `osa_app` and the password a freshly generated value of at least 16
characters from `A-Za-z0-9._~-` (it is used in the URL without escaping),
different from `POSTGRES_PASSWORD`. Any other user or password stops the
deploy before anything is changed, also under `--check`. A rotated password
in `DATABASE_URL` reaches the role with the next deploy.

**Switching an existing stage** (production's database was created with the
bootstrap role as the owner of everything):

1. Take a fresh backup (`podman exec osa-backend python scripts/backup_db.py`)
   and make sure no session of the bootstrap role is open on the application
   database.
2. Make sure the running `osa-backend` image already restores with
   `--no-owner --no-acl`; otherwise the nightly refresh of non-production
   stages and a manual restore fail with ownership errors once the
   application role owns the database.
3. `ansible-vault edit secrets/<stage>/osa-backend.env.j2`: set `DATABASE_URL`
   to the user `osa_app` and a new generated password.
4. `deploy.yml --check --diff`, then apply. The role is set up first, then
   the changed env file restarts the backend pod, which now connects as
   `osa_app`.
5. Verify: `podman exec osa-backend-pg sh -c 'psql -U "$POSTGRES_USER" -d
   "$POSTGRES_DB" -Atc "select usename, count(*) from pg_stat_activity where
   datname = current_database() and pid <> pg_backend_pid() group by 1"'`
   lists `osa_app` only, and the application works.

The playbook insists on `osa_app`, so there is no switch back through it; a
manual fallback is the `DATABASE_URL` line in `~/.env/osa-backend.env` on the
host plus a restart of the pod.

### Standing up test/qa: permanent stage vs. one-off smoke test

Filling in `inventory/<stage>.ini` with a real host and creating that
stage's real secrets (as above) means one of two things, and which one it
is decides where that work gets committed:

- **Permanent stage** — the box keeps serving as `test`/`qa` going forward
  (its IP can still change later if it gets rebuilt). Commit
  `inventory/<stage>.ini` and `secrets/<stage>/*` straight onto
  `development`, exactly like `production`. Rebuilding the box later is a
  config update (new IP, rotated secrets) done directly on `development`,
  not a revert.
- **One-off smoke test** — the box only exists to validate the deploy
  tooling itself and gets torn down right after; the stage should read as
  unprovisioned again afterward. Do this on a throwaway branch that never
  merges into `development`, so the real host/domain and the
  vault-encrypted secrets never enter `development`'s history at all:

  ```bash
  git checkout development && git pull
  git checkout -b qa-smoketest-YYYY-MM-DD    # never push this branch

  # fill in inventory/<stage>.ini + create/encrypt secrets/<stage>/* as above
  git add inventory/qa.ini secrets/qa/
  git commit -m "qa-smoketest: <what this validates>"

  # deploy, verify, tear the VPS down again, then:
  git checkout development
  git branch -D qa-smoketest-YYYY-MM-DD      # gone, no trace in development
  ```

  `development`'s `inventory/<stage>.ini` stays at
  `CHANGEME.example.invalid` and `secrets/<stage>/` keeps only its
  `.example` templates throughout — there is nothing to revert there.

## Phase 2 — day-2 ops: keeping secrets/quadlets/timers in sync

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --check --diff   # check first
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass                  # then apply
```

Syncs secrets, quadlet **definitions** (Caddy/Logging/Backend/Frontend),
the Caddyfile itself, and the native podman-prune service/timer; starts
and keeps running the entire stack (Caddy + Backend + Frontend + Dozzle)
plus the weekly podman-prune cleanup run. Builds no images, clones no
repos, restores no database. On a
freshly stood-up stage (see "Standing up test/qa" above), that means the
database is empty right after this — see "Fresh or empty Postgres data
directory" below for the one extra step that gets you a working, populated
system instead of an empty login screen.

A changed secret file restarts exactly the units that read it: `caddy.env`
restarts Caddy, `osa-backend.env`, `osa-backend-pg.env` and
`osa-backend-valkey.env` restart the backend pod (backend, worker,
PostgreSQL, Valkey), `osa-frontend.env` restarts the frontend pod. A new
`POSTGRES_PASSWORD` is not enough on its own: the PostgreSQL image applies
it only when it initialises an empty data directory, so an existing
database also needs `ALTER ROLE` (interactive step).

### Immediate deploy (instead of waiting for the nightly auto-update run)

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-backend
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-frontend
```

Pulls the image tag the Quadlet uses (`latest` unless the release is pinned,
see below) and restarts the corresponding pod.

### Pin a release (hold back or roll back an image)

The app Quadlets are rendered from `templates/osa-backend.container.j2`,
`templates/osa-backend-worker.container.j2` and
`templates/osa-frontend.container.j2`. The image tag comes from two optional
variables and defaults to `latest`, so nothing changes until one is set:

| Variable | Containers | Value |
|---|---|---|
| `osa_backend_image_tag` | `osa-backend` and `osa-backend-worker` together | full commit SHA of a release in `osa-backend` |
| `osa_frontend_image_tag` | `osa-frontend` | full commit SHA of a release in `osa-frontend` |

Backend and worker share one variable on purpose: a worker must never run
against a schema that the web container (the only one that migrates) has not
migrated, or the reverse.

The commit SHA is the tag CI pushes next to `latest`; the commit list of
`main` and the package page of the repository on GitHub show it (the copy
button next to a commit gives the full SHA). To roll back, take the merge
commit before the faulty one. Preview first, then apply:

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --check --diff -e osa_backend_image_tag=<full-commit-sha>
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass -e osa_backend_image_tag=<full-commit-sha>
```

The changed Quadlet makes the playbook restart the pod. The nightly
auto-update does not override a pin, because the tag of a commit SHA never
changes. A pin given with `-e` holds for this run only: a later run without
it goes back to `latest`. For a lasting pin, add the variable under
`[vps:vars]` of the stage's inventory file (for example
`osa_backend_image_tag=<full-commit-sha>`) and remove the line again once the
fix is released. `grep image_tag inventory/*.ini` shows whether a pin is
still active; while it is, the service stays on that version unnoticed.
Pinning an older release does not undo database migrations the newer release
already applied.

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
overwriting the live database). The script then names the target database
and the backup and asks you to type the database name before it replaces
anything (`--yes` skips that question for unattended runs). Always verify
afterward that real data actually landed, not just that the command exited
cleanly.

`osa-backend`'s own scheduler additionally runs this restore **automatically,
every night, on every non-production stage** (the `downsync` job — one hour
after production's nightly backup, always pulling the latest production
backup, never the other way around) so `test`/`qa` naturally stay close to
production's real data without anyone triggering it by hand. See
`osa-backend`'s README for the scheduler's full job list and timing.

A manual backup before risky changes:
`podman exec osa-backend python scripts/backup_db.py`. Full docs for both
scripts (all parameters incl. `--dry-run`/`--cleanup`) in
[`osa-backend`'s README](https://github.com/Kirchenmusik-St-Augustin/osa-backend).

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

Run both commands in the same sitting. The weekly `podman-prune.timer`
(Sundays 04:00) removes any image not currently in use by a container, and
a pre-pulled-but-not-yet-started image is exactly that -- Podman has no
"reserved for later" state a prune can respect. If the actual bump won't
happen within the same week as the pre-pull (e.g. waiting on a maintenance
window), either pre-pull again immediately before starting the service, or
`systemctl --user stop podman-prune.timer` for the gap and re-enable it
afterward.

The application role `osa_app` lives inside the data directory too, so an
empty one has none: the backend cannot log in, and a restore cannot run,
until the next `deploy.yml` run has set the role up (see "Database roles"
under "Maintaining secrets").

Either way, the database that comes up is schema-migrated but **empty** —
alembic creates the tables, it doesn't seed any rows. On a non-production
stage, get a working system with real data by pulling down the latest
production backup (auto-selects it without `--backup-name`, no `--force`
needed — that flag only guards `APP_ENVIRONMENT=production`):

```bash
ssh service@<stage-host>
podman exec -it osa-backend python scripts/restore_db.py
```

This is the same thing the nightly `downsync` scheduled job already does
automatically on every non-production stage (see "Disaster recovery"
below) — triggering it manually here just avoids waiting for that nightly
run right after standing up a stage. (On production itself, an empty
directory only happens via the major-version-bump case above; recover it
with `restore_db.py --force` from the backup taken right before the bump,
same as any other production restore under "Disaster recovery" — a
downsync only ever pulls *from* production, never restores *into* it.)

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
it appears. `<your-shorturl-domain>` is a second, independent placeholder
— often just "go." plus your main dev domain, but not necessarily, exactly
like production's own `shorturl_domain` isn't derived from
`backend_domain`/`frontend_domain` either (see "Stages" above). Nothing in
`dev/` hardcodes one specific domain.

### Directory layout

Unlike production/test/qa, local dev has no Caddy Quadlet (Caddy runs as a
regular host-wide service on the dev machine, shared with whatever else it
proxies, not containerized) and no `logging` Quadlet or podman-prune
service/timer equivalents (Dozzle and the prune timer are prod-only
conveniences). Only backend and frontend get Quadlets:

| `dev/` path | Copy to | Purpose |
|---|---|---|
| `dev/quadlets/backend/osa-backend-dev.build.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-dev.build` | Builds `localhost/osa-backend-dev:latest` from `osa-backend`'s own `Dockerfile`, `dev` target |
| `dev/quadlets/backend/osa-backend.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend.container` | Runs the backend, bind-mounts the repo, `uvicorn --reload` |
| `dev/quadlets/backend/osa-backend-pg.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-pg.container` | Dev's own PostgreSQL, separate from every Ansible-managed stage's |
| `dev/postgres-init/10-app-role.sh` | (mounted in place, not copied) | Creates the non-superuser role `osa_app` on the first start with an empty Postgres data directory |
| `dev/quadlets/backend/osa-backend-valkey.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-valkey.container` | Dev's own Valkey queue backend for the arq worker below |
| `dev/quadlets/backend/osa-backend-worker.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-worker.container` | Runs the arq job worker, bind-mounts the repo, `--watch` reload |
| `dev/quadlets/backend/osa-backend.pod.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend.pod` | Publishes backend/Postgres ports, pod-wide settings |
| `dev/quadlets/frontend/osa-frontend.container.example` | `~/.config/containers/systemd/osa/osa-frontend/osa-frontend.container` | Runs `npm run dev`, bind-mounts the repo, no build/pod unit needed |
| `dev/env/osa-backend.env.example` | `~/.env/osa-backend.env` | Backend secrets/config (dev-sized subset, see below) |
| `dev/env/osa-backend-pg.env.example` | `~/.env/osa-backend-pg.env` | Dev Postgres credentials |
| `dev/env/osa-backend-valkey.env.example` | `~/.env/osa-backend-valkey.env` | Dev Valkey password |
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
install -d -m 700 ~/.env
mkdir -p ~/.config/containers/systemd/osa/osa-backend ~/.config/containers/systemd/osa/osa-frontend ~/data/osa-backend/postgres ~/data/osa-backend/valkey

for f in dev/quadlets/backend/*.example; do
  cp "$f" ~/.config/containers/systemd/osa/osa-backend/"$(basename "${f%.example}")"
done
for f in dev/quadlets/frontend/*.example; do
  cp "$f" ~/.config/containers/systemd/osa/osa-frontend/"$(basename "${f%.example}")"
done
for f in dev/env/*.example; do
  cp "$f" ~/.env/"$(basename "${f%.example}")"
done
chmod 600 ~/.env/osa-backend.env ~/.env/osa-backend-pg.env ~/.env/osa-backend-valkey.env ~/.env/osa-frontend.env
```

`~/data/osa-backend/postgres`/`~/data/osa-backend/valkey` are
`osa-backend-pg.container.example`'s/`osa-backend-valkey.container.example`'s
bind mount targets — Podman does not create a missing host directory for a
bind mount, it fails outright (`Error: statfs ...: no such file or
directory`) if it's absent.

Edit each `~/.env/*.env` file next: fill in `<your-dev-domain>`,
`<your-shorturl-domain>`, `SECRET_KEY`, `POSTGRES_PASSWORD`,
`OSA_APP_PASSWORD` (a different value; it is also the password in
`DATABASE_URL` and `TEST_DATABASE_URL`), `VALKEY_PASSWORD` (matching value in both
`osa-backend.env` and `osa-backend-valkey.env`), and optionally
`GOOGLE_CLIENT_ID`/`KOOFR_USER`/`KOOFR_PASSWORD`. Then:

```bash
systemctl --user daemon-reload
systemctl --user start osa-backend-pg.service osa-backend-valkey.service osa-backend-worker.service osa-backend.service osa-frontend.service
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
   └─ <your-shorturl-domain> (optional, short-URL testing)
        └─ /*  → 127.0.0.1:21000 (with /go prefix) → osa-backend
```

Append `dev/Caddyfile.dev.example`'s two vhost blocks to your own Caddyfile
(location depends on how Caddy is installed on your machine — see Caddy's
own docs for the default path on your platform), replace
`<your-dev-domain>`/`<your-shorturl-domain>`/`<your-allowed-cidrs>`, then
reload Caddy. `<your-allowed-cidrs>` lists the networks that may reach the
dev stage; everyone else gets `403`. Do not leave the dev vhost open: the
dev database is refreshed from a production snapshot and the Vite dev
server serves source files. The `<your-shorturl-domain>` vhost is
optional, only needed for local short-URL redirect testing, and stays
public because it is only a redirect service.

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
This needs working `KOOFR_USER`/`KOOFR_PASSWORD` in `~/.env/osa-backend.env`;
retrieve the read-only account with
`ansible-vault view secrets/nonproduction/koofr.yml` (vault password
needed) if you don't have it yet.

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

- `DATABASE_URL`/`TEST_DATABASE_URL` use port `5432` — `osa-backend` reaches
  Postgres over the pod's shared network namespace, on Postgres' own
  container-internal port, not on the host-published port (`5433`) that
  `osa-backend.pod.example` offers as a commented-out option. Nothing in
  the running stack needs that host port, and every local account on the
  dev host could reach it, so it is off by default: uncomment the
  `PublishPort` line in `osa-backend.pod.example` only if you want manual
  `psql` debugging directly from the dev host (loopback-only, same purpose
  as production's equivalent port, never reachable from outside the
  machine), and change the host port there if `5433` is already bound by
  another project.
- `VITE_API_BASE_URL` is a full absolute URL, not the relative `/api` the
  app otherwise recommends — the Vite dev server itself doesn't do the
  `/api` path split, only your own Caddy in front of it does. A different
  mechanism entirely from production's runtime-injected `API_BASE_URL`
  (see `osa-frontend`'s README for the `VITE_*` vs. runtime-config
  distinction, not repeated here).
- `TEST_DATABASE_URL` is dev/test-only, consumed by `osa-backend`'s pytest
  suite (`podman exec osa-backend pytest ...`, see `osa-backend`'s README)
  — not part of production's env template at all. Its database (`osa_test`
  by default) is a separate database on the same Postgres server, not just
  a separate table set — create it once before the first test run:
  `podman exec osa-backend-pg psql -U osa -d osa -c "CREATE DATABASE osa_test OWNER osa_app;"`
  (schema/tables come from `osa-backend`'s own test suite running the real
  Alembic migrations on first use, no separate migration step needed here).
- SMTP is optional. Point `SMTP_HOST`/`SMTP_PORT` at any local
  SMTP-capturing tool you like, or leave both commented out — mail sending
  then just fails silently, same as every other non-production stage.
- The dev Postgres has two roles. `POSTGRES_USER` (`osa`) is the cluster
  bootstrap role: always a superuser, for administration only. The backend
  connects as `osa_app` (`OSA_APP_PASSWORD`), a non-superuser role that owns
  the database, so a flaw in the application cannot reach `COPY ... PROGRAM`,
  server-side files or role management. `dev/postgres-init/10-app-role.sh`
  creates it, but init scripts only run on an empty data directory. An
  existing dev database is switched once with the same script the deployed
  stages use (choose the password, then put it into `OSA_APP_PASSWORD` and
  the two database URLs), run from the root of this repository:

  ```bash
  OSA_APP_PASSWORD='<OSA_APP_PASSWORD>' podman exec -i -e OSA_APP_PASSWORD osa-backend-pg \
      sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -v ON_ERROR_STOP=1' \
      < config/postgres/split-app-role.sql
  ```

  The script is idempotent and moves the ownership of every object in the
  `public` schema, which `REASSIGN OWNED BY osa` cannot do: the bootstrap
  role also owns the system databases, which PostgreSQL refuses to
  reassign. Restart the
  backend and the worker afterwards. A restore from a backup (including the
  nightly refresh of non-production stages) wipes the `public` schema first
  and terminates every other session of the database; PostgreSQL does not
  let `osa_app` terminate a session of the bootstrap role, so do not keep
  one open on the application database during a restore (use the `postgres`
  database for administration).

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
| [`osa-backend`](https://github.com/Kirchenmusik-St-Augustin/osa-backend) | Backend: Dienstplan-/Besetzungsverwaltung für Kirchenmusiker | Python 3.14, FastAPI, SQLAlchemy, PostgreSQL | `osa-backend`-Pod |
| [`osa-frontend`](https://github.com/Kirchenmusik-St-Augustin/osa-frontend) | Frontend zu `osa-backend`: Vue-3-SPA | Vue 3 (`<script setup>`, TypeScript), Vite, nginx | `osa-frontend`-Pod |
| `osa-deploy` (dieses Repo) | Betrieb: Ansible, Caddy, Quadlets, Secrets | Ansible, systemd Quadlets | läuft nicht selbst als Service — konfiguriert die anderen |

Beide App-Repos bauen ihr Container-Image selbst per CI/CD (GitHub Actions)
und pushen es bei jedem Merge nach `main` automatisch nach
`ghcr.io/kirchenmusik-st-augustin/<repo>:latest` und
`...:<voller Commit-SHA>`. `osa-deploy` baut **keine**
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
- **Zwei Pods für die App:** `osa-backend-pod` (der Backend-Container, ein
  `osa-backend-pg`-PostgreSQL-Sidecar, ein `osa-backend-valkey`-Queue-
  Sidecar und ein `osa-backend-worker`-Container mit dem arq-Job-Worker)
  und `osa-frontend-pod` (nginx) — der einzige laufende Stack.
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
  bei Änderungen zu verteilen, für einen Sofort-Deploy und um ein Release
  zu pinnen (siehe unten).
- **Dozzle** ist ein schreibgeschützter Log-Viewer für alle laufenden
  Container, erreichbar über `einteilung.hochamt.at/logging/dozzle` hinter
  Basic-Auth.
- **podman-prune.timer** stößt wöchentlich einen nativen systemd-Oneshot-
  Service an (`podman system prune -a -f`) — läuft direkt auf dem Host,
  kein Container, kein Podman-Socket-Mount nötig.

### Container-Härtung: read-only Rootfs & Graceful Shutdown

`osa-backend` und `osa-backend-worker` laufen mit `ReadOnly=true`,
`NoNewPrivileges=true` und `DropCapability=all` (dasselbe Muster wie bei
`caddy`/`logging-dozzle`). Der einzige Schreibbedarf beider Prozesse ist
eine Temp-Datei — Gunicorns Worker-Heartbeat-Datei sowie das
heruntergeladene Backup-Archiv, das `backup_koofr`/`downsync` vor der
Übergabe an `pg_restore` ablegen — abgedeckt durch ein explizites
`Mount=type=tmpfs,target=/tmp` (zusätzlich zu Podmans eigenem
`ReadOnlyTmpfs`-Default, der das bereits abdeckt).

Die Shutdown-Timeouts sind bewusst so gestaffelt, dass ein Neustart nie
eine laufende Anfrage oder einen laufenden Background-Job abschneidet:
Gunicorns `--graceful-timeout` und arqs `job_completion_wait`
(`app/worker/settings.py`) spiegeln jeweils ein bereits bestehendes Limit,
das derselbe Prozess im Normalbetrieb ohnehin durchsetzt (Gunicorns
eigenes `--timeout`, arqs eigener Default-`job_timeout`) — eine Anfrage
oder ein Job kann so oder so nie länger laufen, die Grace-Period muss also
nicht mehr erlauben. Das `StopTimeout=` (Podman) und `TimeoutStopSec=`
(systemd) jeder Quadlet-Datei bauen mit Puffer auf genau diesen Werten
auf, damit weder Podman noch systemd den Prozess killt, bevor sein eigenes
Graceful Shutdown wirklich fertig werden konnte. Die exakten Zahlen samt
Herleitung stehen als Kommentar in `templates/osa-backend.container.j2`
und `templates/osa-backend-worker.container.j2`.

Die lokalen Dev-Container (`dev/quadlets/backend/*.example`) behalten ein
leichteres Profil als die Produktion (der Quellbaum ist für Hot-Reload
beschreibbar eingebunden, keine Speicherlimits für die
Anwendungs-Container), sind aber nicht wegwerfbar: der nächtliche
`downsync` lädt einen Produktions-Snapshot in die Dev-Datenbank. Sie laufen
deshalb trotzdem mit `NoNewPrivileges=true`, ohne Capabilities, mit
schreibgeschütztem eingebundenem `.git`-Verzeichnis und einem
schreibgeschützten, capability-beschränkten Datenbank-Container.

CI setzt hier an: `container-readonly-smoke-test` in `osa-backend`s
Workflow baut das Produktions-Image, startet beide Container unter
exakt denselben Restriktionen und lässt den Build vor `release-image`
scheitern, falls einer der beiden den Health-Check nicht besteht oder
eine Read-only-Filesystem-Verletzung loggt.

Auch `osa-frontend` läuft read-only, allerdings anders gelöst: Das
nginx-Basisimage läuft standardmäßig als root und wechselt intern per
`chown`/`setuid` zu einem unprivilegierten User für seine Worker-Prozesse
— das würde unter `DropCapability=all` `CAP_CHOWN`/`CAP_SETUID`/
`CAP_SETGID` zurück-erfordern. Die Quadlet-Datei setzt stattdessen direkt
`User=101:101` (nginx' eigene UID) — nginx erkennt, dass es bereits
nicht-root ist, und überspringt den internen Privilegien-Wechsel
komplett, wodurch nur `CAP_NET_BIND_SERVICE` (Port 80 als Nicht-root-
Prozess binden) zurückgebraucht wird. Zwei `Mount=type=tmpfs`-Ausnahmen:
`/run` (nginx' PID-Datei sowie das zur Laufzeit generierte `config.js`,
das `docker-entrypoint.d/40-generate-runtime-config.sh` aus
`API_BASE_URL`/`APP_ENVIRONMENT`/`GOOGLE_CLIENT_ID` erzeugt — siehe dieses
Skript und `nginx.conf`s `/config.js`-Location) und `/var/cache/nginx`
(nginx legt seine client/proxy/fastcgi/uwsgi/scgi-Temp-Verzeichnisse dort
beim Start bedingungslos an, unabhängig davon, ob eine rein statische
Seite sie je nutzt). Kein Shutdown-Timeout-Tuning nötig, anders als beim
Backend: das Basisimage setzt bereits `STOPSIGNAL SIGQUIT` (nginx' eigenes
Graceful-Shutdown-Signal, verifiziert dass Podman das auch tatsächlich
durchreicht), und ein statischer Fileserver hat keine lang laufende
Anfrage/keinen Job zu schützen wie der synchrone Backup/Restore-Trigger
des Backends.

Auch alle übrigen Container laufen mit denselben drei Direktiven:
`osa-backend-pg` (Postgres) setzt aus demselben Grund wie `osa-frontend`
`User=999:999` — der interne root-zu-postgres-Privilegienwechsel des
offiziellen Images bräuchte unter `DropCapability=all` `CAP_CHOWN` zurück,
läuft also stattdessen direkt unter eigener UID (verlässt sich dabei auf
die bereits bestehenden `podman unshare chown -R 999:999`/`chmod
700`-Deploy-Tasks, die die Berechtigungen vorher korrekt setzen).
`osa-backend-valkey` brauchte keine Anpassung — der `sh -c`-`Exec=`-Wrapper
(siehe Kommentar in dieser Quadlet-Datei) hält es ohnehin schon vom selben
internen Privilegienwechsel-Pfad fern. Beide gleich verifiziert: exaktes
Image gebaut/gepullt, unter exakt denselben Restriktionen gegen ein
Wegwerf-Volume gestartet, Funktion bestätigt, dann je eine Restriktion
einzeln entfernt und den vorhergesagten Fehlschlag bestätigt.

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

Jede `inventory/<stage>.ini` trägt außerdem `acme_email` (die
Kontaktadresse, mit der sich Caddy bei der Zertifizierungsstelle
registriert) sowie `backend_domain`, `frontend_domain` und
`shorturl_domain`. Die drei Domains sind unabhängige Variablen (Backend
und Frontend müssen sich keine Domain teilen, siehe `osa-backend`s
`samesite=none`/CSRF-Origin-Check-Unterstützung für Cross-Domain), die zur
Deploy-Zeit ins Caddyfile und in `secrets/<stage>/*.env.j2` einfließen.
Auch das Postgres-Datenverzeichnis jeder Stage liegt unter einem eigenen
Pfad, `~/data/osa/<stage>/postgres` auf dem jeweiligen Host.

**`test`/`qa` auf einem frischen VPS aufsetzen:** `inventory/test.ini` bzw.
`inventory/qa.ini` direkt editieren — die Platzhalterdatei liegt bereits im
Repo, kein Kopierschritt nötig. Den `[vps]`-Eintrag durch den neuen Host
ersetzen (IP oder Hostname, alles, was `ssh` bereits erreicht), und alle
drei `CHANGEME.example.invalid`-Domainwerte und den `acme_email`-Platzhalter
unter `[vps:vars]` durch die echten
`backend_domain`/`frontend_domain`/`shorturl_domain` dieser Stage und eine
eigene Mailadresse ersetzen;
dabei auch gleich den Platzhalter-Kommentar am Dateianfang ("PLACEHOLDER,
kein echter VPS vorhanden") aktualisieren oder entfernen, sonst behauptet
er das auch nach dem Befüllen mit einer echten, funktionierenden Stage
weiter. Mehr muss nicht angepasst werden, bevor Phase 1 unten dagegen
laufen kann.

Siehe "Lokale Entwicklungsumgebung" unten für die eine Stage, die dieses
Repo bewusst nicht verwaltet.

## Phase 1 — VPS-Grundkonfiguration (nur bei Neuaufsetzen nötig)

Macht aus einem nackten Host ein gehärtetes, rootless-Podman-fähiges
System:

- native Paketinstallation (git, podman, netavark, Firewall-Tooling, ...)
  direkt aus dem eigenen Repository der Distribution, kein
  Drittanbieter-Repo wird eingerichtet; ein vollständiges Distributions-
  Upgrade läuft nur mit `-e full_upgrade=true` (bei einer Neuinstallation
  verwenden), und tägliche automatische Sicherheitsupdates werden
  eingeschaltet (Debian- und RedHat-Familie; ein Kernel-Update braucht
  weiterhin einen Neustart)
- **prüft die installierte Podman-Major-Version und bricht bei einer zu
  alten Version mit klarer Fehlermeldung ab** (siehe "Unterstützte
  Distributionen" unten) — bevor irgendetwas anderes läuft
- legt die User `admin` (sudo) und `service` (rootless Podman) an,
  verteilt den eigenen lokalen SSH-Public-Key an beide
- rootless-Podman-Kernel-/Storage-Tuning
- **startet den Host mittendrin neu** (finalisiert Kernel-/Podman-/
  Sysctl-Änderungen, bewusst vor der SSH-Härtung)
- Firewall-Lockdown auf nur noch SSH/80/443 (SSH ratenbegrenzt)
- **härtet SSH am Ende**: Key-only-Login für jeden Account (Passwort- und
  Keyboard-Interactive-Login aus), root-Login deaktiviert, höchstens drei
  Anmeldeversuche, kein X11-Forwarding. Danach liest das Playbook die
  wirksame `sshd`-Konfiguration und bricht ab, falls eine Datei unter
  `/etc/ssh/sshd_config.d/` diese Einstellungen überschreibt

Die vollständige Task-für-Task-Begründung steht in `setup_vps.yml`s
eigenen Kommentaren.

### Unterstützte Distributionen

Dieses Playbook braucht mindestens **Podman-Major-Version 5**, installiert
aus dem jeweils eigenen Standard-Repository der Distribution — nicht aus
einem Drittanbieter-Repo. Podman 5.0 ist eine echte technische
Voraussetzung, keine bloße Präferenz: Quadlets `.pod`-Unit-Typ (den jeder
Pod in diesem Repo nutzt, z.B. `quadlets/backend/osa-backend.pod`) gibt es
erst ab Podman 5.0. `setup_vps.yml`s Versions-Check-Task bricht sauber ab
— bevor irgendein destruktiver Schritt (Reboot, Firewall, SSH-Härtung)
läuft — falls das native Repository des Ziels etwas Älteres liefert,
statt die Lücke still mit einem Drittanbieter-Repo unbekannter
Vertrauenswürdigkeit zu stopfen.

Bekannt gute Release-Zweige (native Podman-Version verifiziert am
27.08.2026, ändert sich mit der Zeit — die gezeigten Patch-Versionen sind
eine Momentaufnahme, entscheidend ist allein die Major-Version-Prüfung,
die das Playbook zur Laufzeit durchführt):

| Distribution | Native Podman-Version |
| --- | --- |
| Debian 13 ("trixie") | 5.4.2 |
| Ubuntu 26.04 LTS | 5.7.0 |
| Rocky Linux / AlmaLinux / CentOS Stream 10 | 5.6.0+ |
| Fedora (aktuelles Stable-Release) | 5.8.x |
| openSUSE Leap 16.0 | 5.4.2 |

Bekannt zu alte Release-Zweige — vor dem Einsatz dieses Playbooks lieber
neu aufsetzen statt gegen diese laufen zu lassen: Debian 12, Ubuntu 24.04
LTS (nativ 4.9.3), jedes RHEL-Familie-9.x-Release, openSUSE Leap 15.x.

```bash
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root -e full_upgrade=true
# Falls nur Passwort-Login moeglich ist:
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u root -e full_upgrade=true --ask-pass

# Re-Run / Verifikation (root-Login ist danach deaktiviert):
ansible-playbook -i inventory/production.ini playbooks/setup_vps.yml -u admin --ask-become-pass --check --diff
```

Der Produktions-Host ist bereits gehärtet — ein erster Lauf hier sollte nur
zur Verifikation dienen (`--check --diff`, erwartet quasi kein Diff), nicht
zur Neuinstallation.

## Secrets pflegen

`secrets/<stage>/caddy.env`, `secrets/<stage>/osa-backend.env.j2`,
`secrets/<stage>/osa-frontend.env.j2`, `secrets/production/koofr.yml` und
`secrets/nonproduction/koofr.yml` liegen `ansible-vault`-verschlüsselt im
Repo, nie im Klartext committet. Die
passenden `*.example`-Dateien sind die (Klartext-sicheren) Vorlagen.

Jedes `secrets/<stage>/`-Verzeichnis führt dieselben fünf
`.example`-Vorlagen (`caddy.env.example`, `osa-backend-pg.env.example`,
`osa-backend-valkey.env.example`, `osa-backend.env.j2.example`,
`osa-frontend.env.j2.example`), unabhängig davon, ob diese Stage schon
deployt ist -- sie enthalten keine Secrets, es gibt also keinen Grund, sie
vorzuenthalten. Die fünf passenden echten, vault-verschlüsselten Dateien
entstehen erst, sobald eine Stage ein tatsächliches Ziel in
`inventory/<stage>.ini` hat (nicht nur einen
`CHANGEME.example.invalid`-Platzhalter): `production` und jede gerade
aufgesetzte Stage haben alle fünf, eine noch nicht bereitgestellte Stage
noch keine.

Die Koofr-Zugangsdaten liegen außerhalb der Stage-Verzeichnisse, in
`secrets/production/koofr.yml` und `secrets/nonproduction/koofr.yml` (das
zweite Verzeichnis ist keine Stage, es bedient `test` und `qa`), jeweils mit
ihrer `.example`-Vorlage.

Die beiden `.j2`-Dateien enthalten zusätzlich Jinja2-Ausdrücke
(`{{ backend_domain }}` etc., befüllt aus `inventory/<stage>.ini`) neben
den echten Secrets -- Vault-Verschlüsselung und Jinja2-Templating stehen
sich nicht im Weg: `ansible.builtin.template` entschlüsselt den
Vault-Inhalt zuerst, rendert danach das Jinja2, der Edit-Workflow unten
bleibt also unverändert:

```bash
cp secrets/<stage>/osa-backend.env.j2.example secrets/<stage>/osa-backend.env.j2
$EDITOR secrets/<stage>/osa-backend.env.j2
ansible-vault encrypt secrets/<stage>/osa-backend.env.j2

# Später bearbeiten:
ansible-vault edit secrets/<stage>/osa-backend.env.j2
ansible-vault view secrets/<stage>/osa-backend.env.j2
```

`secrets/<stage>/caddy.env`s `LOGGING_USER`/`LOGGING_PASSWORD_HASH` folgen
derselben Regel wie `osa-backend-pg.env` unten — pro Stage ein frisches
Passwort generieren, niemals das einer anderen Stage wiederverwenden, damit
ein geleakter Nicht-Produktions-Dozzle-Login nicht auch als
Produktions-Login funktioniert. Dozzle hält den Podman-API-Socket, sein Login
ist daher so sensibel wie die Secrets der Stage. Der Zugriff lässt sich
zusätzlich mit der optionalen Inventory-Variable `logging_allowed_cidrs` auf
Netze beschränken, eine Liste in den `[vps:vars]` der Stage, zum Beispiel
`logging_allowed_cidrs=["203.0.113.0/24"]`. Ein Client außerhalb der Liste
bekommt `403`, bevor Zugangsdaten geprüft werden; ohne die Variable ist der
Viewer hinter dem Basic-Auth-Login von überall erreichbar. Die Liste so klein
wie möglich halten.
`KOOFR_USER`/`KOOFR_PASSWORD` der `osa-backend.env.j2` stammen aus zwei
Vault-Dateien, damit kein Host mehr Zugriff hat, als er braucht:

- `secrets/production/koofr.yml`: das Konto, dem der Backup-Ordner gehört.
  Production schreibt damit die nächtlichen Dumps und räumt alte auf.
- `secrets/nonproduction/koofr.yml`: ein zweites Konto, das den
  Backup-Ordner von Production nur **lesen** kann. `test` und `qa` nutzen es,
  weil sie nur das jüngste Production-Backup herunterladen (`downsync`); ein
  kompromittierter Host dort darf die Backups weder lesen, überschreiben noch
  löschen.

`playbooks/deploy.yml` lädt die passende Datei per `vars_files:` und bricht
mit einer Meldung ab, wenn sie fehlt; jede Stage-`osa-backend.env.j2`
referenziert nur `{{ koofr_user }}`/`{{ koofr_password }}`, pro Stage muss
nichts ausgefüllt werden. Das Lese-Konto richtest du einmal ein: zweites
Koofr-Konto anlegen, den Backup-Ordner schreibgeschützt mit ihm teilen und in
diesem Konto prüfen, dass Auflisten und Herunterladen funktionieren, Hochladen
und Löschen abgelehnt werden und unter welchem WebDAV-Pfad der geteilte
Ordner erscheint (dieser Pfad kommt als `KOOFR_BACKUP_PATH` in die
`osa-backend.env.j2` von `test`/`qa`). Dann:
```bash
cp secrets/nonproduction/koofr.yml.example secrets/nonproduction/koofr.yml
$EDITOR secrets/nonproduction/koofr.yml
ansible-vault encrypt secrets/nonproduction/koofr.yml
```
Eine Koofr-Passwort-Rotation ist ein einziger Edit der passenden Datei:
```bash
ansible-vault edit secrets/production/koofr.yml      # oder secrets/nonproduction/koofr.yml
```
`scripts/backup_db.py`/`restore_db.py` (siehe "Disaster Recovery" unten)
greifen so stageunabhängig auf denselben Backup-Bestand zu (Non-Production-
Stages über die Lese-Freigabe). `SMTP_*` sollte
auf Non-Prod-Stages auskommentiert bleiben (kein Mailversand außerhalb von
Prod) — `SMTP_PORT` ist int-typisiert, ein leerer Wert scheitert beim Start
an der Validierung; nur ein auskommentierter Schlüssel zählt als unset.

`secrets/<stage>/osa-backend-pg.env` folgt demselben `.example`-Workflow
wie die beiden `.j2`-Dateien oben — pro Stage frisch anlegen, mit einem neu
generierten Passwort, niemals das einer anderen Stage wiederverwenden:
```bash
cp secrets/<stage>/osa-backend-pg.env.example secrets/<stage>/osa-backend-pg.env
$EDITOR secrets/<stage>/osa-backend-pg.env
ansible-vault encrypt secrets/<stage>/osa-backend-pg.env
```

`secrets/<stage>/osa-backend-valkey.env` folgt demselben Workflow — sein
`VALKEY_PASSWORD` muss mit dem Wert übereinstimmen, der in der
`osa-backend.env.j2` derselben Stage eingetragen wird (siehe
`templates/osa-backend-valkey.container.j2`):
```bash
cp secrets/<stage>/osa-backend-valkey.env.example secrets/<stage>/osa-backend-valkey.env
$EDITOR secrets/<stage>/osa-backend-valkey.env
ansible-vault encrypt secrets/<stage>/osa-backend-valkey.env
```

### Datenbank-Rollen

Das PostgreSQL jeder Stage hat zwei Rollen. `POSTGRES_USER` in
`osa-backend-pg.env` (`osa`) ist die Bootstrap-Rolle des Clusters: immer
Superuser, nur für die Administration. Backend und Worker verbinden sich als
`osa_app`, eine Rolle ohne Superuser-Rechte, der die Anwendungsdatenbank
gehört; ein Fehler in der Anwendung erreicht so weder `COPY ... PROGRAM`
noch Server-Dateien oder die Rollenverwaltung.

Die Rolle legt niemand von Hand an. Jeder `deploy.yml`-Lauf wendet
`config/postgres/split-app-role.sql` im Datenbank-Container an, nachdem der
Stack läuft und bevor der Pod neu startet: Es legt die Rolle an, setzt das
Passwort aus `DATABASE_URL` in der `osa-backend.env.j2` und überträgt ihr den
Besitz von allem im Schema `public`. Der Benutzer in `DATABASE_URL` muss
`osa_app` sein, das Passwort ein neu generierter Wert mit mindestens 16
Zeichen aus `A-Za-z0-9._~-` (es steht ohne Escaping in der URL) und ein
anderer als `POSTGRES_PASSWORD`. Jeder andere Benutzer oder jedes andere
Passwort stoppt den Deploy, bevor etwas geändert wird, auch unter `--check`.
Ein rotiertes Passwort in `DATABASE_URL` erreicht die Rolle mit dem nächsten
Deploy.

**Bestehende Stage umstellen** (die Produktions-Datenbank wurde mit der
Bootstrap-Rolle als Besitzer von allem angelegt):

1. Frisches Backup ziehen (`podman exec osa-backend python scripts/backup_db.py`)
   und sicherstellen, dass keine Sitzung der Bootstrap-Rolle auf der
   Anwendungsdatenbank offen ist.
2. Sicherstellen, dass das laufende `osa-backend`-Image schon mit
   `--no-owner --no-acl` wiederherstellt; sonst scheitern die nächtliche
   Auffrischung der Non-Production-Stages und ein manueller Restore mit
   Besitzer-Fehlern, sobald die Anwendungsrolle die Datenbank besitzt.
3. `ansible-vault edit secrets/<stage>/osa-backend.env.j2`: `DATABASE_URL`
   auf den Benutzer `osa_app` und ein neu generiertes Passwort setzen.
4. `deploy.yml --check --diff`, dann anwenden. Zuerst wird die Rolle
   eingerichtet, dann startet die geänderte Env-Datei den Backend-Pod neu,
   der sich nun als `osa_app` verbindet.
5. Prüfen: `podman exec osa-backend-pg sh -c 'psql -U "$POSTGRES_USER" -d
   "$POSTGRES_DB" -Atc "select usename, count(*) from pg_stat_activity where
   datname = current_database() and pid <> pg_backend_pid() group by 1"'`
   listet nur `osa_app`, und die Anwendung funktioniert.

Das Playbook besteht auf `osa_app`, einen Rückweg über es gibt es also nicht;
manuell hilft die Zeile `DATABASE_URL` in `~/.env/osa-backend.env` auf dem
Host plus ein Neustart des Pods.

### test/qa aufsetzen: dauerhafte Stage vs. Wegwerf-Smoke-Test

`inventory/<stage>.ini` mit einem echten Host zu befüllen und die echten
Secrets dieser Stage anzulegen (siehe oben) bedeutet eines von zwei Dingen
— und das entscheidet, wo dieser Stand committet wird:

- **Dauerhafte Stage** — der Host bleibt dauerhaft `test`/`qa` (die IP darf
  sich später bei einem Neuaufbau trotzdem ändern). `inventory/<stage>.ini`
  und `secrets/<stage>/*` ganz normal auf `development` committen, genau
  wie bei `production`. Ein späterer Neuaufbau des Hosts ist eine
  Config-Änderung (neue IP, rotierte Secrets) direkt auf `development`,
  kein Revert.
- **Wegwerf-Smoke-Test** — der Host existiert nur, um die Deploy-Tools
  selbst zu prüfen, und wird danach sofort wieder abgebaut; die Stage soll
  anschließend wieder als unprovisioniert gelten. Das gehört auf einen
  Wegwerf-Branch, der nie in `development` gemerged wird, damit der echte
  Host/die Domain und die vault-verschlüsselten Secrets niemals in
  `development`s History landen:

  ```bash
  git checkout development && git pull
  git checkout -b qa-smoketest-YYYY-MM-DD    # diesen Branch nie pushen

  # inventory/<stage>.ini befüllen + secrets/<stage>/* wie oben anlegen/verschlüsseln
  git add inventory/qa.ini secrets/qa/
  git commit -m "qa-smoketest: <was hier geprüft wird>"

  # deployen, verifizieren, VPS wieder abbauen, dann:
  git checkout development
  git branch -D qa-smoketest-YYYY-MM-DD      # weg, keine Spur in development
  ```

  `development`s `inventory/<stage>.ini` bleibt dabei durchgehend auf
  `CHANGEME.example.invalid`, `secrets/<stage>/` behält nur seine
  `.example`-Vorlagen — dort gibt es nichts zu reverten.

## Phase 2 — Tag-2-Betrieb: Secrets/Quadlets/Timer synchron halten

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --check --diff   # erst pruefen
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass                  # dann anwenden
```

Synct Secrets, Quadlet-**Definitionen** (Caddy/Logging/Backend/Frontend),
die Caddyfile selbst und den nativen podman-prune-Service/-Timer; startet
und hält den kompletten Stack am Laufen (Caddy + Backend + Frontend +
Dozzle) plus den wöchentlichen podman-prune-Aufräumlauf. Baut keine
Images, klont keine Repos, restored keine Datenbank.
Bei einer frisch aufgesetzten Stage (siehe "test/qa aufsetzen" oben)
bedeutet das: die Datenbank ist danach leer — siehe "Frisches oder leeres
Postgres-Datenverzeichnis" unten für den einen zusätzlichen Schritt, der
statt eines leeren Login-Screens ein funktionierendes, befülltes System
liefert.

Eine geänderte Secret-Datei startet genau die Units neu, die sie lesen:
`caddy.env` startet Caddy neu, `osa-backend.env`, `osa-backend-pg.env` und
`osa-backend-valkey.env` starten den Backend-Pod neu (Backend, Worker,
PostgreSQL, Valkey), `osa-frontend.env` startet den Frontend-Pod neu. Ein
neues `POSTGRES_PASSWORD` reicht allein nicht: Das PostgreSQL-Image wendet
es nur beim Initialisieren eines leeren Datenverzeichnisses an, eine
bestehende Datenbank braucht zusätzlich `ALTER ROLE` (interaktiver
Schritt).

### Sofort-Deploy (statt auf den nächtlichen Auto-Update-Lauf zu warten)

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-backend
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-frontend
```

Pullt das Image-Tag, das die Quadlet verwendet (`latest`, solange kein
Release gepinnt ist, siehe unten), und startet den zugehörigen Pod neu.

### Release pinnen (Image zurückhalten oder zurückrollen)

Die App-Quadlets werden aus `templates/osa-backend.container.j2`,
`templates/osa-backend-worker.container.j2` und
`templates/osa-frontend.container.j2` gerendert. Das Image-Tag kommt aus
zwei optionalen Variablen und ist standardmäßig `latest`, es ändert sich
also nichts, solange keine gesetzt ist:

| Variable | Container | Wert |
|---|---|---|
| `osa_backend_image_tag` | `osa-backend` und `osa-backend-worker` gemeinsam | voller Commit-SHA eines Releases in `osa-backend` |
| `osa_frontend_image_tag` | `osa-frontend` | voller Commit-SHA eines Releases in `osa-frontend` |

Backend und Worker teilen sich absichtlich eine Variable: Ein Worker darf
nie gegen ein Schema laufen, das der Web-Container (der einzige, der
migriert) noch nicht migriert hat, und umgekehrt.

Der Commit-SHA ist das Tag, das die CI neben `latest` pusht; die Commit-Liste
von `main` und die Paketseite des Repositories auf GitHub zeigen ihn (der
Kopieren-Knopf neben einem Commit liefert den vollen SHA). Für einen
Rollback nimmst du den Merge-Commit vor dem fehlerhaften. Erst die Vorschau,
dann anwenden:

```bash
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass --check --diff -e osa_backend_image_tag=<voller-commit-sha>
ansible-playbook -i inventory/production.ini playbooks/deploy.yml --ask-vault-pass -e osa_backend_image_tag=<voller-commit-sha>
```

Die geänderte Quadlet veranlasst das Playbook, den Pod neu zu starten. Das
nächtliche Auto-Update überschreibt einen Pin nicht, weil das Tag eines
Commit-SHA sich nie ändert. Ein Pin mit `-e` gilt nur für diesen Lauf: Ein
späterer Lauf ohne `-e` geht zurück auf `latest`. Für einen dauerhaften Pin
trägst du die Variable unter `[vps:vars]` der Inventory-Datei der Stage ein
(zum Beispiel `osa_backend_image_tag=<voller-commit-sha>`) und entfernst die
Zeile wieder, sobald der Fix veröffentlicht ist. `grep image_tag
inventory/*.ini` zeigt, ob noch ein Pin aktiv ist; solange er es ist, bleibt
der Dienst unbemerkt auf dieser Version. Ein Pin auf ein älteres Release
macht Datenbank-Migrationen nicht rückgängig, die das neuere Release schon
angewendet hat.

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
versehentlichem Überschreiben der Live-Datenbank). Das Skript nennt dann die
Ziel-Datenbank und das Backup und verlangt, dass der Name der Datenbank
eingetippt wird, bevor es etwas ersetzt (`--yes` überspringt die Rückfrage
für unbeaufsichtigte Läufe). Danach immer verifizieren, dass wirklich echte
Daten da sind, nicht nur, dass der Befehl fehlerfrei durchlief.

`osa-backend`s eigener Scheduler führt diesen Restore zusätzlich
**automatisch, jede Nacht, auf jeder Nicht-Production-Stage** aus (der
`downsync`-Job — eine Stunde nach dem nächtlichen Production-Backup, zieht
immer das neueste Production-Backup, nie umgekehrt), damit `test`/`qa` ganz
von selbst nah an den echten Production-Daten bleiben, ohne dass das jemand
manuell anstoßen muss. Volle Job-Liste und Zeitplan in `osa-backend`s
README.

Ein manueller Backup vor riskanten Änderungen:
`podman exec osa-backend python scripts/backup_db.py`. Volle Doku zu
beiden Skripten (alle Parameter inkl. `--dry-run`/`--cleanup`) in
[`osa-backend`s README](https://github.com/Kirchenmusik-St-Augustin/osa-backend).

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

Beide Befehle in einem Zug ausführen. Der wöchentliche `podman-prune.timer`
(Sonntags 04:00) entfernt jedes Image, das gerade von keinem Container
verwendet wird — und ein vorab gepulltes, aber noch nicht gestartetes
Image ist genau das. Podman kennt keinen "für später reserviert"-Zustand,
den ein Prune respektieren könnte. Liegt der eigentliche Bump nicht in
derselben Woche wie der Pre-Pull (z.B. weil ein Wartungsfenster abgewartet
wird), entweder unmittelbar vor dem Start erneut pullen, oder für die
Zwischenzeit `systemctl --user stop podman-prune.timer` setzen und danach
wieder aktivieren.

Die Anwendungsrolle `osa_app` liegt ebenfalls im Datenverzeichnis, ein
leeres hat sie also nicht: Das Backend kann sich nicht anmelden und ein
Restore nicht laufen, bis der nächste `deploy.yml`-Lauf die Rolle
eingerichtet hat (siehe „Datenbank-Rollen“ unter „Secrets pflegen“).

So oder so kommt die Datenbank danach schema-migriert, aber **leer** hoch —
alembic legt die Tabellen an, befüllt aber keine Zeile. Auf einer
Nicht-Production-Stage kommt man mit dem letzten Production-Backup zu einem
funktionierenden System mit echten Daten (wählt es ohne `--backup-name`
automatisch aus, `--force` ist nicht nötig — das Flag schützt nur
`APP_ENVIRONMENT=production`):

```bash
ssh service@<stage-host>
podman exec -it osa-backend python scripts/restore_db.py
```

Das ist genau das, was der nächtliche `downsync`-Scheduled-Job ohnehin
automatisch auf jeder Nicht-Production-Stage macht (siehe "Disaster
Recovery" oben) — der manuelle Trigger hier erspart nur das Warten auf
diesen nächtlichen Lauf direkt nach dem Aufsetzen einer Stage. (Auf
Production selbst tritt ein leeres Verzeichnis nur beim
Major-Version-Bump-Fall oben auf; dort mit `restore_db.py --force` aus dem
Backup unmittelbar vor dem Bump wiederherstellen, wie jeder andere
Production-Restore unter "Disaster Recovery" — ein Downsync zieht immer nur
*von* Production, restored aber nie *in* Production.)

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
wo sie auftaucht. `<your-shorturl-domain>` ist ein zweiter, unabhängiger
Platzhalter — oft einfach "go." plus eurer Haupt-Dev-Domain, aber nicht
zwingend, genau wie Productions eigene `shorturl_domain` auch nicht von
`backend_domain`/`frontend_domain` abgeleitet ist (siehe "Stages" oben).
Nichts unter `dev/` hardcoded eine bestimmte Domain.

### Verzeichnisstruktur

Anders als Produktion/Test/QA hat lokale Entwicklung kein Caddy-Quadlet
(Caddy läuft als gewöhnlicher, host-weiter Dienst auf der Dev-Maschine,
gemeinsam genutzt mit allem anderen, was sie sonst noch proxyt, nicht
containerisiert) und keine `logging`-Quadlet- oder
podman-prune-Service/-Timer-Entsprechung (Dozzle und der Prune-Timer sind
reine Prod-Annehmlichkeiten). Nur Backend und Frontend bekommen Quadlets:

| `dev/`-Pfad | Kopieren nach | Zweck |
|---|---|---|
| `dev/quadlets/backend/osa-backend-dev.build.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-dev.build` | Baut `localhost/osa-backend-dev:latest` aus `osa-backend`s eigenem `Dockerfile`, Target `dev` |
| `dev/quadlets/backend/osa-backend.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend.container` | Startet das Backend, mountet das Repo, `uvicorn --reload` |
| `dev/quadlets/backend/osa-backend-pg.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-pg.container` | Eigenes Dev-PostgreSQL, getrennt von jeder Ansible-verwalteten Stage |
| `dev/postgres-init/10-app-role.sh` | (direkt eingehängt, nicht kopiert) | Legt beim ersten Start mit leerem Postgres-Datenverzeichnis die Nicht-Superuser-Rolle `osa_app` an |
| `dev/quadlets/backend/osa-backend-valkey.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-valkey.container` | Eigener Dev-Valkey-Queue-Backend für den arq-Worker unten |
| `dev/quadlets/backend/osa-backend-worker.container.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-worker.container` | Startet den arq-Job-Worker, mountet das Repo, `--watch`-Reload |
| `dev/quadlets/backend/osa-backend.pod.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend.pod` | Veröffentlicht Backend-/Postgres-Ports, Pod-weite Einstellungen |
| `dev/quadlets/frontend/osa-frontend.container.example` | `~/.config/containers/systemd/osa/osa-frontend/osa-frontend.container` | Startet `npm run dev`, mountet das Repo, keine Build-/Pod-Unit nötig |
| `dev/env/osa-backend.env.example` | `~/.env/osa-backend.env` | Backend-Secrets/-Config (dev-großer Teilumfang, siehe unten) |
| `dev/env/osa-backend-pg.env.example` | `~/.env/osa-backend-pg.env` | Dev-Postgres-Zugangsdaten |
| `dev/env/osa-backend-valkey.env.example` | `~/.env/osa-backend-valkey.env` | Dev-Valkey-Passwort |
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
install -d -m 700 ~/.env
mkdir -p ~/.config/containers/systemd/osa/osa-backend ~/.config/containers/systemd/osa/osa-frontend ~/data/osa-backend/postgres ~/data/osa-backend/valkey

for f in dev/quadlets/backend/*.example; do
  cp "$f" ~/.config/containers/systemd/osa/osa-backend/"$(basename "${f%.example}")"
done
for f in dev/quadlets/frontend/*.example; do
  cp "$f" ~/.config/containers/systemd/osa/osa-frontend/"$(basename "${f%.example}")"
done
for f in dev/env/*.example; do
  cp "$f" ~/.env/"$(basename "${f%.example}")"
done
chmod 600 ~/.env/osa-backend.env ~/.env/osa-backend-pg.env ~/.env/osa-backend-valkey.env ~/.env/osa-frontend.env
```

`~/data/osa-backend/postgres`/`~/data/osa-backend/valkey` sind
`osa-backend-pg.container.example`s/`osa-backend-valkey.container.example`s
Bind-Mount-Ziele — Podman legt ein fehlendes Host-Verzeichnis für einen
Bind-Mount nicht selbst an, sondern scheitert hart
(`Error: statfs ...: no such file or directory`), wenn es fehlt.

Danach jede `~/.env/*.env`-Datei bearbeiten: `<your-dev-domain>`,
`<your-shorturl-domain>`, `SECRET_KEY`, `POSTGRES_PASSWORD`,
`OSA_APP_PASSWORD` (ein anderer Wert; es ist auch das Passwort in
`DATABASE_URL` und `TEST_DATABASE_URL`), `VALKEY_PASSWORD` (muss in `osa-backend.env` und
`osa-backend-valkey.env` übereinstimmen) sowie optional
`GOOGLE_CLIENT_ID`/`KOOFR_USER`/`KOOFR_PASSWORD` eintragen. Dann:

```bash
systemctl --user daemon-reload
systemctl --user start osa-backend-pg.service osa-backend-valkey.service osa-backend-worker.service osa-backend.service osa-frontend.service
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
   └─ <your-shorturl-domain> (optional, zum Testen von Kurz-URLs)
        └─ /*  → 127.0.0.1:21000 (mit /go-Präfix) → osa-backend
```

Die beiden Vhost-Blöcke aus `dev/Caddyfile.dev.example` in die eigene
Caddyfile einfügen (Speicherort abhängig davon, wie Caddy auf eurer
Maschine installiert ist — siehe Caddys eigene Doku für den Default-Pfad
auf eurer Plattform), `<your-dev-domain>`/`<your-shorturl-domain>`/
`<your-allowed-cidrs>` ersetzen, dann Caddy neu laden.
`<your-allowed-cidrs>` listet die Netze, die die Dev-Stage erreichen
dürfen; alle anderen bekommen `403`. Den Dev-Vhost nicht offen lassen: Die
Dev-Datenbank wird aus einem Produktions-Snapshot aufgefrischt, und der
Vite-Dev-Server liefert Quelldateien aus. Der `<your-shorturl-domain>`-Vhost
ist optional, nur nötig, um Kurz-URL-Redirects lokal zu testen, und bleibt
öffentlich, weil er nur ein Redirect-Dienst ist.

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
`~/.env/osa-backend.env` gebraucht; das Lese-Konto mit
`ansible-vault view secrets/nonproduction/koofr.yml` abrufen (Vault-Passwort
nötig), falls noch nicht vorhanden. Ohne Koofr-Zugang gibt
es aktuell keinen dokumentierten Weg zu einer befüllten, login-fähigen
Dev-Datenbank (siehe Anmerkung am Ende dieses Abschnitts).

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

- `DATABASE_URL`/`TEST_DATABASE_URL` nutzen Port `5432` — `osa-backend`
  erreicht Postgres über den gemeinsamen Pod-Netzwerk-Namespace, auf
  Postgres' eigenem containerinternem Port, nicht über den nach außen
  veröffentlichten Host-Port (`5433`), den `osa-backend.pod.example` als
  auskommentierte Option anbietet. Der laufende Stack braucht diesen
  Host-Port nicht, und jedes lokale Konto auf dem Dev-Host könnte ihn
  erreichen, deshalb ist er standardmäßig aus: die `PublishPort`-Zeile in
  `osa-backend.pod.example` nur einkommentieren, wenn manuelles
  `psql`-Debugging direkt vom Dev-Host aus gewünscht ist (nur Loopback,
  derselbe Zweck wie der äquivalente Port in Produktion, nie von
  außerhalb der Maschine erreichbar), und den Host-Port dort ändern, falls
  `5433` schon von einem anderen Projekt belegt ist.
- `VITE_API_BASE_URL` ist eine vollständige absolute URL, nicht das
  relative `/api`, das die App sonst empfiehlt — der Vite-Dev-Server
  selbst macht den `/api`-Pfad-Split nicht, das übernimmt nur das eigene
  Caddy davor. Ein komplett anderer Mechanismus als das Runtime-injizierte
  `API_BASE_URL` in Produktion (siehe `osa-frontend`s README für die
  `VITE_*`-vs.-Runtime-Config-Unterscheidung, hier nicht wiederholt).
- `TEST_DATABASE_URL` ist nur für Dev/Test, genutzt von `osa-backend`s
  Pytest-Suite (`podman exec osa-backend pytest ...`, siehe
  `osa-backend`s README) — in der Produktions-Env-Vorlage gar nicht
  enthalten. Ihre Datenbank (per Default `osa_test`) ist eine eigene
  Datenbank auf demselben Postgres-Server, nicht nur ein eigener
  Tabellensatz — vor dem ersten Testlauf einmalig anlegen:
  `podman exec osa-backend-pg psql -U osa -d osa -c "CREATE DATABASE osa_test OWNER osa_app;"`
  (Schema/Tabellen kommen von `osa-backend`s eigener Test-Suite, die beim
  ersten Gebrauch die echten Alembic-Migrationen ausführt, kein separater
  Migrationsschritt nötig).
- SMTP ist optional. `SMTP_HOST`/`SMTP_PORT` auf ein beliebiges lokales
  SMTP-Capturing-Tool zeigen lassen, oder beide auskommentiert lassen —
  Mailversand schlägt dann einfach still fehl, wie auf jeder anderen
  Non-Production-Stage.
- Das Dev-Postgres hat zwei Rollen. `POSTGRES_USER` (`osa`) ist die
  Bootstrap-Rolle des Clusters: immer Superuser, nur für die Administration.
  Das Backend verbindet sich als `osa_app` (`OSA_APP_PASSWORD`), eine
  Nicht-Superuser-Rolle, der die Datenbank gehört; ein Fehler in der
  Anwendung erreicht so weder `COPY ... PROGRAM` noch Server-Dateien oder
  die Rollenverwaltung. `dev/postgres-init/10-app-role.sh` legt sie an, aber
  Init-Skripte laufen nur bei einem leeren Datenverzeichnis. Eine bestehende
  Dev-Datenbank wird einmalig mit demselben Skript umgestellt, das auch die
  ausgerollten Stages nutzen (Passwort wählen und in `OSA_APP_PASSWORD` sowie
  in beide Datenbank-URLs eintragen), ausgeführt im Wurzelverzeichnis dieses
  Repositories:

  ```bash
  OSA_APP_PASSWORD='<OSA_APP_PASSWORD>' podman exec -i -e OSA_APP_PASSWORD osa-backend-pg \
      sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -v ON_ERROR_STOP=1' \
      < config/postgres/split-app-role.sql
  ```

  Das Skript ist idempotent und überträgt den Besitz jedes Objekts im Schema
  `public`, was `REASSIGN OWNED BY osa` nicht kann: Die Bootstrap-Rolle
  besitzt auch die System-Datenbanken, die PostgreSQL nicht übertragen lässt.
  Anschließend Backend und Worker neu starten. Ein Restore aus einem Backup
  (auch die nächtliche Auffrischung der Non-Production-Stages) leert zuerst
  das Schema `public` und beendet alle anderen Sitzungen der Datenbank;
  PostgreSQL erlaubt `osa_app` nicht, eine Sitzung der Bootstrap-Rolle zu
  beenden. Während eines Restores deshalb keine Sitzung der Bootstrap-Rolle
  auf der Anwendungsdatenbank offen halten (zur Administration die Datenbank
  `postgres` nutzen).
