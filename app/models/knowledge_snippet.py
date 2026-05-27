from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.vector import Vector
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class KnowledgeSnippet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Reusable template, objection answer, or knowledge item for brain RAG."""

    __tablename__ = "knowledge_snippets"

    snippet_type: Mapped[str] = mapped_column(String(50), default="template", index=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255), index=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding = mapped_column(Vector(1536), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
