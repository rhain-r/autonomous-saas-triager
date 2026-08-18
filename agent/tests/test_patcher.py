"""The patch gate.

A patch that does not apply is worse than no patch: it looks like work, and the
engineer finds out only after trying it. Every test here is about refusing one.
"""

from __future__ import annotations

from agent.code_tools import _read
from agent.patcher import describe_patch, verify_patch
from agent.schemas import PatchProposal


def _proposal(**kwargs) -> PatchProposal:
    base = {
        "path": "src/auth/reset.ts",
        "old_text": "  if (age > RESET_TOKEN_TTL_SECONDS) {",
        "new_text": "  if (age > RESET_TOKEN_TTL_SECONDS * 1000) {",
        "rationale": "age is milliseconds; the TTL constant is seconds.",
        "test_hint": "A token issued 90 seconds ago must still verify.",
    }
    base.update(kwargs)
    return PatchProposal(**base)


# --- The fix that should work ------------------------------------------------


def test_a_correct_fix_produces_a_diff_that_applies():
    """The real defect in the sandbox, fixed by hand, through the real gate."""
    patch = verify_patch(_proposal())
    assert patch.applies
    assert patch.rejection_reason is None
    assert "-  if (age > RESET_TOKEN_TTL_SECONDS) {" in patch.diff
    assert "+  if (age > RESET_TOKEN_TTL_SECONDS * 1000) {" in patch.diff
    assert patch.anchor_sha


def test_the_generated_diff_reconstructs_the_intended_file():
    """A diff is only worth attaching if applying it produces what was meant."""
    patch = verify_patch(_proposal())
    original = _read("src/auth/reset.ts")
    rebuilt = _apply(original, patch.diff)
    assert "if (age > RESET_TOKEN_TTL_SECONDS * 1000) {" in rebuilt
    assert rebuilt.count("RESET_TOKEN_TTL_SECONDS") == original.count("RESET_TOKEN_TTL_SECONDS")


def test_a_reindented_anchor_still_applies_and_is_re_indented_to_the_file():
    patch = verify_patch(
        _proposal(
            old_text="        if (age > RESET_TOKEN_TTL_SECONDS) {",
            new_text="if (age > RESET_TOKEN_TTL_SECONDS * 1000) {",
        )
    )
    assert patch.applies
    assert "+  if (age > RESET_TOKEN_TTL_SECONDS * 1000) {" in patch.diff


def test_a_multi_line_anchor_applies():
    patch = verify_patch(
        _proposal(
            old_text=(
                "const age = Date.now() - record.issuedAt;\n"
                "if (age > RESET_TOKEN_TTL_SECONDS) {"
            ),
            new_text=(
                "const ageMs = Date.now() - record.issuedAt;\n"
                "if (ageMs > RESET_TOKEN_TTL_SECONDS * 1000) {"
            ),
        )
    )
    assert patch.applies
    assert "ageMs" in patch.diff


# --- The refusals ------------------------------------------------------------


def test_an_anchor_that_is_not_in_the_file_is_refused():
    """The most common model failure: quoting code that does not exist."""
    patch = verify_patch(_proposal(old_text="  if (tokenAge > MAX_TOKEN_AGE_MS) {"))
    assert not patch.applies
    assert "not found" in patch.rejection_reason


def test_an_ambiguous_anchor_is_refused_rather_than_guessed():
    """`return null;` appears twice in reset.ts. Editing the wrong one silently
    is worse than editing neither."""
    patch = verify_patch(_proposal(old_text="return null;", new_text="return undefined;"))
    assert not patch.applies
    assert "ambiguous" in patch.rejection_reason or "matches" in patch.rejection_reason


def test_a_no_op_is_refused():
    patch = verify_patch(
        _proposal(
            old_text="  if (age > RESET_TOKEN_TTL_SECONDS) {",
            new_text="  if (age > RESET_TOKEN_TTL_SECONDS) {",
        )
    )
    assert not patch.applies
    assert "no-op" in patch.rejection_reason


def test_a_missing_file_is_refused_not_raised():
    patch = verify_patch(_proposal(path="src/auth/does_not_exist.ts"))
    assert not patch.applies
    assert "cannot read" in patch.rejection_reason


def test_a_path_outside_the_sandbox_is_refused():
    patch = verify_patch(_proposal(path="../../../.env", old_text="SECRET=1"))
    assert not patch.applies


def test_an_anchor_longer_than_the_file_is_refused():
    patch = verify_patch(_proposal(old_text="\n".join(f"line {i}" for i in range(500))))
    assert not patch.applies


# --- Description -------------------------------------------------------------


def test_describe_counts_the_real_changed_lines():
    assert describe_patch(verify_patch(_proposal())) == "src/auth/reset.ts (+1/-1, typescript)"


def test_describe_explains_a_refusal():
    patch = verify_patch(_proposal(old_text="not in the file at all"))
    assert describe_patch(patch).startswith("no patch —")


def _apply(original: str, diff: str) -> str:
    """Minimal unified-diff applier, so the test does not trust the generator."""
    lines = original.splitlines(keepends=True)
    out: list[str] = []
    cursor = 0
    diff_lines = diff.splitlines()
    i = 0
    while i < len(diff_lines):
        line = diff_lines[i]
        if line.startswith("@@"):
            start = int(line.split()[1].split(",")[0][1:]) - 1
            out.extend(lines[cursor:start])
            cursor = start
            i += 1
            while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
                hunk = diff_lines[i]
                if hunk.startswith("+") and not hunk.startswith("+++"):
                    out.append(hunk[1:] + "\n")
                elif hunk.startswith("-") and not hunk.startswith("---"):
                    cursor += 1
                elif hunk.startswith(" "):
                    out.append(lines[cursor])
                    cursor += 1
                i += 1
            continue
        i += 1
    out.extend(lines[cursor:])
    return "".join(out)
