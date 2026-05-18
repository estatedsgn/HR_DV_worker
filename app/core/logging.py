import logging
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from app.core.config import get_settings

_LOG_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})
_CONTEXT_FIELDS = ("account_id", "dialog_id", "message_id")


class ContextFilter(logging.Filter):
    """Inject known context fields into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _LOG_CONTEXT.get()
        for field in _CONTEXT_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, context.get(field, "-"))
        return True


class StructuredFormatter(logging.Formatter):
    """Format records as compact key-value structured logs."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        timestamp = self.formatTime(record, self.datefmt)
        fields: Mapping[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "account_id": getattr(record, "account_id", "-"),
            "dialog_id": getattr(record, "dialog_id", "-"),
            "message_id": getattr(record, "message_id", "-"),
        }
        return " ".join(f"{key}={value!r}" for key, value in fields.items())


def bind_log_context(**kwargs: Any):
    """Bind account/dialog/message identifiers for logs emitted in the current context."""

    current = _LOG_CONTEXT.get().copy()
    current.update({key: value for key, value in kwargs.items() if key in _CONTEXT_FIELDS})
    return _LOG_CONTEXT.set(current)


def reset_log_context(token) -> None:
    _LOG_CONTEXT.reset(token)


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter(datefmt="%Y-%m-%dT%H:%M:%S%z"))
    handler.addFilter(ContextFilter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.log_level.upper())
