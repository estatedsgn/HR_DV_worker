"""Shared persona/voice preamble for the funnel LLM prompts.

The persona is authored in ``prompts/persona.md`` and prepended to the
semantic-analyzer and reply-orchestrator system prompts so every LLM-written
message matches the real recruiter voice and never reveals it is an AI.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PERSONA_PATH = PROJECT_ROOT / "prompts" / "persona.md"


@lru_cache(maxsize=1)
def load_persona() -> str:
    try:
        return PERSONA_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def with_persona(prompt_text: str) -> str:
    """Prepend the persona block to a component system prompt."""
    persona = load_persona()
    if not persona:
        return prompt_text
    return f"{persona}\n\n---\n\n{prompt_text}"
