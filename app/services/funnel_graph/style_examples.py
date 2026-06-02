"""Few-shot style/meaning anchors mined from the real recruiter transcripts.

The examples live in ``knowledge/style_examples.json`` as ``{stage, topics,
cand, reply}`` records. At runtime we inject the most relevant 2-4 into the
reply-orchestrator prompt so the LLM copies how the real recruiter actually
reasons and phrases answers in a similar situation (tone + dialogue logic),
not just abstract rules.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
STYLE_EXAMPLES_PATH = PROJECT_ROOT / "knowledge" / "style_examples.json"


@lru_cache(maxsize=1)
def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(STYLE_EXAMPLES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def select_style_examples(stage: str | None, topics: list[str] | None, *, k: int = 3) -> list[dict[str, str]]:
    """Pick up to ``k`` examples ranked by topic overlap, then stage match."""
    wanted = {str(topic).strip().lower() for topic in (topics or []) if topic}
    scored: list[tuple[float, dict[str, Any]]] = []
    for example in _load():
        score = 0.0
        if stage and example.get("stage") == stage:
            score += 1.0
        example_topics = {str(topic).strip().lower() for topic in example.get("topics") or []}
        score += 2.0 * len(wanted & example_topics)
        if score > 0:
            scored.append((score, example))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"cand": str(example.get("cand") or ""), "reply": str(example.get("reply") or "")}
        for _, example in scored[:k]
    ]
