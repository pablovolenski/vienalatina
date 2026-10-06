# Server setup — step by step (Hetzner CX22, Ubuntu 24.04)

Every command you need, in order. Commands prefixed `local$` run on your own
computer; everything else runs on the server over SSH. Budget ~2–3 hours.

Where you see `pablo`, `<SERVER-IP>`, or a password placeholder, substitute
your own values.

---

## 0. Order the server

At [console.hetzner.com](https://console.hetzner.com) → Create Server:

- **Location:** Nuremberg
- **Image:** Ubuntu 24.04
- **Type:** Shared vCPU → **CX22** (2 vCPU, 4 GB RAM, €4.51/mo)
- **SSH key:** add your public key (`local$ cat ~/.ssh/id_ed25519.pub` — if you
  don't have one: `local$ ssh-keygen -t ed25519`). Adding it here means root
  login works by key from the start, no password emails.

Note the server's IP address — that's `<SERVER-IP>` everywhere below.

## 1. DNS (do this first — it needs time to propagate)

At your domain registrar, add two **A records**:

| Name | Type | Value | TTL |
|---|---|---|---|
| `git` | A | `<SERVER-IP>` | 300 |
| `ci` | A | `<SERVER-IP>` | 300 |

**Do NOT touch the record for `vienalatina.com` itself** — the live WordPress
site keeps running until cutover day.

Check propagation (repeat until it prints the server IP):

```sh
local$ dig +short git.vienalatina.com
```

## 2. First login + basic hardening

```sh
local$ ssh root@<SERVER-IP>
```

Update and create your user:

```sh
apt update && apt -y upgrade

adduser pablo                 # pick a strong password, skip the questions
usermod -aG sudo pablo

# give your user the same SSH key root has
rsync --archive --chown=pablo:pablo ~/.ssh /home/pablo
```

Lock SSH down to keys only:

```sh
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh
```

**Before closing this terminal**, open a second one and confirm you can get in:

```sh
local$ ssh pablo@<SERVER-IP>
```

From here on, work as `pablo` and prefix privileged commands with `sudo`
(or run `sudo -i` once).

Firewall + brute-force protection:

```sh
sudo apt install -y ufw fail2ban
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo systemctl enable --now fail2ban
```

## 3. Install Caddy

```sh
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## 4. Install Docker

```sh
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker pablo
```

Log out and back in (`exit`, then `ssh pablo@<SERVER-IP>`) so the `docker`
group takes effect. Verify: `docker ps` should print an empty table, not a
permission error.

## 5. Get this repo's infra files onto the server

```sh
sudo mkdir -p /srv /var/www/vienalatina.com
cd ~
git clone https://github.com/pablovolenski/vienalatina.git
sudo cp -r vienalatina/infra/gitea /srv/gitea
sudo cp -r vienalatina/infra/woodpecker /srv/woodpecker
sudo cp vienalatina/infra/caddy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 6. Bring up Gitea

```sh
cd /srv/gitea
sudo docker compose up -d
```

Wait ~30 s, then open **https://git.vienalatina.com** in your browser (the
certificate is fetched automatically; if you get an error, wait a minute for
DNS/certificate and reload). You'll see Gitea's install page:

- Database: **SQLite3** (fine at this scale)
- Server domain / base URL: leave as pre-filled (`git.vienalatina.com`)
- **Administrator account** (bottom of the page — expand it): username
  `pablo`, your email, a strong password. Create it now; the first account
  is the admin.
- Click **Install Gitea**.

Then create the two OAuth apps and the bot user, all in the Gitea web UI:

1. **Woodpecker OAuth app:** profile icon → Site Administration →
   Integrations → Applications → *Create new OAuth2 application*
   - Name: `woodpecker`
   - Redirect URI: `https://ci.vienalatina.com/authorize`
   - Save the **Client ID** and **Client Secret** — needed in step 7.
2. **Decap OAuth app:** same screen, second application
   - Name: `decap-cms`
   - Redirect URI: `https://vienalatina.com/admin/` (exactly, trailing slash
     included — Gitea matches it literally)
   - **Untick "Confidential Client".** Decap runs in the browser and
     authenticates with PKCE; a confidential app makes Gitea demand a client
     secret that a browser cannot keep, and the login fails *after* you
     authorize, which makes it look like a Decap bug.
   - Save the **Client ID** — it goes into `static/admin/config.yml` (step 9).
     There is no secret to save, and the Client ID is not one either: it is
     published in the site's JavaScript by design.
3. **Translations bot:** Site Administration → Identity & Access →
   User Accounts → *Create User Account*
   - Username: `translations`, email: `translations@vienalatina.com`,
     any strong password.
   - Log in **as the bot user** once (private window), go to Settings →
     Applications → *Generate New Token*, scopes: **repository (write)**.
     Save the token — it becomes the `gitea_push_token` secret in step 8.

## 7. Bring up Woodpecker

```sh
cd /srv/woodpecker
sudo cp .env.example .env
openssl rand -hex 32        # copy the output
sudo nano .env              # paste Woodpecker OAuth client ID + secret + the random hex
sudo mkdir -p data && sudo chown -R 1000:1000 data   # v3 images run as uid 1000
sudo docker compose up -d
```

Open **https://ci.vienalatina.com** → *Login* → it bounces you to Gitea →
*Authorize*. You're in, as admin (the `WOODPECKER_ADMIN=pablo` line in the
compose file — edit it if your Gitea username differs).

## 8. Create the site repo and wire the pipeline

Create the repo in Gitea: **+** (top right) → New Repository → name
`vienalatina`, owner `pablo`, **not** initialized with anything → Create.

Add the `translations` bot as collaborator: repo → Settings →
Collaborators → add `translations` with **Write** access.

Push the code into Gitea (from the server clone you made in step 5, or from
your laptop):

```sh
cd ~/vienalatina
git remote add gitea https://git.vienalatina.com/pablo/vienalatina.git
git push gitea main

# Name the GitHub remote too, while you are here. The clone in step 5 called it
# `origin`, but `main` came to track `gitea/main` — so a bare `git pull` asks
# Gitea, and new work arrives on GitHub. Having both named saves you from typing
# the URL every time you deploy.
git remote add github https://github.com/pablovolenski/vienalatina.git
git remote -v
```

(Gitea will ask for your Gitea username/password.)

In Woodpecker (**https://ci.vienalatina.com**):

1. *Repositories* → *Add repository* → enable `pablo/vienalatina`
   (this auto-creates the push webhook in Gitea).
2. Repo → Settings → *Project settings* → check **Trusted** (needed so the
   deploy step may mount `/var/www/vienalatina.com`).
3. Repo → Settings → *Secrets* → add `gitea_push_token`, the bot token from
   step 6.3. That is the only secret — translation runs locally and needs no key.

Build the translation image before the first run (~5 minutes; it downloads
about 1GB of model):

```sh
cd ~/vienalatina
docker build -t vienalatina/translate:1 docker/translate
```

The push in the step above has already triggered a first pipeline — it ran
before the image and secret existed, so **push a new commit rather than using
Restart**. Restart replays the old commit, and a restart's empty diff range
makes the translate step find nothing to do. All three steps (translate →
build → deploy) should go green, and `/var/www/vienalatina.com/` on the server
now contains the built site:

```sh
ls /var/www/vienalatina.com     # index.html, de/, pt-br/, robots.txt, llms.txt …
```

## 9. Point Decap at Gitea

On your working copy: edit `static/admin/config.yml`, replace
`REPLACE_WITH_GITEA_OAUTH_CLIENT_ID` with the Decap OAuth Client ID from
step 6.2, commit, push to Gitea. (You can't log into `/admin` until the main
domain is live — that's expected.)

```sh
cd ~/vienalatina
sed -i 's/REPLACE_WITH_GITEA_OAUTH_CLIENT_ID/<client id>/' static/admin/config.yml
grep app_id static/admin/config.yml
git commit -am "Wire Decap to the Gitea OAuth app" && git push gitea main
```

This value is per-deployment: the placeholder is what belongs in the repo, so
leave it in place in any copy of this platform that is not this server.

If `/admin/` still shows *Client ID not registered* afterwards, the page is
serving a cached `config.yml` — hard-reload it. If it fails *after* the Gitea
authorize screen instead, the app was created as a confidential client; delete
it and recreate it with that box unticked.

## 10. Test the translation loop end-to-end

```sh
cd ~/vienalatina
cat > content/post/mi-test.es.md <<'EOF'
---
title: "Artículo de prueba"
date: 2026-08-01
lang: es
manual_translation: false
---

Esto es una prueba del flujo de traducción automática en el Grätzl.
EOF
git add . && git commit -m "test: pipeline round-trip" && git push gitea main
```

Within ~60 s the Woodpecker pipeline should finish and
`content/post/mi-test.de.md` + `content/post/mi-test.pt-br.md` appear in the
Gitea repo as commits by `translations`. Note "Grätzl" survives untranslated
(protected term). Delete all three test files with another commit when done.

Saturday complete. 🎉

---

## Cutover day (Sunday evening)

1. Run the content migration and push (see README, "One-shot content
   migration"). Migrated Polylang siblings arrive frozen
   (`manual_translation: true`) because they are *human* translations — the
   machine engine must never overwrite them. Then
   `python scripts/translate.py --backfill` fills in any set that WordPress
   had no translation for. Spot-check the built site with
   `grep` on `/var/www/vienalatina.com/index.html`; the
   `curl -H "Host: vienalatina.com" http://127.0.0.1/` trick only works after
   step 3, since Caddy has no matching site block until then.
2. At the registrar: lower the `vienalatina.com` A record TTL to 300, wait
   for the old TTL to expire, then change the A record to `<SERVER-IP>`
   (and `www` too, as CNAME to `vienalatina.com` or A to the same IP).
3. On the server: uncomment the `vienalatina.com` blocks in
   `/etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`. Caddy fetches
   the certificate as soon as DNS resolves to this server.
4. Verify: the checklist in the migration plan (hreflang tags, robots.txt,
   llms.txt, Lighthouse, `manual_translation: true` freeze test, and the
   loop-prevention test — after the bot pushes siblings, the pipeline it
   triggers must report "nothing to translate" rather than translating the
   siblings back).
5. Keep the WP host untouched for 30 days as fallback; watch Google Search
   Console and add Caddy 301s for any 404s it reports.

## Optional: nightly backups (restic → Hetzner Storage Box)

```sh
sudo apt install -y restic
sudo restic -r sftp:uXXXXXX@uXXXXXX.your-storagebox.de:backups init
# then a root cron entry, e.g.:
# 0 3 * * * restic -r sftp:... backup /srv /var/www --password-file /root/.restic-pw
```

## 11. Members area (`/comunidad/`)

The private area: roles and an internal board. It is the only part of the site
that runs code to answer a request, and the only data on the server that is not
already in git.

### 11.1 No OAuth application, and no accounts for members

**Members sign in on vienalatina.com, against a password stored here.** They
have no account on the git server at all. If you are reading an older copy of
this file: it described registering an OAuth application and handing members to
git.vienalatina.com to type their password. That is gone, along with every
problem it caused — the hand-off to a differently-designed domain, a *Forgot
password?* that could never work, and a *Salir* that could not finish because
the session belonged to a server we could not reach.

What is left of the git server, as far as members are concerned, is nothing.
It stores the site's content. One token lets the editor commit there
(`CONTENT_TOKEN`, §11.2), and only pablo and the pipeline bots have logins.

Passwords are scrypt hashes via `werkzeug.security`, which arrives with Flask.
Nobody — not an admin, not the server — ever sees a member's password: a new
member's `password_hash` is NULL until they choose one through the invitation
link, and a NULL hash cannot be signed in with.

**If a member already had a git-server account** from the old flow, it is now
an orphan. Delete those at **git.vienalatina.com/-/admin/users**, keeping only
`pablo` and the bots. Nothing here reads them any more.

### 11.2 The token the editor commits with

The members area needs one credential on the git server: something that can
write to the site repository when somebody publishes a post.

Gitea → as **pablo** → Settings → Applications → *Generate New Token*, scope
**repository: Read and Write**. Put it in `/srv/board/.env` as `CONTENT_TOKEN`.

```
CONTENT_TOKEN=
```

**The scope matters more than this page used to admit.** Gitea's scopes are per
category, and `admin` does not contain `repository` — a site-admin token gets a
403 from the contents API, no matter how powerful it is elsewhere. So the
fallback to `GITEA_ADMIN_TOKEN` when `CONTENT_TOKEN` is empty keeps the app
*running*, but it does not keep publishing working: with an admin-only token the
editor cannot commit at all. That is exactly what happened here, and it went
undiagnosed for three phases because the refusal escaped as a 500 on
*Contenido* and, when approving a submission, as a message blaming a missing
photograph. Both now say what is wrong, but the fix is this token.

Once `CONTENT_TOKEN` is in place, **revoke `GITEA_ADMIN_TOKEN`**: it can create
and modify every account on the instance, members no longer have accounts to
create, and it has no remaining job.

If *Contenido* says *"El servidor de git rechazó el token del editor"*, this
section is the answer: the token is missing, revoked, or scoped to something
other than the repository.

**Commits still say who wrote them.** One token does the committing, and each
commit names its author, so `git log` shows the member and there is somebody to
ask about a page a year from now.

### 11.3 Build and run

```sh
cd ~/vienalatina
docker build -t vienalatina/board:1 -f docker/board/Dockerfile .

sudo mkdir -p /srv/board/data
sudo cp -r infra/board/. /srv/board/
cd /srv/board
sudo cp .env.example .env
openssl rand -hex 32          # paste as BOARD_SECRET_KEY
sudo nano .env                # client id, secret, BOARD_OWNER, optional admin token
sudo chown -R 1000:1000 /srv/board/data
sudo docker compose up -d
```

`BOARD_OWNER` is applied once, to an empty database, and ignored from then on.
It cannot be used to take ownership later: that is deliberate, because otherwise
editing a file on disk would be a quieter route to the top than asking for it.
Ownership moves only through *Transferir titularidad* inside the app.

#### Updating it later — a pull is not a deploy

The app's code is **inside the image**: `docker/board/Dockerfile` ends with
`COPY apps /srv/apps`. So pulling new commits into `~/vienalatina` changes
nothing that is running, and neither does `docker compose up -d
--force-recreate` — same image tag, same layers, same old code. Everything
reports success and the server behaves exactly as it did before, which is the
most expensive kind of nothing.

**And `git pull` on its own will not fetch it.** This is worth knowing before
it costs you an afternoon: `main` on the server tracks `gitea/main`, while new
work is pushed to a branch on **GitHub**. So a bare `git pull` asks Gitea,
finds Gitea level with your local `main`, and answers *"Already up to date."* —
which is true about the wrong remote, and reads exactly like there is nothing
to do.

**Paste one line at a time.** Every deploy that has gone wrong here went wrong
in the terminal rather than in the code: three commands pasted on one line
separated by commas, so git looked for a branch whose name ended in a comma; and
a block pasted into a session that was still answering `ssh`'s host-key
question, which swallowed the rest as answers. One line, one Enter, read what it
says.

An update is **two** named pulls, a push to Gitea so the build pipeline sees it,
and a rebuild:

```sh
cd ~/vienalatina
git pull --no-rebase --no-edit github <the-branch-name>
git pull --no-rebase --no-edit gitea main      # see below — Gitea moves on its own
sudo bash scripts/deploy-board.sh
git push gitea main
```

**The second pull is the one this page used to be missing**, and the push fails
without it with a non-fast-forward rejection. The translate step of the pipeline
*commits* the generated German and Portuguese files back to Gitea
(`translate: update N generated siblings [skip-translate]`), so every time
anything is published, `gitea/main` gains a commit this checkout does not have.
A push then has nothing to fast-forward from. Pulling it first merges the
pipeline's own work into yours, which is all that is needed — there is never a
conflict, because nothing but the pipeline writes those files.

**The order of the last two lines matters when a release changes both halves.**
`deploy-board.sh` puts the members area up; the push is what rebuilds the public
site. Anything on the static side that calls the app — the members' bar asking
`/comunidad/sesion.json` — then finds it already there.

Both flags earn their place. `main` and the branch have genuinely diverged —
main carries the previous merge, the branch carries the new work — and a git
with no `pull.rebase` set refuses to guess, with *"fatal: Need to specify how to
reconcile divergent branches."* `--no-rebase` says merge, which is what every
deploy here has done. `--no-edit` then accepts the default merge message instead
of opening an editor, which is a strange place to find yourself mid-deploy.

If `github` is not a remote yet, add it once — see the end of step 8:

```sh
git remote add github https://github.com/pablovolenski/vienalatina.git
```

That takes a backup, rebuilds the image, copies the compose file across,
restarts, and prints the log. It never touches `/srv/board/.env` — that file
holds the secrets and lives only on the server — but it does compare it against
`.env.example` and name any setting that has appeared in the repository and is
missing from yours. New settings are always added by hand.

**The backup is part of the deploy now, not a thing to remember.** The app
applies numbered migrations at start-up and two of them rebuild a table in
place — `attachments`, and `members`, which holds your own account. Neither can
be undone afterwards, and "back up first" works reliably until the one deploy
where it mattered. `BOARD_SKIP_BACKUP=1` skips it if you have just taken one.

The database schema is applied at start-up with `CREATE TABLE IF NOT EXISTS`,
so a release that adds a table needs no migration step: the table appears when
the new code does. Anything that *changes* an existing table is a numbered
migration instead — §11.11 — and the deploy's log tail names each one it ran.

### 11.4 Route it through Caddy

Add to the `vienalatina.com` block in `/etc/caddy/Caddyfile` (already present in
`infra/caddy/Caddyfile`):

```
@board path /comunidad /comunidad/*
reverse_proxy @board 127.0.0.1:8080
```

Then `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.

Both paths are matched on purpose: Flask redirects `/comunidad` to
`/comunidad/`, and matching only the trailing-slash form lets the bare path fall
through to the static site and 404.

### 11.5 Back it up — this part is not optional

Everything else on this server is reproducible from the repository. The board's
threads, comments and membership exist in exactly one place.

```sh
sudo apt install -y sqlite3
crontab -e
# 15 4 * * *  /home/pablo/vienalatina/scripts/backup-board.sh >> /home/pablo/board-backup.log 2>&1
```

The script uses SQLite's `.backup` rather than copying the file, because the
database is live and in WAL mode — a plain `cp` can capture it missing its most
recent commits. Test a restore before you rely on it: stop the container, gunzip
a backup over `/srv/board/data/board.db`, start it again.

**It also archives `/srv/board/data/uploads`**, the pictures members attach to
threads, as a second file `uploads-<stamp>.tar.gz`. This is not an extra: a
database backup that completes looks exactly like a backup that worked, so
before uploads were covered the nightly job would have gone on reporting
success while silently leaving every photograph out. Restoring them is a plain
`tar -xzf`, into `/srv/board/data/`.

If there are no uploads yet the log says so by name, rather than saying
nothing — "nobody has posted a photo" and "the path moved a month ago and this
has been archiving air" are otherwise the same empty line.

### 11.6 Who can do what

Five roles, and **two of them are single chairs held by the database** rather
than by the application: a partial unique index each, so a bug in a handler
cannot produce a second of either.

| | Superadministrador | Responsable | Administrador | Moderador | Miembro |
|---|---|---|---|---|---|
| Wall: post, comment, edit own | ✓ | ✓ | ✓ | ✓ | ✓ |
| Private messages, own profile | ✓ | ✓ | ✓ | ✓ | ✓ |
| Propose a public post | ✓ | ✓ | ✓ | ✓ | ✓ |
| Publish a public post directly | ✓ | ✓ | ✓ | ✓ | — |
| Approve or return a proposal | ✓ | ✓ | ✓ | ✓ | — |
| Edit and delete published posts | ✓ | ✓ | ✓ | ✓ | — |
| Static pages of the site | ✓ | ✓ | ✓ | — | — |
| Delete any wall post, pin and close | ✓ | ✓ | ✓ | — | — |
| Edit someone else's post | — | — | — | — | — |
| Create members and moderators | ✓ | ✓ | ✓ | — | — |
| Change a role below admin | ✓ | ✓ | ✓ | — | — |
| Suspend a member or moderator | ✓ | ✓ | ✓ | — | — |
| Erase a member | ✓ | ✓ | — | — | — |
| Name the responsable | ✓ | ✓ | — | — | — |
| **Create, promote, suspend an admin** | ✓ | — | — | — | — |
| **Marca: the look and the identity** | ✓ | — | — | — | — |
| **Estado del sistema, Registro** | ✓ | — | — | — | — |
| **Hand the platform on** | ✓ | — | — | — | — |

**The two chairs do different jobs, and that is the point.** The
*superadministrador* runs the **platform**: admin accounts, the look, the
identity, the system tools. The *responsable* runs the **association**: members,
moderators, invitations, erasure. Installed for a second organisation, the first
is whoever maintains the box and the second is their president — which is what
makes the responsable chair safe to hand to a customer, and why an admin account
is not theirs to grant.

**Neither chair can be touched by anybody, including themselves.** The
superadministrator because there is nothing above them; the responsable because
the chair belongs to the association, and a platform administrator quietly
removing the president of the organisation they host is exactly the move this
separation exists to prevent. Each steps down by handing their own chair on, and
whoever gives one up becomes an administrator — there is no role above the one
they just gave away.

**A moderator has no power over people.** They decide what the public reads and
nothing else: no creating, suspending, promoting or erasing anybody. That line is
the whole point of having the role — a moderator who could also suspend the
author of a post they had just returned would be a second kind of administrator.

Both rules live in one place each — `may_create` and `may_manage` in
`apps/board/members.py` — and the role picker on a member's page is built from
`assignable_roles(actor)`, the same predicate, so it can never offer a role the
handler will refuse. "This role or anything above it" is `is_at_least` in
`apps/board/security.py`, over one ordered `LADDER`: it replaced seven literal
tuples scattered through the app, each of which had silently excluded the
superadministrator the moment a tier appeared above owner.

Nobody edits anyone else's words, administrators included. Taking a post down is
visible to the person who wrote it; rewriting it is not, and an admin who could
do that could leave a sentence attributed to a member who never wrote it.

**On upgrade**, migration 7 promotes the existing owner — Pablo — to
superadministrator and leaves the responsable chair empty. Nothing is erased and
nobody has to type anything: a superadministrator passes every check an owner
passed. The chair is filled from *Miembros* → somebody's page → **Nombrar
responsable** whenever there is somebody to put in it.

`BOARD_OWNER` keeps its name and now seeds a superadministrator. It refuses to
change an existing one, so editing the environment cannot hand anybody the
platform — and the `ON CONFLICT` clause that writes the role is pinned to
`superadmin` for a reason worth knowing: left saying `owner`, it would have
demoted the superadministrator on every container restart.

### 11.7 Personal data

Members' names, emails and writing are personal data under GDPR.

- **Erasure:** the owner's *Eliminar* removes the member row entirely and
  reassigns their threads and comments to a tombstone shown as "Miembro
  eliminado", so conversations other people took part in stay readable. Private
  messages and unapproved proposals are deleted outright, pictures included. An
  already-published post stays on the site: what remains of it is a commit in
  the content repository carrying their name, and rewriting git history from the
  members area is not something this app does.
- **Access:** any member can download everything they have written from
  *Descargar mis datos*.
- **Retention:** soft-deleted posts stay in the database until removed by hand.
  If you want a real retention limit, that is a `DELETE ... WHERE deleted_at <`
  in this same cron slot — and a decision to take deliberately, not by default.

### 11.8 The content editor (`/comunidad/contenido/`)

Admins and the owner can write, edit and delete posts and pages from inside the
members area, instead of Decap at `/admin/`.

**Decap is still there and still works.** Nothing was removed. Use the new
editor for a few real posts first; if something turns out to be missing, switch
tabs. Removing Decap is a separate decision — see below.

Nothing extra to install or configure: it runs in the container already serving
`/comunidad/`, and commits through the Gitea OAuth application registered in
§11.1. Two settings exist if the repository is ever renamed:

```
CONTENT_REPO=pablo/vienalatina      # owner/repo inside Gitea
CONTENT_BRANCH=main
```

**How publishing works.** The editor is a form that commits a file through
Gitea's contents API. Gitea's webhook fires Woodpecker, and translate → build →
deploy runs exactly as it does for a Decap commit — the pipeline cannot tell
which editor wrote the file, which is what makes running both at once safe.

**Commits are made with your own account**, not a bot's, so `git log` shows who
wrote each post and Gitea's permissions apply unchanged. Your access token is
stored in the members-area database (never in a cookie) and refreshed
automatically; Gitea expires them after about an hour, and without refreshing,
saving would start failing mid-afternoon for no visible reason.

**Filenames follow the same rules Decap used**, because `scripts/translate.py`
reads them: `YYYY-MM-DD-slug.es.md` for posts, `slug.es.md` for pages. A file
whose name breaks that contract publishes in Spanish and is never translated,
with nothing reported anywhere — which is why the tests import translate.py and
run its parser over what the editor writes.

**Two people editing one post** is a visible conflict, not a silent overwrite:
the form carries the file's git sha and Gitea rejects a write whose sha has
moved on. You are asked to reopen the post rather than losing the other edit.

**Images** are committed as a second, separate commit before the post itself,
so publishing with a picture produces two pipeline runs. Harmless, and the
alternative — batching both into one commit via the git trees API — is
considerably more code for something nobody sees.

**What it deliberately does not do:** rich-text editing (markdown with a
preview button instead), a media library, drafts, or editing the generated
German and Portuguese files. Those stay the pipeline's, and a hand-written
translation is still frozen with `manual_translation: true`.

#### Worth tightening later

The OAuth application requests no explicit scope, so Gitea grants the default —
full access to the account, which is more than the editor needs. Narrowing it to
`read:user write:repository` is a one-line change in `apps/board/gitea.py`'s
`authorize_url()`, but it invalidates existing authorisations: everyone has to
approve the app again. Worth doing while the member list is short, and worth
testing on a throwaway account first, since a wrong scope string breaks sign-in
for everybody.

#### Removing Decap, once you are confident

Not urgent — leaving it costs a folder and one `wget` in the pipeline:

1. `rm -rf static/admin/`
2. Delete the `wget … decap-cms.js` line from `.woodpecker.yml`
3. Delete the `decap-cms` OAuth application in Gitea

### 11.9 Pictures on the board

Members can attach images when they start a thread or reply. Nothing to
install: the files go to `/data/uploads` inside the container, which is
`/srv/board/data/uploads` on the host — the same volume that already holds
`board.db`, so there is one directory to back up rather than two.

**They are deliberately not in the site repository.** `/comunidad/contenido/`
uploads pictures by committing them, which is right for a post about to be
published. A photo in a private thread is the opposite: committing it would
send it through the build pipeline and out onto vienalatina.com. These are
served by the app, behind the same login as the thread.

Three things the code does that are worth knowing if you ever change it:

- **The type is read from the first bytes, not the filename.** A file called
  `gato.png` containing HTML is refused. Served back as `image/png` from our
  own domain, it would otherwise be a script running on vienalatina.com.
- **The stored name is generated.** The name the browser sent is kept only as
  text to show a person, never as a path.
- **Deleting a post hides its pictures.** Threads and comments are soft-deleted,
  so the serving route checks the parent is still live. Without that, taking a
  post down would leave its photo readable by anyone who noted the URL.

Limits: 4 images per message, and `BOARD_UPLOAD_MAX_BYTES` (8MB by default)
each. Adding a picture to a post *after* publishing it means posting a reply —
editing changes the words, and leaves the pictures alone.

### 11.10 Private messages (on each person's page)

Between two members: conversations, unread badges, photos, blocking.
Deliberately not live chat — that needs a connection held open per signed-in
member, which gunicorn's sync workers cannot do, and it would be the first
thing on this box with a real scaling limit.

**There is no inbox.** You write to somebody by opening their page —
`/comunidad/miembro/<usuario>`, reached from *Miembros* or from their name
anywhere on the wall — and the whole conversation with them is on it, under
their photo and bio, with the write box at the foot. One place per person
rather than a mailbox listing people.

Three consequences worth knowing:

* **The unread badge is on *Miembros***, and repeated on the card of whoever
  wrote. Those cards are sorted so that anybody with something unread is at the
  top of the grid — without that, the person waiting for an answer is wherever
  the alphabet put them.
* **Opening the page marks it read.** There is no second step any more, so
  there is no moment where the messages are on screen and still counted unread.
* **The old addresses redirect.** `/comunidad/privados` goes to the directory
  and `/comunidad/privados/con/<id>` to that person's page; the POST that sends
  a message is still `/comunidad/privados/con/<id>`, which is why the two look
  alike in the logs.

A member's own page carries no conversation and no box: it is there so they can
see what the others see, and their own private messages are each on the page of
the person they had them with.

**Blocking is symmetric.** One block stops messages in both directions, and
either person can only remove their own. A block that silenced just the blocked
person would leave the blocker able to keep writing, which is a megaphone
rather than a safety feature.

**Erasing a member deletes their private messages, both sides.** A thread
outlives its author as *Miembro eliminado*, because other people replied and
the conversation would lose its shape; a two-party exchange has no such
remainder. This does destroy the other person's copy — the uncomfortable half
of the choice, and deliberate.

### 11.10b The members' bar on the public site

A signed-in member sees a slim bar above the public header on vienalatina.com,
with the same sections the members area has. The public pages are files on
disk and get cached, so the bar is drawn in the browser:
`themes/vienalatina/assets/js/memberbar.js` asks `/comunidad/sesion.json` who is
reading, and that endpoint is the only thing that decides anything — a visitor
gets `{"signed_in": false}` and the answer is `Cache-Control: no-store`.

**The hint cookie is why this is not a request per page view.** Signing in also
sets `vl_sesion=1`, readable by scripts, holding nothing but the digit 1; without
it the script makes no request at all, so a stranger reading one article costs
the app nothing. It is never trusted: the session cookie stays `HttpOnly` and
signed, and forging the hint earns a reply saying you are not signed in.

*Comunidad* is in the public menu in all three languages, signed in or not,
because somebody who has an account needs to find the door.

### 11.11 Schema changes: `PRAGMA user_version`

`schema.sql` is all `CREATE TABLE IF NOT EXISTS`, which handles exactly one
kind of change — a brand-new table — and silently ignores every other. Until
private messages, every change happened to be a new table, so nothing noticed.

`apps/board/migrations.py` holds numbered steps applied once each, in order,
recorded in SQLite's own `user_version`. `init_db` runs the schema first and
the migrations second: on an empty database the schema builds the current
shape and each step finds its work done; on an existing one the schema adds
what is new and the steps fix up what it could not touch.

**Never edit a step that has shipped**, and never renumber one. A server that
has run it will not run it again, so a correction is a new step.

The first step rebuilds `attachments` so a picture can belong to a private
message. It has to be a rebuild rather than an `ALTER`, because the table
carries a CHECK constraint and SQLite has no `DROP CONSTRAINT` — adding the
column works and the next insert is refused by a constraint that can no longer
be removed. That is tested against a database built in the old shape, with rows
in it, because a migration tested only on a fresh database is tested against
the one case it was never needed for.

**After deploying this, check that an existing photo still renders.** That is
the proof the rebuild kept real rows. Back up first — `scripts/backup-board.sh`
— as with any migration.

**Step 4 rebuilds `members` for the same reason**, to widen the role CHECK so
`moderator` is a legal value. It is the more delicate of the two: eight tables
point at `members(id)`, and the procedure drops and renames the table with
foreign keys off. The clauses in those tables say `REFERENCES members`, resolved
by name, so they find the new table by themselves — but the partial unique index
that guarantees one owner is *not* carried over by the copy and has to be
recreated, and `PRAGMA foreign_key_check` runs before the transaction commits.
Both are asserted by tests that build a database in the old shape with a row in
every referencing table.

One trap worth recording, because it cost a debugging session: the allowed roles
can only be read back out of the `CREATE TABLE` text in `sqlite_master`, and
SQLite stores that text verbatim — comments included. The comment in `schema.sql`
that explains why `moderator` needed a rebuild contains the word, so the step's
"have I already run?" check found it and skipped itself. Comments are stripped
before the check now.

**Back up before deploying this one in particular**: the table being rebuilt is
the one your own account is in, and it is irreversible in place.

### 11.12 Public profiles at `vienalatina.com/su-nombre`

A member fills in *Mi perfil* and ticks *Publicar mi página*. Until they do,
there is no page: `profile_published` starts at 0 and an unpublished profile
404s exactly like a name that was never a member.

**The routing is the part to understand before changing anything.** A profile
sits at the top level, sharing a namespace with the whole static site. Caddy
forwards a request to the app only when all three hold:

- the path is **one segment** — `/page/acerca/` never reaches the app
- it is not `/comunidad`
- **Hugo has built nothing there** (`not file` with `try_files`)

That last one is what makes it safe, and why there is no reserved-path list in
the Caddyfile: the question is answered by the filesystem, so it stays correct
when the site grows a page nobody remembered. A member called `de` shadows
nothing — `/de/` is a real directory, so it is served, and their profile is
merely unreachable. The app refuses such names at sign-up for that reason.

An unknown name 404s from the app, and `handle_response` turns that into the
site's own `404.html`, so a visitor never sees the members area's error page.

**After reloading Caddy, check the public site first**, before looking at any
profile:

```sh
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
for path in / /de/ /pt-br/ /page/acerca/ /page/agenda/ /robots.txt /comunidad/ /noexiste; do
  printf '%-20s %s\n' "$path" "$(curl -s -o /dev/null -w '%{http_code}' https://vienalatina.com$path)"
done
```

Everything but the last should be 200 (or 301). `/noexiste` should be 404 and
should look like the site's own 404 page. A routing change that shadows the
public site is worse than having no profiles, so if anything there is wrong,
revert the Caddyfile and reload before investigating.

**The profile page repeats the brand colours** instead of sharing them. Hugo
fingerprints its stylesheet, so the app cannot link to it with a stable URL —
the same problem the Gitea theme has. If the colours change in
`themes/vienalatina/assets/css/main.css`, change them in
`apps/board/templates/profile.html` too.

### 11.13 The six sections, and the moderation queue

Signing in lands on **Inicio**, a dashboard of what has happened since the last
visit. The navigation is the same for everybody except the last entry:

| | Where | Who |
|---|---|---|
| **Inicio** | `/comunidad/` | everybody |
| **Muro** | `/comunidad/muro` | everybody |
| **Miembros** | `/comunidad/miembros` | everybody |
| **Publicaciones** | `/comunidad/publicaciones` | everybody, with two different pages behind it |
| **Calendario** | `/comunidad/calendario` | everybody; admins add events |
| **Gestión** | `/comunidad/gestion` | admins and the owner |

**Miembros replaced Privados**, which was a list of conversations. The people
are the section now — a grid of faces and names — and each person's page holds
their profile, the private conversation with them, and (for an admin) the
controls for that one person. §11.10 has the mechanics. The unread badge moved
to this entry with the messages.

Gestión holds Miembros' *Dar de alta*, the published **Artículos**, the site's
static **Páginas** and **Marca**. A moderator cannot open it, so their route to
the published articles is the *Ya publicado* link on Publicaciones — the only
one they have, which is why it sits beside the button rather than inside a
paragraph.

*Mi perfil*, *Descargar mis datos* and *Salir* are in the menu under the member's
own name, top right. They are not sections of the site, and while they were in the
navigation three of the entries were admin-only — which meant an ordinary
member signed in to a wall and a member list, with their own profile and their own
private messages hidden from them. `apps/board/tests/test_navigation.py` asserts
the navigation per role now, so that cannot come back quietly.

The wall moved from `/comunidad/` to `/comunidad/muro`. Thread addresses did not
move, so anything linked from a private message still works.

**Publicaciones is the queue.** A member writes a proposal; it lives in the
`submissions` table and goes nowhere near the site. A moderator or admin reads it
and either approves it — which commits it to the content repository under the
member's own name, so `git log` names the writer and not the approver — or returns
it with a reason the author reads on their own Publicaciones page. Moderators and
admins do not queue: they write in *Contenido* and their post is committed as they
save it.

Three things worth knowing about that design:

- **A pending post is not in git at all.** It could not be: a commit to that
  repository *is* publication, because Gitea's webhook starts the translate →
  build → deploy pipeline within seconds. There is no draft state on the site.
- **One publish path.** Approval calls `content.publish`, the same function the
  editor uses, so an approved proposal is byte-identical to a post written by a
  moderator and the pipeline cannot tell them apart.
- **Its picture is private until approval**, served from `/data/uploads` to the
  author and the moderators only. Approving commits it to the repository and
  deletes the private copy, so there is one file in one place.
- **There is no way to promote a wall thread into a proposal**, deliberately.
  Writing for the public is a separate act from talking to the people you already
  know, and the site is curated on purpose: fewer posts, each one read by somebody
  first.

Editing a returned proposal puts it back in the queue automatically. An approved
one can no longer be edited from there — the file is in the repository, and that
is what *Contenido* is for.

**Published posts carry a byline.** The editor writes `author` into the
frontmatter, and `author_url` only when that member has published their own page
— so the article says *por Fulana* and the name links through when there is
somewhere to go. `scripts/translate.py` copies both into the German and
Portuguese siblings by itself: it carries the whole frontmatter and translates
only the title and the description. Editing a post keeps the author it already
had, so correcting somebody's typo never re-signs their article.

**On the wall itself**: one 👍 per member per post or comment, which clicking
again takes back, and every name is a link to that person's page inside the
members area — which exists for everybody, published page or not. The one name
that is not a link is *Miembro eliminado*, the tombstone an erased member's
writing is reassigned to. Both rules live in one place each
(`templates/_reaction.html`, `templates/_person.html`) so no screen has to
remember them.

**Miembros is a directory of faces and names**: a card per person with their
photo, username and role, and the whole card is a link to their page. Everything
about one person is on that page — their bio and links, the private conversation
with them, and, for an admin, the controls (suspend, re-invite, change role,
erase) inside a *Gestionar* block, closed by default. A member's photo shows
here even when their public page is unpublished, because this side is behind the
login; `profiles.member_photo` serves it and the profile form says so in plain
words.

### 11.15 The public menu is built from the pages

It used to be three names per language in `config.yaml`, which meant a page
published through *Contenido* never appeared on the site, and that the German and
Portuguese menus had to be edited by hand to match. `partials/header.html` now
builds it from the pages themselves, so a page shows up when it is published,
with its own translated title.

Two fields in the editor decide where it goes:

- **`parent`** — another page's basename (`acerca` for `acerca.es.md`), which puts
  this page in the submenu under that one. The basename is what `translate.py`
  uses to pair siblings, so a parent chosen once holds in all three languages.
- **`weight`** — the order, lower first, 50 by default.

**URLs do not change.** A subpage is still `/page/<slug>/`; only the menu nests,
so nothing in the Caddyfile, the pipeline or the editor's filename rules has to
learn about folders. The dropdown is the one the theme already had, ported from
WordPress: hover and `:focus-within`, inline on a phone, no script.

Only *Inicio* remains in `config.yaml`. Weight under 5 puts a menu entry before
the pages, 5 or more after them. *Comunidad* used to be the entry at weight 9 and
is a button in the bar now — see §11.19.

### 11.15b Two calendars

**Public events are posts carrying an `event_date`**, with two more fields of
their own: `event_time` and `event_location`. The post's own `date` is when it
was announced — usually weeks earlier, and what Hugo sorts the blog by — so the
event's date cannot be the same field.

**The date is the whole declaration.** There used to be an «Evento» category to
tick first, and ticking it without filling the date in was an error the form had
to refuse; with the categories gone there is nothing to tick, and no way left to
say "this is an event" without saying when. A date that is not a date is still
refused. Members propose events exactly as they propose anything else, and a
moderator approves them.

An event's page leads with the day, the time and the place, shows its picture
full width, and offers **Añadir a mi calendario**: a `.ics` beside the page,
generated because the editor writes `outputs: ["HTML", "ics"]` into an event's
frontmatter. It also carries `Event` structured data, which is what puts a date
beside the result in a search listing.

`vienalatina.com/page/agenda/` is a month grid per month, from the first event to
the last, built at build time. `assets/js/agenda.js` shows one and moves between
them — so with JavaScript off every month is on the page as a list, and with it
on, moving months costs no request and starts on the month the *reader* is in
rather than the month the site was built in.

**The private calendar** is `/comunidad/calendario`: rows in SQLite, written by
admins, read by every member, never published. It shows the public events too,
marked, so a member has one place to look; those come from the `content_cache`
that the editor's listing already fills, which is why **migration 5** adds three
columns to it and to `submissions`. If the git server is unreachable the private
events still render and the page says so — `gitea.py` now turns a refused
connection into a `GiteaError` rather than letting `requests`' own exception
through, which is what made that possible.

The month grid is Python's `calendar.Calendar`; there is no date arithmetic of
ours anywhere in it. Erasing an admin clears `events.created_by` and leaves the
event standing: the meeting still happens after they leave.

### 11.16 Sharing, SEO and answer engines

`partials/seo-head.html` was already emitting hreflang, Open Graph, a Twitter
card and `BlogPosting` for articles. What it now also does:

- **Static pages get all of it** — they had none, so *Acerca de* shared as a bare
  link. `WebPage` schema, Open Graph, the card
- **`og:image` with an alt**, from the post's own picture or `params.defaultImage`
  as a fallback. That param is empty: fill it in when there is a logo, and every
  link shared on WhatsApp, Signal, Mastodon or Bluesky starts carrying a picture
- **`Organization`** on the home, with `areaServed: Wien` and a `sameAs` list
  ready for social profiles — this is how a search or answer engine knows the
  Instagram account and this site are one body
- **`BreadcrumbList`** on every post and page, and the author's profile URL in the
  article's structured data
- **Optional `FAQPage`**: `pregunta|respuesta` per line in the editor, rendered at
  the foot of the page *and* described in the schema. Current guidance for
  generative engines puts a short factual answer to a concrete question at the
  top of what gets quoted, and this is the cheapest way to write one
- `llms.txt` lists the static pages as well as the articles

The translation pipeline carries all of it: `image_alt` joins the translated
keys, and `faq` is translated line by line with each half handled separately so
the `|` survives.

### 11.17 Writing boxes

Every box where somebody writes prose — the wall, a comment, a private message, a
proposal, the editor, a profile bio — carries a markdown toolbar and a *Vista
previa*, opted in with `data-markdown` and attached by `static/board.js`. The
preview POSTs to `/comunidad/previsualizar`, which renders with `render.to_html`:
the same function the board and the site use, so the preview cannot promise
something publishing will not deliver. A markdown parser in the browser would be
a second opinion about what somebody's text means.

Choosing a picture shows a thumbnail with its name and size before anything is
uploaded. That is the one reason `blob:` is in the `img-src` policy — a handle to
bytes already in the page, which reaches no network.

### 11.18 Marca: the admin owns the look

*Gestión* → **Marca**, admins only. Colours, a logo and a favicon for both
halves of the site, with an *Avanzado* fold holding every token as a text field
and a free CSS box.

**`apps/board/brand.py`'s `FACTORY` is the single source of truth for the
palette.** The two `:root` blocks — `themes/vienalatina/assets/css/main.css` and
`apps/board/static/board.css` — each still carry their own copy, because they are
served by different things and each has to stand alone; what changed is that
`apps/board/tests/test_brand.py` parses both and fails the build if either
disagrees with `FACTORY`. The comment that used to say *"If the brand colours
change, change them in both"* was an instruction, which is the weakest kind of
guarantee there is.

**One save, two destinations**, because the two halves cannot read the same
store:

| Where | What | Why |
|---|---|---|
| SQLite, table `brand`, one row | the tokens, the custom CSS, both pictures under `/data/uploads` | the members area must render when the git server is down |
| the content repository: `data/brand.yaml`, `static/brand/logo.*`, `static/brand/favicon.png` | the same values | the public site is files on disk and has no database |

**SQLite first, then the commits.** If Gitea is unreachable the change still
takes effect in the members area, `published_at` stays NULL, and the screen says
so with a *Reintentar publicar* button. The other order would let a git outage
block a change that does not need git. A colour therefore reaches
vienalatina.com on the next pipeline run — a couple of minutes — and the members
area immediately.

Three details worth knowing before changing any of it:

* **Only what differs from the factory is stored**, as JSON in one column. A
  token added here later arrives with its default already in place for every
  existing install, which a snapshot of all thirteen would not.
* **`--brand-dark`, `--brand-soft` and `--brand-softer` are worked out from
  `--brand`** in HLS (hue and saturation kept, lightness moved), so an admin
  picks one colour and gets a coherent set. Each can be overridden in
  *Avanzado*. With no brand colour stored, the factory hexes are used verbatim —
  deriving from an unchanged colour would shift the palette by a rounding error
  the first time anybody opened the screen.
* **A WCAG contrast warning, not a refusal.** Text on background below 4.5:1,
  or white on the brand colour below 3:1, is named with its ratio and saved
  anyway. An admin may have a reason; what this prevents is shipping an
  unreadable site without once being told.

**The Marca screen deliberately does not load the generated stylesheet.** Every
other page does. That is what keeps *Restaurar lo de fábrica* reachable after
somebody puts `* { display: none }` in the CSS box — and the reason the colours
an admin picks do not show on the page where they pick them. The live preview
block is where they see them, restyled by `board.js` setting the custom
properties on that one element (a DOM write, not an inline `style` attribute,
which is what makes it work under a policy with no `'unsafe-inline'`).

**No SVG and no ICO.** The logo takes PNG, JPG or WebP and the favicon PNG only,
both identified from their bytes by the same sniffer the wall uses. An SVG is XML
that can carry script and would be served from our own origin; ICO would be one
more branch in the sniffer for a format every current browser no longer needs.

The pictures are committed to stable paths, so saving a new logo replaces the
file instead of leaving every previous one deployed and unreferenced; a logo
saved in a different format has its predecessor deleted. `scripts/translate.py`
needs nothing — it walks `content/` only, and `data/` and `static/` are outside
it. A site with no `data/brand.yaml` builds byte-for-byte the HTML it built
before any of this existed, which is asserted by building both ways.

### 11.19 The chrome on a phone

**The public menu opens from a ☰ below 768px**, and above it is the horizontal
strip it has always been. No JavaScript: `header.html` carries a visually hidden
checkbox immediately before the menu, the ☰ is a `<label>` for it anywhere in the
bar, and one rule in `main.css` —
`.site-menu__state:checked ~ .site-bar__pages { display: flex }` — does the rest.

That is worth knowing because of how it was broken. The panel's styles, the ☰'s
styles and the animated ✕ were all ported from the WordPress theme when the menu
was built; the **button was not**, and nothing ever set the class the CSS was
waiting for. So for three phases every phone saw the site with no navigation at
all — brand, language letters, posts — while every rule involved was correct.
`apps/board/tests/test_templates.py` now fails if the stylesheet hides the menu
behind a state the header does not carry.

*Why a checkbox and not `<details>`*, which is this project's usual answer for a
control without a script: on desktop this menu has to be open and horizontal, and
current Chrome hides a closed `<details>`'s content with `content-visibility` on
`::details-content`, which a `display` rule on the child no longer overrides. A
checkbox behaves the same in every engine. It stays in the tab order and keeps
its label, so the menu opens from a keyboard.

**Comunidad is a button in the bar**, at every width — brand-coloured through the
same tokens Gestión → Marca writes, so it follows the site's colours. Its three
translations are in `partials/t.html` rather than in three `menus.main` blocks:
the URL is the same in every language, so the menu entry was only ever carrying
a label. The language switcher is **rendered twice**, in the bar for desktop and
at the foot of the menu panel for the phone — a brand, a button, a ☰ and three
language letters do not fit across 360px, and CSS cannot lift an element out of
the bar and into the panel.

In the members area the navigation **wraps** on a phone rather than scrolling:
six sections over two lines shows all six, where a swipe strip would have left
Calendario and Gestión off the right edge with nothing to say they were there.

## 12. Make Gitea look like the site

Members sign in to `/comunidad/` through Gitea, so Gitea's sign-in form and its
authorize dialog are part of the journey for everyone — not just for you, and
not just for people who open a repository. Unthemed they are two dark screens in
the middle of a cream-coloured site.

How often anyone sees them is worth knowing before judging the result: the
**authorize dialog appears once per person, ever** — Gitea remembers the grant —
and the **sign-in form only when their Gitea session has lapsed**, which
"Remember This Device" pushes out to weeks. This is a first-impression fix.

### The name, not just the colours

Themed or not, those two screens said **Gitea** — in the tab, the heading and
the footer. A member has no idea what that is, and for anyone the platform is
ever sold to it is a competitor's name on their login page. Three settings in
`/srv/gitea/docker-compose.yml` take care of it:

```
GITEA__DEFAULT__APP_NAME=Viena Latina
GITEA__other__SHOW_FOOTER_POWERED_BY=false
GITEA__other__SHOW_FOOTER_VERSION=false
```

`APP_NAME` lives in `app.ini`'s unnamed root section, which the environment
mapping spells `DEFAULT`.

**`/srv/gitea/docker-compose.yml` is a copy, and nothing kept it in step with
this repository.** That is worth stating plainly because it cost a week: every
Gitea setting added here — CORS, the theme, OpenID, the register button, the
footer — was committed and documented and never reached the server, because the
only thing that syncs a compose file is `deploy-board.sh`, and it syncs the
board's. The file on the server stays valid, the container stays healthy, and
the setting is simply absent.

```sh
cd ~/vienalatina && sudo bash scripts/deploy-gitea.sh
```

That copies `infra/gitea/docker-compose.yml` across (keeping the old one as
`.bak` and printing the diff, since it may have been hand-edited), restarts,
and then **reads the settings back out of the running container** and prints
them. Treat that output as the only evidence: a line missing there is a setting
not in effect, whatever the compose file says.

That should show `APP_NAME = Viena Latina`. Hiding the version is the one with
a security argument as well as a cosmetic one: it tells a passer-by exactly
which advisories to try.

**On the licence**, since this is rebranding somebody else's software: Gitea is
MIT, whose only obligation is that the copyright and permission notice travel
with copies of the software. We are not redistributing it — the official image
runs unmodified, with its own `LICENSE` file untouched, and we talk to it over
HTTP. MIT requires no attribution in a user interface, and Gitea itself ships
`SHOW_FOOTER_POWERED_BY` as a supported setting, which settles what the project
intends. The name is a trademark of Gitea Limited; that restricts using it to
brand something else, not declining to display it. Redistributing a modified
Gitea under its own name would be a different question — this is not that.

### The theme

Unlike Decap, Gitea supports this properly: a theme is a CSS file in a directory
it already reads.

```sh
cd ~/vienalatina
git pull --no-rebase --no-edit gitea main
bash scripts/gitea-theme.sh
```

`GITEA__ui__DEFAULT_THEME=vienalatina` is already in
`infra/gitea/docker-compose.yml`, so it arrives with the sync — no hand-editing
of the server's copy:

```sh
cd ~/vienalatina && sudo bash scripts/deploy-gitea.sh
```

The script prints `DEFAULT_THEME` back out of the container. If it says
`vienalatina` and the screens are still grey, the setting is fine and the
*theme file* was never built — run `bash scripts/gitea-theme.sh` first. Those
are two different failures with one symptom, which is why the script names
both.

Check it in a private window at **https://git.vienalatina.com/user/login** —
cream background, the Viena Latina wordmark, `#c0391c` buttons. Then browse a
repository and open a commit: a theme that only looks right on the login page is
half done.

### Re-run it after every Gitea upgrade

The theme is Gitea's own light theme with our colours appended, and the base is
read out of the running container so it matches the installed version. A new
Gitea release can introduce variables our overrides do not mention, and a base
frozen in the repository would drift out of date in ways nobody notices until a
page looks wrong.

```sh
bash scripts/gitea-theme.sh
cd /srv/gitea && sudo docker compose restart gitea
```

Nothing breaks if you forget — an unknown variable is a declaration nobody
reads, so the worst case is a corner that stays grey.

### Signing out

One click. *Salir* clears the session and returns to the sign-in form, which
asks for a password.

This section used to explain at length why that was not true — the session
belonged to the git server, its logout is POST-only and unreachable from
another domain, and one click on *Entrar* signed you straight back in. All of
that followed from delegating identity, and none of it survived taking it back.

### 11.14 When nobody can sign in

Every path to a first password goes through email: the invitation when a member
is added, and *¿olvidaste tu contraseña?* afterwards. If the mailbox is down
and the owner is locked out, that is a circle with no way in.

```sh
sudo bash scripts/set-password.sh pablo
```

Prompts for a password without echoing it, hashes it with the same code the
application uses, inside the running container. Never takes the password as an
argument — an argument is visible in `ps` to everyone on the box.

**Test it while you still have another way in**, not on the day you need it.

## 13. Email: invitations and passwords

Until this is configured, an admin can add members but **nobody else can get
in**. The password was shown once to the admin, and Gitea's *Forgot password*
answers "Account recovery is disabled because no email is set up". That was the
state the members area shipped in; this section is what fixes it.

### 13.1 What happens now

An admin enters a username and an email. The server creates the Gitea account
with a random password **nobody ever sees, the admin included**, and emails the
member a link. The link opens a Viena Latina page where they choose their own
password, and only then can the account be used.

The same mechanism powers *¿Olvidaste tu contraseña?* on the sign-in page, which
replaces Gitea's dead recovery page. Nobody leaves the site for either.

### 13.2 SMTP settings

These are the details of the `hola@vienalatina.com` mailbox. If that mailbox is
part of the old Hetzner shared hosting, they are in the Konsole panel under the
email account.

```sh
sudo nano /srv/board/.env
```

```
MAIL_HOST=
MAIL_PORT=587
MAIL_SECURITY=starttls
MAIL_USER=hola@vienalatina.com
MAIL_PASSWORD=
MAIL_FROM=Viena Latina <hola@vienalatina.com>
```

**Port and security go together.** 465 means `MAIL_SECURITY=ssl`; 587 means
`starttls`. Mismatching the pair is the usual reason a mailbox that works
perfectly in a mail client fails here, and the error it produces is a timeout
rather than anything that names the cause.

```sh
cd ~/vienalatina && sudo bash scripts/deploy-board.sh
```

Not `docker compose up -d --force-recreate` on its own: if the code that reads
these settings arrived in the same pull, that restart runs the old image and
mail stays unconfigured with the settings sitting right there in `.env`. The
script rebuilds first.

The variables also have to be listed in `/srv/board/docker-compose.yml`, which
they now are. Compose does not hand `.env` to a container — it substitutes into
the compose file — so a setting added to `.env` and not to the compose file is
read by nobody. `apps/board/tests/test_deployment.py` fails the build if the
two ever drift apart again.

Test it by adding a member with an address you can read. If the mail cannot be
sent, the screen says so and shows you the invitation link to pass on by hand —
the account is created either way, so a mail problem delays somebody rather
than stranding them.

### 13.3 What the links are, and why they expire

A link is enough to set the password on that account, so it is treated as a
credential:

- **single use** — following it and choosing a password spends it
- **invitations last 7 days, resets 1 hour**
- **only a hash is stored**, so a leaked database backup is a list of useless
  hashes rather than a set of live keys
- asking for a new link **invalidates the previous one**, so an older email
  sitting in an inbox stops working
- recovery answers identically for an address that belongs to a member and one
  that does not, and stops after three attempts in fifteen minutes

If a member says a link does not work, the fix is always to send another. There
is deliberately no way to find out *why* one failed from the page itself: that
distinction would tell whoever holds a stale link something about the account
behind it.

### 13.4 The sign-in page loses two tabs

`GITEA__openid__ENABLE_OPENID_SIGNIN=false` and
`GITEA__service__SHOW_REGISTRATION_BUTTON=false`, already in
`infra/gitea/docker-compose.yml`. OpenID is sign-in with an external identity
URL, which nobody here will use, and the register button contradicts
`DISABLE_REGISTRATION` — it invited people to try something the server then
refused.

It also loses a third thing, the *Forgot password?* link, which goes to a page
that answers "Account recovery is disabled because no email is set up" and
always will: the SMTP details are the members area's, and this container has no
mailer and needs none. Recovery lives at **vienalatina.com/comunidad/recuperar**
and works. The link is hidden by a rule in the theme file rather than by
replacing the template, so a Gitea upgrade cannot quietly undo it — and if the
selector ever stops matching, the link reappears rather than the page breaking.

```sh
cd ~/vienalatina && sudo bash scripts/deploy-gitea.sh
```
