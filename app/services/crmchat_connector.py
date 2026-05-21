import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings, get_settings

CRMCHAT_TELEGRAM_ALLOWED_METHODS = frozenset(
    {
        "contacts.resolveUsername",
        "contacts.search",
        "messages.getDialogs",
        "messages.getHistory",
        "messages.readHistory",
        "messages.sendMessage",
        "messages.editMessage",
    }
)

_FLOOD_WAIT_RE = re.compile(r"FLOOD_WAIT_(\d+)")


@dataclass(slots=True, frozen=True)
class CRMChatOrganization:
    id: str
    name: str | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class CRMChatWorkspace:
    id: str
    organization_id: str | None = None
    name: str | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class CRMChatTelegramAccount:
    id: str
    workspace_id: str | None = None
    status: str | None = None
    username: str | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class CRMChatBootstrapContext:
    organization: CRMChatOrganization
    workspace: CRMChatWorkspace
    telegram_account: CRMChatTelegramAccount


@dataclass(slots=True, frozen=True)
class TelegramPeer:
    peer_type: str
    peer_id: str
    access_hash: str | None = None
    username: str | None = None
    display_name: str | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class TelegramMessageSnapshot:
    message_id: str
    peer: TelegramPeer | None
    text: str | None = None
    date: str | None = None
    outgoing: bool = False
    raw: Mapping[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class TelegramDialogSnapshot:
    peer: TelegramPeer
    top_message_id: str | None = None
    unread_count: int | None = None
    raw: Mapping[str, Any] | None = None


class CRMChatConnectorError(Exception):
    """Base exception for CRMchat connector failures."""


class CRMChatConfigurationError(CRMChatConnectorError):
    """Raised when required CRMchat configuration is missing."""


class CRMChatMethodNotAllowedError(CRMChatConnectorError):
    """Raised when code tries to call a Telegram method outside the local allowlist."""


class CRMChatAPIError(CRMChatConnectorError):
    """Raised for non-successful CRMchat API responses."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class TelegramFloodWaitError(CRMChatAPIError):
    """Raised when Telegram asks the client to retry after FLOOD_WAIT_N seconds."""

    def __init__(
        self, retry_after_seconds: int, *, payload: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(
            f"Telegram FLOOD_WAIT_{retry_after_seconds}",
            status_code=429,
            payload=payload,
        )
        self.retry_after_seconds = retry_after_seconds


class CRMChatConnector:
    """Async adapter for CRMchat REST and Telegram Raw API calls.

    The connector is intentionally mockable: tests can pass an ``httpx.AsyncClient``
    with a mock transport, while production code can let the connector create a
    client from environment settings.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        allowed_methods: frozenset[str] = CRMCHAT_TELEGRAM_ALLOWED_METHODS,
    ) -> None:
        self.settings = settings or get_settings()
        self.allowed_methods = allowed_methods
        self._owns_http_client = http_client is None
        self.http_client = http_client or self._build_http_client()

    def _build_http_client(self) -> httpx.AsyncClient:
        if not self.settings.crmchat_api_base_url:
            raise CRMChatConfigurationError(
                "CRMCHAT_API_BASE_URL is required for CRMchat API calls"
            )
        if not self.settings.crmchat_api_key:
            raise CRMChatConfigurationError(
                "CRMCHAT_API_KEY is required for CRMchat API calls"
            )

        return httpx.AsyncClient(
            base_url=self.settings.crmchat_api_base_url,
            headers={"Authorization": f"Bearer {self.settings.crmchat_api_key}"},
            timeout=self.settings.crmchat_timeout_seconds,
        )

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def __aenter__(self) -> "CRMChatConnector":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> Mapping[str, Any]:
        response = await self.http_client.request(method, url, **kwargs)
        payload = self._decode_json(response)
        if response.is_error:
            self._raise_api_error(payload, response.status_code)
        return payload

    def _decode_json(self, response: httpx.Response) -> Mapping[str, Any]:
        data = response.json()
        if not isinstance(data, Mapping):
            raise CRMChatAPIError(
                "CRMchat response must be a JSON object",
                status_code=response.status_code,
            )
        return data

    def _raise_api_error(
        self, payload: Mapping[str, Any], status_code: int | None = None
    ) -> None:
        message = self._extract_error_message(payload)
        flood_wait_seconds = parse_flood_wait_seconds(message)
        if flood_wait_seconds is not None:
            raise TelegramFloodWaitError(flood_wait_seconds, payload=payload)
        raise CRMChatAPIError(
            message or "CRMchat API request failed",
            status_code=status_code,
            payload=payload,
        )

    def _extract_error_message(self, payload: Mapping[str, Any]) -> str:
        data = payload.get("data")
        candidates = [payload.get("message")]
        if isinstance(data, Mapping):
            candidates.extend([data.get("tlErrorMessage"), data.get("message")])
        return " ".join(str(candidate) for candidate in candidates if candidate)

    async def list_organizations(self) -> list[CRMChatOrganization]:
        payload = await self._request("GET", "/v1/organizations")
        return [parse_organization(item) for item in extract_collection(payload)]

    async def list_workspaces(
        self, organization_id: str | None = None
    ) -> list[CRMChatWorkspace]:
        effective_organization_id = (
            organization_id or self.settings.crmchat_organization_id
        )
        params = (
            {"organizationId": effective_organization_id}
            if effective_organization_id
            else None
        )
        payload = await self._request("GET", "/v1/workspaces", params=params)
        return [parse_workspace(item) for item in extract_collection(payload)]

    async def list_telegram_accounts(
        self, workspace_id: str
    ) -> list[CRMChatTelegramAccount]:
        payload = await self._request(
            "GET", f"/v1/workspaces/{workspace_id}/telegram-accounts"
        )
        return [
            parse_telegram_account(item, workspace_id=workspace_id)
            for item in extract_collection(payload)
        ]

    async def get_active_telegram_account(
        self, workspace_id: str
    ) -> CRMChatTelegramAccount:
        accounts = await self.list_telegram_accounts(workspace_id)
        for account in accounts:
            if account.status == "active":
                return account
        raise CRMChatAPIError("No active Telegram account found for workspace")

    async def bootstrap(self) -> CRMChatBootstrapContext:
        organization = await self._select_organization()
        workspace = await self._select_workspace(organization.id)
        telegram_account = await self._select_telegram_account(workspace.id)
        return CRMChatBootstrapContext(
            organization=organization,
            workspace=workspace,
            telegram_account=telegram_account,
        )

    async def _select_organization(self) -> CRMChatOrganization:
        organizations = await self.list_organizations()
        if self.settings.crmchat_organization_id:
            for organization in organizations:
                if organization.id == self.settings.crmchat_organization_id:
                    return organization
            raise CRMChatAPIError("Configured CRMCHAT_ORGANIZATION_ID was not found")
        if not organizations:
            raise CRMChatAPIError("No CRMchat organizations found")
        return organizations[0]

    async def _select_workspace(self, organization_id: str) -> CRMChatWorkspace:
        workspaces = await self.list_workspaces(organization_id)
        if self.settings.crmchat_workspace_id:
            for workspace in workspaces:
                if workspace.id == self.settings.crmchat_workspace_id:
                    return workspace
            raise CRMChatAPIError("Configured CRMCHAT_WORKSPACE_ID was not found")
        if not workspaces:
            raise CRMChatAPIError("No CRMchat workspaces found")
        return workspaces[0]

    async def _select_telegram_account(
        self, workspace_id: str
    ) -> CRMChatTelegramAccount:
        accounts = await self.list_telegram_accounts(workspace_id)
        if self.settings.crmchat_default_telegram_account_id:
            for account in accounts:
                if account.id == self.settings.crmchat_default_telegram_account_id:
                    return account
            raise CRMChatAPIError(
                "Configured CRMCHAT_DEFAULT_TELEGRAM_ACCOUNT_ID was not found"
            )
        for account in accounts:
            if account.status == "active":
                return account
        raise CRMChatAPIError("No active Telegram account found")

    async def call_telegram_method(
        self,
        workspace_id: str,
        account_id: str,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self._validate_telegram_method(method)
        payload = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/telegram-accounts/{account_id}/call/{method}",
            json={"params": dict(params or {})},
        )
        result = payload.get("result", payload)
        if not isinstance(result, Mapping):
            raise CRMChatAPIError(
                "Telegram method result must be a JSON object", payload=payload
            )
        return result

    def _validate_telegram_method(self, method: str) -> None:
        if method not in self.allowed_methods:
            raise CRMChatMethodNotAllowedError(
                f"Telegram method is not allowed: {method}"
            )

    async def resolve_username(
        self, workspace_id: str, account_id: str, username: str
    ) -> Mapping[str, Any]:
        return await self.call_telegram_method(
            workspace_id,
            account_id,
            "contacts.resolveUsername",
            {"username": username.lstrip("@")},
        )

    async def search_contacts(
        self,
        workspace_id: str,
        account_id: str,
        query: str,
        limit: int = 20,
    ) -> Mapping[str, Any]:
        return await self.call_telegram_method(
            workspace_id,
            account_id,
            "contacts.search",
            {"q": query, "limit": limit},
        )

    async def get_dialogs(
        self,
        workspace_id: str,
        account_id: str,
        limit: int = 50,
        offset_date: int = 0,
        offset_id: int = 0,
        offset_peer: Mapping[str, Any] | None = None,
        hash_value: int | str = "0",
    ) -> Mapping[str, Any]:
        return await self.call_telegram_method(
            workspace_id,
            account_id,
            "messages.getDialogs",
            {
                "offsetDate": offset_date,
                "offsetId": offset_id,
                "offsetPeer": offset_peer or {"_": "inputPeerEmpty"},
                "limit": limit,
                "hash": str(hash_value),
            },
        )

    async def get_history(
        self,
        workspace_id: str,
        account_id: str,
        peer: Mapping[str, Any],
        limit: int = 50,
        offset_id: int = 0,
        offset_date: int = 0,
        add_offset: int = 0,
        max_id: int = 0,
        min_id: int = 0,
        hash_value: int | str = "0",
    ) -> Mapping[str, Any]:
        return await self.call_telegram_method(
            workspace_id,
            account_id,
            "messages.getHistory",
            {
                "peer": dict(peer),
                "offsetId": offset_id,
                "offsetDate": offset_date,
                "addOffset": add_offset,
                "limit": limit,
                "maxId": max_id,
                "minId": min_id,
                "hash": str(hash_value),
            },
        )

    async def read_history(
        self,
        workspace_id: str,
        account_id: str,
        peer: Mapping[str, Any],
        max_id: int = 0,
    ) -> Mapping[str, Any]:
        return await self.call_telegram_method(
            workspace_id,
            account_id,
            "messages.readHistory",
            {"peer": dict(peer), "maxId": max_id},
        )

    async def send_message(
        self,
        workspace_id: str,
        account_id: str,
        peer: Mapping[str, Any],
        message: str,
        random_id: int | str,
    ) -> Mapping[str, Any]:
        return await self.call_telegram_method(
            workspace_id,
            account_id,
            "messages.sendMessage",
            {"peer": dict(peer), "message": message, "randomId": str(random_id)},
        )

    async def parse_incoming_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def fetch_dialog(self, dialog_id: str) -> dict[str, Any] | None:
        raise NotImplementedError("CRMchat dialog fetching is not implemented yet")


def extract_collection(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data", payload)
    if isinstance(data, Mapping):
        items = data.get("data", data.get("items", []))
    else:
        items = data
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def parse_organization(raw: Mapping[str, Any]) -> CRMChatOrganization:
    return CRMChatOrganization(
        id=str(raw.get("id")), name=optional_str(raw.get("name")), raw=raw
    )


def parse_workspace(raw: Mapping[str, Any]) -> CRMChatWorkspace:
    return CRMChatWorkspace(
        id=str(raw.get("id")),
        organization_id=optional_str(
            raw.get("organizationId") or raw.get("organization_id")
        ),
        name=optional_str(raw.get("name")),
        raw=raw,
    )


def parse_telegram_account(
    raw: Mapping[str, Any], workspace_id: str | None = None
) -> CRMChatTelegramAccount:
    return CRMChatTelegramAccount(
        id=str(raw.get("id")),
        workspace_id=workspace_id
        or optional_str(raw.get("workspaceId") or raw.get("workspace_id")),
        status=optional_str(raw.get("status")),
        username=optional_str(raw.get("username") or raw.get("telegramUsername")),
        raw=raw,
    )


def parse_flood_wait_seconds(message: str | None) -> int | None:
    if not message:
        return None
    match = _FLOOD_WAIT_RE.search(message)
    return int(match.group(1)) if match else None


def normalize_dialogs_response(
    payload: Mapping[str, Any],
) -> list[TelegramDialogSnapshot]:
    users_by_id = {
        str(user.get("id")): user
        for user in payload.get("users", [])
        if isinstance(user, Mapping)
    }
    chats_by_id = {
        str(chat.get("id")): chat
        for chat in payload.get("chats", [])
        if isinstance(chat, Mapping)
    }
    messages_by_id = {
        str(message.get("id")): message
        for message in payload.get("messages", [])
        if isinstance(message, Mapping)
    }

    snapshots: list[TelegramDialogSnapshot] = []
    for dialog in payload.get("dialogs", []):
        if not isinstance(dialog, Mapping):
            continue
        peer = normalize_peer(
            dialog.get("peer"), users_by_id=users_by_id, chats_by_id=chats_by_id
        )
        if peer is None:
            continue
        top_message_id = optional_str(
            dialog.get("topMessage") or dialog.get("top_message")
        )
        raw_message = messages_by_id.get(top_message_id or "")
        snapshots.append(
            TelegramDialogSnapshot(
                peer=peer,
                top_message_id=top_message_id,
                unread_count=optional_int(
                    dialog.get("unreadCount") or dialog.get("unread_count")
                ),
                raw={"dialog": dialog, "top_message": raw_message},
            )
        )
    return snapshots


def normalize_messages_response(
    payload: Mapping[str, Any], fallback_peer: TelegramPeer | None = None
) -> list[TelegramMessageSnapshot]:
    users_by_id = {
        str(user.get("id")): user
        for user in payload.get("users", [])
        if isinstance(user, Mapping)
    }
    chats_by_id = {
        str(chat.get("id")): chat
        for chat in payload.get("chats", [])
        if isinstance(chat, Mapping)
    }

    snapshots: list[TelegramMessageSnapshot] = []
    for raw_message in payload.get("messages", []):
        if not isinstance(raw_message, Mapping):
            continue
        message_id = optional_str(raw_message.get("id"))
        if not message_id:
            continue
        raw_peer = raw_message.get("peerId") or raw_message.get("peer_id")
        peer = (
            normalize_peer(raw_peer, users_by_id=users_by_id, chats_by_id=chats_by_id)
            or fallback_peer
        )
        snapshots.append(
            TelegramMessageSnapshot(
                message_id=message_id,
                peer=peer,
                text=optional_str(
                    raw_message.get("message")
                    or raw_message.get("text")
                    or raw_message.get("body")
                    or ""
                ),
                date=optional_str(raw_message.get("date")),
                outgoing=bool(raw_message.get("out") or raw_message.get("outgoing")),
                raw=raw_message,
            )
        )
    return snapshots


def build_input_peer_from_resolve_username(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if not isinstance(data, Mapping):
        raise ValueError("resolveUsername payload must be an object")

    peer = data.get("peer")
    if isinstance(peer, Mapping):
        peer_type = str(peer.get("_") or "").lower()
        if "peeruser" in peer_type and peer.get("userId") is not None:
            user_id = int(peer["userId"])
            access_hash = find_access_hash_for_user(data, user_id)
            if access_hash is None:
                raise ValueError("Unable to find accessHash for resolved user")
            return {
                "_": "inputPeerUser",
                "userId": user_id,
                "accessHash": str(access_hash),
            }

    users = data.get("users") if isinstance(data.get("users"), list) else []
    if users and isinstance(users[0], Mapping):
        user = users[0]
        if user.get("id") is None or user.get("accessHash") is None:
            raise ValueError("Resolved user payload missing id/accessHash")
        return {
            "_": "inputPeerUser",
            "userId": int(user["id"]),
            "accessHash": str(user["accessHash"]),
        }

    raise ValueError("Could not build inputPeerUser from resolveUsername payload")


def find_access_hash_for_user(data: Mapping[str, Any], user_id: int) -> str | None:
    users = data.get("users") if isinstance(data.get("users"), list) else []
    for raw_user in users:
        if not isinstance(raw_user, Mapping):
            continue
        if int(raw_user.get("id", -1)) == user_id and raw_user.get("accessHash") is not None:
            return str(raw_user["accessHash"])
    return None


def normalize_peer(
    raw_peer: Any,
    *,
    users_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    chats_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> TelegramPeer | None:
    if not isinstance(raw_peer, Mapping):
        return None
    constructor = raw_peer.get("_")
    users_by_id = users_by_id or {}
    chats_by_id = chats_by_id or {}

    if constructor in {"peerUser", "inputPeerUser"}:
        peer_id = str(
            raw_peer.get("userId") or raw_peer.get("user_id") or raw_peer.get("id")
        )
        user = users_by_id.get(peer_id, {})
        return TelegramPeer(
            peer_type="user",
            peer_id=peer_id,
            access_hash=optional_str(
                raw_peer.get("accessHash") or user.get("accessHash")
            ),
            username=optional_str(user.get("username")),
            display_name=join_display_name(user.get("firstName"), user.get("lastName")),
            raw={"peer": raw_peer, "user": user},
        )

    if constructor in {"peerChannel", "inputPeerChannel"}:
        peer_id = str(
            raw_peer.get("channelId")
            or raw_peer.get("channel_id")
            or raw_peer.get("id")
        )
        chat = chats_by_id.get(peer_id, {})
        return TelegramPeer(
            peer_type="channel",
            peer_id=peer_id,
            access_hash=optional_str(
                raw_peer.get("accessHash") or chat.get("accessHash")
            ),
            username=optional_str(chat.get("username")),
            display_name=optional_str(chat.get("title")),
            raw={"peer": raw_peer, "chat": chat},
        )

    if constructor in {"peerChat", "inputPeerChat"}:
        peer_id = str(
            raw_peer.get("chatId") or raw_peer.get("chat_id") or raw_peer.get("id")
        )
        chat = chats_by_id.get(peer_id, {})
        return TelegramPeer(
            peer_type="chat",
            peer_id=peer_id,
            display_name=optional_str(chat.get("title")),
            raw={"peer": raw_peer, "chat": chat},
        )
    return None


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def join_display_name(first_name: Any, last_name: Any) -> str | None:
    parts = [str(part) for part in (first_name, last_name) if part]
    return " ".join(parts) or None
