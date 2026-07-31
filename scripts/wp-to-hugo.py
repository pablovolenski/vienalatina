#!/usr/bin/env python3
"""One-shot WordPress → Hugo content migration (disposable).

Pulls every post from the WP REST API (with Polylang language codes),
converts HTML bodies to Markdown, downloads images into static/uploads/,
rewrites their URLs, and writes content/post/<slug>.<lang>.md files whose
basenames pair Polylang siblings the way Hugo expects.

Usage:
    pip install requests html2text
    python scripts/wp-to-hugo.py https://vielac.at

Existing Spanish slugs are preserved so inbound links keep resolving after
the DNS cutover (add Caddy 301s for anything that doesn't map cleanly).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import html2text
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = REPO_ROOT / "content" / "post"
UPLOADS_DIR = REPO_ROOT / "static" / "uploads"

# Polylang slug -> Hugo language slug used in filenames.
LANG_MAP = {"es": "es", "de": "de", "pt-br": "pt-br", "pt": "pt-br"}


def fetch_all(base_url: str, endpoint: str) -> list[dict]:
    items, page = [], 1
    while True:
        resp = requests.get(
            f"{base_url}/wp-json/wp/v2/{endpoint}",
            params={"per_page": 100, "page": page, "_embed": "1", "lang": ""},
            timeout=30,
        )
        if resp.status_code == 400:  # past the last page
            break
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        if page >= int(resp.headers.get("X-WP-TotalPages", 1)):
            break
        page += 1
    return items


def download_image(url: str) -> str:
    """Fetch a WP upload into static/uploads/, return its new site-relative URL."""
    name = Path(urlparse(url).path).name
    if not name:
        return url
    dest = UPLOADS_DIR / name
    if not dest.exists():
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"  ! image fetch failed ({resp.status_code}): {url}")
            return url
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
    return f"/uploads/{name}"


def rewrite_images(html: str, base_url: str) -> str:
    pattern = re.escape(base_url.rstrip("/")) + r"/wp-content/uploads/[^\s\"'<>)]+"
    return re.sub(pattern, lambda m: download_image(m.group(0)), html)


def to_markdown(html: str) -> str:
    conv = html2text.HTML2Text()
    conv.body_width = 0
    conv.protect_links = True
    return conv.handle(html).strip()


def frontmatter(post: dict, lang: str, categories: dict[int, str]) -> str:
    title = to_markdown(post["title"]["rendered"]).replace('"', '\\"')
    cats = [categories[c] for c in post.get("categories", []) if c in categories]
    lines = [
        "---",
        f'title: "{title}"',
        f"date: {post['date']}",
        f"slug: {post['slug']}",   # preserve the exact WP slug per language
        f"lang: {lang}",
        "manual_translation: false",
        f"categories: [{', '.join(cats)}]",
    ]
    excerpt = to_markdown(post.get("excerpt", {}).get("rendered", ""))
    if excerpt:
        lines.append(f'description: "{excerpt[:300].replace(chr(34), chr(39))}"')
    embedded = post.get("_embedded", {}).get("wp:featuredmedia", [])
    if embedded and embedded[0].get("source_url"):
        lines.append(f"image: {download_image(embedded[0]['source_url'])}")
    lines.append("---")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/wp-to-hugo.py https://your-wp-site.example")
    base_url = sys.argv[1].rstrip("/")

    categories = {c["id"]: c["name"] for c in fetch_all(base_url, "categories")}
    posts = fetch_all(base_url, "posts")
    print(f"Fetched {len(posts)} posts, {len(categories)} categories.")

    by_id = {p["id"]: p for p in posts}
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    written = 0

    for post in posts:
        lang = LANG_MAP.get(post.get("lang", "es"))
        if not lang:
            continue

        # Polylang exposes sibling IDs in `translations`; use the Spanish
        # sibling's slug as the shared basename so Hugo pairs the set.
        translations = post.get("translations", {})
        es_id = translations.get("es", post["id"])
        basename = by_id.get(es_id, post)["slug"]

        body_html = rewrite_images(post["content"]["rendered"], base_url)
        md = frontmatter(post, lang, categories) + "\n\n" + to_markdown(body_html) + "\n"

        out = POSTS_DIR / f"{basename}.{lang}.md"
        out.write_text(md, encoding="utf-8")
        written += 1
        print(f"  {out.relative_to(REPO_ROOT)}")

    print(f"Wrote {written} files. Spot-check ~10 before committing (formatting fidelity).")


if __name__ == "__main__":
    main()
