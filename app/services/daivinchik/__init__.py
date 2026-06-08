"""Дайвинчик (@leomatchbot) auto-swiper.

A self-contained, async service that drives the Дайвинчик dating bot through the
existing CRMChat Telegram bridge: it reads the bot's latest message + button
layout, decides what to press (like/dislike 50/50, always-like on incoming
likes, second-from-right on ads), and captures mutual matches as recruiter
leads. Everything it cannot classify is dumped to a review log so the rules can
be tuned against real message samples.
"""

from app.services.daivinchik.keyboards import Button, BotMessage, parse_bot_message
from app.services.daivinchik.classifier import Decision, classify

__all__ = [
    "Button",
    "BotMessage",
    "parse_bot_message",
    "Decision",
    "classify",
]
