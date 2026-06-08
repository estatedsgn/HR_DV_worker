"""Rotating pool of interchangeable LLM API keys.

The funnel runs several worker processes (a scoped autopilot per account), all
hitting the same LLM provider (Qwen / Alibaba DashScope) on a pool of keys that
each carry a small prepaid / free-tier balance. When the active key runs out of
quota the provider returns an "exhausted / insufficient balance" error; instead
of falling into the ugly deterministic fallback we transparently rotate to the
next key.

The active index is persisted to a small JSON file so every process shares one
cursor: when one autopilot exhausts a key and bumps the cursor, the others pick
up the new key on their next call. The update is compare-and-swap by the failed
key's slot, so two processes failing on the same key both land on the next slot
(idempotent) rather than skipping one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from app.core.config import Settings, get_settings

logger = logging.getLogger("llm_key_pool")

# Substrings (case-insensitive) that mark a key as spent / unusable so we should
# rotate to the next one rather than retry the same key.
_EXHAUSTION_MARKERS = (
    "exhausted",
    "insufficient",
    "arrearage",
    "欠费",
    "out of quota",
    "quota exceeded",
    "exceeded your current quota",
    "billing",
    "expired",
    "invalid api key",
    "invalid api-key",
    "incorrect api key",
    "access denied",
)


def is_exhaustion_error(message: str | None) -> bool:
    """True when ``message`` indicates the current key is spent/unusable."""
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _EXHAUSTION_MARKERS)


class LLMKeyPool:
    """Process-shared rotating cursor over a list of LLM keys."""

    def __init__(self, keys: list[str], state_path: str | os.PathLike[str]) -> None:
        self._keys = list(keys)
        self._state_path = Path(state_path)
        self._lock = threading.Lock()

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def _read_index(self) -> int:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            index = int(data.get("index", 0))
        except (FileNotFoundError, ValueError, OSError, TypeError):
            return 0
        if not self._keys:
            return 0
        return index % len(self._keys)

    def _write_index(self, index: int) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a concurrent reader never sees a half-written file.
        fd, tmp = tempfile.mkstemp(dir=str(self._state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"index": index}, handle)
            os.replace(tmp, self._state_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def active_key(self) -> str | None:
        if not self._keys:
            return None
        with self._lock:
            return self._keys[self._read_index()]

    def mark_exhausted(self, key: str) -> str | None:
        """Rotate past ``key`` if it is still the active one. Returns the new
        active key (which may equal ``key`` when there is nothing else to switch
        to)."""
        if not self._keys or key not in self._keys:
            return self.active_key()
        with self._lock:
            current = self._read_index()
            failed = self._keys.index(key)
            # Only advance when the failed key is still the active slot — makes
            # concurrent rotations on the same key idempotent.
            if current % len(self._keys) != failed % len(self._keys):
                return self._keys[current]
            if len(self._keys) == 1:
                return self._keys[current]
            new_index = (current + 1) % len(self._keys)
            self._write_index(new_index)
            logger.warning(
                "LLM key #%d exhausted; rotated to key #%d (of %d)",
                failed + 1,
                new_index + 1,
                len(self._keys),
            )
            return self._keys[new_index]


_pool: LLMKeyPool | None = None
_pool_signature: tuple[str, ...] | None = None


def get_key_pool(settings: Settings | None = None) -> LLMKeyPool:
    """Process-wide singleton pool, rebuilt only if the configured keys change."""
    global _pool, _pool_signature
    settings = settings or get_settings()
    keys = settings.llm_api_key_pool()
    signature = tuple(keys)
    if _pool is None or _pool_signature != signature:
        _pool = LLMKeyPool(keys, settings.llm_key_state_path)
        _pool_signature = signature
    return _pool
