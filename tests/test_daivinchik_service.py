from __future__ import annotations

import asyncio
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


class _BurstConnector:
    """Фейковый коннектор: эмулирует messages.getHistory с пагинацией.

    Хранит полный список raw-сообщений; на каждый вызов отдаёт страницу из `limit`
    самых новых сообщений с id < offset_id (offset_id=0 => самые новые), как Telegram.
    """

    def __init__(self, raw_messages: list[dict]) -> None:
        self._all = sorted(raw_messages, key=lambda m: int(m["id"]), reverse=True)
        self.calls = 0

    async def get_history(self, ws, acc, peer, *, limit=50, offset_id=0, **kwargs):
        self.calls += 1
        pool = [m for m in self._all if offset_id == 0 or int(m["id"]) < offset_id]
        return {"messages": pool[:limit]}


def _fetch_svc(max_pages: int = 8) -> DaivinchikService:
    svc = DaivinchikService.__new__(DaivinchikService)
    svc.cfg = SimpleNamespace(daivinchik_history_max_pages=max_pages)
    return svc


def _raw(message_id: int, text: str = "") -> dict:
    return {"id": str(message_id), "message": text, "out": False}


def test_fetch_messages_pages_back_to_cover_cursor() -> None:
    # Бурст из 100 сообщений; матч лежит на id=30 — далеко за первым окном (61..100).
    raw = [_raw(i) for i in range(1, 101)]
    raw[29] = _raw(30, "Взаимная симпатия! Начинай общаться 👉 @match_user")
    conn = _BurstConnector(raw)
    ctx = SimpleNamespace(workspace=SimpleNamespace(id="w"), telegram_account=SimpleNamespace(id="a"))

    svc = _fetch_svc()
    messages = asyncio.run(
        svc._fetch_messages(conn, ctx, {"_": "peer"}, limit=40, min_id=10)
    )

    ids = {m.message_id for m in messages}
    # Без пагинации вернулись бы только 61..100 и матч на id=30 потерялся бы навсегда.
    assert 30 in ids
    assert conn.calls >= 2  # пришлось листать назад
    matched = next(m for m in messages if m.message_id == 30)
    assert "@match_user" in matched.text


def test_fetch_messages_single_page_when_no_cursor() -> None:
    raw = [_raw(i) for i in range(1, 101)]
    conn = _BurstConnector(raw)
    ctx = SimpleNamespace(workspace=SimpleNamespace(id="w"), telegram_account=SimpleNamespace(id="a"))

    svc = _fetch_svc()
    messages = asyncio.run(
        svc._fetch_messages(conn, ctx, {"_": "peer"}, limit=40, min_id=0)
    )

    # Без курсора (свежий старт) не листаем всю историю — одна страница.
    assert conn.calls == 1
    assert len(messages) == 40


def test_fetch_messages_respects_page_cap() -> None:
    raw = [_raw(i) for i in range(1, 1001)]
    conn = _BurstConnector(raw)
    ctx = SimpleNamespace(workspace=SimpleNamespace(id="w"), telegram_account=SimpleNamespace(id="a"))

    svc = _fetch_svc(max_pages=3)
    asyncio.run(svc._fetch_messages(conn, ctx, {"_": "peer"}, limit=40, min_id=1))

    assert conn.calls == 3  # остановились на лимите страниц, не зациклились
