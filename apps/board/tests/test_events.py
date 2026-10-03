"""Two calendars that must not be confused with each other.

The public one is posts with the Evento category: published, translated,
indexed, shareable. The private one is rows in this database: admins write them,
members read them, and nothing about them reaches the public site. Most of what
is worth testing here is that boundary, and the handful of date rules that are
easy to get subtly wrong and hard to notice.
"""

from __future__ import annotations

from datetime import date

import pytest

from apps.board import events, gitea


@pytest.fixture
def quiet_gitea(monkeypatch):
    """The private calendar asks the git server for the published events. These
    tests are about the private half, so it answers with nothing."""
    monkeypatch.setattr(gitea, "list_directory", lambda *a, **k: [])


@pytest.fixture
def an_event(post, owner_id, sign_in, quiet_gitea):
    def _add(title="Asamblea", starts_on="2026-10-24", **extra):
        sign_in(owner_id)
        data = {"title": title, "starts_on": starts_on}
        data.update(extra)
        post("/comunidad/calendario/nuevo", data)
        return title
    return _add


# --- who writes, who reads ------------------------------------------------

def test_an_admin_adds_an_event_and_every_member_sees_it(
        client, an_event, make_member, sign_in):
    an_event()

    sign_in(make_member("maria"))
    page = client.get("/comunidad/calendario?mes=2026-10").get_data(as_text=True)

    assert "Asamblea" in page
    assert "cal__add" not in page            # no + on the days
    assert "Añadir evento" not in page       # and no button


def test_a_member_cannot_reach_the_form_or_the_handler(client, post, make_member, sign_in):
    sign_in(make_member("maria"))

    assert client.get("/comunidad/calendario/nuevo").status_code == 403
    assert post("/comunidad/calendario/nuevo",
                {"title": "Mío", "starts_on": "2026-10-24"}).status_code == 403


def test_a_moderator_cannot_either(client, make_member, sign_in):
    """Moderating what the public reads is not the same as putting something in
    the association's diary."""
    sign_in(make_member("luisa", role="moderator"))
    assert client.get("/comunidad/calendario/nuevo").status_code == 403


def test_the_calendar_itself_is_behind_the_login(client):
    response = client.get("/comunidad/calendario")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


# --- the month -----------------------------------------------------------

def test_the_month_shows_its_own_events_and_not_the_neighbours(
        client, an_event, owner_id, sign_in):
    an_event("Asamblea", "2026-10-24")
    an_event("Ensayo", "2026-11-03")

    sign_in(owner_id)
    october = client.get("/comunidad/calendario?mes=2026-10").get_data(as_text=True)

    assert "Asamblea" in october
    assert "Ensayo" not in october


def test_the_arrows_cross_the_year(client, an_event, owner_id, sign_in):
    sign_in(owner_id)
    page = client.get("/comunidad/calendario?mes=2026-12").get_data(as_text=True)

    assert "mes=2027-01" in page
    assert "mes=2026-11" in page


def test_shifting_months_wraps_properly():
    assert events.shift(2026, 12, 1) == (2027, 1)
    assert events.shift(2026, 1, -1) == (2025, 12)
    assert events.shift(2026, 6, 0) == (2026, 6)


def test_a_mistyped_month_shows_today_rather_than_an_error(
        client, owner_id, sign_in, quiet_gitea):
    """A calendar that refuses to draw because a query string was mistyped is
    worse than one that shows now."""
    sign_in(owner_id)
    today = date.today()

    page = client.get("/comunidad/calendario?mes=pumpkin")

    assert page.status_code == 200
    assert events.month_label(today.year, today.month) in page.get_data(as_text=True)


def test_february_in_a_leap_year_has_twenty_nine_days(client, owner_id, sign_in, quiet_gitea):
    sign_in(owner_id)
    page = client.get("/comunidad/calendario?mes=2028-02").get_data(as_text=True)
    assert ">29<" in page
    assert ">30<" not in page


# --- what the git server does to this ------------------------------------

def test_the_private_events_survive_a_git_server_that_is_down(
        client, post, owner_id, sign_in, monkeypatch):
    """The half that only exists here must not be taken down by the half that
    does not. A refused connection reaches this module as a GiteaError because
    gitea.py translates it — before that it was a bare OSError and this page
    would have 500ed."""
    import requests
    sign_in(owner_id)
    post("/comunidad/calendario/nuevo", {"title": "Asamblea", "starts_on": "2026-10-24"})

    def refuse(*args, **kwargs):
        raise requests.ConnectionError("nobody home")
    monkeypatch.setattr(gitea.requests, "request", refuse)

    page = client.get("/comunidad/calendario?mes=2026-10")

    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Asamblea" in body
    assert "faltan los eventos del sitio" in body


# --- the .ics -------------------------------------------------------------

def test_the_ics_is_a_calendar_a_phone_understands(client, an_event, owner_id, sign_in):
    an_event("Asamblea", "2026-10-24", starts_at="19:00",
             location="Local de la asociación")
    sign_in(owner_id)

    response = client.get("/comunidad/calendario/1.ics")

    assert response.mimetype == "text/calendar"
    body = response.get_data(as_text=True)
    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert body.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;TZID=Europe/Vienna:20261024T190000" in body
    assert "SUMMARY:Asamblea" in body
    assert "UID:evento-1@vienalatina.com" in body      # stable, so re-adding updates


def test_an_event_with_no_time_lasts_the_whole_day(client, an_event, owner_id, sign_in):
    """VALUE=DATE rather than inventing midnight, which is how a calendar shows
    it as a band across the top of the day."""
    an_event("Jornada", "2026-10-24")
    sign_in(owner_id)

    body = client.get("/comunidad/calendario/1.ics").get_data(as_text=True)

    assert "DTSTART;VALUE=DATE:20261024" in body


def test_a_comma_in_the_place_is_escaped(client, an_event, owner_id, sign_in):
    """An unescaped comma in LOCATION makes the rest of the line a second
    value, and the address arrives truncated."""
    an_event("Asamblea", "2026-10-24", location="Plaza de Ottakring, junto al mercado")
    sign_in(owner_id)

    body = client.get("/comunidad/calendario/1.ics").get_data(as_text=True)

    assert "LOCATION:Plaza de Ottakring\\, junto al mercado" in body


def test_the_ics_needs_a_session(client, an_event):
    an_event()
    # The fixture had to sign in to create it; this is about the visitor who
    # finds the URL afterwards.
    with client.session_transaction() as session:
        session.clear()

    response = client.get("/comunidad/calendario/1.ics")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


# --- refusals -------------------------------------------------------------

def test_an_event_without_a_date_is_refused(client, db, post, owner_id, sign_in, quiet_gitea):
    sign_in(owner_id)

    response = post("/comunidad/calendario/nuevo", {"title": "Algún día"})

    assert response.status_code == 400
    assert db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0


def test_a_nonsense_time_is_refused(client, db, post, owner_id, sign_in, quiet_gitea):
    sign_in(owner_id)

    post("/comunidad/calendario/nuevo",
         {"title": "Asamblea", "starts_on": "2026-10-24", "starts_at": "25:99"})

    assert db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0


# --- erasure --------------------------------------------------------------

def test_erasing_the_admin_leaves_the_event_standing(
        db, post, an_event, owner_id, make_member, sign_in, quiet_gitea):
    """The meeting still happens after they leave: the event belongs to the
    association, so the row stays and the name goes."""
    admin = make_member("admina", role="admin")
    sign_in(admin)
    post("/comunidad/calendario/nuevo", {"title": "Asamblea", "starts_on": "2026-10-24"})

    sign_in(owner_id)
    post(f"/comunidad/miembros/{admin}/eliminar")

    row = db.execute("SELECT title, created_by FROM events").fetchone()
    assert (row["title"], row["created_by"]) == ("Asamblea", None)
