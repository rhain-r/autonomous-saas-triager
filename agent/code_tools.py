"""Source-tree tools. No LLM calls live in this module.

These are the agent's hands: grep the tree, read a file, read the history, and
— the one that matters — turn a model's claim about a file into a `CodeSpan`
only if the claimed text is actually in that file.

Everything is scoped to `agent/sandbox/repo`. `_resolve()` refuses to leave it,
because an investigator that can be talked into reading `../../.env` is not a
support agent, it is an exfiltration path.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from agent.config import SANDBOX_REPO
from agent.schemas import CodeMatch, CodeSpan, Commit, tokenize

SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rb", ".java"})

_LANGUAGE_BY_SUFFIX = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
}

# Collapses runs of whitespace so a quote that survived a round trip through a
# model — reindented, rewrapped — can still be matched to its source.
_WHITESPACE = re.compile(r"\s+")


class SandboxEscape(ValueError):
    """A tool was asked for a path outside the sandbox repository."""


def _resolve(path: str, root: Path = SANDBOX_REPO) -> Path:
    """Resolve a repo-relative path, refusing anything that escapes the root."""
    candidate = (root / path.lstrip("/\\")).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise SandboxEscape(f"path escapes the sandbox repository: {path}")
    return candidate


def _relative(path: Path, root: Path = SANDBOX_REPO) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def language_for(path: str) -> str:
    return _LANGUAGE_BY_SUFFIX.get(Path(path).suffix, "text")


@lru_cache(maxsize=256)
def _read(path: str) -> str:
    return _resolve(path).read_text(encoding="utf-8")


def file_sha(path: str) -> str:
    """SHA-256 of a file's current contents, used to pin patches to a version."""
    return hashlib.sha256(_read(path).encode("utf-8")).hexdigest()[:16]


def list_files(prefix: str = "") -> list[str]:
    """Every source file under `prefix`, repo-relative and sorted."""
    root = _resolve(prefix) if prefix else SANDBOX_REPO
    if root.is_file():
        return [_relative(root)]
    return sorted(
        _relative(p)
        for p in root.rglob("*")
        if p.is_file() and p.suffix in SOURCE_SUFFIXES
    )


# --- Tools -------------------------------------------------------------------


def search_codebase(
    query: str,
    *,
    path_prefix: str = "",
    max_results: int = 25,
) -> list[CodeMatch]:
    """Grep the source tree for lines matching any term in `query`.

    Ranks by how many distinct query terms a line carries, with a small bonus
    when the file path itself matches — an agent looking for "oauth redirect"
    should see `src/auth/config.ts` before an incidental mention elsewhere.
    """
    terms = tokenize(query)
    if not terms:
        return []

    matches: list[CodeMatch] = []
    for rel in list_files(path_prefix):
        path_tokens = tokenize(rel)
        path_bonus = 0.5 * len(terms & path_tokens)
        for line_no, line in enumerate(_read(rel).splitlines(), start=1):
            hits = terms & tokenize(line)
            if not hits:
                continue
            matches.append(
                CodeMatch(
                    path=rel,
                    line_no=line_no,
                    line=line.rstrip(),
                    score=len(hits) + path_bonus,
                )
            )

    matches.sort(key=lambda m: (-m.score, m.path, m.line_no))
    return matches[:max_results]


def read_file(path: str, start: int = 1, end: int | None = None) -> str:
    """Return a line-numbered slice of a file, as handed to a model.

    Line numbers are part of the payload on purpose: they are what the model
    quotes back, and what `cite_code()` then checks independently.
    """
    lines = _read(path).splitlines()
    start = max(1, start)
    end = len(lines) if end is None else min(end, len(lines))
    width = len(str(end))
    return "\n".join(f"{n:>{width}} | {lines[n - 1]}" for n in range(start, end + 1))


def cite_code(path: str, quote: str) -> CodeSpan | None:
    """Verify that `quote` appears in `path`, and return where.

    This is the gate between "the model said" and "the file says". A quote that
    does not occur returns `None`, and the caller records a fabrication rather
    than a citation. Whitespace is normalised before comparison — models reflow
    indentation constantly, and rejecting a real quote over two spaces would
    push the agent toward citing nothing at all.
    """
    needle = _WHITESPACE.sub(" ", quote).strip()
    if not needle:
        return None

    try:
        raw = _read(path)
    except (FileNotFoundError, SandboxEscape, IsADirectoryError):
        return None

    lines = raw.splitlines()
    normalised = [_WHITESPACE.sub(" ", line).strip() for line in lines]

    span = _locate(normalised, needle)
    if span is None:
        return None

    start, end = span
    return CodeSpan(
        path=path,
        line_start=start + 1,
        line_end=end + 1,
        snippet="\n".join(lines[start : end + 1]),
        language=language_for(path),
        content_sha=file_sha(path),
    )


def _locate(normalised: list[str], needle: str) -> tuple[int, int] | None:
    """Find the smallest window of lines whose joined text contains `needle`."""
    for i, line in enumerate(normalised):
        if needle in line:
            return i, i

    # Multi-line quote: grow a window from each start until it covers the quote
    # or exceeds it. Bounded so a pathological quote cannot walk a whole file.
    max_window = min(len(normalised), 60)
    for i in range(len(normalised)):
        joined = normalised[i]
        for j in range(i + 1, min(i + max_window, len(normalised))):
            joined = f"{joined} {normalised[j]}".strip()
            if needle in joined:
                return i, j
            if len(joined) > len(needle) * 2 + 200:
                break
    return None


def git_log(path_prefix: str = "", limit: int = 10) -> list[Commit]:
    """Recent commits, optionally filtered to those touching `path_prefix`.

    Reads `agent/sandbox/repo/commits.json`. A real deployment would shell out
    to `git log --format=...` or call the GitHub API; the shape returned here is
    the same either way, which is the point of keeping it behind a function.
    """
    manifest = SANDBOX_REPO / "commits.json"
    if not manifest.exists():
        return []

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    commits = [Commit.model_validate(c) for c in payload.get("commits", [])]
    if path_prefix:
        commits = [c for c in commits if any(f.startswith(path_prefix) for f in c.files)]
    commits.sort(key=lambda c: c.authored_at, reverse=True)
    return commits[:limit]


def clear_cache() -> None:
    """Drop the file cache. Call after writing to the sandbox in a test."""
    _read.cache_clear()
