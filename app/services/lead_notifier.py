from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_PREVIEW_LIMIT = 200


class LeadNotifier:
    """Best-effort Telegram alert to the supervisor admin chat on lead intake.

    Reuses the supervisor control-bot credentials (CONTROL_BOT_TOKEN /
    CONTROL_ADMIN_CHAT_ID) so production gets a ping the moment a lead is
    recorded and the first outreach message is queued. Sending is best-effort:
    a failed alert must never break intake.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.lead_alerts_enabled
            and self.settings.control_bot_token
            and self.settings.control_admin_chat_id
        )

    @property
    def _handoff_token(self) -> str | None:
        return self.settings.handoff_bot_token or self.settings.control_bot_token

    @property
    def _handoff_chat_id(self) -> str | None:
        return self.settings.handoff_chat_id or self.settings.control_admin_chat_id

    @property
    def handoff_enabled(self) -> bool:
        return bool(
            self.settings.lead_alerts_enabled
            and self._handoff_token
            and self._handoff_chat_id
        )

    async def notify_new_lead(
        self,
        *,
        telegram_username: str,
        account_label: str,
        source: str,
        first_message: str | None = None,
    ) -> None:
        lines = [
            "🆕 Новый лид",
            f"• Контакт: {telegram_username}",
            f"• Источник: {source}",
            f"• Аккаунт: {account_label}",
            "• Очередь на первое сообщение поставлена ✅",
        ]
        if first_message:
            preview = first_message.strip()
            if len(preview) > _PREVIEW_LIMIT:
                preview = preview[: _PREVIEW_LIMIT - 1] + "…"
            lines.append(f"• Первое сообщение: {preview}")
        await self._send("\n".join(lines))

    async def notify_intake_blocked(
        self,
        *,
        telegram_username: str,
        source: str,
        reason: str,
    ) -> None:
        await self._send(
            "⚠️ Лид не принят в работу\n"
            f"• Контакт: {telegram_username}\n"
            f"• Источник: {source}\n"
            f"• Причина: {reason}"
        )

    async def notify_handoff(
        self,
        *,
        telegram_username: str | None,
        account_label: str | None,
        profile: dict[str, Any] | None,
        reason: str | None = None,
    ) -> None:
        """Уведомить человека, что воронка собрала всю инфу и лида пора устраивать.

        Шлёт карточку кандидата (имя, телефон, возраст, время собеса, оборудование
        и т.д.) в выделенный хендофф-бот (HANDOFF_BOT_TOKEN / HANDOFF_CHAT_ID),
        либо в supervisor-чат как фолбэк. Best-effort: сбой алерта не должен
        ломать сам хендофф.
        """
        profile = profile or {}
        lines = [
            "🤝 Лид готов — нужно устроить на работу!",
            f"• Контакт: {telegram_username or '—'}",
        ]
        if account_label:
            lines.append(f"• Аккаунт: {account_label}")
        for label, value in _handoff_card_rows(profile):
            lines.append(f"• {label}: {value}")
        if reason:
            lines.append(f"• Причина передачи: {reason}")
        lines.append("• Дальше связывается человек 🙌")
        await self._send(
            "\n".join(lines),
            token=self._handoff_token,
            chat_id=self._handoff_chat_id,
        )

    @staticmethod
    def build_handoff_card(profile: dict[str, Any] | None) -> list[tuple[str, str]]:
        return _handoff_card_rows(profile or {})

    async def _send(
        self,
        text: str,
        *,
        token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        token = token or self.settings.control_bot_token
        chat_id = chat_id or self.settings.control_admin_chat_id
        if not (self.settings.lead_alerts_enabled and token and chat_id):
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": text},
                )
            data = response.json()
            if not data.get("ok"):
                logger.warning("lead notify rejected by Telegram: %s", data)
        except (httpx.HTTPError, ValueError) as exc:  # noqa: BLE001
            logger.warning("lead notify failed: %s", exc)


def _handoff_card_rows(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """Собрать читаемую карточку кандидата из собранного воронкой профиля."""
    rows: list[tuple[str, str]] = []

    name = _clean(profile.get("candidate_name"))
    if name:
        rows.append(("Имя", name))

    phone = _clean(profile.get("phone_number"))
    if phone:
        rows.append(("Телефон", phone))

    age = _clean(profile.get("age"))
    if age:
        rows.append(("Возраст", age))

    when = _interview_slot(profile)
    if when:
        rows.append(("Собеседование", when))

    phone_model = _clean(profile.get("phone_model"))
    if phone_model:
        rows.append(("Телефон (модель)", phone_model))

    room = profile.get("room_available")
    if room is True:
        rows.append(("Отдельная комната", "есть"))
    elif room is False:
        rows.append(("Отдельная комната", "нет"))

    about = _clean(profile.get("profile_info") or profile.get("work_or_study") or profile.get("hobbies"))
    if about:
        rows.append(("О себе", _truncate(about)))

    return rows


def _interview_slot(profile: dict[str, Any]) -> str | None:
    custom = _clean(profile.get("custom_interview_datetime"))
    if custom:
        return custom
    day = _clean(profile.get("interview_day"))
    if not day and profile.get("interview_day_confirmed") is True:
        day = "завтра"
    time = _clean(profile.get("interview_time"))
    parts = [part for part in (day, time) if part]
    return " ".join(parts) if parts else None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truncate(text: str) -> str:
    if len(text) > _PREVIEW_LIMIT:
        return text[: _PREVIEW_LIMIT - 1] + "…"
    return text
