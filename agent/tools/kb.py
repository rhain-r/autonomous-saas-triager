"""Help-centre search. No LLM calls live in this module.

The knowledge base is the agent's cheapest correct answer — and its most
dangerous one. A plausible article makes closing a ticket feel finished, which
is exactly why `agent.challenger` re-examines every resolution that leans on it.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from agent.config import SANDBOX_KB
from agent.schemas import KbHit, tokenize

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


class Article:
    """One help-centre article, with its frontmatter parsed out."""

    __slots__ = ("article_id", "body", "path", "tags", "title")

    def __init__(self, article_id: str, title: str, tags: list[str], body: str, path: str) -> None:
        self.article_id = article_id
        self.title = title
        self.tags = tags
        self.body = body
        self.path = path

    @property
    def searchable(self) -> str:
        return f"{self.title} {' '.join(self.tags)} {self.body}"


def _parse_article(path: Path) -> Article:
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(raw)
    meta: dict[str, str] = {}
    body = raw
    if match:
        body = raw[match.end() :]
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()

    tags = [t.strip() for t in meta.get("tags", "").strip("[]").split(",") if t.strip()]
    return Article(
        article_id=meta.get("article_id", path.stem),
        title=meta.get("title", path.stem),
        tags=tags,
        body=body.strip(),
        path=path.name,
    )


@lru_cache(maxsize=1)
def _load_all() -> tuple[Article, ...]:
    return tuple(_parse_article(p) for p in sorted(SANDBOX_KB.glob("*.md")))


def search_docs(query: str, *, limit: int = 3) -> list[KbHit]:
    """Rank help-centre articles against a query.

    Title and tag matches outweigh body matches. Support articles are titled for
    the symptom the customer types, so a title hit is a much stronger signal
    than the same word appearing once in a troubleshooting table.
    """
    terms = tokenize(query)
    if not terms:
        return []

    hits: list[KbHit] = []
    for article in _load_all():
        title_hits = terms & tokenize(article.title)
        tag_hits = terms & tokenize(" ".join(article.tags))
        body_hits = terms & tokenize(article.body)
        score = 3.0 * len(title_hits) + 2.0 * len(tag_hits) + 1.0 * len(body_hits)
        if score <= 0:
            continue
        hits.append(
            KbHit(
                article_id=article.article_id,
                title=article.title,
                path=article.path,
                excerpt=_excerpt(article.body, terms),
                score=score,
            )
        )

    hits.sort(key=lambda h: (-h.score, h.article_id))
    return hits[:limit]


def _excerpt(body: str, terms: set[str], width: int = 320) -> str:
    """The first paragraph that mentions a query term, else the opening one."""
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip() and not p.startswith("#")]
    for paragraph in paragraphs:
        if terms & tokenize(paragraph):
            return paragraph[:width]
    return paragraphs[0][:width] if paragraphs else ""


def get_article(article_id: str) -> Article | None:
    for article in _load_all():
        if article.article_id == article_id:
            return article
    return None


def all_articles() -> tuple[Article, ...]:
    return _load_all()


def clear_cache() -> None:
    """Drop the article cache. Call after writing a KB file in a test."""
    _load_all.cache_clear()
