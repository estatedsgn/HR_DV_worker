from __future__ import annotations

from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType):
    """Minimal pgvector SQLAlchemy type used for knowledge snippet embeddings."""

    cache_ok = True

    def __init__(self, dimensions: int | None = None) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw) -> str:  # noqa: ANN003
        if self.dimensions:
            return f"vector({self.dimensions})"
        return "vector"
