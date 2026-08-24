# osa-deploy

Central operations handbook for **OSA**'s production
(`einteilung.hochamt.at`, Orchester-Einteilung — the scheduling/casting
system for the church musicians of Kirchenmusik St. Augustin): which repo
is for what, how production is built, and how to set it up and deploy it.
This repo itself contains everything operational: Ansible playbooks, Caddy
configuration, Podman Quadlets, and (vault-encrypted) secrets.

**Every command in this repo/README is only ever described by Claude,
never executed by it — carrying it out is exclusively manual.**

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
administrative root tasks (`sudo`), it never runs containers itself (same
security boundary as the sister project `vb-fastapi-vue`/`vb-deploy`).

- **systemd Quadlets** instead of `docker-compose`: every container/pod is
  described as a `.container`/`.pod`/`.volume` file, which `systemd --user`
  translates into a real systemd service automatically.
- **Two pods for the app:** `osa-backend-pod` (the backend container plus
  an `osa-backend-pg` PostgreSQL sidecar) and `osa-frontend-pod` (nginx).
  Legacy (PHP/Laravel) has been fully decommissioned — this is the only
  stack running.
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
Frontend), the Caddyfile itself, and the maintenance timer; starts and
keeps running the entire stack (Caddy + Backend + Frontend + Maintenance +
Dozzle). Builds no images, clones no repos, restores no database.

### Immediate deploy (instead of waiting for the nightly auto-update run)

```bash
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-backend
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-frontend
```

Pulls the current `:latest` image and restarts the corresponding pod.

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

`secrets/osa-backend-pg.env` has no `.example` template (mirrors the
sister project's `vb-api-pg.env`) — create it fresh with a freshly
generated password, never reuse the dev one:
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
authoritative deploy repo. All three, plus `osa/osa-einteilung.hochamt.at`
(the Legacy Laravel app itself), are archived on GitHub (read-only, not
deleted) now that Legacy has been fully decommissioned.

---

# Deutsch

Zentrales Betriebshandbuch für die Produktion von **OSA** (Orchester-Einteilung,
`einteilung.hochamt.at`): welches Repo wofür da ist, wie die Produktion
aufgebaut ist, und wie man sie aufsetzt und deployt. Dieses Repo selbst
enthält alles Betriebliche: Ansible-Playbooks, Caddy-Konfiguration,
Podman-Quadlets und (vault-verschlüsselte) Secrets.

**Jeder Befehl in diesem Repo/README wird von Claude nur beschrieben, nie
ausgeführt — die Durchführung ist ausschließlich manuell.**

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
administrative Root-Aufgaben (`sudo`), er betreibt selbst keine Container
(gleiche Sicherheitsgrenze wie im Schwesterprojekt `vb-fastapi-vue`/
`vb-deploy`).

- **systemd Quadlets** statt `docker-compose`: jeder Container/Pod wird als
  `.container`/`.pod`/`.volume`-Datei beschrieben, die `systemd --user`
  automatisch in einen echten systemd-Service übersetzt.
- **Zwei Pods für die App:** `osa-backend-pod` (der Backend-Container plus
  ein `osa-backend-pg`-PostgreSQL-Sidecar) und `osa-frontend-pod` (nginx).
  Legacy (PHP/Laravel) ist vollständig stillgelegt — das hier ist der
  einzige laufende Stack.
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
Frontend), die Caddyfile selbst und den Maintenance-Timer; startet und hält
den kompletten Stack am Laufen (Caddy + Backend + Frontend + Maintenance +
Dozzle). Baut keine Images, klont keine Repos, restored keine Datenbank.

### Sofort-Deploy (statt auf den nächtlichen Auto-Update-Lauf zu warten)

```bash
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-backend
ansible-playbook -i inventory/hosts.ini playbooks/deploy.yml --ask-vault-pass --tags deploy-frontend
```

Pullt das aktuelle `:latest`-Image und startet den zugehörigen Pod neu.

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

`secrets/osa-backend-pg.env` hat keine `.example`-Vorlage (analog zu
`vb-api-pg.env` im Schwesterprojekt) — frisch anlegen, mit einem neu
generierten Passwort, niemals das Dev-Passwort wiederverwenden:
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
autoritatives Deploy-Repo. Alle drei, plus `osa/osa-einteilung.hochamt.at`
(die Legacy-Laravel-App selbst), sind auf GitHub archiviert (read-only,
nicht gelöscht), jetzt wo Legacy vollständig stillgelegt ist.
