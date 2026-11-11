# Übergabe

[English](HANDOVER.md) · [README](README.de.md)

Eine Nachfolgerin bzw. ein Nachfolger erhält genau drei Dinge.

1. **Die Repositories** der GitHub-Organisation
   <https://github.com/Kirchenmusik-St-Augustin>:
   - `osa-deploy` (öffentlich): dieses Repository, das Betriebswerkzeug
   - `osa-backend` (öffentlich) und `osa-frontend` (öffentlich): die Anwendung
   - `osa-access` (privat): Inventar, vault-verschlüsselte Secrets,
     Operator-Key und die Zugangsnotiz `credentials.md`
2. **Zugang zum privaten Repository**: ein GitHub-Konto, das Mitglied der
   Organisation mit der Rolle Owner ist, samt zweitem Faktor und
   Wiederherstellungscodes, und das Rollen-Postfach, das dessen
   Passwort-Rücksetzungen empfängt.
3. **Das Vault-Passwort**, getrennt übergeben. Es steht nie in einem
   Repository und nie in diesem Dokument.

Hosting, DNS, Mail, Koofr, Google und jedes weitere Login stehen in
`credentials.md` von `osa-access`, vault-verschlüsselt. Was diese Notiz öffnet
(Punkte 2 und 3), steht nicht in ihr. Den GitHub-Zugang und das Vault-Passwort an
verschiedenen Orten verwahren. Mehr muss nicht weitergegeben werden.

## Erste Schritte

Auf dem Arbeitsrechner braucht man `git`, `ssh`, `ansible` (vollständiges Paket) und
Python 3.12 oder neuer. Beide Repositories nebeneinander klonen:

```bash
git clone https://github.com/Kirchenmusik-St-Augustin/osa-deploy.git
git clone https://github.com/Kirchenmusik-St-Augustin/osa-access.git
cd osa-deploy
./osa vault production view ../../credentials.md   # Zugangsnotiz lesen
./osa ssh production                               # Shell auf dem Live-Server
```

Beide Befehle fragen nach dem Vault-Passwort. Weiterlesen: [README](README.de.md),
Kapitel 3 wenn etwas ausfällt, Kapitel 5 für die täglichen Befehle.
