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
   - Redirect URI: `https://vienalatina.com/admin/`
   - Save the **Client ID** — it goes into `static/admin/config.yml` (step 9).
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
```

(It will ask for your Gitea username/password.)

In Woodpecker (**https://ci.vienalatina.com**):

1. *Repositories* → *Add repository* → enable `pablo/vienalatina`
   (this auto-creates the push webhook in Gitea).
2. Repo → Settings → *Project settings* → check **Trusted** (needed so the
   deploy step may mount `/var/www/vienalatina.com`).
3. Repo → Settings → *Secrets* → add:
   - `deepl_api_key` — your DeepL key (the same one from the WP plugin
     settings page).
   - `gitea_push_token` — the bot token from step 6.3.

The push in the step above has already triggered a first pipeline — it likely
ran before the secrets existed, so open it and press the retry button. All
three steps (translate → build → deploy) should go green, and
`/var/www/vienalatina.com/` on the server now contains the built site:

```sh
ls /var/www/vienalatina.com     # index.html, de/, pt-br/, robots.txt, llms.txt …
```

## 9. Point Decap at Gitea

On your working copy: edit `static/admin/config.yml`, replace
`REPLACE_WITH_GITEA_OAUTH_CLIENT_ID` with the Decap OAuth Client ID from
step 6.2, commit, push to Gitea. (You can't log into `/admin` until the main
domain is live — that's expected.)

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
   migration"), spot-check the built site by IP or with
   `curl -H "Host: vienalatina.com" http://127.0.0.1/...` on the server.
2. At the registrar: lower the `vienalatina.com` A record TTL to 300, wait
   for the old TTL to expire, then change the A record to `<SERVER-IP>`
   (and `www` too, as CNAME to `vienalatina.com` or A to the same IP).
3. On the server: uncomment the `vienalatina.com` blocks in
   `/etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`. Caddy fetches
   the certificate as soon as DNS resolves to this server.
4. Verify: the checklist in the migration plan (hreflang tags, robots.txt,
   llms.txt, Lighthouse, red-pipeline DeepL failure test,
   `manual_translation: true` freeze test).
5. Keep the WP host untouched for 30 days as fallback; watch Google Search
   Console and add Caddy 301s for any 404s it reports.

## Optional: nightly backups (restic → Hetzner Storage Box)

```sh
sudo apt install -y restic
sudo restic -r sftp:uXXXXXX@uXXXXXX.your-storagebox.de:backups init
# then a root cron entry, e.g.:
# 0 3 * * * restic -r sftp:... backup /srv /var/www --password-file /root/.restic-pw
```
