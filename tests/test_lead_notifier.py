from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.lead_notifier import LeadNotifier


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "control_bot_token": "token",
        "control_admin_chat_id": "123",
        "lead_alerts_enabled": True,
        "handoff_bot_token": None,
        "handoff_chat_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_disabled_without_credentials() -> None:
    assert LeadNotifier(_settings(control_bot_token=None)).enabled is False
    assert LeadNotifier(_settings(control_admin_chat_id=None)).enabled is False
    assert LeadNotifier(_settings(lead_alerts_enabled=False)).enabled is False
    assert LeadNotifier(_settings()).enabled is True


def test_handoff_enabled_falls_back_to_control_bot() -> None:
    # Без выделенного хендофф-бота используем supervisor-чат.
    assert LeadNotifier(_settings()).handoff_enabled is True
    # Выделенный бот включает хендофф даже без control-чата.
    notifier = LeadNotifier(
        _settings(control_bot_token=None, control_admin_chat_id=None, handoff_bot_token="ht", handoff_chat_id="999")
    )
    assert notifier.handoff_enabled is True
    assert notifier._handoff_token == "ht"
    assert notifier._handoff_chat_id == "999"
    # Глобальный выключатель гасит и хендофф.
    assert LeadNotifier(_settings(lead_alerts_enabled=False)).handoff_enabled is False


@pytest.mark.asyncio
async def test_handoff_sends_via_dedicated_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier = LeadNotifier(_settings(handoff_bot_token="HTOKEN", handoff_chat_id="555"))
    posted: dict = {}

    class _Resp:
        @staticmethod
        def json():
            return {"ok": True}

    async def fake_post(self, url, json):  # noqa: ANN001
        posted["url"] = url
        posted["json"] = json
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    await notifier.notify_handoff(
        telegram_username="@lead",
        account_label=None,
        profile={"candidate_name": "Аня"},
    )
    assert "HTOKEN" in posted["url"]
    assert posted["json"]["chat_id"] == "555"
    assert "нужно устроить на работу" in posted["json"]["text"]


@pytest.mark.asyncio
async def test_send_noops_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier = LeadNotifier(_settings(lead_alerts_enabled=False))

    called = False

    async def fail_post(*args, **kwargs):  # pragma: no cover - must not run
        nonlocal called
        called = True

    monkeypatch.setattr("httpx.AsyncClient.post", fail_post)
    await notifier.notify_new_lead(
        telegram_username="@lead",
        account_label="acc-1",
        source="internal",
    )
    assert called is False


@pytest.mark.asyncio
async def test_new_lead_message_format() -> None:
    notifier = LeadNotifier(_settings())
    sent: list[str] = []

    async def capture(text: str) -> None:
        sent.append(text)

    notifier._send = capture  # type: ignore[method-assign]

    await notifier.notify_new_lead(
        telegram_username="@lead",
        account_label="acc-1",
        source="telegram",
        first_message="Здравствуйте! Удобно обсудить?",
    )

    assert len(sent) == 1
    text = sent[0]
    assert "🆕 Новый лид" in text
    assert "@lead" in text
    assert "acc-1" in text
    assert "telegram" in text
    assert "Очередь на первое сообщение поставлена" in text
    assert "Здравствуйте! Удобно обсудить?" in text


@pytest.mark.asyncio
async def test_handoff_message_format() -> None:
    notifier = LeadNotifier(_settings())
    sent: list[str] = []

    async def capture(text: str, **kwargs) -> None:
        sent.append(text)

    notifier._send = capture  # type: ignore[method-assign]

    await notifier.notify_handoff(
        telegram_username="@lead",
        account_label="acc-1",
        profile={
            "candidate_name": "Аня",
            "phone_number": "+7 999 123-45-67",
            "age": 19,
            "interview_day_confirmed": True,
            "interview_time": "14:00",
            "phone_model": "iPhone 13",
            "room_available": True,
            "profile_info": "учусь в универе, люблю рисовать",
        },
        reason="funnel_requested_handoff",
    )

    assert len(sent) == 1
    text = sent[0]
    assert "нужно устроить на работу" in text
    assert "@lead" in text
    assert "acc-1" in text
    assert "Аня" in text
    assert "+7 999 123-45-67" in text
    assert "19" in text
    assert "завтра 14:00" in text
    assert "iPhone 13" in text
    assert "есть" in text  # отдельная комната
    assert "funnel_requested_handoff" in text


def test_handoff_card_skips_missing_fields() -> None:
    rows = LeadNotifier.build_handoff_card(
        {"candidate_name": "Лена", "phone_number": "", "room_available": False}
    )
    labels = {label for label, _ in rows}
    assert "Имя" in labels
    assert "Телефон" not in labels  # пустая строка отбрасывается
    assert ("Отдельная комната", "нет") in rows


def test_handoff_card_prefers_custom_datetime() -> None:
    rows = dict(
        LeadNotifier.build_handoff_card(
            {"custom_interview_datetime": "в субботу в 12", "interview_time": "14:00"}
        )
    )
    assert rows["Собеседование"] == "в субботу в 12"
