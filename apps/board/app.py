"""Application factory for the members area.

Served at /comunidad/ on the main domain, behind Caddy, alongside the static
Hugo output and Decap. It is the only part of vienalatina.com that runs code to
answer a request; everything else is a file on disk.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Flask, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .db import close_db, init_db
from .security import check_csrf, csrf_token

URL_PREFIX = "/comunidad"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def create_app(overrides: dict | None = None) -> Flask:
    # Static files have to live under the prefix too: Caddy only forwards
    # /comunidad/*, so a default /static/… would fall through to the Hugo
    # file_server and 404.
    app = Flask(__name__, static_url_path=f"{URL_PREFIX}/static")

    # Caddy terminates TLS and forwards plain HTTP, so without this the app
    # believes every request arrived unencrypted and sends its redirects to
    # http:// — an extra hop, and a moment where the session cookie could
    # travel in the clear.
    #
    # Trusting these headers is only safe because the container binds to
    # 127.0.0.1 and nothing but Caddy can reach it. Expose the port and a
    # client can forge its own address and scheme.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SECRET_KEY=os.environ.get("BOARD_SECRET_KEY", ""),
        DB_PATH=os.environ.get("BOARD_DB", "/data/board.db"),
        GITEA_URL=os.environ.get("GITEA_URL", "https://git.vienalatina.com"),
        ADMIN_TOKEN=os.environ.get("GITEA_ADMIN_TOKEN", ""),
        # What the editor commits with. Needs write access to one
        # repository — not the admin token, which can create and modify
        # every account on the instance. Falls back to it so nothing
        # breaks on deploy, but the narrower token is the right one.
        CONTENT_TOKEN=os.environ.get("CONTENT_TOKEN", ""),
        OWNER_LOGIN=os.environ.get("BOARD_OWNER", ""),
        BASE_URL=os.environ.get("BOARD_BASE_URL", "https://vienalatina.com"),
        # The repository the editor commits to — the same one Woodpecker builds,
        # which is what makes publishing from here indistinguishable from
        # publishing from Decap as far as the pipeline is concerned.
        CONTENT_REPO=os.environ.get("CONTENT_REPO", "pablo/vienalatina"),
        CONTENT_BRANCH=os.environ.get("CONTENT_BRANCH", "main"),
        # Outgoing mail. Without MAIL_HOST the app still runs, but nobody
        # can be invited or recover a password, so the admin screens say so
        # rather than failing at the moment somebody presses send.
        MAIL_HOST=os.environ.get("MAIL_HOST", ""),
        MAIL_PORT=os.environ.get("MAIL_PORT", "587"),
        MAIL_SECURITY=os.environ.get("MAIL_SECURITY", "starttls"),
        MAIL_USER=os.environ.get("MAIL_USER", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", "Viena Latina <hola@vienalatina.com>"),
        URL_PREFIX=URL_PREFIX,
        COOLDOWN_SECONDS=int(os.environ.get("BOARD_COOLDOWN_SECONDS", "20")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Off only for tests and local http; on the server this must stay true
        # or the session cookie travels in cleartext the first time someone
        # types the address without https.
        SESSION_COOKIE_SECURE=_env_flag("BOARD_COOKIE_SECURE", True),
        SESSION_COOKIE_NAME="vl_board",
        # Generous enough for a photo off a phone. The board itself needs a
        # fraction of this; the editor uploads images, and a writer whose
        # picture is silently refused has no way to tell what went wrong.
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        UPLOAD_MAX_BYTES=int(os.environ.get("BOARD_UPLOAD_MAX_BYTES", 8 * 1024 * 1024)),
        # Board pictures, deliberately inside the volume that already
        # holds board.db: one directory to back up, not two, and no
        # second mount to remember when moving the app to a new box.
        UPLOAD_DIR=os.environ.get("BOARD_UPLOAD_DIR", "/data/uploads"),
    )
    if overrides:
        app.config.update(overrides)

    if not app.config["SECRET_KEY"]:
        # A generated key would "work" and silently log everyone out on every
        # restart, which is a confusing way to find out the variable is unset.
        raise RuntimeError("BOARD_SECRET_KEY is required (generate one with `openssl rand -hex 32`).")

    from . import (auth, board, content, events, home, members, messages,
                   profiles, submissions, uploads)
    app.register_blueprint(auth.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(home.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(submissions.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(events.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(members.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(content.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(uploads.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(messages.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(board.bp, url_prefix=URL_PREFIX)
    app.register_blueprint(profiles.bp, url_prefix=URL_PREFIX)
    # No prefix: a public profile lives at the top level of the
    # site. Caddy only forwards a single-segment path that Hugo
    # has not built, so this never competes with the static site.
    app.register_blueprint(profiles.public_bp)

    app.teardown_appcontext(close_db)
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["url_prefix"] = URL_PREFIX

    @app.context_processor
    def _year():
        return {"current_year": datetime.now(timezone.utc).year}

    @app.context_processor
    def _badges():
        # Lazily, as functions: base.html is rendered for signed-out pages and
        # error pages too, and neither should run a query to draw a badge
        # nobody sees.
        return {"unread_private": messages.unread_count,
                "pending_review": submissions.pending_count,
                "may_moderate": submissions.may_moderate}

    @app.before_request
    def _before():
        # Order matters: reject forged writes before any handler can act on
        # them, but after the member is known so errors can render the chrome.
        auth.load_member()
        check_csrf()

    @app.after_request
    def _harden(response):
        # The members area must never appear in a search result. Both halves
        # are needed: robots.txt asks crawlers not to fetch, this tells the
        # ones that fetched anyway not to index.
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # No 'unsafe-inline'. Two consequences worth knowing rather than
        # rediscovering: an onsubmit="" or onclick="" attribute in a template
        # is silently ignored (see static/board.js), and a markdown image
        # pointing at another site will not load — which for a private board is
        # the right answer anyway, since an external image is a request that
        # tells someone else who read the thread and when.
        #
        # `blob:` is in img-src for one thing only: the thumbnail of a picture
        # somebody has just chosen and not yet uploaded. A blob URL is a handle
        # to bytes already in this page; it reaches no network and cannot be
        # pointed at another site.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'"
        )
        return response

    @app.errorhandler(400)
    def _bad_request(error):
        return render_template("error.html", code=400,
                               message=getattr(error, "description", "Solicitud inválida.")), 400

    @app.errorhandler(403)
    def _forbidden(_error):
        return render_template("error.html", code=403,
                               message="No tienes permiso para hacer esto."), 403

    @app.errorhandler(404)
    def _not_found(_error):
        return render_template("error.html", code=404,
                               message="No encontramos esa página."), 404

    @app.errorhandler(413)
    def _too_large(_error):
        return render_template("error.html", code=413,
                               message="El mensaje es demasiado largo."), 413

    @app.errorhandler(500)
    def _crashed(error):
        """The page an unexpected exception produces.

        Without this Flask prints its own, in English, with no way back into the
        site — which is what a stray PermissionError from the git client looked
        like for two phases: a white page reading "Internal Server Error", giving
        no hint that the cause was a token scope. The traceback goes to the log
        with the path beside it, and the member gets the site's own page.

        `original_exception` is what Flask hands the handler; logging it here
        rather than relying on the default keeps the request path in the same
        line, which is the difference between a searchable log and a pile of
        tracebacks.
        """
        app.logger.error("Unhandled error at %s: %s", request.path,
                         getattr(error, "original_exception", error), exc_info=True)
        return render_template(
            "error.html", code=500,
            message="Algo falló de nuestro lado. Ya ha quedado anotado."), 500

    init_db(app)
    return app
