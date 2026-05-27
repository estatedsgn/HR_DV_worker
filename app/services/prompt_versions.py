from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompt_version import PromptVersion
from app.repositories.prompt_version import PromptVersionRepository


@dataclass(slots=True, frozen=True)
class PromptSeedResult:
    prompt: PromptVersion
    created: bool
    updated: bool


class PromptVersionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = PromptVersionRepository(session)

    async def get_active_or_file(
        self, *, name: str, version: str, file_name: str | None = None
    ) -> tuple[str, str]:
        active = await self.repository.get_active(name)
        if active is not None:
            return active.content, f"{active.name}:{active.version}:{active.checksum}"
        path = prompt_path(file_name or f"{name}_v1.md")
        content = path.read_text(encoding="utf-8")
        checksum = checksum_text(content)
        return content, f"{name}:{version}:{checksum}"

    async def seed_from_file(
        self,
        *,
        name: str,
        version: str,
        file_name: str,
        activate: bool = True,
        changelog: str | None = None,
    ) -> PromptSeedResult:
        content = prompt_path(file_name).read_text(encoding="utf-8")
        checksum = checksum_text(content)
        existing = await self.repository.get_by_name_version(name, version)
        created = updated = False
        if existing is None:
            existing = PromptVersion(
                name=name,
                version=version,
                content=content,
                checksum=checksum,
                status="draft",
                changelog=changelog,
            )
            self.session.add(existing)
            created = True
        elif existing.checksum != checksum or existing.content != content:
            existing.content = content
            existing.checksum = checksum
            existing.changelog = changelog
            updated = True
        await self.session.flush()
        if activate:
            await self.repository.activate(name, version)
        await self.session.commit()
        return PromptSeedResult(prompt=existing, created=created, updated=updated)

    async def activate(self, *, name: str, version: str) -> PromptVersion:
        prompt = await self.repository.activate(name, version)
        prompt.activated_at = datetime.now(UTC)
        await self.session.commit()
        return prompt


def prompt_path(file_name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / "brain" / file_name


def checksum_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
