from __future__ import annotations

import random

from app.services.daivinchik.classifier import classify, extract_lead
from app.services.daivinchik.keyboards import parse_bot_message


def _reply_row(*texts: str) -> dict:
    return {"_": "keyboardButtonRow", "buttons": [{"_": "keyboardButton", "text": t} for t in texts]}


def _inline_row(*pairs: tuple[str, str]) -> dict:
    return {
        "_": "keyboardButtonRow",
        "buttons": [
            {"_": "keyboardButtonCallback", "text": text, "data": data} for text, data in pairs
        ],
    }


def _msg(message_id: int, text: str, rows: list[dict] | None = None, *, inline: bool = False) -> dict:
    raw: dict = {"id": message_id, "message": text}
    if rows is not None:
        markup_type = "replyInlineMarkup" if inline else "replyKeyboardMarkup"
        raw["replyMarkup"] = {"_": markup_type, "rows": rows}
    return raw


# --- keyboard parsing -------------------------------------------------------

def test_parse_reply_keyboard() -> None:
    message = parse_bot_message(_msg(5, "Анна, 25", [_reply_row("❤️", "👎", "💌")]))
    assert message is not None
    assert message.message_id == 5
    assert [b.text for b in message.buttons] == ["❤️", "👎", "💌"]
    assert all(b.kind == "text" for b in message.buttons)
    assert message.is_inline is False


def test_parse_inline_keyboard_and_from_right() -> None:
    message = parse_bot_message(
        _msg(7, "Реклама", [_inline_row(("Купить", "d1"), ("Позже", "d2"), ("Закрыть", "d3"))], inline=True)
    )
    assert message is not None
    assert message.is_inline is True
    assert message.button_from_right(1).text == "Закрыть"
    assert message.button_from_right(2).text == "Позже"
    assert message.button_from_right(2).data == "d2"


def test_parse_message_without_id_returns_none() -> None:
    assert parse_bot_message({"message": "no id"}) is None


# --- classification ---------------------------------------------------------

def test_rate_card_like_when_roll_below_threshold() -> None:
    message = parse_bot_message(_msg(10, "Маша, 23\nМосква", [_reply_row("❤️", "👎")]))
    decision = classify(message, like_probability=0.5, rng=random.Random(1))
    other = classify(message, like_probability=0.5, rng=random.Random(2))
    intents = {decision.intent, other.intent}
    assert intents == {"rate"}
    # one seed must like, another dislike given 50/50 over many seeds
    likes = sum(
        classify(message, like_probability=0.5, rng=random.Random(s)).press.text == "❤️"
        for s in range(20)
    )
    assert 3 < likes < 17


def test_rate_card_always_like_with_probability_one() -> None:
    message = parse_bot_message(_msg(11, "Катя, 20", [_reply_row("❤️", "👎")]))
    for seed in range(10):
        decision = classify(message, like_probability=1.0, rng=random.Random(seed))
        assert decision.press.text == "❤️"


def test_incoming_like_forces_like_on_rating_card() -> None:
    message = parse_bot_message(
        _msg(12, "Ты понравился кому-то!\nОля, 22", [_reply_row("❤️", "👎")])
    )
    decision = classify(message, like_probability=0.0, rng=random.Random(0))
    assert decision.intent == "incoming_like_rate"
    assert decision.press.text == "❤️"


def test_incoming_like_presses_show_button() -> None:
    message = parse_bot_message(
        _msg(13, "Кто-то лайкнул твою анкету!", [_reply_row("Посмотреть", "Позже")])
    )
    decision = classify(message, rng=random.Random(0))
    assert decision.intent == "incoming_like_show"
    assert decision.press.text == "Посмотреть"


def test_mutual_match_captures_lead() -> None:
    message = parse_bot_message(
        _msg(14, "У вас взаимная симпатия!\nИван\nНапиши: @ivan_cool", [_reply_row("❤️")])
    )
    decision = classify(message, rng=random.Random(0))
    assert decision.intent == "match"
    assert decision.lead is not None
    assert decision.lead.telegram_username == "@ivan_cool"


def test_extract_lead_from_tme_url_button() -> None:
    message = parse_bot_message(
        {
            "id": 15,
            "message": "Это мэтч!",
            "replyMarkup": {
                "_": "replyInlineMarkup",
                "rows": [
                    {
                        "_": "keyboardButtonRow",
                        "buttons": [{"_": "keyboardButtonUrl", "text": "Написать", "url": "https://t.me/nice_girl"}],
                    }
                ],
            },
        }
    )
    lead = extract_lead(message)
    assert lead.telegram_username == "@nice_girl"


def test_match_lead_from_name_entity_username() -> None:
    raw = {
        "id": 40,
        "message": "Начинай общаться 🥳\nЮля",
        "entities": [
            {"_": "messageEntityTextUrl", "offset": 17, "length": 3, "url": "https://t.me/yulia_k"}
        ],
    }
    message = parse_bot_message(raw)
    decision = classify(message, rng=random.Random(0))
    assert decision.intent == "match"
    assert decision.lead.telegram_username == "@yulia_k"
    assert decision.lead.link == "https://t.me/yulia_k"


def test_match_lead_from_mention_name_entity_id_only() -> None:
    raw = {
        "id": 41,
        "message": "Начинай общаться\nДима",
        "entities": [
            {"_": "messageEntityMentionName", "offset": 17, "length": 4, "userId": 555000111}
        ],
    }
    message = parse_bot_message(raw)
    decision = classify(message, rng=random.Random(0))
    assert decision.intent == "match"
    assert decision.lead.telegram_username is None
    assert decision.lead.telegram_user_id == "555000111"
    assert decision.lead.link == "tg://user?id=555000111"
    assert decision.lead.external_id == "id:555000111"


def test_ad_presses_second_from_right() -> None:
    message = parse_bot_message(
        _msg(16, "Рекламное предложение от партнёра", [_inline_row(("A", "a"), ("B", "b"), ("C", "c"))], inline=True)
    )
    decision = classify(message, ad_button_from_right=2, rng=random.Random(0))
    assert decision.intent == "ad"
    assert decision.press.text == "B"
    assert decision.press.kind == "callback"


def test_limit_reached_pauses() -> None:
    message = parse_bot_message(_msg(17, "На сегодня всё, лайки закончились. Приходи завтра"))
    decision = classify(message, rng=random.Random(0))
    assert decision.intent == "limit"


def test_menu_starts_browsing() -> None:
    message = parse_bot_message(
        _msg(18, "Главное меню\n1 — Смотреть анкеты", [_reply_row("1 — Смотреть анкеты", "2")])
    )
    decision = classify(message, rng=random.Random(0))
    assert decision.intent == "menu"
    assert decision.press is not None


def test_force_like_overrides_random_on_rating_card() -> None:
    message = parse_bot_message(_msg(20, "", [_reply_row("❤️", "👎", "жалоба", "💤")]))
    decision = classify(message, like_probability=0.0, force_like=True, rng=random.Random(0))
    assert decision.intent == "incoming_like_rate"
    assert decision.press.text == "❤️"


def test_empty_media_message_waits() -> None:
    message = parse_bot_message(_msg(21, ""))
    decision = classify(message, rng=random.Random(0))
    assert decision.intent == "wait"


def test_rate_card_ignores_complaint_and_sleep_buttons() -> None:
    message = parse_bot_message(_msg(22, "", [_reply_row("❤️", "👎", "жалоба", "💤")]))
    likes = sum(
        classify(message, like_probability=1.0, rng=random.Random(s)).press.text == "❤️"
        for s in range(5)
    )
    assert likes == 5


def test_real_someone_liked_your_profile_forces_like() -> None:
    # Captured live from @leomatchbot review log.
    text = (
        "Кому-то понравилась твоя анкета(и еще 1)\n\n"
        "Вика, 20, Ростов-на-Дону – Ищу подругу..."
    )
    message = parse_bot_message(_msg(30, text, [_reply_row("❤️", "👎", "жалоба", "💤")]))
    decision = classify(message, like_probability=0.0, rng=random.Random(0))
    assert decision.intent == "incoming_like_rate"
    assert decision.press.text == "❤️"


def test_unknown_is_flagged_for_review() -> None:
    message = parse_bot_message(_msg(19, "Что-то совершенно непонятное без кнопок"))
    decision = classify(message, rng=random.Random(0))
    assert decision.intent == "unknown"
    assert decision.review is True
