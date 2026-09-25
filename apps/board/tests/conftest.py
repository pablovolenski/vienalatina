"""Shared fixtures.

Tests drive the app through Flask's test client rather than poking functions
directly, because most of what is worth checking here is a decision about who
may do what — and that decision is only real once it has survived routing,
the session and the CSRF hook.

Sign-in is faked by writing the session cookie: the OAuth round trip belongs to
Gitea, and the tests that care about it stub the two network calls instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from apps.board.app import create_app  # noqa: E402
from apps.board.db import connect  # noqa: E402


@pytest.fixture
def app(tmp_path):
    application = create_app({
        "SECRET_KEY": "test-secret",
        "DB_PATH": str(tmp_path / "board.db"),
        "OWNER_LOGIN": "owner",
        "SESSION_COOKIE_SECURE": False,
        "COOLDOWN_SECONDS": 0,
        "BASE_URL": "http://localhost",
        "OAUTH_CLIENT_ID": "cid",
        "OAUTH_CLIENT_SECRET": "secret",
        "ADMIN_TOKEN": "admintoken",
        "TESTING": True,
    })
    yield application


@pytest.fixture
def db(app):
    conn = connect(app.config["DB_PATH"])
    yield conn
    conn.close()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_member(db):
    """Insert a member and return its id. The owner already exists, seeded."""
    def _make(login, role="user", active=1):
        cursor = db.execute(
            """INSERT INTO members (gitea_login, display_name, email, role, active)
               VALUES (?, ?, ?, ?, ?)""",
            (login, login.title(), f"{login}@example.com", role, active),
        )
        return cursor.lastrowid
    return _make


@pytest.fixture
def owner_id(db):
    return db.execute("SELECT id FROM members WHERE role = 'owner'").fetchone()["id"]


@pytest.fixture
def sign_in(client):
    def _sign_in(member_id):
        with client.session_transaction() as session:
            session["member_id"] = member_id
            session["csrf"] = "token-for-tests"
    return _sign_in


@pytest.fixture
def post(client):
    """POST with a valid CSRF token, so tests exercise authorisation rather
    than repeatedly rediscovering that the CSRF hook works."""
    def _post(url, data=None, **kwargs):
        # A real anonymous visitor gets a CSRF token when the form renders, so
        # signed-out pages (invitations, password recovery) need one here too —
        # otherwise every such test fails on the hook rather than on its subject.
        with client.session_transaction() as session:
            session.setdefault("csrf", "token-for-tests")
        payload = dict(data or {})
        payload.setdefault("csrf_token", "token-for-tests")
        return client.post(url, data=payload, **kwargs)
    return _post
