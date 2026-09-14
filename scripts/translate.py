#!/usr/bin/env python3
"""Self-hosted translation step for Woodpecker CI.

Replaces the DeepL API call this script used to make, and restores the
multi-source behaviour of the original WordPress plugin: any of the three site
languages may be the authored original, and the other two are generated from
it. Translation runs on a model shipped inside the pipeline image, so there is
no API key, no quota and no third-party request.

Loop prevention, which was structural back when only Spanish could be a source:
  * a generated sibling carries `translated_from`, and a file carrying it is
    never itself treated as a source;
  * the bot's own commits carry [skip-translate] and are skipped outright.

`manual_translation: true` means "hands off", on both sides:
  * on an authored source — do not generate siblings for this post at all;
  * on a generated sibling — never overwrite it again.

Failure behaviour: any translation error exits non-zero, the pipeline goes red,
and nothing partial is committed — no half-translated sets.

Environment:
  MT_MODEL_DIR / MT_TOKENIZER / MT_COMPUTE_TYPE / MT_THREADS   see translation/
  CI_COMMIT_SHA / CI_PREV_COMMIT_SHA / CI_COMMIT_MESSAGE       from Woodpecker
  GITEA_PUSH_TOKEN                                             bot push token

Dependencies: ctranslate2, transformers, sentencepiece, sentencex, pyyaml
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

import yaml

from translation.ctranslate_provider import CTranslate2Provider
from translation.markdown import translate_markdown, translate_text
from translation.provider import SITE_LANGS

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = REPO_ROOT / "content"

# Frontmatter strings translated alongside the body. Note `categories` is
# deliberately absent: the taxonomy terms stay Spanish in every language, or
# Hugo would fork the taxonomy per language.
TRANSLATED_KEYS = ("title", "description")

BOT_NAME = "vienalatina-translations"
BOT_EMAIL = "translations@vienalatina.com"
SKIP_MARKER = "[skip-translate]"


def run(*args: str, check: bool = True) -> str:
    result = subprocess.run(args, cwd=REPO_ROOT, check=check, capture_output=True, text=True)
    return result.stdout.strip()


def split_lang(path: Path) -> tuple[str, str] | None:
    """('mi-articulo', 'es') for mi-articulo.es.md, else None."""
    if path.suffix != ".md":
        return None
    stem = path.name[: -len(".md")]
    for lang in SITE_LANGS:
        if stem.endswith(f".{lang}"):
            return stem[: -(len(lang) + 1)], lang
    return None


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def join_frontmatter(fm: dict, body: str) -> str:
    front = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{front}---\n\n{body.lstrip()}"


def read_frontmatter(path: Path) -> dict:
    if not path.exists():
        return {}
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return fm


def is_generated(fm: dict) -> bool:
    return bool(fm.get("translated_from"))


def is_frozen(fm: dict) -> bool:
    return bool(fm.get("manual_translation"))


def changed_markdown() -> list[Path]:
    """Content files touched by the pushed commits."""
    head = os.environ.get("CI_COMMIT_SHA", "HEAD")
    prev = os.environ.get("CI_PREV_COMMIT_SHA", "")
    if prev and not set(prev) <= {"0"}:
        out = run("git", "diff", "--name-only", "--diff-filter=AM", prev, head)
    elif run("git", "rev-list", "--count", "HEAD") != "1":
        out = run("git", "diff", "--name-only", "--diff-filter=AM", "HEAD~1", "HEAD")
    else:  # first commit in the repo
        out = run("git", "ls-files")

    paths = []
    for line in out.splitlines():
        rel = Path(line.strip())
        if rel.parts[:1] == ("content",) and (REPO_ROOT / rel).exists():
            paths.append(REPO_ROOT / rel)
    return paths


def authored_sources(paths: list[Path]) -> list[tuple[Path, str, str]]:
    """(path, basename, lang) for files that may act as a translation source."""
    sources = []
    for path in paths:
        parsed = split_lang(path)
        if not parsed:
            continue
        basename, lang = parsed
        fm = read_frontmatter(path)
        if is_generated(fm):
            continue  # machine output is never a source
        if is_frozen(fm):
            print(f"{path.relative_to(REPO_ROOT)}: manual_translation=true — not translated")
            continue
        sources.append((path, basename, lang))
    return sources


def missing_siblings() -> list[Path]:
    """Every authored source missing at least one sibling."""
    incomplete = []
    for path in sorted(CONTENT_DIR.rglob("*.md")):
        parsed = split_lang(path)
        if not parsed:
            continue
        basename, lang = parsed
        if is_generated(read_frontmatter(path)):
            continue
        for target in SITE_LANGS:
            if target != lang and not path.with_name(f"{basename}.{target}.md").exists():
                incomplete.append(path)
                break
    return incomplete


def translate_file(source: Path, basename: str, src_lang: str, provider) -> list[Path]:
    fm, body = split_frontmatter(source.read_text(encoding="utf-8"))
    written: list[Path] = []

    for tgt in SITE_LANGS:
        if tgt == src_lang:
            continue
        target = source.with_name(f"{basename}.{tgt}.md")
        target_fm = read_frontmatter(target)
        if is_frozen(target_fm):
            print(f"  {target.relative_to(REPO_ROOT)}: manual_translation=true — skipped")
            continue

        new_fm = dict(fm)
        for key in TRANSLATED_KEYS:
            if fm.get(key):
                new_fm[key] = translate_text(str(fm[key]), src_lang, tgt, provider)
        new_fm["lang"] = tgt
        new_fm["translated_from"] = src_lang
        new_fm["manual_translation"] = False

        # Never inherit the source's slug: that would move a migrated sibling's
        # URL onto the source's and break inbound links. Keep the slug the
        # target already had; otherwise let Hugo fall back to the filename.
        new_fm.pop("slug", None)
        if target_fm.get("slug"):
            new_fm["slug"] = target_fm["slug"]

        target.write_text(
            join_frontmatter(new_fm, translate_markdown(body, src_lang, tgt, provider)),
            encoding="utf-8",
        )
        written.append(target)
        print(f"  {target.relative_to(REPO_ROOT)}: written")

    return written


def commit_and_push(files: list[Path]) -> None:
    branch = os.environ.get("CI_COMMIT_BRANCH", "main")
    run("git", "config", "user.name", BOT_NAME)
    run("git", "config", "user.email", BOT_EMAIL)
    token = os.environ.get("GITEA_PUSH_TOKEN", "")
    repo = os.environ.get("CI_REPO", "pablo/vienalatina")
    if token:  # clone credentials are read-only; pushing needs the bot token
        run("git", "remote", "set-url", "origin",
            f"https://{BOT_NAME}:{token}@git.vienalatina.com/{repo}.git")
    run("git", "add", *[str(f) for f in files])
    if not run("git", "status", "--porcelain"):
        print("Translations identical to committed siblings — nothing to push.")
        return
    run("git", "commit", "-m", f"translate: update generated siblings {SKIP_MARKER}")
    run("git", "push", "origin", f"HEAD:{branch}")
    print(f"Pushed sibling commit to {branch}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate translated content siblings.")
    parser.add_argument("--backfill", action="store_true",
                        help="translate every source missing siblings, not just changed files")
    parser.add_argument("--no-push", action="store_true",
                        help="write siblings but do not commit or push (local testing)")
    args = parser.parse_args()

    if SKIP_MARKER in os.environ.get("CI_COMMIT_MESSAGE", ""):
        print(f"{SKIP_MARKER} commit — nothing to translate.")
        return

    paths = missing_siblings() if args.backfill else changed_markdown()
    sources = authored_sources(paths)
    if not sources:
        print("No authored content changed — nothing to translate.")
        return

    provider = CTranslate2Provider()
    written: list[Path] = []
    for source, basename, lang in sources:
        print(f"Translating {source.relative_to(REPO_ROOT)} (from {lang}):")
        written.extend(translate_file(source, basename, lang, provider))

    if not written:
        print("Every sibling is frozen — nothing written.")
        return
    if args.no_push:
        print(f"--no-push: wrote {len(written)} files, leaving them uncommitted.")
        return
    commit_and_push(written)


if __name__ == "__main__":
    main()
