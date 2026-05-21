from datetime import UTC, datetime

from app.services.crmchat_connector import (
    TelegramPeer,
    normalize_messages_response,
)
from app.services.crmchat_connector import TelegramDialogSnapshot
from app.services.telegram_polling import (
    build_dialog_external_id,
    build_message_external_id,
    initial_dialog_status,
    parse_telegram_datetime,
    should_sync_dialog,
)


def test_normalize_messages_response_uses_fallback_peer_and_direction() -> None:
    peer = TelegramPeer(peer_type="user", peer_id="123", access_hash="hash")

    messages = normalize_messages_response(
        {
            "messages": [
                {"id": "10", "message": "hi", "date": "2026-05-18T00:00:00Z"},
                {"id": "11", "message": "hello", "out": True},
            ]
        },
        fallback_peer=peer,
    )

    assert len(messages) == 2
    assert messages[0].peer == peer
    assert messages[0].message_id == "10"
    assert messages[0].text == "hi"
    assert not messages[0].outgoing
    assert messages[1].outgoing


def test_polling_external_ids_are_stable() -> None:
    peer = TelegramPeer(peer_type="user", peer_id="123", access_hash="hash")

    class DialogSnapshot:
        pass

    snapshot = DialogSnapshot()
    snapshot.peer = peer

    assert (
        build_dialog_external_id("account-1", snapshot) == "telegram:account-1:user:123"
    )
    assert (
        build_message_external_id("workspace-1", "account-1", "user", "123", "10")
        == "telegram:workspace-1:account-1:user:123:10"
    )


def test_parse_telegram_datetime_accepts_unix_and_iso_values() -> None:
    assert parse_telegram_datetime("0") == datetime.fromtimestamp(0, UTC)
    assert parse_telegram_datetime("2026-05-18T00:00:00Z") == datetime(
        2026, 5, 18, tzinfo=UTC
    )
    assert parse_telegram_datetime(None) is None
    assert parse_telegram_datetime("not-a-date") is None


def test_initial_dialog_status_ignores_non_user_and_bot_dialogs() -> None:
    human = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="123", raw={"user": {"bot": False}})
    )
    bot = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="456", raw={"user": {"bot": True}})
    )
    channel = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="channel", peer_id="789")
    )

    assert initial_dialog_status(human) == "pending_review"
    assert initial_dialog_status(bot) == "ignored"
    assert initial_dialog_status(channel) == "ignored"


def test_should_sync_dialog_filters_by_username() -> None:
    target = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="123", username="IamNekiy")
    )
    other = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="456", username="someone_else")
    )
    missing = TelegramDialogSnapshot(peer=TelegramPeer(peer_type="user", peer_id="789"))

    assert should_sync_dialog(target, "@iamnekiy")
    assert not should_sync_dialog(other, "@iamnekiy")
    assert not should_sync_dialog(missing, "@iamnekiy")
