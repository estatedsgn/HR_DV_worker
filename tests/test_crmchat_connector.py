import json
from pathlib import Path

import pytest
import httpx

from app.core.config import Settings
from app.services.crmchat_connector import (
    CRMChatConnector,
    CRMChatMethodNotAllowedError,
    TelegramFloodWaitError,
    extract_reply_to_message_id,
    normalize_dialogs_response,
    normalize_messages_response,
    parse_flood_wait_seconds,
)


def test_extract_reply_to_message_id() -> None:
    assert extract_reply_to_message_id(
        {"id": 2736, "message": ".", "replyTo": {"_": "messageReplyHeader", "replyToMsgId": 2720}}
    ) == "2720"
    assert extract_reply_to_message_id({"id": 2730, "message": "hi"}) is None
    assert extract_reply_to_message_id({"id": 2730, "replyTo": {"_": "messageReplyHeader"}}) is None


def test_normalize_messages_response_captures_reply_to() -> None:
    payload = {
        "messages": [
            {"id": 2736, "message": ".", "replyTo": {"_": "messageReplyHeader", "replyToMsgId": 2720}},
            {"id": 2720, "message": "Поко м6 про"},
        ]
    }
    snaps = {s.message_id: s for s in normalize_messages_response(payload)}
    assert snaps["2736"].reply_to_message_id == "2720"
    assert snaps["2720"].reply_to_message_id is None


@pytest.mark.asyncio
async def test_call_telegram_method_uses_expected_url_and_payload() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"result": {"ok": True}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.crmchat.example",
    )
    connector = CRMChatConnector(settings=Settings(), http_client=client)

    result = await connector.call_telegram_method(
        "workspace-1",
        "account-1",
        "messages.sendMessage",
        {"message": "hello"},
    )

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://api.crmchat.example/v1/workspaces/workspace-1/"
        "telegram-accounts/account-1/call/messages.sendMessage"
    )
    assert captured["body"] == '{"params":{"message":"hello"}}'

    await client.aclose()


@pytest.mark.asyncio
async def test_get_dialogs_includes_required_hash_param() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"result": {"dialogs": []}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.crmchat.ai"
    )
    connector = CRMChatConnector(settings=Settings(), http_client=client)

    result = await connector.get_dialogs("workspace-1", "account-1", limit=5)

    assert result == {"dialogs": []}
    assert captured["body"] == (
        '{"params":{"offsetDate":0,"offsetId":0,'
        '"offsetPeer":{"_":"inputPeerEmpty"},"limit":5,"hash":"0"}}'
    )

    await client.aclose()


@pytest.mark.asyncio
async def test_get_history_includes_required_pagination_params() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"result": {"messages": []}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.crmchat.ai"
    )
    connector = CRMChatConnector(settings=Settings(), http_client=client)

    result = await connector.get_history(
        "workspace-1",
        "account-1",
        {"_": "inputPeerUser", "userId": 123, "accessHash": "hash"},
        limit=5,
    )

    assert result == {"messages": []}
    assert captured["body"] == (
        '{"params":{"peer":{"_":"inputPeerUser","userId":123,"accessHash":"hash"},'
        '"offsetId":0,"offsetDate":0,"addOffset":0,"limit":5,'
        '"maxId":0,"minId":0,"hash":"0"}}'
    )

    await client.aclose()


@pytest.mark.asyncio
async def test_send_reaction_uses_telegram_reaction_shape() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"result": {"ok": True}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.crmchat.ai"
    )
    connector = CRMChatConnector(settings=Settings(), http_client=client)

    await connector.send_reaction(
        "workspace-1",
        "account-1",
        {"_": "inputPeerUser", "userId": 123, "accessHash": "hash"},
        77,
        "👍",
    )

    assert captured["url"].endswith(
        "/v1/workspaces/workspace-1/telegram-accounts/account-1/call/messages.sendReaction"
    )
    assert captured["body"] == (
        '{"params":{"peer":{"_":"inputPeerUser","userId":123,"accessHash":"hash"},'
        '"msgId":77,"reaction":[{"_":"reactionEmoji","emoticon":"👍"}]}}'
    )

    await client.aclose()


@pytest.mark.asyncio
async def test_call_telegram_method_rejects_disallowed_method() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        base_url="https://api.crmchat.example",
    )
    connector = CRMChatConnector(settings=Settings(), http_client=client)

    with pytest.raises(CRMChatMethodNotAllowedError):
        await connector.call_telegram_method(
            "workspace-1", "account-1", "account.deleteAccount", {}
        )

    await client.aclose()


@pytest.mark.asyncio
async def test_flood_wait_error_is_parsed_from_api_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": "BAD_REQUEST",
                "message": "FLOOD_WAIT_42",
                "data": {"tlErrorMessage": "FLOOD_WAIT_42"},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.crmchat.example"
    )
    connector = CRMChatConnector(settings=Settings(), http_client=client)

    with pytest.raises(TelegramFloodWaitError) as exc_info:
        await connector.call_telegram_method(
            "workspace-1", "account-1", "messages.sendMessage", {}
        )

    assert exc_info.value.retry_after_seconds == 42
    await client.aclose()


def test_parse_flood_wait_seconds() -> None:
    assert parse_flood_wait_seconds("FLOOD_WAIT_42") == 42
    assert parse_flood_wait_seconds("BAD_REQUEST") is None


@pytest.mark.asyncio
async def test_set_voice_recording_uses_record_audio_action() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"result": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.crmchat.ai"
    )
    connector = CRMChatConnector(settings=Settings(), http_client=client)

    await connector.set_voice_recording(
        "workspace-1",
        "account-1",
        {"_": "inputPeerUser", "userId": 123, "accessHash": "hash"},
    )

    assert captured["url"].endswith("/call/messages.setTyping")
    assert captured["body"] == (
        '{"params":{"peer":{"_":"inputPeerUser","userId":123,"accessHash":"hash"},'
        '"action":{"_":"sendMessageRecordAudioAction"}}}'
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_send_voice_note_uploads_file_and_sends_media() -> None:
    calls = []
    voice_path = Path("data/voice_intro/voice_intro_01_offer_overview.ogg")

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), json.loads(request.content.decode())))
        return httpx.Response(200, json={"result": {"ok": True}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.crmchat.ai"
    )
    connector = CRMChatConnector(settings=Settings(), http_client=client)

    await connector.send_voice_note(
        "workspace-1",
        "account-1",
        {"_": "inputPeerUser", "userId": 123, "accessHash": "hash"},
        voice_path,
        "777",
        duration_seconds=12,
    )

    assert calls[0][0].endswith("/call/upload.saveFilePart")
    assert calls[0][1]["params"]["filePart"] == 0
    assert calls[0][1]["params"]["bytes"]
    assert calls[1][0].endswith("/call/messages.sendMedia")
    media = calls[1][1]["params"]["media"]
    assert media["_"] == "inputMediaUploadedDocument"
    assert media["mimeType"] == "audio/ogg"
    assert media["attributes"][0]["voice"] is True
    assert calls[1][1]["params"]["randomId"] == "777"
    await client.aclose()


def test_normalize_dialogs_response_joins_peer_data() -> None:
    snapshots = normalize_dialogs_response(
        {
            "dialogs": [
                {
                    "peer": {"_": "peerUser", "userId": "123"},
                    "topMessage": "10",
                    "unreadCount": 2,
                }
            ],
            "users": [
                {
                    "id": "123",
                    "accessHash": "hash",
                    "username": "lead",
                    "firstName": "Ada",
                }
            ],
            "messages": [{"id": "10", "message": "Hi"}],
        }
    )

    assert len(snapshots) == 1
    assert snapshots[0].peer.peer_type == "user"
    assert snapshots[0].peer.peer_id == "123"
    assert snapshots[0].peer.access_hash == "hash"
    assert snapshots[0].peer.username == "lead"
