"""Candidate fix generation, and the gate that stops a fix that is fiction.

Models are poor at emitting unified diffs. They hallucinate context lines,
mis-count hunk offsets, and produce patches that look plausible and apply to
nothing. So this module never asks for a diff. It asks for *the exact text to
replace* (`PatchProposal`), locates that text in the real file, performs the
replacement itself, and generates the diff from the before and after it just
computed. A diff produced this way applies by construction.

The checks that remain are the ones that matter: does the anchor exist, is it
unique, and does the edit actually change anything.
"""

from __future__ import annotations

import difflib
import textwrap

from agent.code_tools import _read, file_sha, language_for
from agent.schemas import Patch, PatchProposal


def verify_patch(proposal: PatchProposal) -> Patch:
    """Turn a proposal into a `Patch`, applied or explicitly rejected.

    Never raises for a bad proposal. A rejected patch carries its reason and
    still reaches the report — "the model's fix did not apply" is information
    the reviewing engineer wants, not an error to swallow.
    """
    base = Patch(
        path=proposal.path,
        rationale=proposal.rationale,
        test_hint=proposal.test_hint,
        applies=False,
        rejection_reason="not evaluated",
    )

    try:
        original = _read(proposal.path)
    except Exception as exc:
        return base.model_copy(update={"rejection_reason": f"cannot read {proposal.path}: {exc}"})

    anchor = _locate_anchor(original, proposal.old_text, proposal.new_text)
    if anchor.error:
        return base.model_copy(update={"rejection_reason": anchor.error})

    patched = original[: anchor.start] + anchor.replacement + original[anchor.end :]
    if patched == original:
        return base.model_copy(
            update={"rejection_reason": "proposal is a no-op: new_text matches the existing text"}
        )

    return base.model_copy(
        update={
            "applies": True,
            "diff": unified_diff_for(proposal.path, original, patched),
            "anchor_sha": file_sha(proposal.path),
            "rejection_reason": None,
        }
    )


class _Anchor:
    """Where the anchor text sits in the file, and what replaces it."""

    __slots__ = ("end", "error", "replacement", "start")

    def __init__(
        self,
        start: int = 0,
        end: int = 0,
        replacement: str = "",
        error: str | None = None,
    ) -> None:
        self.start = start
        self.end = end
        self.replacement = replacement
        self.error = error


def _locate_anchor(original: str, old_text: str, new_text: str) -> _Anchor:
    """Find `old_text` in `original`, exactly if possible, by line shape if not."""
    occurrences = original.count(old_text)
    if occurrences == 1:
        start = original.index(old_text)
        return _Anchor(start, start + len(old_text), new_text)
    if occurrences > 1:
        return _Anchor(
            error=(
                f"anchor text appears {occurrences} times in the file; an ambiguous "
                "anchor could edit the wrong call site"
            )
        )
    return _locate_by_lines(original, old_text, new_text)


def _locate_by_lines(original: str, old_text: str, new_text: str) -> _Anchor:
    """Whitespace-tolerant fallback.

    Models reindent quoted code constantly. Refusing a fix over two spaces of
    leading whitespace would reject correct patches and push the agent toward
    proposing nothing, so the anchor is matched on stripped line content and the
    replacement is reindented to whatever the file actually uses at that point.
    """
    file_lines = original.splitlines(keepends=True)
    stripped_file = [line.strip() for line in file_lines]
    anchor_lines = [line.strip() for line in old_text.splitlines() if line.strip()]

    if not anchor_lines:
        return _Anchor(error="anchor text is empty after normalisation")
    if len(anchor_lines) > len(file_lines):
        return _Anchor(error="anchor text is longer than the file it claims to be from")

    hits = [
        i
        for i in range(len(stripped_file) - len(anchor_lines) + 1)
        if stripped_file[i : i + len(anchor_lines)] == anchor_lines
    ]

    if not hits:
        return _Anchor(
            error=(
                "anchor text was not found in the file; the model quoted code that is "
                "not there, so the fix it proposes cannot be trusted"
            )
        )
    if len(hits) > 1:
        return _Anchor(
            error=(
                f"anchor text matches {len(hits)} locations once whitespace is "
                "normalised; refusing to guess which one was meant"
            )
        )

    first = hits[0]
    last = first + len(anchor_lines) - 1
    start = sum(len(line) for line in file_lines[:first])
    end = start + sum(len(line) for line in file_lines[first : last + 1])

    ends_with_newline = file_lines[last].endswith("\n")
    replacement = _reindent(new_text, _indent_of(file_lines[first]))
    if ends_with_newline and not replacement.endswith("\n"):
        replacement += "\n"
    if not ends_with_newline:
        replacement = replacement.rstrip("\n")

    return _Anchor(start, end, replacement)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(text: str, base_indent: str) -> str:
    """Strip the block's own common indentation and re-apply the file's."""
    dedented = textwrap.dedent(text.strip("\n"))
    return "\n".join(
        f"{base_indent}{line}" if line.strip() else line for line in dedented.splitlines()
    )


def unified_diff_for(path: str, original: str, patched: str) -> str:
    """Standalone diff helper, used by the reporter and by tests."""
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


def describe_patch(patch: Patch) -> str:
    """One line for the terminal and for the tracker description."""
    if not patch.applies:
        return f"no patch — {patch.rejection_reason}"
    lines = patch.diff.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return f"{patch.path} (+{added}/-{removed}, {language_for(patch.path)})"
