"""Shared fixtures.

Tests drive the app through Flask's test client rather than poking functions
directly, because most of what is worth checking here is a decision about who
may do what — and that decision is only real once it has survived routing,
the session and the CSRF hook.

Sign-in is faked by writing the session cookie: the OAuth round trip belongs to
Gitea, and the tests that care about it stub the two network calls instead.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from apps.board import gitea  # noqa: E402
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
        "ADMIN_TOKEN": "admintoken",
        "UPLOAD_DIR": str(tmp_path / "uploads"),
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
def superadmin_id(db):
    """The account BOARD_OWNER seeds: the superadministrator, who runs the
    platform and passes every check below them."""
    return db.execute(
        "SELECT id FROM members WHERE role = 'superadmin'").fetchone()["id"]


@pytest.fixture
def owner_id(db):
    """A responsable — the association's chair, which is empty on a fresh
    install. Minted here because the two roles are deliberately different
    people, and a test that means one should not reach for the other."""
    return db.execute(
        """INSERT INTO members (gitea_login, display_name, email, role)
           VALUES ('presidenta', 'Presidenta', 'presidenta@example.com', 'owner')"""
    ).lastrowid


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


# --- the git server, as a dictionary --------------------------------------
#
# Shared because two features commit to the content repository now: the editor
# (test_content.py) and the brand (test_brand.py). What matters in both is the
# payload — which files, with which bytes, under whose name — and `requests` can
# be trusted to do HTTP.

class FakeRepo:
    """A repository in a dict, with shas that change when content does."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.commits: list[str] = []
        self.authors: list[dict] = []
        self.reads = 0

    @staticmethod
    def _sha(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    def list_directory(self, path, token=None):
        out = []
        for name, data in self.files.items():
            if name.startswith(path + "/") and "/" not in name[len(path) + 1:]:
                out.append({"name": name.rsplit("/", 1)[1], "path": name,
                            "type": "file", "sha": self._sha(data)})
        return out

    def read_file(self, path, token=None):
        self.reads += 1
        if path not in self.files:
            raise gitea.GiteaError("Ese archivo ya no existe.")
        return self.files[path].decode("utf-8"), self._sha(self.files[path])

    def write_file(self, path, data, message, token=None, sha=None, member=None):
        if sha and self.files.get(path) is not None and self._sha(self.files[path]) != sha:
            raise gitea.StaleFile("Alguien más guardó este archivo mientras lo editabas.")
        self.files[path] = data
        self.commits.append(message)
        self.authors.append(gitea._identity(member))
        return self._sha(data)

    def delete_file(self, path, sha, message, token=None, member=None):
        self.files.pop(path, None)
        self.commits.append(message)


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    for name in ("list_directory", "read_file", "write_file", "delete_file"):
        monkeypatch.setattr(gitea, name, getattr(fake, name))
    return fake


