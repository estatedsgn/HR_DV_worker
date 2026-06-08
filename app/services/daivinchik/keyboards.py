"""Parse Telegram reply markup (as returned by messages.getHistory) into a
structured, easy-to-reason-about keyboard.

CRMChat returns raw Telegram TL objects as JSON with camelCase keys and the
constructor name under ``_``. We stay defensive about key naming because
different bridge versions occasionally snake_case things.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class Button:
    """A single keyboard button.

    kind:
      - "text":     a reply-keyboard button. Pressing it == sending ``text``.
      - "callback": an inline button. Pressing it == getBotCallbackAnswer(data).
      - "url":      an inline link button. We never auto-open these.
    """

    text: str
    kind: str = "text"
    data: str | None = None
    url: str | None = None


@dataclass(slots=True, frozen=True)
class BotMessage:
    message_id: int
    text: str
    rows: list[list[Button]] = field(default_factory=list)
    outgoing: bool = False
    # Links/user-ids harvested from message entities. Дайвинчик puts the matched
    # person's Telegram link on their *name* (a text-url / mention entity) rather
    # than as a plain @handle in the text, so we must read entities to get it.
    entity_links: tuple[str, ...] = ()
    mention_user_ids: tuple[str, ...] = ()
    raw: Mapping[str, Any] | None = None

    @property
    def buttons(self) -> list[Button]:
        """All buttons flattened left-to-right, top-to-bottom."""
        return [button for row in self.rows for button in row]

    @property
    def has_buttons(self) -> bool:
        return any(self.rows)

    @property
    def is_inline(self) -> bool:
        return any(b.kind in {"callback", "url"} for b in self.buttons)

    def button_from_right(self, position: int) -> Button | None:
        """1 == rightmost, 2 == second from the right, across all buttons."""
        flat = self.buttons
        if position < 1 or position > len(flat):
            return None
        return flat[len(flat) - position]


def _get(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _parse_button(raw: Mapping[str, Any]) -> Button:
    constructor = str(_get(raw, "_", default="")).lower()
    text = str(_get(raw, "text", default="")).strip()
    if "callback" in constructor:
        data = _get(raw, "data")
        return Button(text=text, kind="callback", data=str(data) if data is not None else None)
    if "url" in constructor:
        return Button(text=text, kind="url", url=_get(raw, "url"))
    return Button(text=text, kind="text")


def parse_reply_markup(raw_markup: Any) -> list[list[Button]]:
    if not isinstance(raw_markup, Mapping):
        return []
    rows: list[list[Button]] = []
    for raw_row in _get(raw_markup, "rows", default=[]) or []:
        if not isinstance(raw_row, Mapping):
            continue
        buttons: list[Button] = []
        for raw_button in _get(raw_row, "buttons", default=[]) or []:
            if isinstance(raw_button, Mapping):
                buttons.append(_parse_button(raw_button))
        if buttons:
            rows.append(buttons)
    return rows


def parse_entities(raw_message: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (entity_links, mention_user_ids) from a raw message's entities."""
    links: list[str] = []
    user_ids: list[str] = []
    for entity in _get(raw_message, "entities", default=[]) or []:
        if not isinstance(entity, Mapping):
            continue
        constructor = str(_get(entity, "_", default="")).lower()
        url = _get(entity, "url")
        if url:
            links.append(str(url))
        if "mentionname" in constructor:
            user_id = _get(entity, "userId", "user_id")
            if user_id is not None:
                user_ids.append(str(user_id))
    return tuple(links), tuple(user_ids)


def parse_bot_message(raw_message: Mapping[str, Any]) -> BotMessage | None:
    """Build a BotMessage from a raw Telegram message dict.

    ``raw_message`` is the ``raw`` field of a TelegramMessageSnapshot (the full
    TL message object). Returns None if it lacks a usable id.
    """
    if not isinstance(raw_message, Mapping):
        return None
    raw_id = _get(raw_message, "id")
    if raw_id is None or not str(raw_id).isdigit():
        return None
    text = str(
        _get(raw_message, "message", "text", "body", default="") or ""
    )
    raw_markup = _get(raw_message, "replyMarkup", "reply_markup")
    entity_links, mention_user_ids = parse_entities(raw_message)
    return BotMessage(
        message_id=int(raw_id),
        text=text,
        rows=parse_reply_markup(raw_markup),
        outgoing=bool(_get(raw_message, "out", "outgoing", default=False)),
        entity_links=entity_links,
        mention_user_ids=mention_user_ids,
        raw=raw_message,
    )
