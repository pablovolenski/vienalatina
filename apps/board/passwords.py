"""Storing and checking passwords, now that they are ours to keep.

Until this existed the members area had no passwords: it asked another server
whether somebody was who they said, which is why signing in meant leaving
vienalatina.com and why signing out could never finish. Taking the job back
means taking the responsibility with it, and the two things that go wrong are
both in here: how the hash is made, and how many guesses a stranger gets.

Hashing is `werkzeug.security`, which arrives with Flask — no new dependency,
and BSD-3, which matters for a platform meant to be resold. Its default is
scrypt with sensible parameters. Deliberately not a hand-rolled `hashlib`
call: the parameters, the salting and the constant-time comparison are exactly
the details worth not inventing.
"""

from __future__ import annotations

from datetime import timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

MINIMUM = 10

# Guessing budget. Generous enough that nobody typing their own password badly
# notices, small enough that a list of common passwords is not worth running.
ATTEMPT_WINDOW = timedelta(minutes=15)
ATTEMPT_LIMIT = 10

# Compared against when there is no such member, so that a wrong username and a
# wrong password take the same time to answer. Without it the reply comes back
# measurably faster for a name that does not exist, and the login form becomes
# a way to find out who is a member. The value is a real scrypt hash of a
# string nobody will guess; what it hashes is irrelevant.
_DECOY = generate_password_hash("no-such-member-" + "x" * 32)


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify(stored: str | None, password: str) -> bool:
    """Check a password, spending the same time when there is nothing to check.

    A member with no password yet — invited but never arrived — has NULL here.
    That must never be treated as "matches anything", and it must not answer
    faster than a real failure either.
    """
    if not stored:
        check_password_hash(_DECOY, password)
        return False
    return check_password_hash(stored, password)


def record_attempt(identifier: str) -> None:
    get_db().execute(
        "INSERT INTO login_attempts (identifier) VALUES (?)", (identifier.lower(),)
    )


def too_many_attempts(identifier: str) -> bool:
    """Counted inside SQLite, for the reason written up in invites.py.

    `created_at` is written by SQLite's own `datetime('now')` and compared
    against SQLite's own clock. Handing it a Python timestamp instead puts two
    formats on either side of a string comparison, and the limit silently never
    fires — which is how the reset limiter was broken before anybody noticed.
    """
    minutes = int(ATTEMPT_WINDOW.total_seconds() // 60)
    row = get_db().execute(
        """SELECT COUNT(*) AS n FROM login_attempts
            WHERE identifier = ? AND created_at > datetime('now', ?)""",
        (identifier.lower(), f"-{minutes} minutes"),
    ).fetchone()
    return row["n"] >= ATTEMPT_LIMIT


def forget_attempts(identifier: str) -> None:
    """Called on a successful sign-in, so a member who mistyped four times and
    then got it right does not carry those four into the next hour."""
    get_db().execute("DELETE FROM login_attempts WHERE identifier = ?",
                     (identifier.lower(),))


def prune_attempts() -> None:
    """Old rows are of no interest to anyone and are a small record of who
    tried to sign in and when. Dropped on each successful login rather than by
    a scheduled job, because there is no scheduler here."""
    get_db().execute(
        "DELETE FROM login_attempts WHERE created_at < datetime('now', '-1 day')")
