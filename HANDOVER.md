# Hand-over

[Deutsch](HANDOVER.de.md) · [README](README.md)

A successor receives exactly three things.

1. **The repositories** of the GitHub organisation
   <https://github.com/Kirchenmusik-St-Augustin>:
   - `osa-deploy` (public): this repository, the operations tooling
   - `osa-backend` (public) and `osa-frontend` (public): the application
   - `osa-access` (private): inventory, vault-encrypted secrets, operator key
     and the access note `credentials.md`
2. **Access to the private repository**: a GitHub account that is a member of
   the organisation with the owner role, with its second factor and recovery
   codes, and the role mailbox that receives its password resets.
3. **The vault password**, handed over separately. It is never stored in a
   repository and never written into this document.

Hosting, DNS, mail, Koofr, Google and every other login are written down in
`credentials.md` of `osa-access`, vault-encrypted. What opens that note (items
2 and 3) is not written in it. Keep the GitHub access and the vault password in
different places. Nothing else has to be passed on.

## First steps

On the workstation you need `git`, `ssh`, `ansible` (full package) and Python
3.12 or newer. Clone both repositories side by side:

```bash
git clone https://github.com/Kirchenmusik-St-Augustin/osa-deploy.git
git clone https://github.com/Kirchenmusik-St-Augustin/osa-access.git
cd osa-deploy
./osa vault production view ../../credentials.md   # read the access note
./osa ssh production                               # shell on the live server
```

Both commands ask for the vault password. Further reading:
[README](README.md), chapter 3 when something is down, chapter 5 for the daily
commands.
