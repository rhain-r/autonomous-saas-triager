"""The tools, and the citation gates built into them.

These run against the real sandbox files rather than mocks. That is the point:
`cite_code()` is only meaningful if it is checking actual bytes, so a test that
stubbed the filesystem would be testing nothing.
"""

from __future__ import annotations

import pytest

from agent import code_tools
from agent.code_tools import (
    SandboxEscape,
    cite_code,
    git_log,
    list_files,
    read_file,
    search_codebase,
)
from agent.tests.conftest import at
from agent.tools import kb, logs
from agent.tools.logs import cite_log, parse_line, read_log_window, search_error_logs

# --- Sandbox containment -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../../../.env", "../../config.py", "src/../../secrets.txt", "C:/Windows/win.ini"],
)
def test_traversal_out_of_the_sandbox_is_refused(path):
    """An investigator that can be talked into reading `../../.env` is not a
    support agent, it is an exfiltration path."""
    with pytest.raises(SandboxEscape):
        code_tools._resolve(path)


@pytest.mark.parametrize("path", ["/etc/passwd", "//srv/secrets", "\\windows\\system.ini"])
def test_an_absolute_path_is_treated_as_sandbox_relative(path):
    """Rooted paths are re-rooted rather than rejected — the same contract a
    chroot gives. They resolve inside the sandbox, where there is nothing to
    find, instead of reaching the host filesystem."""
    resolved = code_tools._resolve(path)
    assert code_tools.SANDBOX_REPO.resolve() in resolved.parents
    assert not resolved.exists()


def test_cite_code_returns_none_for_an_escaping_path_rather_than_raising():
    assert cite_code("../../../.env", "anything") is None


# --- Code search -------------------------------------------------------------


def test_search_finds_the_symbol_behind_a_log_field():
    hits = search_codebase("redirect_uri_mismatch", path_prefix="src/auth")
    assert any(h.path == "src/auth/config.ts" for h in hits)


def test_search_ranks_the_matching_file_path_higher():
    hits = search_codebase("reset token expired")
    assert hits[0].path == "src/auth/reset.ts"


def test_search_returns_nothing_for_a_query_of_only_stopwords():
    assert search_codebase("the and for") == []


def test_read_file_is_line_numbered_so_quotes_can_be_checked():
    text = read_file("src/auth/config.ts", 1, 3)
    assert text.splitlines()[0].strip().startswith("1 |")


def test_list_files_only_returns_source():
    files = list_files()
    assert "src/auth/config.ts" in files
    assert not any(f.endswith(".json") for f in files)


# --- cite_code: the gate -----------------------------------------------------


def test_cite_code_locates_a_real_quote_at_real_line_numbers():
    span = cite_code("src/auth/config.ts", "redirectUri: `${STAGING_ORIGIN}/auth/callback`,")
    assert span is not None
    assert span.path == "src/auth/config.ts"
    assert read_file("src/auth/config.ts", span.line_start, span.line_end).endswith(
        "redirectUri: `${STAGING_ORIGIN}/auth/callback`,"
    )


def test_cite_code_rejects_an_invented_quote():
    """The whole point. A plausible line that is not in the file is not evidence."""
    assert cite_code("src/auth/config.ts", "const sessionCache = new LruCache(512);") is None


def test_cite_code_rejects_a_real_quote_attributed_to_the_wrong_file():
    quote = "redirectUri: `${STAGING_ORIGIN}/auth/callback`,"
    assert cite_code("src/auth/session.ts", quote) is None


def test_cite_code_tolerates_reindentation():
    """Models reflow whitespace constantly; rejecting over two spaces would push
    the agent toward citing nothing at all."""
    span = cite_code(
        "src/auth/config.ts", "     redirectUri:   `${STAGING_ORIGIN}/auth/callback`,"
    )
    assert span is not None


def test_cite_code_matches_a_multi_line_quote():
    span = cite_code(
        "src/auth/reset.ts",
        "const age = Date.now() - record.issuedAt;\n  if (age > RESET_TOKEN_TTL_SECONDS) {",
    )
    assert span is not None
    assert span.line_end > span.line_start


def test_cite_code_pins_the_file_contents():
    span = cite_code("src/auth/config.ts", "export const STAGING_ORIGIN")
    assert span is not None
    assert span.content_sha == code_tools.file_sha("src/auth/config.ts")


# --- Log parsing -------------------------------------------------------------


def test_parse_line_extracts_the_structured_fields():
    raw = (
        '2026-08-14T09:12:45.660Z ERROR auth-svc msg="token exchange rejected" '
        'code="OAUTH_REDIRECT_MISMATCH" user="usr_8812" detail="error=redirect_uri_mismatch"'
    )
    event = parse_line(raw, "auth-svc-2026-08-14.log", 3)
    assert event is not None
    assert event.code == "OAUTH_REDIRECT_MISMATCH"
    assert event.user_id == "usr_8812"
    assert event.fields["detail"].startswith("error=")
    assert event.locator == "auth-svc-2026-08-14.log:3"


def test_an_unparseable_line_is_dropped_not_guessed_at():
    assert parse_line("this is not a log line", "x.log", 1) is None
    assert parse_line("", "x.log", 1) is None


def test_signature_is_the_diagnostic_part_only():
    event = logs.all_events()[0]
    assert str(event.timestamp.year) not in event.signature


# --- Log search --------------------------------------------------------------


def test_search_defaults_to_failures():
    events = search_error_logs(user_id="usr_8812")
    assert events
    assert all(e.level.is_failure for e in events)


def test_search_respects_the_time_window():
    near = search_error_logs(around=at(9, 12), window_hours=1)
    far = search_error_logs(around=at(9, 12), window_hours=1000)
    assert len(far) > len(near)
    assert all(abs((e.timestamp - at(9, 12)).total_seconds()) <= 3600 for e in near)


def test_a_single_incidental_word_does_not_match_a_long_query():
    """The bug this check exists for: a customer writing "request a new one"
    pulling in "retrying upstream request" from an unrelated service."""
    query = (
        "Password reset link says it has expired. I asked for a password reset "
        "and clicked it, and I had to request a new one three times."
    )
    events = search_error_logs(around=at(15, 0, day=16), window_hours=24, query=query)
    assert events
    assert all(e.service == "auth-svc" for e in events)
    assert not any(e.code == "UPSTREAM_RETRY" for e in events)


def test_a_short_deliberate_query_still_matches_on_one_term():
    assert search_error_logs(query="LEDGER_DUPLICATE")


def test_read_log_window_includes_the_info_lines_around_a_failure():
    context = read_log_window("billing-svc-2026-08-15.log", 7, before=3, after=1)
    assert any(e.code == "LEDGER_CREDIT" for e in context)


# --- cite_log: the gate ------------------------------------------------------


def test_cite_log_resolves_a_real_event():
    assert cite_log("auth-svc-2026-08-14#3") is not None


def test_cite_log_rejects_an_invented_event_id():
    assert cite_log("auth-svc-2026-08-14#9999") is None
    assert cite_log("no-such-file#1") is None


def test_cite_log_rejects_text_attributed_to_the_wrong_line():
    assert cite_log("auth-svc-2026-08-14#3", "EXPORT_TOO_LARGE") is None
    assert cite_log("auth-svc-2026-08-14#3", "OAUTH_REDIRECT_MISMATCH") is not None


# --- Help centre -------------------------------------------------------------


def test_docs_search_ranks_a_title_match_above_a_body_mention():
    hits = kb.search_docs("export size limit")
    assert hits[0].article_id == "KB-0031"


def test_docs_search_returns_nothing_for_an_unrelated_query():
    assert kb.search_docs("the and for") == []


def test_every_article_parses_its_frontmatter():
    for article in kb.all_articles():
        assert article.article_id.startswith("KB-")
        assert article.tags


# --- History -----------------------------------------------------------------


def test_git_log_filters_to_the_touched_prefix():
    commits = git_log("src/auth")
    assert commits
    assert all(any(f.startswith("src/auth") for f in c.files) for c in commits)


def test_git_log_is_newest_first():
    commits = git_log()
    assert commits == sorted(commits, key=lambda c: c.authored_at, reverse=True)
