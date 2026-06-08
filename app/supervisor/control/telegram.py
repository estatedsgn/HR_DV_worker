"""Telegram-пульт: кнопки ▶️ Старт / ⏹ Стоп / 📊 Статус / 📜 Логи.

Реализован на голом Bot API через httpx (long polling) — без новых
зависимостей. Отвечает только владельцу: chat_id берётся из настроек или
запоминается при первом /start (тогда подсказывает вписать его в .env).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.supervisor.control.base import ControlAdapter
from app.supervisor.supervisor import Supervisor

_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "▶️ Старт", "callback_data": "start"},
            {"text": "⏹ Стоп", "callback_data": "stop"},
        ],
        [
            {"text": "📊 Статус", "callback_data": "status"},
            {"text": "📜 Логи", "callback_data": "logs"},
        ],
    ]
}

_HELP = (
    "🎛 Пульт агента Profitcast.\n\n"
    "▶️ Старт — поднять Docker/Postgres, миграции, проверить CRMChat и запустить агента.\n"
    "⏹ Стоп — остановить агента (работает, пока не нажмёшь).\n"
    "📊 Статус — текущее состояние.\n"
    "📜 Логи — последние строки автопилота.\n\n"
    "Команды: /start /menu /status /stop /logs"
)


class TelegramControlAdapter(ControlAdapter):
    def __init__(
        self,
        supervisor: Supervisor,
        *,
        token: str,
        admin_chat_id: str | None,
        root: Path,
        poll_timeout: int = 30,
    ) -> None:
        self._token = token
        self._admin_chat_id = str(admin_chat_id) if admin_chat_id else None
        self._root = root
        self._poll_timeout = poll_timeout
        self._base = f"https://api.telegram.org/bot{token}"
        self._client: httpx.AsyncClient | None = None
        self._offset: int | None = None
        super().__init__(supervisor)

    # ---------------------------------------------------------------- helpers
    async def _api(self, method: str, **payload) -> dict | None:
        assert self._client is not None
        try:
            resp = await self._client.post(f"{self._base}/{method}", json=payload)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[telegram] {method} error: {exc}")
            return None
        if not data.get("ok"):
            print(f"[telegram] {method} not ok: {data}")
        return data

    def _authorized(self, chat_id: str) -> bool:
        if self._admin_chat_id is None:
            # Первый, кто написал, становится владельцем на эту сессию.
            self._admin_chat_id = chat_id
            return True
        return chat_id == self._admin_chat_id

    async def notify(self, text: str) -> None:
        if self._admin_chat_id is None or self._client is None:
            return
        await self._api(
            "sendMessage",
            chat_id=self._admin_chat_id,
            text=text,
            reply_markup=_KEYBOARD,
        )

    async def _send(self, chat_id: str, text: str) -> None:
        await self._api("sendMessage", chat_id=chat_id, text=text, reply_markup=_KEYBOARD)

    # ------------------------------------------------------------------- loop
    async def run(self) -> None:
        timeout = httpx.Timeout(self._poll_timeout + 15)
        async with httpx.AsyncClient(timeout=timeout) as client:
            self._client = client
            await self._api("deleteWebhook", drop_pending_updates=False)
            startup = "🎛 Пульт запущен и слушает команды."
            if self._admin_chat_id:
                await self.notify(startup)
            else:
                print("[telegram] CONTROL_ADMIN_CHAT_ID не задан — напиши боту /start, "
                      "чтобы привязать чат.")
            try:
                await self._poll_loop()
            finally:
                await self.supervisor.shutdown()

    async def _poll_loop(self) -> None:
        while True:
            data = await self._api(
                "getUpdates",
                offset=self._offset,
                timeout=self._poll_timeout,
                allowed_updates=["message", "callback_query"],
            )
            if not data or not data.get("ok"):
                await asyncio.sleep(3)
                continue
            for update in data["result"]:
                self._offset = update["update_id"] + 1
                try:
                    await self._handle(update)
                except Exception as exc:  # noqa: BLE001
                    print(f"[telegram] handler error: {exc}")

    async def _handle(self, update: dict) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            chat_id = str(cq["message"]["chat"]["id"])
            await self._api("answerCallbackQuery", callback_query_id=cq["id"])
            if not self._authorized(chat_id):
                return
            await self._dispatch(chat_id, cq.get("data", ""))
            return

        message = update.get("message")
        if not message or "text" not in message:
            return
        chat_id = str(message["chat"]["id"])
        text = message["text"].strip().lstrip("/").split("@")[0].lower()
        if not self._authorized(chat_id):
            await self._api(
                "sendMessage",
                chat_id=chat_id,
                text="⛔️ Этот пульт привязан к другому владельцу.",
            )
            return
        if text in ("start", "menu", "help"):
            await self._send(chat_id, _HELP + f"\n\nТвой chat_id: {chat_id}\n"
                             f"(впиши в .env CONTROL_ADMIN_CHAT_ID={chat_id} для постоянной привязки)")
            await self._send(chat_id, self.supervisor.status_text())
            return
        await self._dispatch(chat_id, text)

    async def _dispatch(self, chat_id: str, action: str) -> None:
        if action == "start":
            asyncio.create_task(self.supervisor.start())
        elif action == "stop":
            asyncio.create_task(self.supervisor.stop())
        elif action == "status":
            await self._send(chat_id, self.supervisor.status_text())
        elif action == "logs":
            await self._send(chat_id, "📜 Логи автопилота:\n" + self.supervisor.logs_text())
        else:
            await self._send(chat_id, _HELP)
