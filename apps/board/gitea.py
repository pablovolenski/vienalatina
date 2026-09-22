"""The only place that talks to Gitea.

Two unrelated conversations happen here and are worth keeping apart in your
head:

* **Sign-in** uses OAuth2 on behalf of the person at the keyboard. The app is
  registered as a *confidential* client with a secret, which it can hold
  because it runs on the server. The Decap CMS app is the opposite — a public
  client using PKCE — because that one runs in the visitor's browser and has
  nowhere to keep a secret.

* **Creating an account** uses a site-admin token belonging to the instance,
  not to any member. That token can create and modify any Gitea user, so the
  environment holding it is as sensitive as Gitea's own admin password.
"""

from __future__ import annotations

import secrets
import string
from urllib.parse import urlencode

import requests
from flask import current_app

TIMEOUT = 10


class GiteaError(RuntimeError):
    """Gitea refused a request. The message is safe to show a member."""


def _base() -> str:
    return current_app.config["GITEA_URL"].rstrip("/")


def _api(path: str) -> str:
    return f"{_base()}/api/v1{path}"


def authorize_url(state: str, redirect_uri: str) -> str:
    query = urlencode({
        "client_id": current_app.config["OAUTH_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    })
    return f"{_base()}/login/oauth/authorize?{query}"


def exchange_code(code: str, redirect_uri: str) -> str:
    response = requests.post(
        f"{_base()}/login/oauth/access_token",
        json={
            "client_id": current_app.config["OAUTH_CLIENT_ID"],
            "client_secret": current_app.config["OAUTH_CLIENT_SECRET"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise GiteaError("No se pudo completar el inicio de sesión.")
    token = response.json().get("access_token")
    if not token:
        raise GiteaError("Gitea no devolvió un token de acceso.")
    return token


def fetch_user(token: str) -> dict:
    response = requests.get(
        _api("/user"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise GiteaError("No se pudo leer el perfil desde Gitea.")
    return response.json()


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
        raise GiteaError("Ese usuario o correo ya existe en Gitea.")
    if response.status_code in (401, 403):
        raise GiteaError("El token de administración de Gitea no es válido.")
    raise GiteaError(f"Gitea rechazó la creación del usuario ({response.status_code}).")
