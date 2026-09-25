"""Sending mail, through the association's own mailbox.

`smtplib` from the standard library rather than a mail framework: this sends
two kinds of message, both short and both plain text. A dependency would buy
templating and queueing that nothing here asks for.

Plain text only, no HTML alternative. An invitation is a sentence and a link —
HTML would add a second body to keep in step with the first, another place for
an escaping mistake, and a slightly worse chance of landing in the inbox rather
than the spam folder.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app


class MailNotConfigured(RuntimeError):
    """No SMTP host is set, so this server cannot send anything."""


class MailFailed(RuntimeError):
    """The mail server refused or could not be reached."""


def configured() -> bool:
    return bool(current_app.config.get("MAIL_HOST"))


def send(to: str, subject: str, body: str) -> None:
    if not configured():
        raise MailNotConfigured(
            "No hay servidor de correo configurado (MAIL_HOST)."
        )

    config = current_app.config
    message = EmailMessage()
    message["From"] = config["MAIL_FROM"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    host, port = config["MAIL_HOST"], int(config["MAIL_PORT"])
    security = (config.get("MAIL_SECURITY") or "starttls").lower()

    try:
        # 465 speaks TLS from the first byte; 587 starts in the clear and
        # upgrades. Getting this pair wrong is the usual reason a mailbox that
        # works in a mail client fails here, so it is configuration rather than
        # a guess from the port number.
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)

        with server:
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if config.get("MAIL_USER"):
                server.login(config["MAIL_USER"], config["MAIL_PASSWORD"])
            server.send_message(message)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        # The caller decides what to do about it — for an invitation that means
        # showing the admin the link so the member is not stranded.
        current_app.logger.warning("Mail to %s failed: %s", to, exc)
        raise MailFailed(str(exc)) from exc


def send_invite(to: str, name: str, url: str) -> None:
    send(
        to,
        "Tu acceso a Viena Latina",
        f"""Hola {name},

Te damos de alta en el área de la comunidad de Viena Latina.

Elige tu contraseña aquí:

{url}

El enlace sirve una sola vez y caduca en 7 días. Si caduca, pide a un
administrador que te envíe uno nuevo.

Si no esperabas este correo, puedes ignorarlo.

— Viena Latina
""",
    )


def send_reset(to: str, name: str, url: str) -> None:
    send(
        to,
        "Restablecer tu contraseña — Viena Latina",
        f"""Hola {name},

Alguien pidió restablecer la contraseña de tu cuenta.

Si fuiste tú, elige una nueva aquí:

{url}

El enlace sirve una sola vez y caduca en 1 hora.

Si no fuiste tú, ignora este correo: tu contraseña no ha cambiado.

— Viena Latina
""",
    )
