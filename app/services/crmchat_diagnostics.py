from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.services.crmchat_connector import TelegramDialogSnapshot, TelegramPeer

SENSITIVE_KEYS = frozenset(
    {
        "accessHash",
        "access_hash",
        "apiKey",
        "api_key",
        "authorization",
        "body",
        "message",
        "phone",
        "phoneNumber",
        "phone_number",
        "secret",
        "text",
        "token",
    }
)


def redact_value(value: Any) -> Any:
    """Recursively redact sensitive values while preserving response shape."""

    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if is_sensitive_key(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def is_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return key in SENSITIVE_KEYS or normalized in {
        item.lower() for item in SENSITIVE_KEYS
    }


def build_input_peer(peer: TelegramPeer) -> dict[str, Any]:
    """Build a Telegram Raw API InputPeer from a normalized dialog peer."""

    if peer.peer_type == "user":
        return {
            "_": "inputPeerUser",
            "userId": int_or_str(peer.peer_id),
            "accessHash": required_access_hash(peer),
        }
    if peer.peer_type == "channel":
        return {
            "_": "inputPeerChannel",
            "channelId": int_or_str(peer.peer_id),
            "accessHash": required_access_hash(peer),
        }
    if peer.peer_type == "chat":
        return {"_": "inputPeerChat", "chatId": int_or_str(peer.peer_id)}
    raise ValueError(f"Unsupported Telegram peer type: {peer.peer_type}")


def required_access_hash(peer: TelegramPeer) -> str:
    if not peer.access_hash:
        raise ValueError(
            f"Telegram {peer.peer_type} peer {peer.peer_id} has no access_hash"
        )
    return peer.access_hash


def int_or_str(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def summarize_dialogs(
    dialogs: Sequence[TelegramDialogSnapshot],
) -> list[dict[str, Any]]:
    return [summarize_dialog(dialog) for dialog in dialogs]


def summarize_dialog(dialog: TelegramDialogSnapshot) -> dict[str, Any]:
    return {
        "peer_type": dialog.peer.peer_type,
        "peer_id": dialog.peer.peer_id,
        "username": dialog.peer.username,
        "display_name": dialog.peer.display_name,
        "top_message_id": dialog.top_message_id,
        "unread_count": dialog.unread_count,
        "has_access_hash": bool(dialog.peer.access_hash),
    }
