# osa-deploy

[English](README.md) · [Übergabeblatt](HANDOVER.de.md)

Betriebs-Repository für **OSA** (`einteilung.hochamt.at`, das Einteilungs- und
Besetzungssystem der Kirchenmusik St. Augustin): Ansible-Playbooks,
Caddy-Konfiguration, Podman-Quadlets und der Befehl `./osa`, der sie ausführt.
Das Repository enthält **keine Secrets und keine Installationsdaten**.
Inventar, vault-verschlüsselte Secrets, Operator-Key und Zugangsnotiz jeder
Stage liegen im privaten Site-Repository `osa-access`, das neben diesem
Repository liegt (`../osa-access/sites/<stage>/`).

## 1. Wo anfangen

| Situation | Weiter bei |
|---|---|
| Die Seite ist down oder verhält sich falsch | [3. Wenn etwas ausfällt](#3-wenn-etwas-ausfällt) |
| Neuer Server oder Umzug auf einen anderen Host | [4. Neuaufbau oder Umzug](#4-neuaufbau-oder-umzug) |
| Änderung ausrollen, Image pinnen oder zurückrollen | [5. Der Befehl ./osa](#5-der-befehl-osa) |
| Entwicklungsrechner einrichten | [6. Lokale Entwicklung](#6-lokale-entwicklung) |
| Den Betrieb von jemand anderem übernehmen | [HANDOVER.de.md](HANDOVER.de.md) |
| Welches Secret wo liegt | [7.1 Secrets](#71-secrets) |

## 2. Architektur

### 2.1 Die Repositories

| Repository | Was | Läuft als |
|---|---|---|
| [`osa-backend`](https://github.com/Kirchenmusik-St-Augustin/osa-backend) | FastAPI, SQLAlchemy, PostgreSQL, arq-Worker | Pod `osa-backend` |
| [`osa-frontend`](https://github.com/Kirchenmusik-St-Augustin/osa-frontend) | Vue-3-Single-Page-Anwendung, ausgeliefert von nginx | Pod `osa-frontend` |
| [`osa-deploy`](https://github.com/Kirchenmusik-St-Augustin/osa-deploy) (dieses) | Playbooks, Templates, Quadlets, der Befehl `./osa` | konfiguriert die anderen |
| `osa-access` (privat) | Inventar, vault-verschlüsselte Secrets, Operator-Key, Zugangsnotiz | wird von `./osa` gelesen |

Die CI beider Anwendungs-Repositories baut bei jedem Merge nach `main` ein
Image und pusht es nach `ghcr.io/kirchenmusik-st-augustin/<repo>` als `latest`
und als vollständigen Commit-SHA. Dieses Repository baut kein Image und
klont kein Anwendungs-Repository auf den Host; es verteilt Konfiguration und
Secrets und stellt sicher, dass die richtigen Images laufen.

### 2.2 Laufzeit-Aufbau

Ein VPS trägt eine Stage. Alles läuft **rootless** unter dem unprivilegierten
Benutzer `service`; der Benutzer `admin` ist nur für Root-Aufgaben (`sudo`)
da und startet nie Container. Container sind als systemd-Quadlets
(`.container`, `.pod`, `.volume`) beschrieben, die `systemd --user` in
Services übersetzt.

- `osa-backend-pod`: das Backend, ein PostgreSQL-Sidecar `osa-backend-pg`,
  ein Queue-Sidecar `osa-backend-valkey` und der `osa-backend-worker`
  (arq-Jobs: Mail, Backup, Downsync, Aufräumarbeiten).
- `osa-frontend-pod`: nginx mit der Single-Page-Anwendung.
- Caddy ist der einzige öffentliche Listener (80/443), terminiert TLS und
  leitet pfadbasiert weiter:

  ```
  Internet :80/:443
     ▼
  Caddy (Host-Netzwerk)
     └─ einteilung.hochamt.at
          ├─ /logging/dozzle*  → 127.0.0.1:8081 → Dozzle (Basic Auth)
          ├─ /go/*             → Redirect → go.hochamt.at
          ├─ /api, /api/*      → 127.0.0.1:21000 → osa-backend-pod (Präfix entfernt)
          └─ alles andere      → 127.0.0.1:21001 → osa-frontend-pod
     └─ go.hochamt.at (Kurz-URLs) → 127.0.0.1:21000 (mit Präfix /go)
  ```
- Die Anwendungs-Container nutzen `Image=…:latest` und `AutoUpdate=registry`:
  Podmans eigener `podman-auto-update.timer` zieht und startet sie täglich neu,
  ohne Ansible. `./osa deploy` braucht man, um Konfiguration zu verteilen, für
  ein sofortiges Deploy und um einen Release zu pinnen.
- Dozzle ist ein schreibgeschützter Log-Viewer hinter Basic Auth. Der
  wöchentliche `podman-prune.timer` führt `podman system prune -a -f` auf dem
  Host aus.

### 2.3 Container-Härtung

Alle Container laufen mit `ReadOnly=true`, `NoNewPrivileges=true` und
`DropCapability=all`, mit einem kleinen `tmpfs`, wo ein Prozess schreiben muss.
Images, die intern sonst Rechte abgeben würden, laufen direkt als eigener
Benutzer (`User=101:101` für nginx, `User=999:999` für PostgreSQL).
Speicher- und Prozesslimits sind pro Container gesetzt. Die Kommentare in
`templates/` und `quadlets/` enthalten die Zahlen und ihre Herleitung. Die
Shutdown-Timeouts von Backend und Worker sind bewusst gestaffelt:
`StopTimeout=` (Podman) und `TimeoutStopSec=` (systemd) liegen über dem
Graceful-Shutdown-Fenster von gunicorn und arq, damit ein Neustart nie einen
Request oder ein laufendes Backup abschneidet. Die CI von `osa-backend` baut
das Produktions-Image und startet beide Container unter diesen Einschränkungen,
bevor ein Release entsteht.

### 2.4 Stages und das Site-Repository

`./osa` kennt die Stages `production`, `qa` und `test` (der Wert von
`app_environment`). Eine Stage ist ein Verzeichnis des Site-Repositorys:

```
osa-access/sites/<stage>/
  inventory.ini          Host, Domains, optionale Image-Pins
  operator_key.vault     SSH-Key des Operators (vault-verschlüsselt)
  operator_keys.pub      öffentliche(r) Key(s), für admin und service installiert
  secrets/               vault-verschlüsselte Umgebungsdateien
```

`inventory.ini` enthält `app_environment`, `backend_domain`,
`frontend_domain`, `shorturl_domain` und `acme_email`. Die Domains sind
unabhängig voneinander (Backend und Frontend können eine teilen, wie auf
Production, oder nicht) und werden beim Deploy in die Caddyfile und in die
`*.env.j2`-Dateien gerendert. Jede Stage hält ihre Datenbank unter
`~/data/osa/<stage>/postgres` auf ihrem Host. `development` ist hier bewusst
keine Stage: Die lokale Entwicklung läuft auf dem Rechner der Entwicklerin
bzw. des Entwicklers (Kapitel 6).

Heute wird nur `production` ausgerollt. `test` und `qa` entstehen auf dem
gleichen Weg, sobald ein Server dafür existiert (Abschnitt 4.5).

## 3. Wenn etwas ausfällt

### 3.1 Herausfinden, was los ist

```bash
./osa ssh production                      # Shell als Benutzer service
podman ps -a                              # Container und ihr Health-Status
systemctl --user list-units --failed
journalctl --user -u osa-backend-pod -n 100
```

Der Log-Viewer liegt unter `https://einteilung.hochamt.at/logging/dozzle`
(Basic Auth, das Login steht in `secrets/caddy.env`). Die Units heißen
`caddy.service`, `osa-backend-pod.service`, `osa-frontend-pod.service`,
`logging-dozzle.service` und `podman-prune.timer`.

### 3.2 Neu starten

Immer den **Pod-Service** neu starten, nie einen einzelnen Container: Ein
Container hängt am Pod, ein Neustart allein stoppt den Pod, und nichts holt
ihn wieder hoch.

```bash
systemctl --user restart osa-backend-pod.service    # Backend, Worker, PostgreSQL, Valkey
systemctl --user restart osa-frontend-pod.service
systemctl --user restart caddy.service
```

### 3.3 Ein Image zurückrollen

Siehe Abschnitt 5.3: den vorherigen Release pinnen und ausrollen.

### 3.4 Datenbank wiederherstellen

Die Skripte liegen in `osa-backend` und laufen auf dem Zielsystem:

```bash
./osa ssh production
podman exec osa-backend python scripts/backup_db.py --list      # vorhandene Backups
podman exec -it osa-backend python scripts/restore_db.py --force
```

`--force` ist unter `APP_ENVIRONMENT=production` Pflicht. Das Skript nennt die
Zieldatenbank und das Backup und verlangt die Eingabe des Datenbanknamens,
bevor es etwas ersetzt. Danach prüfen, dass wirklich Daten da sind und nicht
nur der Befehl sauber beendet wurde. Ein Restore leert das Schema `public` und
beendet alle anderen Sitzungen der Datenbank; PostgreSQL erlaubt `osa_app`
nicht, eine Sitzung der Bootstrap-Rolle zu beenden, also keine solche Sitzung
auf der Anwendungsdatenbank offen halten (für Administration die Datenbank
`postgres` nehmen).

Jede Nicht-Production-Stage spielt das neueste Production-Backup jede Nacht
selbst ein (Job `downsync`, eine Stunde nach dem Production-Backup), damit
`test` und `qa` nah an Production bleiben. Ein manuelles Backup vor einer
riskanten Änderung: `podman exec osa-backend python scripts/backup_db.py`.
Alle Parameter stehen in der README von `osa-backend`.

**Leeres Datenverzeichnis.** Startet `osa-backend-pg` gegen ein Verzeichnis
ohne Cluster (neue Stage oder nach einem PostgreSQL-Major-Wechsel, bei dem das
alte Verzeichnis beiseite geschoben wurde), kommt die Datenbank migriert, aber
leer hoch. Das Image zuerst ziehen (`podman pull
docker.io/library/postgres:<version>`, die Version steht in
`templates/osa-backend-pg.container.j2`) und den Service in derselben Sitzung
starten: Quadlet baut einen Pod ab, wenn er leer aussieht, und ein
unfertiger Pull zählt als leer. Der wöchentliche Prune entfernt Images, die
kein Container nutzt; ein vorab gezogenes Image muss also innerhalb der Woche
gestartet oder der Prune-Timer für die Lücke gestoppt werden. Die Rolle
`osa_app` liegt ebenfalls im Datenverzeichnis: Das Backend kann sich erst
anmelden, wenn das nächste `./osa deploy` sie angelegt hat. Danach die Daten
einspielen mit `podman exec -it osa-backend python scripts/restore_db.py`
(auf Nicht-Production-Stages ohne `--force`). Auf Production mit `--force` und
dem Backup, das direkt vor dem Wechsel gemacht wurde.

## 4. Neuaufbau oder Umzug

### 4.1 Voraussetzungen

- Auf dem Arbeitsrechner: `git`, `ssh`, `ansible` (das vollständige Paket: die
  Playbooks nutzen die Collections `ansible.posix` und `community.general`),
  `sshpass` (für den ersten Lauf gegen ein Passwort-Login) und Python 3.12 oder neuer.
- Die Repositories `osa-deploy` und `osa-access`, nebeneinander geklont. Das
  Vault-Passwort (siehe [HANDOVER.de.md](HANDOVER.de.md)).
- Ein Server mit Debian 13 oder einem anderen Release mit **Podman 5 oder
  neuer** aus dem eigenen Repository der Distribution. Podman 5.0 ist eine
  harte Anforderung (den Quadlet-Typ `.pod` gibt es ab 5.0).
  `setup_vps.yml` bricht vor jedem zerstörerischen Schritt ab, wenn die
  Version kleiner ist.

  | Distribution | Natives Podman |
  |---|---|
  | Debian 13 | 5.4.2 |
  | Ubuntu 26.04 LTS | 5.7.0 |
  | Rocky / AlmaLinux / CentOS Stream 10 | 5.6.0+ |
  | Fedora (aktuell) | 5.8.x |
  | openSUSE Leap 16.0 | 5.4.2 |

  Zu alt: Debian 12, Ubuntu 24.04, RHEL-Familie 9.x, openSUSE Leap 15.x.
- **DNS** von `backend_domain`, `frontend_domain` und `shorturl_domain` muss
  vor dem ersten `./osa deploy` auf den Server zeigen. Caddy holt die
  Zertifikate selbst bei Let's Encrypt und braucht dafür Port 80; eine Domain,
  die noch nicht auflöst, lässt die Zertifikatsausstellung scheitern und
  zählt gegen die Rate-Limits von Let's Encrypt. Prüfen mit
  `dig +short <domain>`.

### 4.2 Die Stage anlegen

```bash
./osa init production        # fragt nach einem NEUEN Vault-Passwort (mindestens 20 Zeichen)
$EDITOR ../osa-access/sites/production/inventory.ini
```

`init` kopiert die Vorlage aus `site-template/`, erzeugt das Operator-
Schlüsselpaar, verschlüsselt den privaten Schlüssel mit dem Vault-Passwort und
legt den öffentlichen Schlüssel in `operator_keys.pub` ab. Für einen Umzug ein
anderes Verzeichnis nehmen und die Umgebung nennen: `./osa init production-new
--environment production`.

Danach die Secret-Dateien aus ihren Vorlagen anlegen (Abschnitt 7.1 erklärt
jeden Wert):

```bash
cd ../osa-access/sites/production/secrets
for f in caddy.env osa-backend.env.j2 osa-backend-worker.env osa-backend-pg.env \
         osa-backend-valkey.env osa-frontend.env.j2; do cp $f.example $f; done
$EDITOR *.env *.env.j2                                    # Werte eintragen
cd ../../../../osa-deploy
./osa vault production encrypt secrets/caddy.env secrets/osa-backend.env.j2 \
    secrets/osa-backend-worker.env secrets/osa-backend-pg.env \
    secrets/osa-backend-valkey.env secrets/osa-frontend.env.j2
```

### 4.3 Den Server vorbereiten (einmalig)

```bash
./osa setup production -u root --ask-pass -e full_upgrade=true      # erster Lauf
./osa setup production -u admin --ask-become-pass --check --diff    # später: Verifikation
```

Das Playbook installiert die Pakete aus der Distribution, prüft die
Podman-Version, legt `admin` (sudo) und `service` (rootless Podman) an,
installiert die Keys aus `operator_keys.pub` für beide, stimmt rootless Podman
ab, **startet zwischendurch neu**, sperrt die Firewall auf SSH/80/443 (SSH
ratenbegrenzt) und härtet SSH am Ende (nur Key-Login, kein Root-Login, drei
Versuche, kein X11). Es schlägt fehl, wenn eine Datei unter
`/etc/ssh/sshd_config.d/` diese Einstellungen überschreibt. Tägliche
automatische Sicherheitsupdates werden eingeschaltet. Vor dem ersten Lauf
einmal von Hand verbinden (`ssh service@<host>`) und den Host-Fingerprint
über einen unabhängigen Kanal prüfen: Die Host-Key-Prüfung ist strikt.

Ein Server, der aufgesetzt wurde, bevor es den Operator-Key gab, braucht den
Key einmalig von Hand, weil `./osa` nur diesen Key anbietet: die Zeile aus
`operator_keys.pub` an `~/.ssh/authorized_keys` von `admin` und von `service`
anhängen (über ein bestehendes Login), dann mit `./osa ssh <stage>` prüfen.

### 4.4 Erstes Deploy

```bash
./osa check production        # Trockenlauf mit Diff
./osa deploy production
```

Danach das PostgreSQL-Image wie in 3.4 ziehen, wenn das Datenverzeichnis neu
ist, die Daten einspielen (3.4) und die Prüfung aus 5.4 durchführen.

### 4.5 Test- und QA-Stages

Eine Stage ist **dauerhaft** (der Server bleibt als `test` oder `qa` im
Dienst) oder ein **einmaliger Smoke-Test** (ein Server, der nur das Werkzeug
prüft und danach abgebaut wird). Beides ist ein Verzeichnis in `osa-access`;
bei einem einmaligen Test auf einem Wegwerf-Branch von `osa-access` arbeiten,
der nie gemergt wird, damit der echte Host und die verschlüsselten Dateien des
Versuchs nie in die Hauptgeschichte gelangen. Nicht-Production-Stages nutzen
den **Read-only**-Koofr-Account in `osa-backend-worker.env` (Abschnitt 7.1)
und lassen `SMTP_*` in `osa-backend.env.j2` auskommentiert.

## 5. Der Befehl ./osa

### 5.1 Befehle

| Befehl | Macht |
|---|---|
| `./osa init <stage> [--environment E]` | Stage aus der Vorlage mit neuem Operator-Key anlegen |
| `./osa setup <stage> [Ansible-Optionen]` | `setup_vps.yml` |
| `./osa check <stage> [Ansible-Optionen]` | `deploy.yml --check --diff` |
| `./osa deploy <stage> [Ansible-Optionen]` | `deploy.yml` |
| `./osa ssh <stage> [ssh-Optionen]` | Shell als `service` mit dem Operator-Key |
| `./osa vault <stage> encrypt\|decrypt\|edit\|view DATEI…` | Dateien relativ zum Stage-Verzeichnis bearbeiten |
| `./osa vault <stage> rekey` | alle Vault-Dateien der Stage unter neuem Passwort neu verschlüsseln |

`--sites-dir VERZEICHNIS` (vor dem Befehl) zeigt auf ein anderes
Site-Verzeichnis. Der Befehl fragt das Vault-Passwort ab, legt Passwortdatei
und Operator-Key für die Dauer des Laufs in einem privaten Verzeichnis in
`/dev/shm` ab und entfernt beides danach. Alles nach dem Stage-Namen geht
unverändert an `ansible-playbook`.

### 5.2 Deploy im Tagesbetrieb

`./osa check production` und `./osa deploy production` gleichen Secrets,
Quadlet-Definitionen, Caddyfile und die nativen Prune-Units ab und halten den
gesamten Stack am Laufen. Sie bauen kein Image, klonen kein Repository und
spielen keine Datenbank ein. Eine geänderte Secret-Datei startet genau die
Units neu, die sie lesen: `caddy.env` startet Caddy neu; `osa-backend.env`,
`osa-backend-worker.env`, `osa-backend-pg.env` und `osa-backend-valkey.env`
starten den Backend-Pod neu; `osa-frontend.env` startet den Frontend-Pod neu.
Ein neues `POSTGRES_PASSWORD` allein reicht nicht: PostgreSQL wendet es nur
auf ein leeres Datenverzeichnis an, eine bestehende Datenbank braucht
zusätzlich `ALTER ROLE`.

**Sofort-Deploy** statt auf das nächtliche Auto-Update zu warten:

```bash
./osa deploy production --tags deploy-backend
./osa deploy production --tags deploy-frontend
```

Das zieht das Image-Tag, das das Quadlet nutzt, und startet den Pod nur dann
neu, wenn der laufende Container dieses Image noch nicht nutzt. Mit
`-e force_restart=true` wird in jedem Fall neu gestartet.

### 5.3 Einen Release pinnen (zurückhalten oder zurückrollen)

Das Image-Tag kommt aus zwei optionalen Inventar-Variablen und ist standardmäßig
`latest`:

| Variable | Container | Wert |
|---|---|---|
| `osa_backend_image_tag` | `osa-backend` und `osa-backend-worker` gemeinsam | vollständiger Commit-SHA eines Releases |
| `osa_frontend_image_tag` | `osa-frontend` | vollständiger Commit-SHA eines Releases |

Backend und Worker teilen sich eine Variable mit Absicht: Ein Worker darf nie
gegen ein Schema laufen, das der Web-Container (der einzige, der migriert)
nicht migriert hat. Der SHA ist das Tag, das die CI neben `latest` pusht; zum
Zurückrollen den Merge-Commit vor dem fehlerhaften nehmen.

```bash
./osa check production -e osa_backend_image_tag=<voller-commit-sha>
./osa deploy production -e osa_backend_image_tag=<voller-commit-sha>
```

Ein Pin per `-e` gilt nur für diesen Lauf. Für einen dauerhaften Pin die
Variable unter `[vps:vars]` in `inventory.ini` eintragen und wieder entfernen,
sobald der Fix released ist (`grep image_tag ../osa-access/sites/*/inventory.ini`
zeigt aktive Pins). Das nächtliche Auto-Update überschreibt keinen Pin. Ein Pin
macht Datenbankmigrationen nicht rückgängig, die ein neuerer Release schon
angewendet hat.

### 5.4 Prüfung nach einem Deploy

Container gesund (`podman ps`), keine Fehler in den Logs der Pods und vier
Klicks im Browser: anmelden, eine Besetzung speichern, eine Aufführung
anlegen, eine Nachricht senden.

## 6. Lokale Entwicklung

Die lokale Entwicklung wird pro Entwicklerin bzw. Entwickler von Hand
aufgesetzt, außerhalb von Ansible: Es gibt keine Development-Stage. Das
Verzeichnis `dev/` enthält kopierbare Beispiele. Eine beliebige Domain
verwenden und für `<your-dev-domain>` einsetzen; `<your-shorturl-domain>` ist
ein zweiter, unabhängiger Platzhalter.

| `dev/`-Pfad | Kopieren nach | Zweck |
|---|---|---|
| `quadlets/backend/osa-backend-dev.build.example` | `~/.config/containers/systemd/osa/osa-backend/osa-backend-dev.build` | baut das Target `dev` des Backend-`Dockerfile` |
| `quadlets/backend/osa-backend.container.example` | gleicher Ordner | Backend mit eingebundenem Repository, `uvicorn --reload` |
| `quadlets/backend/osa-backend-pg.container.example` | gleicher Ordner | eigenes PostgreSQL |
| `postgres-init/10-app-role.sh` | wird an Ort und Stelle eingebunden | legt die Rolle `osa_app` bei leerem Datenverzeichnis an |
| `quadlets/backend/osa-backend-valkey.container.example` | gleicher Ordner | Valkey für den Worker |
| `quadlets/backend/osa-backend-worker.container.example` | gleicher Ordner | arq-Worker mit `--watch` |
| `quadlets/backend/osa-backend.pod.example` | gleicher Ordner | Pod-Ports und -Einstellungen |
| `quadlets/frontend/osa-frontend.container.example` | `…/osa/osa-frontend/osa-frontend.container` | `npm run dev` mit eingebundenem Repository |
| `env/*.env.example` | `~/.env/<name>.env` | Umgebung von Backend, PostgreSQL, Valkey und Frontend |
| `Caddyfile.dev.example` | an die eigene Caddyfile anhängen | die zwei unten beschriebenen Vhosts |

Die Pfade gehen davon aus, dass `osa-backend` und `osa-frontend` unter einem
gemeinsamen Elternverzeichnis nebeneinander liegen
(`%h/projects/osa-fastapi-vue/…`); sonst `Volume=` und `SetWorkingDirectory=`
anpassen. Kopieren, dann:

```bash
install -d -m 700 ~/.env
mkdir -p ~/.config/containers/systemd/osa/osa-backend ~/.config/containers/systemd/osa/osa-frontend \
         ~/data/osa-backend/postgres ~/data/osa-backend/valkey
for f in dev/quadlets/backend/*.example; do cp "$f" ~/.config/containers/systemd/osa/osa-backend/"$(basename "${f%.example}")"; done
for f in dev/quadlets/frontend/*.example; do cp "$f" ~/.config/containers/systemd/osa/osa-frontend/"$(basename "${f%.example}")"; done
for f in dev/env/*.example; do cp "$f" ~/.env/"$(basename "${f%.example}")"; done
chmod 600 ~/.env/*.env
```

Podman legt fehlende Bind-Mount-Verzeichnisse nicht an, daher das `mkdir`. Die
Platzhalter der Env-Dateien ausfüllen (`SECRET_KEY`, `POSTGRES_PASSWORD`,
`OSA_APP_PASSWORD`, dasselbe `VALKEY_PASSWORD` in zwei Dateien) und starten:

```bash
systemctl --user daemon-reload
systemctl --user start osa-backend-pg.service osa-backend-valkey.service \
    osa-backend-worker.service osa-backend.service osa-frontend.service
podman exec osa-backend alembic upgrade head       # das Dev-Image migriert nicht von selbst
podman exec -it osa-backend python scripts/restore_db.py   # echte Daten; braucht das Read-only-Koofr-Login
podman exec osa-frontend npm install                       # einmalig
```

Es gibt kein Seed-Skript für Benutzer; das Einspielen des neuesten
Production-Backups ist der Weg zu einem funktionierenden Login. Der nächtliche
Downsync überschreibt die Dev-Datenbank zudem mit dem Schema von Production,
also nach 04:00 erneut `alembic upgrade head` ausführen, wenn ein Branch neuere
Migrationen hat. Die pytest-Datenbank ist eine eigene Datenbank: `podman exec
osa-backend-pg psql -U osa -d osa -c "CREATE DATABASE osa_test OWNER
osa_app;"`.

**Caddy-Routing.** Dev übernimmt den Pfad-Split von Production ohne Dozzle:
`/api` und `/api/*` gehen an `127.0.0.1:21000` (Backend), alles andere an
`127.0.0.1:21001` (Vite). Die zwei Vhosts aus `Caddyfile.dev.example` an die
Caddy des Rechners anhängen und die Platzhalter ersetzen.
`<your-allowed-cidrs>` nennt die Netze, die die Dev-Stage erreichen dürfen;
alle anderen bekommen `403`. Nicht offen lassen: Die Datenbank ist ein
Production-Abzug und Vite liefert Quelldateien aus. Der Kurz-URL-Vhost ist
optional und darf öffentlich bleiben (er leitet nur weiter).

**Hinweise zur Dev-Umgebung.** Die Datenbank-URLs nutzen Port `5432` (der
gemeinsame Netzwerk-Namespace des Pods); der Host-Port `5433` ist eine
auskommentierte Option für manuelles `psql`. `VITE_API_BASE_URL` ist eine
vollständige absolute URL, weil nur die eigene Caddy den `/api`-Split macht.
`VITE_DEV_ALLOWED_HOSTS` nennt die Hostnamen, die Vite außer localhost
akzeptiert (kommagetrennt); ohne die öffentliche Dev-Domain antwortet Vite
mit `403`. SMTP ist optional; ungesetzt schlägt der Mailversand still fehl. Das
Dev-PostgreSQL hat die zwei Rollen aus 7.1; eine bestehende Dev-Datenbank wird
einmalig mit `config/postgres/split-app-role.sql` auf `osa_app` umgestellt:

```bash
OSA_APP_PASSWORD='<passwort>' podman exec -i -e OSA_APP_PASSWORD osa-backend-pg \
    sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -v ON_ERROR_STOP=1' \
    < config/postgres/split-app-role.sql
```

Danach Backend und Worker neu starten.

## 7. Anhänge

### 7.1 Secrets

Alle folgenden Dateien sind mit `ansible-vault` verschlüsselt und liegen in
`osa-access/sites/<stage>/secrets/`; die passenden `*.example`-Dateien in
`site-template/secrets/` sind die Vorlagen. `.env.j2`-Dateien enthalten auch
Jinja2-Ausdrücke (`{{ backend_domain }}` usw., aus `inventory.ini` befüllt);
Vault-Verschlüsselung und Templating vertragen sich, weil das Template-Modul
zuerst entschlüsselt. Später bearbeiten mit
`./osa vault <stage> edit secrets/<datei>`.

| Datei | Inhalt |
|---|---|
| `caddy.env` | `LOGGING_USER`, `LOGGING_PASSWORD_HASH` (bcrypt) des Log-Viewers |
| `osa-backend.env.j2` | Umgebung von Web und Worker: Datenbank-URL, Valkey-Passwort, `SECRET_KEY`, Mail, Zeitplan |
| `osa-backend-worker.env` | nur der Worker: Koofr-Zugangsdaten und Backup-Ziel |
| `osa-backend-pg.env` | Bootstrap-Rolle von PostgreSQL |
| `osa-backend-valkey.env` | `VALKEY_PASSWORD` |
| `osa-frontend.env.j2` | Laufzeitkonfiguration des Frontends |

Für jede Stage ein frisches Passwort erzeugen und nie eines einer anderen
Stage wiederverwenden: `python3 -c "import secrets;
print(secrets.token_urlsafe(32))"`. Das Valkey-Passwort muss in
`osa-backend.env.j2` und `osa-backend-valkey.env` identisch sein. Der
Caddy-Hash wird interaktiv erzeugt, damit das Passwort nie in der
Shell-History landet: `podman run -it --rm docker.io/library/caddy:2.11 caddy
hash-password --algorithm bcrypt --bcrypt-cost 14`. Der Log-Viewer hält den
Podman-API-Socket, sein Login ist also so sensibel wie die Secrets der Stage.
Die optionale Inventar-Variable `logging_allowed_cidrs=["203.0.113.0/24"]`
beantwortet Clients außerhalb der Liste mit `403`, bevor ein Zugangsdatum
geprüft wird.

**Datenbankrollen.** `POSTGRES_USER` (`osa`) ist die Bootstrap-Rolle des
Clusters: immer Superuser, nur für Administration. Backend und Worker
verbinden sich als `osa_app`, eine Rolle ohne Superuser-Rechte, der die
Anwendungsdatenbank gehört. Niemand legt sie von Hand an: Jedes `./osa deploy`
wendet `config/postgres/split-app-role.sql` im Datenbank-Container an, nachdem
der Stack läuft und vor dem Pod-Neustart, setzt das Passwort aus
`DATABASE_URL` und überträgt den Besitz aller Objekte im Schema `public`.
`DATABASE_URL` muss `osa_app` nennen, mit einem Passwort aus mindestens 16
Zeichen von `A-Za-z0-9._~-` (es wird unmaskiert in der URL verwendet),
verschieden von `POSTGRES_PASSWORD`; alles andere stoppt das Deploy, auch unter
`--check`. Umstellen einer Datenbank, die mit der Bootstrap-Rolle als
Besitzerin angelegt wurde: (1) Backup ziehen und sicherstellen, dass keine
Sitzung der Bootstrap-Rolle auf der Anwendungsdatenbank offen ist, (2)
sicherstellen, dass das laufende Image mit `--no-owner --no-acl`
wiederherstellt, (3) `./osa vault <stage> edit secrets/osa-backend.env.j2` und
`DATABASE_URL` setzen, (4) `./osa check`, dann `./osa deploy`, (5) mit
`select usename, count(*) from pg_stat_activity where datname =
current_database() and pid <> pg_backend_pid() group by 1` prüfen, dass nur
`osa_app` verbunden ist. Der Weg zurück von Hand ist die Zeile `DATABASE_URL`
in `~/.env/osa-backend.env` auf dem Host plus ein Pod-Neustart.

**Koofr-Accounts.** `osa-backend-worker.env` hält die WebDAV-Zugangsdaten.
Nur der Worker-Container bekommt sie; der Web-Container nie. Production nutzt
den Account, dem der Backup-Ordner gehört (er schreibt die nächtlichen Dumps
und räumt alte weg). `test` und `qa` nutzen einen zweiten Account, der den
Ordner nur **lesen** kann, weil sie nur das neueste Production-Backup
herunterladen. Den Production-Account nie in die Datei einer anderen Stage
legen. Den Read-only-Account einrichten: einen zweiten Koofr-Account anlegen,
den Backup-Ordner read-only mit ihm teilen, im Account prüfen, dass Auflisten
und Herunterladen funktionieren, Hochladen und Löschen abgelehnt werden, und
unter welchem WebDAV-Pfad der Ordner erscheint (dieser Pfad ist
`KOOFR_BACKUP_PATH` der Stage). Ein Passwort rotiert man durch Bearbeiten
dieser Datei und anschließendes `./osa deploy`.

**Mail.** `SMTP_*` bleibt auf Nicht-Production-Stages auskommentiert (kein
Mailversand außerhalb von Production). `SMTP_PORT` ist als Zahl typisiert, ein
leerer Wert lässt den Start scheitern; nur ein fehlender Schlüssel gilt als
nicht gesetzt.

### 7.2 Fallstricke

- Eine `.env`-Datei mit wörtlichen Anführungszeichen um einen Wert behält
  sie; der Prozess sieht dann die Anführungszeichen.
- `systemctl --user restart` auf eine einzelne Container-Unit stoppt ihren
  Pod und startet ihn nicht neu; die Pod-Unit neu starten (3.2).
- Ein frisches Verzeichnis gehört der UID des aufrufenden Benutzers, das
  PostgreSQL-Image läuft aber als UID 999 in einem eigenen Namespace;
  `deploy.yml` korrigiert den Besitz per `podman unshare` nur, wenn er
  abweicht, damit ein Deploy nie eine laufende Datenbank durchläuft.
- Nicht verwaltete Dateien in `~/.env` stoppen ein Deploy und werden zur
  Prüfung aufgelistet: Verwaiste Secret-Dateien würden veraltete Zugangsdaten
  am Leben halten.
- Der geänderte Hash einer vault-verschlüsselten Umgebungsdatei startet den
  zugehörigen Pod einmal neu, auch wenn nur ein Kommentar geändert wurde.
- Port 2020 auf dem Host nutzt die Health-Prüfung von Caddy; die Admin-API von
  Caddy ist aus.

### 7.3 Zugang und Übergabe

Alles, was ein Nachfolger braucht, steht im [HANDOVER.de.md](HANDOVER.de.md).
Das Vault-Passwort steht in keinem Repository; zum Ändern `./osa vault <stage>
rekey` für jede Stage ausführen und `credentials.md` aus `osa-access` von Hand
mit `ansible-vault rekey` neu verschlüsseln.
