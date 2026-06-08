"""Rule-based decision layer for the Дайвинчик auto-swiper.

This is intentionally a pure, side-effect-free function so it can be unit tested
and tuned against real message samples captured by the service's review log /
probe mode. The service executes whatever Decision this returns.

The rules encode the behaviour the operator asked for:
  * a profile card to rate  -> like/dislike at random (50/50 by default)
  * "someone liked you"      -> always like (press show, then like)
  * mutual match             -> capture the contact as a lead, then continue
  * advertising offer        -> press the second button from the right
  * "out of likes for today" -> pause until the next working window
  * the main menu             -> start browsing profiles
Anything else is flagged for review and left untouched.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from app.services.daivinchik.keyboards import BotMessage, Button

# --- keyword vocabularies (lowercased substring match) ---------------------
LIKE_EMOJI = ("❤", "💛", "💙", "💚", "💜", "🧡", "💖", "💗", "💘", "❣", "👍", "♥")
DISLIKE_EMOJI = ("👎", "💔", "❌", "✖", "🚫")
SHOW_EMOJI = ("👀", "👁", "➡", "▶")

MATCH_MARKERS = (
    "взаимная симпатия",
    "у вас совпадение",
    "это мэтч",
    "это метч",
    "это match",
    "понравились друг другу",
    "взаимность",
    "есть взаимная",
    "начинай общаться",
    "начинай общение",
    "можешь начать общение",
    "обменяйтесь контактами",
    "вы понравились",
)
INCOMING_LIKE_MARKERS = (
    "ты понравил",
    "вы понравил",
    "понравилась кому",
    "понравился кому",
    "кто-то лайкнул",
    "кому-то ты приглянул",
    "оценил твою анкету",
    "оценила твою анкету",
    "твоя анкета понравилась",
    "понравилась твоя анкета",
    "понравился твоя анкета",
    "понравилась твоя анкет",
    "лайкнул",
    "лайкнула",
)
SHOW_MARKERS = ("посмотреть", "показать", "смотреть", "покажи", "глянуть")
LIMIT_MARKERS = (
    "лайки закончил",
    "лайки кончил",
    "лайков больше нет",
    "на сегодня всё",
    "на сегодня все",
    "лимит",
    "закончились лайки",
    "приходи завтра",
    "лайки на сегодня",
    # «Слишком много ❤️ за сегодня. Перейди на Premium…» — это тоже дневной лимит
    # лайков: ставим на паузу до следующего окна, а не крутим вхолостую.
    "слишком много",
    "перейди на premium",
)
MENU_MARKERS = ("смотреть анкеты", "1 — смотреть", "1 - смотреть", "главное меню")
# Меню «Укажите причину жалобы» появляется, если случайно нажали «Пожаловаться».
# Его нужно закрыть (✖️), иначе свайпер залипает на нём и не листает дальше.
COMPLAINT_MENU_MARKERS = ("причину жалобы", "укажите причину")
CANCEL_EMOJI = ("✖", "❌", "🚫", "↩", "⬅")
AD_MARKERS = (
    "реклама",
    "рекламн",
    "промо",
    "партнёр",
    "партнер",
    "скидка",
    "подпишись",
    "подписывайся",
    "переходи",
    "розыгрыш",
    "спонсор",
)

_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,31})")
_TME_RE = re.compile(r"t\.me/([A-Za-z][A-Za-z0-9_]{4,31})")
_TG_ID_RE = re.compile(r"tg://user\?id=(\d+)")


@dataclass(slots=True, frozen=True)
class LeadCapture:
    external_id: str
    telegram_username: str | None
    display_name: str | None
    profile_text: str
    telegram_user_id: str | None = None
    link: str | None = None


@dataclass(slots=True, frozen=True)
class Decision:
    intent: str
    press: Button | None = None          # button to press (text or callback)
    send_text: str | None = None         # raw text to send if no button object
    lead: LeadCapture | None = None      # mutual match to capture
    pause_minutes: int | None = None     # pause the loop (limit reached)
    review: bool = False                 # dump raw message for tuning
    note: str = ""                       # human-readable reason for logs


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _button_has(button: Button, emoji: tuple[str, ...]) -> bool:
    return any(symbol in button.text for symbol in emoji)


def _find_button(message: BotMessage, emoji: tuple[str, ...]) -> Button | None:
    for button in message.buttons:
        if _button_has(button, emoji):
            return button
    return None


def _find_button_by_text(message: BotMessage, markers: tuple[str, ...]) -> Button | None:
    for button in message.buttons:
        lowered = button.text.lower()
        if _contains_any(lowered, markers):
            return button
    return None


def extract_lead(message: BotMessage) -> LeadCapture:
    """Pull whatever contact handle the match message exposes.

    Дайвинчик hangs the matched person's Telegram link on their *name*, so the
    handle most often lives in a message entity (text-url or mention-name), not
    in the plain text. We look across the text, button urls and entities.
    """
    haystacks = [message.text, *message.entity_links]
    for button in message.buttons:
        if button.url:
            haystacks.append(button.url)
    blob = "\n".join(haystacks)

    username = None
    if (m := _USERNAME_RE.search(blob)) is not None:
        username = f"@{m.group(1)}"
    elif (m := _TME_RE.search(blob)) is not None:
        username = f"@{m.group(1)}"

    user_id = None
    if (m := _TG_ID_RE.search(blob)) is not None:
        user_id = m.group(1)
    elif message.mention_user_ids:
        user_id = message.mention_user_ids[0]

    external_id = username or (f"id:{user_id}" if user_id else f"msg:{message.message_id}")
    # Дайвинчик's match line is "Начинай общаться 👉 <Имя>", so the matched
    # person's name follows the arrow. Fall back to the first non-empty line.
    display = None
    if "👉" in message.text:
        display = message.text.split("👉")[-1].strip() or None
    if not display:
        display = next(
            (line.strip() for line in message.text.splitlines() if line.strip()),
            None,
        )
    link = None
    if message.entity_links:
        link = message.entity_links[0]
    elif user_id:
        link = f"tg://user?id={user_id}"
    return LeadCapture(
        external_id=external_id,
        telegram_username=username,
        display_name=display,
        profile_text=message.text,
        telegram_user_id=user_id,
        link=link,
    )


def classify(
    message: BotMessage,
    *,
    like_probability: float = 0.5,
    ad_button_from_right: int = 2,
    force_like: bool = False,
    rng: random.Random | None = None,
) -> Decision:
    rng = rng or random
    text = message.text.lower()

    if message.outgoing:
        return Decision(intent="wait", note="own outgoing message")

    # Empty media message (profile photo) with no actionable keyboard -> wait.
    if not text.strip() and not message.has_buttons:
        return Decision(intent="wait", note="empty media message")

    # 0) Complaint-reason menu (accidental "Пожаловаться") -> close it (✖️) so the
    # swiper doesn't get stuck looping on it and can resume browsing.
    if _contains_any(text, COMPLAINT_MENU_MARKERS) and message.has_buttons:
        cancel = _find_button(message, CANCEL_EMOJI) or message.buttons[-1]
        if cancel is not None:
            return Decision(intent="dismiss", press=cancel, note="complaint menu -> cancel")

    # 1) Daily like limit reached -> pause.
    if _contains_any(text, LIMIT_MARKERS):
        return Decision(intent="limit", note="daily like limit reached")

    # 2) Mutual match -> capture the lead, then continue browsing if possible.
    if _contains_any(text, MATCH_MARKERS):
        lead = extract_lead(message)
        cont = _find_button(message, LIKE_EMOJI) or _find_button_by_text(
            message, ("дальше", "продолжить", "смотреть")
        )
        return Decision(
            intent="match",
            lead=lead,
            press=cont,
            note=f"mutual match -> {lead.telegram_username or lead.external_id}",
        )

    like_button = _find_button(message, LIKE_EMOJI)
    dislike_button = _find_button(message, DISLIKE_EMOJI)
    is_rating_card = like_button is not None and dislike_button is not None

    # 3) Someone liked you.
    if _contains_any(text, INCOMING_LIKE_MARKERS):
        if is_rating_card:
            # Their profile is already shown with like/dislike -> always like.
            return Decision(
                intent="incoming_like_rate",
                press=like_button,
                note="incoming like -> forced like",
            )
        # Offer to view who liked you -> press the show/continue button.
        show = (
            _find_button_by_text(message, tuple(SHOW_MARKERS))
            or _find_button(message, SHOW_EMOJI)
            or _find_button(message, LIKE_EMOJI)
            or (message.buttons[0] if message.has_buttons else None)
        )
        if show is not None:
            return Decision(
                intent="incoming_like_show", press=show, note="incoming like -> show"
            )

    # 4) Advertising / sponsored offer -> press the second button from the right.
    looks_like_ad = _contains_any(text, AD_MARKERS) or (
        message.is_inline and not is_rating_card and not _contains_any(text, MENU_MARKERS)
    )
    if looks_like_ad and message.has_buttons:
        target = message.button_from_right(ad_button_from_right)
        if target is not None:
            return Decision(
                intent="ad",
                press=target,
                note=f"ad offer -> button #{ad_button_from_right} from right "
                f"('{target.text}')",
            )

    # 5) Profile card to rate. In "liker mode" (going through people who liked
    # us) we always like; otherwise like/dislike at random.
    if is_rating_card and force_like:
        return Decision(intent="incoming_like_rate", press=like_button, note="liker mode -> forced like")
    if is_rating_card:
        roll = rng.random()
        if roll < like_probability:
            return Decision(intent="rate", press=like_button, note=f"rate like (roll={roll:.2f})")
        return Decision(intent="rate", press=dislike_button, note=f"rate dislike (roll={roll:.2f})")

    # 6) Main menu -> start browsing profiles.
    if _contains_any(text, MENU_MARKERS):
        start = _find_button_by_text(message, ("смотреть анкеты", "смотреть"))
        if start is not None:
            return Decision(intent="menu", press=start, note="menu -> start browsing")
        return Decision(intent="menu", send_text="1", note="menu -> send 1")

    # 7) Unknown -> flag for review, do nothing.
    return Decision(
        intent="unknown",
        review=True,
        note="unrecognized message; logged for review",
    )
