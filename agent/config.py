"""Single source of truth for models, credentials, and runtime limits.

No model name or numeric limit is hardcoded anywhere else in the codebase. If you
find yourself typing a model string into an agent module, it belongs here instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
PROMPT_DIR = AGENT_DIR / "prompts"

# The simulated production estate the agent investigates: source tree, service
# logs, help-centre articles, and the ticket inbox.
SANDBOX_DIR = AGENT_DIR / "sandbox"
SANDBOX_REPO = SANDBOX_DIR / "repo"
SANDBOX_LOGS = SANDBOX_DIR / "logs"
SANDBOX_KB = SANDBOX_DIR / "kb"
SANDBOX_TICKETS = SANDBOX_DIR / "tickets"


def load_prompt(name: str) -> str:
    """Load a system prompt by stem from `agent/prompts/`.

    Prompts live on disk rather than in string literals because they are the real
    logic of this system and deserve to show up in diffs.
    """
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Providers -----------------------------------------------------------
    anthropic_api_key: str | None = None
    google_api_key: str | None = None

    # Classification, investigation, and patch drafting run on Claude.
    classifier_model: str = "claude-sonnet-5"
    investigator_model: str = "claude-sonnet-5"
    patcher_model: str = "claude-sonnet-5"

    # The challenger deliberately runs on a *different* provider. A resolution
    # and its cross-check sharing a training set is not a cross-check.
    challenger_model: str = "gemini-2.5-pro"
    challenger_provider: str = Field(default="google", pattern="^(google|anthropic)$")

    # --- Investigation limits ------------------------------------------------
    max_tool_calls: int = Field(default=12, ge=1)
    max_log_events: int = Field(default=40, ge=1)
    max_code_matches: int = Field(default=25, ge=1)
    max_file_lines: int = Field(default=400, ge=20)
    max_output_tokens: int = Field(default=4096, ge=256)
    ticket_concurrency: int = Field(default=4, ge=1)

    # How far either side of the ticket timestamp the log search looks. Wide
    # enough to catch a slow report, narrow enough that an unrelated incident
    # on the same day does not become somebody's root cause.
    log_window_hours: int = Field(default=6, ge=1)

    # --- Gates ---------------------------------------------------------------
    # A resolution proposed at or below this confidence is challenged even when
    # the intent looks benign.
    challenge_confidence_floor: float = Field(default=0.85, ge=0.0, le=1.0)

    # Escalations at or above this risk score are marked for immediate paging
    # rather than normal backlog triage.
    page_threshold: float = Field(default=70.0, ge=0.0, le=100.0)

    # Whether an auto-close requires a challenge. With this on and no challenger
    # configured, resolutions are downgraded to a human rather than closed on
    # the investigator's word. Turning it off is the behaviour of an ordinary
    # triage bot, and exists so the eval can measure what that costs.
    require_challenge: bool = True

    # --- Tracker -------------------------------------------------------------
    jira_project: str = "SUP"
    jira_base_url: str = "https://meridian.atlassian.net"
    jira_api_token: str | None = None

    # --- Paths ---------------------------------------------------------------
    report_dir: Path = REPO_ROOT / "reports"
    trace_dir: Path = REPO_ROOT / "runs"

    @property
    def has_challenger_credentials(self) -> bool:
        """Whether a genuine cross-model challenge is possible with current keys."""
        if self.challenger_provider == "google":
            return bool(self.google_api_key)
        return bool(self.anthropic_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Call `get_settings.cache_clear()` in tests."""
    return Settings()
