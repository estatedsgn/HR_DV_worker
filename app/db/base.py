from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Import models so Alembic can discover table metadata from Base.metadata.
from app.models import (  # noqa: E402,F401
    Account,
    AgentActionLog,
    Dialog,
    HumanHandoff,
    Lead,
    Message,
    OutboundSendLog,
)
