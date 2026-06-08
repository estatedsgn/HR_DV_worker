from __future__ import annotations

from types import SimpleNamespace

from app.services.daivinchik.keyboards import BotMessage, Button
from app.services.daivinchik.service import DaivinchikService


def _svc(last_keyboard: list[list[str]] | None = None) -> DaivinchikService:
    # Bypass __init__ (which builds settings/notifier/state) — we only exercise
    # the pure keyboard-resolution logic here.
    svc = DaivinchikService.__new__(DaivinchikService)
    svc.state = SimpleNamespace(last_keyboard=last_keyboard or [])
    return svc


def _photo(message_id: int) -> BotMessage:
    return BotMessage(message_id=message_id, text="", rows=[])


def _card(message_id: int) -> BotMessage:
    rows = [[Button("❤️"), Button("👎"), Button("жалоба"), Button("💤")]]
    return BotMessage(message_id=message_id, text="✨🔍", rows=rows)


def test_effective_borrows_keyboard_from_nearby_message() -> None:
    svc = _svc()
    messages = [_card(100), _photo(101), _photo(102)]
    effective = svc._effective_message(messages)
    assert effective.message_id == 102  # newest drives the text/id
    assert [b.text for b in effective.buttons] == ["❤️", "👎", "жалоба", "💤"]


def test_effective_falls_back_to_remembered_keyboard() -> None:
    # No keyboard anywhere in the window, but we remembered one earlier.
    svc = _svc(last_keyboard=[["❤️", "👎", "жалоба", "💤"]])
    messages = [_photo(200), _photo(201)]
    effective = svc._effective_message(messages)
    assert effective.message_id == 201
    assert [b.text for b in effective.buttons] == ["❤️", "👎", "жалоба", "💤"]


def test_remember_keyboard_records_latest_reply_keyboard() -> None:
    svc = _svc()
    svc._remember_keyboard([_card(300), _photo(301)])
    assert svc.state.last_keyboard == [["❤️", "👎", "жалоба", "💤"]]
