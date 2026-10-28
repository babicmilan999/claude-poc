"""Reading knowledge base articles (Markdown) and splitting them into chunks.

An article may start with YAML front matter (`title`, `id` or `slug`); otherwise the
title is the first `# ` heading and the id is the file name. Chunks follow the `## `
sections; long sections are split on paragraphs. Every chunk starts with the article
title and section heading, so it reads on its own for both search and citation.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_CHUNK_CHARS = 1500

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)
_H2_SPLIT_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Article:
    article_id: str
    title: str
    url: str
    body: str
    content_hash: str


@dataclass(frozen=True)
class Chunk:
    article_id: str
    chunk_index: int
    title: str
    section: str | None
    text: str
    url: str


def slugify(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-")


def parse_article(path: Path, text: str, base_url: str) -> Article:
    meta: dict[str, object] = {}
    body = text
    match = _FRONT_MATTER_RE.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            loaded = None  # broken front matter: fall back to the heading and file name
        if isinstance(loaded, dict):
            meta = loaded
        body = text[match.end() :]
    h1 = _H1_RE.search(body)
    title = str(meta.get("title") or (h1.group(1) if h1 else path.stem.replace("-", " ").capitalize()))
    article_id = slugify(str(meta.get("id") or meta.get("slug") or path.stem))
    if h1:
        body = body[: h1.start()] + body[h1.end() :]
    url = str(meta.get("url") or f"{base_url.rstrip('/')}/{article_id}")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Article(article_id=article_id, title=title, url=url, body=body.strip(), content_hash=content_hash)


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) + 2 > limit:
            parts.append(current)
            current = ""
        while len(paragraph) > limit:
            parts.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        parts.append(current)
    return parts


def chunk_article(article: Article, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    pieces = _H2_SPLIT_RE.split(article.body)
    # pieces = [intro, heading1, text1, heading2, text2, ...]
    sections: list[tuple[str | None, str]] = []
    if pieces[0].strip():
        sections.append((None, pieces[0].strip()))
    for i in range(1, len(pieces), 2):
        sections.append((pieces[i].strip(), pieces[i + 1].strip() if i + 1 < len(pieces) else ""))

    chunks: list[Chunk] = []
    for section, text in sections:
        if not text:
            continue
        header = article.title if section is None else f"{article.title} — {section}"
        for part in _split_long(text, max_chars):
            chunks.append(
                Chunk(
                    article_id=article.article_id,
                    chunk_index=len(chunks),
                    title=article.title,
                    section=section,
                    text=f"{header}\n\n{part}",
                    url=article.url,
                )
            )
    return chunks


def load_articles(folder: Path, base_url: str) -> list[Article]:
    """Every `*.md` file under `folder` except READMEs, sorted by path."""
    if not folder.is_dir():
        return []
    articles: list[Article] = []
    for path in sorted(folder.rglob("*.md")):
        if path.name.lower() in {"readme.md", "index.md"}:
            continue
        articles.append(parse_article(path, path.read_text(encoding="utf-8"), base_url))
    return articles
