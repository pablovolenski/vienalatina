"""Guards on the gap between what the app reads and what the server hands it.

This is the one class of bug the rest of the suite cannot see. Every test here
runs against a Flask config built in-process, so a setting can be documented in
`.env.example`, read in `app.py`, and still reach the container as nothing at
all — because Compose does not pass `.env` to a container, it only substitutes
into the compose file. The symptom is a feature reporting itself unconfigured
with the settings sitting right there on disk, which is a miserable thing to
debug from the outside.
"""

from __future__ import annotations

import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[3] / "infra" / "board"
ENV_EXAMPLE = INFRA / ".env.example"
COMPOSE = INFRA / "docker-compose.yml"

SETTING = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def documented() -> set[str]:
    return set(SETTING.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def test_every_documented_setting_reaches_the_container():
    compose = COMPOSE.read_text(encoding="utf-8")
    missing = sorted(
        name for name in documented()
        if f"${{{name}}}" not in compose and f"${{{name}:-" not in compose
    )
    assert not missing, (
        "these are in .env.example but never substituted into "
        f"docker-compose.yml, so the container never sees them: {missing}"
    )


def test_the_mail_settings_are_optional_at_the_compose_level():
    """`${MAIL_HOST}` without a default makes Compose warn on every command for
    a server that has deliberately not configured mail. Running without it is a
    supported state, so it must be a quiet one."""
    compose = COMPOSE.read_text(encoding="utf-8")
    for name in sorted(n for n in documented() if n.startswith("MAIL_")):
        assert f"${{{name}:-" in compose, f"{name} has no default in docker-compose.yml"
