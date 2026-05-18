import pytest
import httpx

from app.core.config import Settings
from app.services.crmchat_connector import (
    CRMChatConnector,
    CRMChatMethodNotAllowedError,
    TelegramFloodWaitError,
    normalize_dialogs_response,
    parse_flood_wait_seconds,
)


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
