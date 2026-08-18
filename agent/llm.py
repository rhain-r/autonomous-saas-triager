"""Provider-agnostic model access.

Kept deliberately thin. The investigation logic in this repo is the interesting
part and it does not depend on any agent framework — swapping in LangChain,
AutoGen, or LangGraph means implementing `ModelClient` and changing nothing else.

The abstraction also lets the whole test suite and the eval harness run against
`StubClient` with no API keys, no network, and no cost.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from agent.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class ModelError(RuntimeError):
    """A model call failed or returned unusable output after retries."""


class ModelClient(Protocol):
    """Minimal surface every provider adapter must implement."""

    name: str

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str: ...


# --- Adapters ----------------------------------------------------------------


class AnthropicClient:
    """Claude. Classifies tickets, investigates, and drafts patches."""

    def __init__(self, api_key: str, model: str) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self.name = model

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str:
        response = await self._client.messages.create(
            model=self.name,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


class GeminiClient:
    """Gemini. Runs the challenger, so the cross-check is genuinely cross-lab."""

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self.name = model

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str:
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=self.name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
            ),
        )
        return response.text or ""


class StubClient:
    """Deterministic canned responses, for tests and offline demos."""

    def __init__(self, responses: list[str], name: str = "stub") -> None:
        self._responses = list(responses)
        self.name = name
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str:
        self.calls.append((system, prompt))
        if not self._responses:
            raise ModelError("StubClient exhausted: more calls than canned responses.")
        return self._responses.pop(0)


# --- Factory -----------------------------------------------------------------


def build_client(settings: Settings, role: str) -> ModelClient:
    """Construct the client for a role: 'classifier', 'investigator',
    'patcher', or 'challenger'."""
    if role == "challenger":
        if settings.challenger_provider == "google":
            if not settings.google_api_key:
                raise ModelError(
                    "GOOGLE_API_KEY is required for the challenger. Set "
                    "challenger_provider='anthropic' to fall back to a same-provider "
                    "check — but note that weakens the cross-model guarantee."
                )
            return GeminiClient(settings.google_api_key, settings.challenger_model)
        if not settings.anthropic_api_key:
            raise ModelError("ANTHROPIC_API_KEY is required for the challenger.")
        return AnthropicClient(settings.anthropic_api_key, settings.challenger_model)

    if not settings.anthropic_api_key:
        raise ModelError("ANTHROPIC_API_KEY is required. Copy the block in docs/setup-guide.md.")
    model = {
        "classifier": settings.classifier_model,
        "investigator": settings.investigator_model,
        "patcher": settings.patcher_model,
    }.get(role, settings.investigator_model)
    return AnthropicClient(settings.anthropic_api_key, model)


# --- Structured output -------------------------------------------------------


def extract_json(raw: str) -> str:
    """Pull a JSON object out of a model response, fenced or bare."""
    fenced = _JSON_BLOCK.search(raw)
    if fenced:
        return fenced.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        return raw[start : end + 1]
    return raw.strip()


async def complete_structured(
    client: ModelClient,
    system: str,
    prompt: str,
    schema: type[T],
    *,
    max_tokens: int = 4096,
    max_retries: int = 2,
) -> T:
    """Call the model and parse into `schema`, retrying on validation failure.

    The retry feeds the *validation error itself* back to the model. That turns a
    schema into a correction signal rather than just a gate. Output is never
    coerced — an invalid payload either becomes valid on retry or raises.
    """
    attempt_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        raw = await client.complete(system, attempt_prompt, max_tokens=max_tokens)
        try:
            return schema.model_validate_json(extract_json(raw))
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning(
                "%s returned invalid %s (attempt %d/%d): %s",
                client.name,
                schema.__name__,
                attempt + 1,
                max_retries + 1,
                str(exc)[:300],
            )
            attempt_prompt = (
                f"{prompt}\n\n"
                f"Your previous response failed validation against the required schema.\n"
                f"Error:\n{str(exc)[:1000]}\n\n"
                f"Your previous response was:\n{raw[:1500]}\n\n"
                "Return ONLY corrected JSON matching the schema. No prose, no code fence."
            )

    raise ModelError(
        f"{client.name} could not produce valid {schema.__name__} after "
        f"{max_retries + 1} attempts: {last_error}"
    )
