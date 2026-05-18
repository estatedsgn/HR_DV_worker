import pytest

from app.services.crmchat_connector import TelegramDialogSnapshot, TelegramPeer
from app.services.crmchat_diagnostics import (
    build_input_peer,
    redact_value,
    summarize_dialogs,
)


def test_redact_value_preserves_shape_and_redacts_sensitive_keys() -> None:
    payload = {
        "accessHash": "secret-hash",
        "nested": {
            "message": "private text",
            "safe": "visible",
            "items": [{"phoneNumber": "+123", "id": "1"}],
        },
    }

    assert redact_value(payload) == {
        "accessHash": "<redacted>",
        "nested": {
            "message": "<redacted>",
            "safe": "visible",
            "items": [{"phoneNumber": "<redacted>", "id": "1"}],
        },
    }


def test_build_input_peer_for_user() -> None:
    peer = TelegramPeer(peer_type="user", peer_id="123", access_hash="hash")

    assert build_input_peer(peer) == {
        "_": "inputPeerUser",
        "userId": 123,
        "accessHash": "hash",
    }


def test_build_input_peer_requires_access_hash_for_user() -> None:
    peer = TelegramPeer(peer_type="user", peer_id="123")

    with pytest.raises(ValueError, match="has no access_hash"):
        build_input_peer(peer)


def test_build_input_peer_for_chat() -> None:
    peer = TelegramPeer(peer_type="chat", peer_id="456")

    assert build_input_peer(peer) == {"_": "inputPeerChat", "chatId": 456}


def test_summarize_dialogs_does_not_include_access_hash() -> None:
    dialog = TelegramDialogSnapshot(
        peer=TelegramPeer(
            peer_type="user",
            peer_id="123",
            access_hash="secret-hash",
            username="lead",
            display_name="Lead",
        ),
        top_message_id="10",
        unread_count=2,
    )

    assert summarize_dialogs([dialog]) == [
        {
            "peer_type": "user",
            "peer_id": "123",
            "username": "lead",
            "display_name": "Lead",
            "top_message_id": "10",
            "unread_count": 2,
            "has_access_hash": True,
        }
    ]
