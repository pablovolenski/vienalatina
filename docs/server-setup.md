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
categories: [Comunidad]
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

### 11.1 Register the OAuth application

Gitea → **Site Administration → Integrations → Applications** →
*Create new OAuth2 application*:

- Name: `vienalatina-board`
- Redirect URI: `https://vienalatina.com/comunidad/auth/callback`
- **Leave "Confidential Client" TICKED.**

That last point is the opposite of the Decap application in step 6.2, and the
difference is worth understanding rather than memorising. Decap runs in the
visitor's browser, where any secret would be readable by the visitor, so it has
to be a public client using PKCE. The board runs on the server, so it can hold
a secret and should — a confidential client is the stronger of the two.

Save the **Client ID** and the **Client Secret**.

### 11.2 Optional: a token for creating accounts

Without it, admins can add people who already have a Gitea login, and nothing
else changes. With it, they can create the Gitea account from inside the members
area and hand over a one-time password.

Log in as a Gitea **site administrator** → Settings → Applications → *Generate
New Token* → scope **admin (write)**.

Understand what this token is before you create it: it can create and modify any
account on the instance, including administrators. Anything that can read the
board's environment — the compose file, `docker inspect`, a shell in the
container — can use it. If you would rather not have that on the box, leave
`GITEA_ADMIN_TOKEN` empty and create accounts in Gitea by hand.

Leaving it empty is a supported configuration, not a half-finished one: the
*Dar de alta* form checks for the token when it renders, says plainly that this
server cannot create accounts, and links to Gitea's own create-user page. You
then add that username here, with the checkbox already off.

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

An update is a named pull, a push to Gitea so the build pipeline sees it, and a
rebuild:

```sh
cd ~/vienalatina
git pull --no-edit github <the-branch-name>   # e.g. claude/relaxed-faraday-h4zd09
git push gitea main
sudo bash scripts/deploy-board.sh
```

`--no-edit` accepts the default merge message. Without it git opens an editor,
which is a strange place to find yourself mid-deploy.

If `github` is not a remote yet, add it once — see the end of step 8:

```sh
git remote add github https://github.com/pablovolenski/vienalatina.git
```

That rebuilds the image, copies the compose file across, restarts, and prints
the log. It never touches `/srv/board/.env` — that file holds the secrets and
lives only on the server — but it does compare it against `.env.example` and
name any setting that has appeared in the repository and is missing from yours.
New settings are always added by hand.

The database schema is applied at start-up with `CREATE TABLE IF NOT EXISTS`,
so a release that adds a table needs no migration step: the table appears when
the new code does.

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

### 11.6 Who can do what

| | Owner | Admin | User |
|---|---|---|---|
| Post, comment, edit own | ✓ | ✓ | ✓ |
| Delete any post | ✓ | ✓ | — |
| Edit someone else's post | — | — | — |
| Pin and close threads | ✓ | ✓ | — |
| Create users | ✓ | ✓ | — |
| Create admins | ✓ | — | — |
| Suspend a user | ✓ | ✓ | — |
| Suspend an admin | ✓ | — | — |
| Transfer ownership | ✓ | — | — |

Nobody edits anyone else's words, administrators included. Taking a post down is
visible to the person who wrote it; rewriting it is not, and an admin who could
do that could leave a sentence attributed to a member who never wrote it.

There is exactly one owner, and the database enforces it with a unique index
rather than trusting the application to remember. The owner cannot be suspended
or demoted by anyone, themselves included — to step down, transfer ownership to
an admin.

### 11.7 Personal data

Members' names, emails and writing are personal data under GDPR.

- **Erasure:** the owner's *Eliminar* removes the member row entirely and
  reassigns their threads and comments to a tombstone shown as "Miembro
  eliminado", so conversations other people took part in stay readable.
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

## 12. Make Gitea look like the site

Members sign in to `/comunidad/` through Gitea, so Gitea's sign-in form and its
authorize dialog are part of the journey for everyone — not just for you, and
not just for people who open a repository. Unthemed they are two dark screens in
the middle of a cream-coloured site.

How often anyone sees them is worth knowing before judging the result: the
**authorize dialog appears once per person, ever** — Gitea remembers the grant —
and the **sign-in form only when their Gitea session has lapsed**, which
"Remember This Device" pushes out to weeks. This is a first-impression fix.

Unlike Decap, Gitea supports this properly: a theme is a CSS file in a directory
it already reads.

```sh
cd ~/vienalatina
git pull --no-rebase --no-edit gitea main
bash scripts/gitea-theme.sh
```

Then add the line the script prints to `/srv/gitea/docker-compose.yml` (it is
already in `infra/gitea/docker-compose.yml`):

```
- GITEA__ui__DEFAULT_THEME=vienalatina
```

```sh
cd /srv/gitea && sudo docker compose up -d
```

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

### Signing out is two steps, and the app says so

Clicking **Salir** in the members area closes that session and deletes the
stored Gitea token. It cannot close the **Gitea** session in the same browser,
and Gitea remembers that the app was authorised — so without saying anything,
the next click on *Entrar con Gitea* would sign the person straight back in with
no password. On a laptop shared around the association, that is a button that
lies.

Gitea cannot be signed out from another site: its logout has been POST-only
since 1.11.2, so a link cannot trigger it and a cross-site POST would need
Gitea's CSRF token. The `prompt=login` parameter that would force
re-authentication is undocumented in every released version of Gitea's OAuth2
provider, and a security control should not rest on that.

So the logout page says plainly what is and is not closed, and offers the link
that finishes the job. On a shared computer, use it — or close the browser,
which also works. The members-area cookie is already a browser-session cookie,
so it does not survive that either way.

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
`GITEA__service__SHOW_REGISTRATION_BUTTON=false` in
`/srv/gitea/docker-compose.yml`. OpenID is sign-in with an external identity
URL, which nobody here will use, and the register button contradicts
`DISABLE_REGISTRATION` — it invited people to try something the server then
refused.

```sh
cd /srv/gitea && sudo docker compose up -d
```
