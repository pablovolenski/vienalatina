"""The private calendar.

What the association is doing and when, for the people inside: a meeting, a
rehearsal, a visit somewhere. Admins write them, every member reads them, and
nothing here is ever published — no Open Graph, no sitemap, nothing written to
/var/www. That is the whole difference between this and a post in the Evento
category, and it is why the two live in different places rather than behind a
flag on one.

**The public events appear here too**, marked as public and linking to the site,
because a member should have one place to look rather than two. They come from
`content.public_events`, which reads the cache the editor's listing already
fills — so a month view costs one directory request to the git server, not one
per post. When that server cannot be reached the private events still render and
the page says the public ones are missing, rather than failing whole.

The month grid is built with Python's own `calendar` module. There is no
JavaScript here and no date arithmetic of mine to get wrong: the standard
library has known which day of the week the first of the month falls on since
1991.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime

from flask import (Blueprint, Response, abort, flash, g, redirect,
                   render_template, request, url_for)

from . import content, gitea, uploads
from .db import get_db
from .render import to_html
from .security import admin_required, login_required

bp = Blueprint("events", __name__)

TITLE_MAX = 140
BODY_MAX = 20_000
LOCATION_MAX = 200

MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

MONTH_NAMES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
               "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
WEEKDAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def month_label(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"


def shift(year: int, month: int, by: int) -> tuple[int, int]:
    """The month `by` months away. Written out rather than reached for a
    library: twelve months is modular arithmetic, and dateutil is a dependency."""
    index = (year * 12 + month - 1) + by
    return index // 12, index % 12 + 1


def _requested_month() -> tuple[int, int]:
    """`?mes=2026-10`, or the month we are in. A malformed one is today's rather
    than a 400: a calendar that refuses to draw because a query string was
    mistyped is worse than one that shows now."""
    match = MONTH.match(request.args.get("mes", ""))
    if match:
        return int(match.group(1)), int(match.group(2))
    today = date.today()
    return today.year, today.month


def private_events(first: str, last: str) -> list[dict]:
    rows = get_db().execute(
        """SELECT e.*, m.display_name AS author
             FROM events e LEFT JOIN members m ON m.id = e.created_by
            WHERE e.starts_on BETWEEN ? AND ?
            ORDER BY e.starts_on, COALESCE(e.starts_at, '99:99')""",
        (first, last),
    ).fetchall()
    return [{
        "id": row["id"],
        "title": row["title"],
        "starts_on": row["starts_on"],
        "starts_at": row["starts_at"] or "",
        "location": row["location"] or "",
        "url": url_for("events.show", event_id=row["id"]),
        "public": False,
    } for row in rows]


@bp.route("/calendario")
@login_required
def month():
    year, number = _requested_month()
    days_in_month = calendar.monthrange(year, number)[1]
    first = f"{year:04d}-{number:02d}-01"
    last = f"{year:04d}-{number:02d}-{days_in_month:02d}"

    events = private_events(first, last)

    # The public ones, from the site. A git server that is unreachable must not
    # take the private calendar down with it — it is the half that only exists
    # here.
    public_missing = False
    try:
        events += [item for item in content.public_events()
                   if first <= item["starts_on"] <= last]
    except gitea.GiteaError:
        public_missing = True

    by_day: dict[int, list] = {}
    for item in sorted(events, key=lambda e: (e["starts_on"], e["starts_at"] or "99:99")):
        by_day.setdefault(int(item["starts_on"][8:10]), []).append(item)

    previous_year, previous_month = shift(year, number, -1)
    next_year, next_month = shift(year, number, 1)
    today = date.today()

    return render_template(
        "calendar.html",
        weeks=calendar.Calendar(firstweekday=0).monthdayscalendar(year, number),
        weekdays=WEEKDAYS,
        by_day=by_day,
        label=month_label(year, number),
        month_key=f"{year:04d}-{number:02d}",
        previous=f"{previous_year:04d}-{previous_month:02d}",
        next=f"{next_year:04d}-{next_month:02d}",
        this_month=f"{today.year:04d}-{today.month:02d}",
        today=today.day if (today.year, today.month) == (year, number) else 0,
        public_missing=public_missing,
        may_edit=g.member["role"] in ("owner", "admin"),
    )


@bp.route("/calendario/<int:event_id>")
@login_required
def show(event_id: int):
    row = _load(event_id)
    return render_template("event.html", event=row,
                           body_html=to_html(row["body_md"] or ""),
                           when=_spoken_date(row["starts_on"]),
                           may_edit=g.member["role"] in ("owner", "admin"))


@bp.route("/calendario/<int:event_id>.ics")
@login_required
def ics(event_id: int):
    """The same courtesy the public events get, behind the login.

    Hand-written rather than through a library for the same reason the rest of
    this app is: it is fifteen lines of a format that has not changed since
    1998, and the alternative is a dependency to track for them.
    """
    row = _load(event_id)
    start = row["starts_on"].replace("-", "")
    if row["starts_at"]:
        when = f"DTSTART;TZID=Europe/Vienna:{start}T{row['starts_at'].replace(':', '')}00"
    else:
        when = f"DTSTART;VALUE=DATE:{start}"

    def escaped(text: str) -> str:
        return re.sub(r"([,;\\])", r"\\\1", (text or "").replace("\n", " "))

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Viena Latina//calendario//ES", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        # Stable and unique, so adding it twice updates one entry rather than
        # making two.
        f"UID:evento-{row['id']}@vienalatina.com",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        when,
        f"SUMMARY:{escaped(row['title'])}",
    ]
    if row["location"]:
        lines.append(f"LOCATION:{escaped(row['location'])}")
    if row["body_md"]:
        lines.append(f"DESCRIPTION:{escaped(row['body_md'])[:500]}")
    lines += ["END:VEVENT", "END:VCALENDAR"]

    return Response(
        "\r\n".join(lines) + "\r\n",
        mimetype="text/calendar",
        headers={"Content-Disposition":
                 f'attachment; filename="evento-{row["id"]}.ics"'},
    )


# --- writing them, which only admins do -----------------------------------

@bp.route("/calendario/nuevo", methods=["GET", "POST"])
@admin_required
def new():
    if request.method == "GET":
        return render_template("event_form.html", event=None,
                               suggested=request.args.get("dia", ""))

    fields, errors = _read_form()
    if errors:
        return _back(None, fields, errors)

    photo_name = _store_picture()
    cursor = get_db().execute(
        """INSERT INTO events (title, body_md, starts_on, starts_at, location,
                               photo_name, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (fields["title"], fields["body"], fields["starts_on"], fields["starts_at"],
         fields["location"], photo_name, g.member["id"]),
    )
    flash("Evento añadido al calendario.", "ok")
    return redirect(url_for("events.show", event_id=cursor.lastrowid))


@bp.route("/calendario/<int:event_id>/editar", methods=["GET", "POST"])
@admin_required
def edit(event_id: int):
    row = _load(event_id)
    if request.method == "GET":
        return render_template("event_form.html", event=row, suggested="")

    fields, errors = _read_form()
    if errors:
        return _back(row, fields, errors)

    photo_name = row["photo_name"]
    staged = _store_picture()
    if staged:
        if photo_name:
            uploads.remove(photo_name)
        photo_name = staged

    get_db().execute(
        """UPDATE events SET title = ?, body_md = ?, starts_on = ?, starts_at = ?,
                             location = ?, photo_name = ?, updated_at = datetime('now')
            WHERE id = ?""",
        (fields["title"], fields["body"], fields["starts_on"], fields["starts_at"],
         fields["location"], photo_name, event_id),
    )
    flash("Evento actualizado.", "ok")
    return redirect(url_for("events.show", event_id=event_id))


@bp.route("/calendario/<int:event_id>/eliminar", methods=["POST"])
@admin_required
def delete(event_id: int):
    row = _load(event_id)
    get_db().execute("DELETE FROM events WHERE id = ?", (event_id,))
    if row["photo_name"]:
        uploads.remove(row["photo_name"])
    flash(f"«{row['title']}» ya no está en el calendario.", "ok")
    return redirect(url_for("events.month", mes=row["starts_on"][:7]))


@bp.route("/calendario/foto/<stored_name>")
@login_required
def photo(stored_name: str):
    """Behind the login, like the calendar it belongs to."""
    if not uploads.STORED_NAME.match(stored_name):
        abort(404)
    row = get_db().execute(
        "SELECT 1 FROM events WHERE photo_name = ?", (stored_name,)).fetchone()
    if row is None:
        abort(404)
    from flask import send_from_directory
    return send_from_directory(uploads.directory(), stored_name)


# --- helpers --------------------------------------------------------------

def _load(event_id: int):
    row = get_db().execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _spoken_date(stamp: str) -> str:
    when = datetime.strptime(stamp, "%Y-%m-%d").date()
    return f"{when.day} de {MONTH_NAMES[when.month - 1]} de {when.year}"


def _read_form() -> tuple[dict, list[str]]:
    errors = []
    fields = {
        "title": request.form.get("title", "").strip()[:TITLE_MAX],
        "body": request.form.get("body", "").strip()[:BODY_MAX],
        "location": request.form.get("location", "").strip()[:LOCATION_MAX],
        "starts_on": "",
        "starts_at": "",
    }
    if not fields["title"]:
        errors.append("El evento necesita un título.")

    try:
        fields["starts_on"] = datetime.strptime(
            request.form.get("starts_on", "").strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        errors.append("La fecha debe tener el formato AAAA-MM-DD.")

    at = request.form.get("starts_at", "").strip()
    if at and not TIME.match(at):
        errors.append("La hora debe ser HH:MM, de 00:00 a 23:59.")
    else:
        fields["starts_at"] = at or None

    return fields, errors


def _store_picture():
    """One picture, on disk beside the rest, with no `attachments` row — the same
    arrangement a profile photo and a proposal's picture already use."""
    try:
        staged = uploads.stage(request.files.getlist("picture"))
    except uploads.RejectedUpload as exc:
        flash(str(exc), "error")
        return None
    if not staged:
        return None
    item = staged[0]
    uploads.directory().joinpath(item["stored_name"]).write_bytes(item["data"])
    return item["stored_name"]


def _back(event, fields, errors):
    for message in errors:
        flash(message, "error")
    return render_template("event_form.html", event=event, fields=fields,
                           suggested=""), 400
