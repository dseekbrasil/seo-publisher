#!/usr/bin/env python3
"""
Generate a safe, static article page from a submitted GitHub Issue.

This script does NOT touch git or the network. It only:
  1. Parses the four fields out of the GitHub issue-form body.
  2. Validates them (presence, size, URL scheme).
  3. Renders a clean, escaped HTML page to articles/<slug>/index.html.
  4. Updates articles/articles.json (tracking) and articles/index.html (listing).

All untrusted input (title, content, anchor, URL) is HTML-escaped before it
ever reaches the page. Only a small, safe Markdown subset is re-enabled, from
markers the author typed (#, -, **, *), never from raw HTML.

Inputs come from environment variables:
  ISSUE_NUMBER   e.g. "12"
  ISSUE_BODY     the raw issue-form body (GitHub renders fields as "### Label\n\nvalue")
  REPO           e.g. "dseekbrasil/seo-publisher"  (used to build the live URL)

On success, writes machine-readable outputs to the file named by GITHUB_OUTPUT
(if set): slug, live_url, status=published.
On failure, exits non-zero and prints a one-line reason.
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

# ----- limits (reject absurd input early) -----------------------------------
MAX_TITLE = 200
MAX_ANCHOR = 200
MAX_URL = 2000
MAX_CONTENT = 100_000  # ~100 KB of text is plenty for one article

ALLOWED_SCHEMES = {"http", "https"}


# ----- field parsing --------------------------------------------------------
def parse_issue_body(body: str) -> dict:
    """
    GitHub issue forms render the submitted body as repeated blocks:

        ### Field label

        value text (may span multiple lines)

    We split on lines that are exactly '### <label>' and collect the text
    until the next such header. Returns a dict keyed by lowercased label.
    """
    fields = {}
    current = None
    buf = []
    for raw_line in body.splitlines():
        m = re.match(r"^###\s+(.*\S)\s*$", raw_line)
        if m:
            if current is not None:
                fields[current] = "\n".join(buf).strip()
            current = m.group(1).strip().lower()
            buf = []
        elif current is not None:
            buf.append(raw_line)
    if current is not None:
        fields[current] = "\n".join(buf).strip()

    # GitHub writes "_No response_" for empty optional fields.
    for k, v in list(fields.items()):
        if v.strip() == "_No response_":
            fields[k] = ""
    return fields


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:60] or "article"


# ----- safe Markdown subset -> HTML -----------------------------------------
def render_inline(escaped: str) -> str:
    """
    Apply bold/italic to already-HTML-escaped text. The ** and * markers are
    literal characters (never escaped), so operating on escaped text is safe:
    no '<' or '>' from user input survives here.
    """
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def render_content(content: str) -> str:
    """
    Convert the author's Markdown-ish content to safe HTML.
    Supported: #/##/### headings, - or * bullet lists, blank-line paragraphs,
    **bold**, *italic*. Everything is escaped first.
    """
    lines = content.replace("\r\n", "\n").split("\n")
    out = []
    list_open = False
    para = []

    def flush_para():
        nonlocal para
        if para:
            text = render_inline(" ".join(para).strip())
            if text:
                out.append(f"<p>{text}</p>")
            para = []

    def close_list():
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_para()
            close_list()
            continue

        esc = html.escape(stripped)

        h = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if h:
            flush_para()
            close_list()
            level = len(h.group(1)) + 1  # # -> h2, ## -> h3, ### -> h4
            level = min(level, 4)
            inner = render_inline(html.escape(h.group(2).strip()))
            out.append(f"<h{level}>{inner}</h{level}>")
            continue

        li = re.match(r"^[-*]\s+(.*)$", stripped)
        if li:
            flush_para()
            if not list_open:
                out.append("<ul>")
                list_open = True
            inner = render_inline(html.escape(li.group(1).strip()))
            out.append(f"<li>{inner}</li>")
            continue

        para.append(esc)

    flush_para()
    close_list()
    return "\n".join(out)


def insert_anchor(body_html: str, anchor_text: str, url: str, rel_follow: bool) -> tuple[str, bool]:
    """
    Link the first in-text occurrence of the (escaped) anchor text.
    Returns (html, placed_in_text). If the anchor text is not found in the
    body, the caller adds a labelled "Related resource" section instead.
    """
    esc_anchor = html.escape(anchor_text.strip())
    esc_url = html.escape(url, quote=True)
    rel = "" if rel_follow else ' rel="nofollow sponsored"'
    link = f'<a href="{esc_url}"{rel}>{esc_anchor}</a>'
    if esc_anchor and esc_anchor in body_html:
        return body_html.replace(esc_anchor, link, 1), True
    return body_html, False


# ----- page template --------------------------------------------------------
def meta_description(content: str) -> str:
    plain = re.sub(r"[#*>\-]", " ", content)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:155]


def build_page(title, body_html, description, live_url) -> str:
    t = html.escape(title.strip())
    d = html.escape(description, quote=True)
    u = html.escape(live_url, quote=True)
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<link rel="canonical" href="{u}">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
         line-height: 1.7; max-width: 720px; margin: 0 auto; padding: 24px;
         color: #1a1a1a; background: #fff; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6e6e6; background: #121212; }}
    a {{ color: #7db3ff; }}
  }}
  h1 {{ font-size: 1.9rem; line-height: 1.25; margin: 0 0 .6em; }}
  h2 {{ margin-top: 1.6em; }}
  a {{ color: #0057d8; }}
  ul {{ padding-left: 1.3em; }}
  .meta {{ color: #777; font-size: .9rem; margin-bottom: 2em; }}
  .related {{ margin-top: 2.5em; padding-top: 1em; border-top: 1px solid #8884; }}
  footer {{ margin-top: 3em; padding-top: 1em; border-top: 1px solid #8884;
           color: #888; font-size: .85rem; }}
</style>
</head>
<body>
<article>
<h1>{t}</h1>
<div class="meta">Published {datetime.now(timezone.utc).strftime('%B %d, %Y')}</div>
{body_html}
</article>
<footer>&copy; {year} &middot; <a href="../../">Home</a></footer>
</body>
</html>
"""


# ----- tracking + listing ---------------------------------------------------
def update_tracking(root, entry):
    path = os.path.join(root, "articles", "articles.json")
    data = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = []
    # de-duplicate by issue number: replace existing entry if present
    data = [e for e in data if e.get("issue") != entry["issue"]]
    data.append(entry)
    data.sort(key=lambda e: e["issue"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data


def build_listing(root, data):
    items = []
    for e in data:
        t = html.escape(e["title"])
        s = html.escape(e["slug"], quote=True)
        items.append(f'<li><a href="{s}/">{t}</a></li>')
    listing = "\n".join(items) or "<li>No articles yet.</li>"
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Articles</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 0 auto;
        padding: 24px; line-height: 1.7; }}
 a {{ color: #0057d8; }}
 @media (prefers-color-scheme: dark) {{ a {{ color: #7db3ff; }} }}
</style>
</head>
<body>
<h1>Articles</h1>
<ul>
{listing}
</ul>
<p><a href="../">Home</a></p>
</body>
</html>
"""
    with open(os.path.join(root, "articles", "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


# ----- main -----------------------------------------------------------------
def fail(msg: str):
    print(f"VALIDATION_ERROR: {msg}")
    sys.exit(1)


def validate_url(url: str) -> str:
    url = url.strip()
    if not url:
        fail("Target URL is empty.")
    if len(url) > MAX_URL:
        fail("Target URL is too long.")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        fail(f"Target URL must start with http:// or https:// (got scheme '{parsed.scheme}').")
    if not parsed.netloc:
        fail("Target URL has no domain.")
    # block spaces / control chars that could break attribute context
    if re.search(r"\s", url):
        fail("Target URL must not contain spaces.")
    return url


def main():
    root = os.environ.get("REPO_ROOT", ".")
    issue_number = os.environ.get("ISSUE_NUMBER", "").strip()
    body = os.environ.get("ISSUE_BODY", "")
    repo = os.environ.get("REPO", "owner/repo").strip()

    if not issue_number.isdigit():
        fail("ISSUE_NUMBER missing or not numeric.")

    fields = parse_issue_body(body)
    title = fields.get("article title", "").strip()
    content = fields.get("article content", "").strip()
    anchor = fields.get("anchor text", "").strip()
    target = fields.get("target website url", "").strip()
    link_rel = fields.get("link type", "nofollow").strip().lower()
    rel_follow = link_rel.startswith("follow")

    # presence
    if not title:
        fail("Article title is required.")
    if not content:
        fail("Article content is required.")
    if not anchor:
        fail("Anchor text is required.")

    # size
    if len(title) > MAX_TITLE:
        fail("Article title is too long.")
    if len(anchor) > MAX_ANCHOR:
        fail("Anchor text is too long.")
    if len(content) > MAX_CONTENT:
        fail("Article content is too long.")

    target = validate_url(target)

    # slug: title + issue number keeps it unique and avoids path traversal
    slug = f"{slugify(title)}-{issue_number}"
    # defence in depth: slug is already [a-z0-9-], but assert it
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        fail("Internal slug error.")

    owner_repo = repo.split("/")
    if len(owner_repo) == 2:
        owner, reponame = owner_repo
        base = f"https://{owner}.github.io/{reponame}"
    else:
        base = ""
    live_url = f"{base}/articles/{slug}/" if base else f"articles/{slug}/"

    body_html = render_content(content)
    body_html, placed = insert_anchor(body_html, anchor, target, rel_follow)
    if not placed:
        esc_anchor = html.escape(anchor)
        esc_url = html.escape(target, quote=True)
        rel = "" if rel_follow else ' rel="nofollow sponsored"'
        body_html += (
            '\n<section class="related">\n'
            "<h2>Related resource</h2>\n"
            f'<p><a href="{esc_url}"{rel}>{esc_anchor}</a></p>\n'
            "</section>"
        )

    description = meta_description(content)
    page = build_page(title, body_html, description, live_url)

    art_dir = os.path.join(root, "articles", slug)
    os.makedirs(art_dir, exist_ok=True)
    with open(os.path.join(art_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)

    entry = {
        "issue": int(issue_number),
        "title": title,
        "slug": slug,
        "target_url": target,
        "live_url": live_url,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data = update_tracking(root, entry)
    build_listing(root, data)

    # machine-readable outputs for the workflow
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"slug={slug}\n")
            f.write(f"live_url={live_url}\n")
            f.write("status=generated\n")

    print(f"OK: generated articles/{slug}/index.html")
    print(f"LIVE_URL: {live_url}")


if __name__ == "__main__":
    main()
