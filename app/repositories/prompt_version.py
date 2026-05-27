from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from app.models.prompt_version import PromptVersion
from app.repositories.base import BaseRepository


class PromptVersionRepository(BaseRepository[PromptVersion]):
    model = PromptVersion

    async def get_by_name_version(self, name: str, version: str) -> PromptVersion | None:
        result = await self.session.execute(
            select(PromptVersion)
            .where(PromptVersion.name == name, PromptVersion.version == version)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_active(self, name: str) -> PromptVersion | None:
        result = await self.session.execute(
            select(PromptVersion)
            .where(PromptVersion.name == name, PromptVersion.status == "active")
            .order_by(PromptVersion.activated_at.desc().nullslast())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def activate(self, name: str, version: str) -> PromptVersion:
        prompt = await self.get_by_name_version(name, version)
        if prompt is None:
            raise ValueError(f"Prompt version not found: {name}:{version}")
        await self.session.execute(
            update(PromptVersion)
            .where(PromptVersion.name == name, PromptVersion.status == "active")
            .values(status="inactive")
        )
        prompt.status = "active"
        prompt.activated_at = datetime.now(UTC)
        await self.session.flush()
        return prompt
