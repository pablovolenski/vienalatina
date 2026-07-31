#!/usr/bin/env python3
"""Async DeepL translation step for Woodpecker CI.

Replaces the synchronous `save_post` hook from the WordPress plugin
(plataforma_deepl_translate in plugin/plataforma-social/plataforma-social.php).

For each *.es.md changed in the pushed commit range, calls DeepL with
tag_handling=html (markup survives translation) plus a glossary of
community-specific terms, and writes the *.de.md and *.pt-br.md siblings
next to the source. Sibling files carrying `manual_translation: true` in
their frontmatter are never overwritten. The siblings are committed back
to the same branch as a bot commit, then the pipeline builds and deploys.

Failure behaviour: any DeepL error exits non-zero, the pipeline goes red,
and nothing partial is committed — no half-translated sets.

Environment:
  DEEPL_API_KEY   required (Woodpecker secret)
  DEEPL_API_URL   optional, defaults to the free-tier endpoint
  CI_COMMIT_SHA / CI_PREV_COMMIT_SHA   provided by Woodpecker

Dependencies: requests, pyyaml
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEEPL_URL = os.environ.get("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")

# Language slug -> DeepL target code (ported from the plugin's $lang_map).
TARGETS = {
    "de": "DE",
    "pt-br": "PT-BR",
}
SOURCE_SLUG = "es"
DEEPL_SOURCE = "ES"

# Fixed community-specific terms DeepL must not "translate".
# Sent as ignored tags via tag_handling=html: each term is wrapped in
# <keep>…</keep> before the call and unwrapped after, which pins proper
# nouns without needing a server-side DeepL glossary resource.
PROTECTED_TERMS = [
    "Viena Latina",
    "Grätzl",
    "empanadas de viento",
    "Naschmarkt",
]

# Frontmatter keys whose string values get translated alongside the body.
TRANSLATED_KEYS = ("title", "description")

BOT_NAME = "vienalatina-translations"
BOT_EMAIL = "translations@vienalatina.com"


def run(*args: str, check: bool = True) -> str:
    result = subprocess.run(args, cwd=REPO_ROOT, check=check, capture_output=True, text=True)
    return result.stdout.strip()


def changed_source_files() -> list[Path]:
    """Spanish sources touched by the pushed commits.

    Filtering to *.es.md is what makes bot commits (which only add .de.md /
    .pt-br.md) a no-op round — the re-fire loop the WP hook had to guard
    against with meta flags cannot happen here.
    """
    head = os.environ.get("CI_COMMIT_SHA", "HEAD")
    prev = os.environ.get("CI_PREV_COMMIT_SHA", "")
    if prev and not set(prev) <= {"0"}:
        diff_range = [prev, head]
    else:
        diff_range = ["HEAD~1", "HEAD"] if run("git", "rev-list", "--count", "HEAD") != "1" else None

    if diff_range:
        out = run("git", "diff", "--name-only", "--diff-filter=AM", *diff_range)
    else:  # very first commit in the repo: translate everything
        out = run("git", "ls-files")

    files = []
    for line in out.splitlines():
        p = Path(line.strip())
        if p.suffix == ".md" and p.name.endswith(f".{SOURCE_SLUG}.md") and p.parts[:1] == ("content",):
            full = REPO_ROOT / p
            if full.exists():
                files.append(full)
    return files


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def join_frontmatter(fm: dict, body: str) -> str:
    front = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{front}---\n\n{body.lstrip()}"


def protect(text: str) -> str:
    for term in PROTECTED_TERMS:
        text = re.sub(re.escape(term), lambda m: f"<keep>{m.group(0)}</keep>", text, flags=re.IGNORECASE)
    return text


def unprotect(text: str) -> str:
    return re.sub(r"</?keep>", "", text)


def deepl_translate(texts: list[str], target: str, html: bool) -> list[str]:
    """Direct port of plataforma_deepl_translate(): same endpoint, same
    tag_handling=html behaviour, but errors abort the pipeline instead of
    silently shipping a half-translated post."""
    texts = [t for t in texts if isinstance(t, str) and t.strip()]
    if not texts:
        return []

    key = os.environ.get("DEEPL_API_KEY", "")
    if not key:
        sys.exit("DEEPL_API_KEY is not set — configure the Woodpecker secret.")

    data: list[tuple[str, str]] = [
        ("target_lang", target),
        ("source_lang", DEEPL_SOURCE),
        ("tag_handling", "html"),
        ("ignore_tags", "keep"),
    ]
    if not html:
        # Titles/descriptions are plain strings; still use tag handling so
        # <keep> protection works, DeepL just has no other tags to preserve.
        pass
    for t in texts:
        data.append(("text", protect(t)))

    resp = requests.post(
        DEEPL_URL,
        headers={"Authorization": f"DeepL-Auth-Key {key}"},
        data=data,
        timeout=60,
    )
    if resp.status_code != 200:
        sys.exit(f"DeepL HTTP {resp.status_code}: {resp.text[:300]}")

    return [unprotect(item["text"]) for item in resp.json().get("translations", [])]


def sibling_path(source: Path, slug: str) -> Path:
    return source.with_name(source.name.replace(f".{SOURCE_SLUG}.md", f".{slug}.md"))


def is_frozen(path: Path) -> bool:
    if not path.exists():
        return False
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return bool(fm.get("manual_translation"))


def translate_file(source: Path) -> list[Path]:
    raw = source.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    written = []

    for slug, deepl_target in TARGETS.items():
        target_file = sibling_path(source, slug)
        if is_frozen(target_file):
            print(f"  {target_file.relative_to(REPO_ROOT)}: manual_translation=true — skipped")
            continue

        strings = [str(fm[k]) for k in TRANSLATED_KEYS if fm.get(k)]
        translated_strings = deepl_translate(strings, deepl_target, html=False)
        translated_body = deepl_translate([body], deepl_target, html=True) if body.strip() else [""]

        new_fm = dict(fm)
        it = iter(translated_strings)
        for k in TRANSLATED_KEYS:
            if fm.get(k):
                new_fm[k] = next(it)
        new_fm["lang"] = slug
        new_fm["manual_translation"] = False
        # Pin the URL to the shared basename so it never drifts when a title
        # is retranslated (plan: /de/mi-articulo/ pairs with /mi-articulo/).
        new_fm.setdefault("slug", source.name.removesuffix(f".{SOURCE_SLUG}.md"))

        target_file.write_text(join_frontmatter(new_fm, translated_body[0] if translated_body else ""), encoding="utf-8")
        written.append(target_file)
        print(f"  {target_file.relative_to(REPO_ROOT)}: written")

    return written


def commit_and_push(files: list[Path]) -> None:
    branch = os.environ.get("CI_COMMIT_BRANCH", "main")
    run("git", "config", "user.name", BOT_NAME)
    run("git", "config", "user.email", BOT_EMAIL)
    token = os.environ.get("GITEA_PUSH_TOKEN", "")
    repo = os.environ.get("CI_REPO", "pablo/vienalatina")
    if token:  # Woodpecker's clone credentials are read-only; push needs its own token
        run("git", "remote", "set-url", "origin", f"https://{BOT_NAME}:{token}@git.vienalatina.com/{repo}.git")
    run("git", "add", *[str(f) for f in files])
    if not run("git", "status", "--porcelain"):
        print("Translations identical to committed siblings — nothing to push.")
        return
    run("git", "commit", "-m", "translate: update DE and PT-BR siblings [skip-translate]")
    run("git", "push", "origin", f"HEAD:{branch}")
    print(f"Pushed sibling commit to {branch}.")


def main() -> None:
    sources = changed_source_files()
    if not sources:
        print("No changed *.es.md files — nothing to translate.")
        return

    all_written: list[Path] = []
    for source in sources:
        print(f"Translating {source.relative_to(REPO_ROOT)}:")
        all_written.extend(translate_file(source))

    if all_written:
        commit_and_push(all_written)


if __name__ == "__main__":
    main()
