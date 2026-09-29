"""The only place that talks to the git server.

**Sign-in is not here, and that is the point of the file.** Members have
passwords in our own database and no account on this server at all. This talks
to it about one thing: the repository the site is built from.

It used to do much more. Identity was delegated here over OAuth2, each member
had an account, and the editor committed with a token belonging to whoever was
typing. That is what made signing in leave vienalatina.com and signing out
impossible to finish, so it was taken back. What remains:

* **Content** — reading, writing and deleting files in the site repository,
  with one server-side token (`content_token()`), naming the real author on
  every commit so `git log` still says who wrote what.
* **Accounts** — `admin_create_user` and `admin_set_password` still exist for
  the handful of real git users (pablo, the pipeline bots). Nothing in the
  members area calls them any more.
"""

from __future__ import annotations

import base64
import secrets
import string
from urllib.parse import quote, urlencode

import requests
from flask import current_app

TIMEOUT = 10


class GiteaError(RuntimeError):
    """Gitea refused a request. The message is safe to show a member."""


class StaleFile(GiteaError):
    """The file changed since it was loaded into the form.

    Gitea rejects a write whose `sha` no longer matches the branch, which is
    what makes two people editing one post a visible conflict rather than a
    silent overwrite of whoever saved first.
    """


def _base() -> str:
    return current_app.config["GITEA_URL"].rstrip("/")


def _api(path: str) -> str:
    return f"{_base()}/api/v1{path}"


def _repo() -> str:
    return current_app.config["CONTENT_REPO"].strip("/")


def _branch() -> str:
    return current_app.config["CONTENT_BRANCH"]


# --- sign-in -------------------------------------------------------------

# --- account creation (site-admin token) ---------------------------------

def admin_configured() -> bool:
    """Whether this server holds a site-admin token.

    Without one it can neither create accounts nor set passwords. That is a
    supported configuration — server-setup.md §11.2 says why somebody might
    choose it — and it is why both calls below refuse before reaching Gitea.
    Exposed as a predicate so a screen can say so *before* asking somebody to
    fill in a form that cannot be saved, the way `mail.configured()` is used.
    """
    return bool(current_app.config.get("ADMIN_TOKEN"))


def generate_password() -> str:
    # Shown once to the admin, then changed by the member on first login.
    # Punctuation is left out on purpose: this gets read aloud or copied by
    # hand, and a password nobody can transcribe gets written on a note.
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def admin_create_user(login: str, email: str, full_name: str, password: str) -> None:
    token = current_app.config.get("ADMIN_TOKEN")
    if not token:
        raise GiteaError(
            "Falta GITEA_ADMIN_TOKEN: el servidor no puede crear cuentas nuevas."
        )
    response = requests.post(
        _api("/admin/users"),
        headers={"Authorization": f"token {token}"},
        json={
            "username": login,
            "email": email,
            "full_name": full_name,
            "password": password,
            "must_change_password": True,
            "send_notify": False,
        },
        timeout=TIMEOUT,
    )
    if response.status_code in (201, 200):
        return
    if response.status_code == 422:
        raise GiteaError("Ese usuario o ese correo ya están en uso.")
    if response.status_code in (401, 403):
        raise GiteaError("El token de administración no es válido.")
    raise GiteaError(f"No se pudo crear la cuenta ({response.status_code}).")


def admin_set_password(login: str, password: str) -> None:
    """Set a member's password on their behalf, after they chose it here.

    This is what lets the whole invitation and reset flow stay inside
    /comunidad/ instead of handing people to Gitea, whose own recovery page is
    dead without a mailer anyway.

    `login_name` and `source_id` are sent although nothing about them changes:
    Gitea's EditUserOption has historically treated them as required, and
    omitting them has been reported to move a local account onto a different
    authentication source. For a local user they are the username and 0, so
    sending them is a no-op that avoids the question.
    """
    token = current_app.config.get("ADMIN_TOKEN")
    if not token:
        raise GiteaError(
            "Falta GITEA_ADMIN_TOKEN: el servidor no puede cambiar contraseñas."
        )
    response = requests.patch(
        _api(f"/admin/users/{quote(login, safe='')}"),
        headers={"Authorization": f"token {token}"},
        json={
            "password": password,
            "must_change_password": False,
            "login_name": login,
            "source_id": 0,
        },
        timeout=TIMEOUT,
    )
    if response.status_code in (200, 201):
        return
    if response.status_code == 422:
        # Gitea enforces its own minimum length and complexity, and its message
        # is in the admin's language rather than the member's, so it is not
        # passed through.
        raise GiteaError("No se aceptó esa contraseña. Prueba con una más larga.")
    if response.status_code in (401, 403):
        raise GiteaError("El token de administración no es válido.")
    if response.status_code == 404:
        # Reachable: a member can be added here without ticking "crear también
        # su cuenta", and then invited. Everything works right up to this call,
        # which is asked to change the password of an account that was never
        # made. A bare "(404)" sends the admin looking at the wrong thing.
        raise GiteaError(
            f"No existe la cuenta «{login}», así que no se le puede "
            "poner contraseña. Pide a un administrador que la cree."
        )
    raise GiteaError(f"No se pudo cambiar la contraseña ({response.status_code}).")


# --- content (the member's own token) ------------------------------------

def _contents_url(path: str) -> str:
    # quote() with no safe characters: a path segment is data, not structure.
    return _api(f"/repos/{_repo()}/contents/{quote(path, safe='/')}")


def content_token() -> str:
    """The token the editor commits with.

    One service token instead of a token per writer. Members no longer have
    accounts on the git server at all, so there is no per-member token to use —
    and attribution does not need one, because each commit names its author
    (see `_identity`).

    CONTENT_TOKEN is preferred and GITEA_ADMIN_TOKEN is the fallback, so
    nothing breaks on deploy. They can be the same, but they should not stay
    that way: this needs write access to one repository, while the admin token
    can create and modify every account on the instance.
    """
    token = (current_app.config.get("CONTENT_TOKEN")
             or current_app.config.get("ADMIN_TOKEN") or "")
    if not token:
        raise GiteaError(
            "Falta CONTENT_TOKEN: el servidor no puede guardar en el repositorio."
        )
    return token


def _identity(member) -> dict:
    """Who a commit is by.

    Gitea's contents API takes `author` and `committer`, and uses the token's
    own owner only when neither is given. So one token can commit as whoever
    actually wrote the thing, and `git log` still says who to ask about a post.
    """
    name = (member["display_name"] or member["gitea_login"]) if member else "Viena Latina"
    email = (member["email"] if member and member["email"]
             else "hola@vienalatina.com")
    return {"name": name, "email": email}


def _content_request(method: str, url: str, token: str, **kwargs):
    response = requests.request(
        method, url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
        **kwargs,
    )
    if response.status_code in (401, 403):
        raise PermissionError("gitea-unauthorised")  # caller refreshes and retries
    return response


def list_directory(path: str, token: str) -> list[dict]:
    """Names and shas only — the contents API does not return file bodies here."""
    response = _content_request("GET", _contents_url(path), token,
                                params={"ref": _branch()})
    if response.status_code == 404:
        return []          # an empty content folder is normal, not an error
    if response.status_code != 200:
        raise GiteaError(f"No se pudo leer la lista de archivos ({response.status_code}).")
    payload = response.json()
    return [item for item in payload if item.get("type") == "file"]


def read_file(path: str, token: str) -> tuple[str, str]:
    """(text, sha). The sha comes back so a later write can prove it is current."""
    response = _content_request("GET", _contents_url(path), token,
                                params={"ref": _branch()})
    if response.status_code == 404:
        raise GiteaError("Ese archivo ya no existe.")
    if response.status_code != 200:
        raise GiteaError(f"No se pudo leer el archivo ({response.status_code}).")
    payload = response.json()
    text = base64.b64decode(payload.get("content", "")).decode("utf-8")
    return text, payload.get("sha", "")


def write_file(path: str, data: bytes, message: str, token: str,
               sha: str | None = None, member=None) -> str:
    """Create when `sha` is None, update otherwise. Returns the new sha."""
    body = {
        "content": base64.b64encode(data).decode("ascii"),
        "message": message,
        "branch": _branch(),
        "author": _identity(member),
        "committer": _identity(member),
    }
    if sha:
        body["sha"] = sha
    response = _content_request("PUT" if sha else "POST", _contents_url(path),
                                token, json=body)
    if response.status_code in (200, 201):
        return response.json().get("content", {}).get("sha", "")
    if response.status_code in (409, 422):
        raise StaleFile(
            "Alguien más guardó este archivo mientras lo editabas. "
            "Vuelve a abrirlo para no perder su trabajo."
        )
    raise GiteaError(f"No se pudo guardar el archivo ({response.status_code}).")


def delete_file(path: str, sha: str, message: str, token: str, member=None) -> None:
    response = _content_request("DELETE", _contents_url(path), token, json={
        "sha": sha, "message": message, "branch": _branch(),
        "author": _identity(member), "committer": _identity(member),
    })
    if response.status_code in (200, 204):
        return
    if response.status_code in (409, 422):
        raise StaleFile("El archivo cambió desde que lo abriste. Recarga la lista.")
    raise GiteaError(f"No se pudo borrar el archivo ({response.status_code}).")
