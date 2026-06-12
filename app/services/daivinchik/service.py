"""Orchestrator for the Дайвинчик auto-swiper.

Runs as a standalone async service (``python -m app.services.daivinchik``).
It talks to Telegram through the existing CRMChat bridge, so it reuses the
already-authorized session — no separate login is required.

Flow per tick:
  1. gate on the working window (default 10:00–21:00 local tz) and any pause;
  2. fetch the last few messages from the bot dialog;
  3. take the newest *incoming* message we have not acted on yet;
  4. classify it and execute the decision (press a button / capture a lead /
     pause / flag for review);
  5. wait a human-ish random delay and loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.db.session import AsyncSessionLocal
from app.services.crmchat_connector import (
    CRMChatBootstrapContext,
    CRMChatConnector,
    build_input_peer_from_resolve_username,
    normalize_messages_response,
)
from app.services.daivinchik.classifier import Decision, LeadCapture, classify
from app.services.daivinchik.keyboards import BotMessage, Button, parse_bot_message
from app.services.lead_intake import LeadIntakeService
from app.services.lead_notifier import LeadNotifier

if TYPE_CHECKING:
    from app.models.account import Account

logger = logging.getLogger("daivinchik")


class DaivinchikState:
    """Tiny JSON-backed state so restarts don't re-process the same card."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # id of the message we last acted on (sent a button for). We only ever
        # advance this when an action was actually taken, so a not-yet-handled
        # card is never skipped just because we glanced at it.
        self.last_acted_id: int = 0
        self.paused_until: datetime | None = None
        self.liker_mode: bool = False
        # The reply keyboard persists chat-wide in Telegram even if it was set
        # many messages ago, so we remember the last one we saw (button texts).
        self.last_keyboard: list[list[str]] = []
        # Daily lead counter (reset per calendar day in the service timezone).
        self.leads_today: int = 0
        self.leads_date: str = ""
        # Highest message id already swept for mutual matches. Дайвинчик sends the
        # match notification in the middle of a burst (profile -> match -> menu),
        # so we sweep the whole window for matches, not just the newest message.
        self.last_match_id: int = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.last_acted_id = int(data.get("last_acted_id", 0) or 0)
        self.liker_mode = bool(data.get("liker_mode", False))
        self.last_keyboard = data.get("last_keyboard", []) or []
        self.leads_today = int(data.get("leads_today", 0) or 0)
        self.leads_date = str(data.get("leads_date", "") or "")
        self.last_match_id = int(data.get("last_match_id", 0) or 0)
        raw_pause = data.get("paused_until")
        if raw_pause:
            try:
                self.paused_until = datetime.fromisoformat(raw_pause)
            except ValueError:
                self.paused_until = None

    def save(self) -> None:
        payload = {
            "last_acted_id": self.last_acted_id,
            "liker_mode": self.liker_mode,
            "last_keyboard": self.last_keyboard,
            "leads_today": self.leads_today,
            "leads_date": self.leads_date,
            "last_match_id": self.last_match_id,
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
        }
        try:
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            logger.warning("could not persist state: %s", exc)


class _DaivinchikConfig:
    """Per-account daivinchik tuning: values from the account's config JSON win,
    otherwise fall back to the global ``daivinchik_*`` settings. Lets every account
    swipe in its own «режим» (active hours, like probability, daily cap, bot)
    without diverging the code path."""

    def __init__(self, settings: Settings, overrides: dict[str, Any] | None) -> None:
        self._settings = settings
        self._overrides = overrides or {}

    def __getattr__(self, name: str) -> Any:
        overrides = self.__dict__.get("_overrides", {})
        if name in overrides:
            return overrides[name]
        short = name.removeprefix("daivinchik_")
        if short in overrides:
            return overrides[short]
        return getattr(self.__dict__["_settings"], name)


def _account_slug(account: "Account") -> str:
    raw = (account.telegram_username or account.crmchat_account_id or str(account.id))
    return "".join(ch for ch in raw.lstrip("@") if ch.isalnum() or ch in "-_") or "acc"


def _suffixed_path(path: str, suffix: str | None) -> str:
    if not suffix:
        return path
    p = Path(path)
    return str(p.with_name(f"{p.stem}_{suffix}{p.suffix}"))


class DaivinchikService:
    def __init__(
        self,
        settings: Settings | None = None,
        connector: CRMChatConnector | None = None,
        account: "Account | None" = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.account = account
        overrides: dict[str, Any] = {}
        if account is not None and account.daivinchik_config_json:
            try:
                overrides = json.loads(account.daivinchik_config_json) or {}
            except ValueError:
                logger.warning("invalid daivinchik_config_json for account %s", account.id)
        self.cfg = _DaivinchikConfig(self.settings, overrides)
        self._external_connector = connector
        self.tz = ZoneInfo(self.cfg.daivinchik_timezone)
        # Per-account state/leads/review files so two accounts never share a
        # cursor. Single-account (account=None) keeps the original file names.
        suffix = _account_slug(account) if account is not None else None
        self.state = DaivinchikState(Path(_suffixed_path(self.cfg.daivinchik_state_path, suffix)))
        self.review_path = Path(_suffixed_path(self.cfg.daivinchik_review_log_path, suffix))
        self.leads_path = Path(_suffixed_path(self.cfg.daivinchik_leads_path, suffix))
        self.rng = random.Random()
        self.notifier = LeadNotifier(self.settings)
        self._actions_done = 0
        self._last_reviewed_id = 0
        self._stop_on_limit = False
        self._limit_reached = False
        self._daily_cap_reached = False
        # Пользователи из getHistory-ответов (id -> raw user dict с username и
        # accessHash). Нужны, чтобы написать мэтчу, у которого нет публичного
        # @handle: Дайвинчик линкует таких по tg://user?id=…, а peer для отправки
        # собирается из accessHash, который Telegram уже отдал вместе с историей.
        self._users_by_id: dict[str, dict[str, Any]] = {}

    def _make_connector(self) -> CRMChatConnector:
        if self._external_connector is not None:
            return self._external_connector
        if self.account is not None:
            return CRMChatConnector.for_account(self.account, settings=self.settings)
        return CRMChatConnector(settings=self.settings)

    # -- public entrypoints --------------------------------------------------

    async def run(self, *, max_actions: int | None = None, stop_on_limit: bool = False) -> None:
        self._stop_on_limit = stop_on_limit
        self._limit_reached = False
        logger.info(
            "starting Дайвинчик auto-swiper account=%s bot=%s window=%02d-%02d %s",
            _account_slug(self.account) if self.account is not None else "default",
            self.cfg.daivinchik_bot_username,
            self.cfg.daivinchik_active_hours_start,
            self.cfg.daivinchik_active_hours_end,
            self.cfg.daivinchik_timezone,
        )
        connector = self._make_connector()
        try:
            # Bootstrap + peer resolution are retried with backoff: CRMchat can
            # blip with transient network errors, and a blip at startup must NOT
            # crash the whole swiper (it used to exit code 1 and rely on the
            # supervisor to respawn, losing the resolved peer each time).
            context, peer = await self._bootstrap_with_retry(connector)
            logger.info("resolved bot peer: %s", peer.get("userId"))
            self._actions_done = 0
            while True:
                if (self._limit_reached or self._daily_cap_reached) and self._stop_on_limit:
                    logger.info("stop condition reached; stopping (stop_on_limit)")
                    return
                mode = await self._gate_or_sleep()
                if mode == "sleep":
                    continue
                try:
                    await self._tick(connector, context, peer, passive=(mode == "passive"))
                except Exception:  # noqa: BLE001
                    logger.exception("tick failed; backing off")
                    await asyncio.sleep(max(5.0, self.cfg.daivinchik_poll_interval_seconds))
                if self._limit_reached and self._stop_on_limit:
                    logger.info("daily like limit reached; stopping (stop_on_limit)")
                    return
                if max_actions is not None and self._actions_done >= max_actions:
                    logger.info("reached max_actions=%d; stopping", max_actions)
                    return
        finally:
            if self._external_connector is None:
                await connector.aclose()

    async def _bootstrap_with_retry(
        self, connector: CRMChatConnector
    ) -> tuple[CRMChatBootstrapContext, dict[str, Any]]:
        """Bootstrap and resolve the bot peer, retrying transient failures with
        capped exponential backoff so a momentary CRMchat/network blip never kills
        the swiper process."""
        attempt = 0
        while True:
            try:
                context = await connector.bootstrap()
                peer = await self._resolve_bot_peer(connector, context)
                return context, peer
            except Exception:  # noqa: BLE001
                attempt += 1
                delay = min(60.0, 5.0 * (2 ** min(attempt - 1, 4)))
                logger.warning(
                    "bootstrap/resolve failed (attempt %d); retrying in %.0fs",
                    attempt,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)

    async def probe(self, limit: int = 10) -> None:
        """Dump the last messages + parsed buttons. Use this to tune the rules."""
        connector = self._make_connector()
        try:
            context = await connector.bootstrap()
            peer = await self._resolve_bot_peer(connector, context)
            messages = await self._fetch_messages(connector, context, peer, limit=limit)
            for message in messages:
                arrow = "<<" if message.outgoing else ">>"
                print(f"{arrow} id={message.message_id} text={message.text!r}")
                for r, row in enumerate(message.rows):
                    rendered = " | ".join(
                        f"[{b.text!r} {b.kind}{'=' + b.data if b.data else ''}]" for b in row
                    )
                    print(f"     row{r}: {rendered}")
                if not message.has_buttons:
                    print("     (no buttons)")
            effective = self._effective_message(messages)
            if effective is not None:
                decision = self._decide(effective)
                print(
                    f"\n=> effective msg {effective.message_id}: intent={decision.intent} "
                    f"| {decision.note}"
                )
                if decision.press is not None:
                    print(f"   would press: {decision.press.text!r} ({decision.press.kind})")
                elif decision.send_text:
                    print(f"   would send: {decision.send_text!r}")
        finally:
            if self._external_connector is None:
                await connector.aclose()

    # -- internals -----------------------------------------------------------

    def _decide(self, message: BotMessage) -> Decision:
        return classify(
            message,
            like_probability=self.cfg.daivinchik_like_probability,
            ad_button_from_right=self.cfg.daivinchik_ad_button_from_right,
            force_like=self.state.liker_mode,
            rng=self.rng,
        )

    def _remember_keyboard(self, messages: list[BotMessage]) -> None:
        """Persist the most recent reply keyboard seen in the window."""
        for candidate in sorted(messages, key=lambda m: m.message_id, reverse=True):
            if candidate.rows and not candidate.is_inline:
                self.state.last_keyboard = [[b.text for b in row] for row in candidate.rows]
                return

    def _effective_message(self, messages: list[BotMessage]) -> BotMessage | None:
        """Combine the newest incoming message with the active reply keyboard.

        Дайвинчик sends a profile as several messages (photos with empty text)
        while the ❤️/👎 reply keyboard stays active chat-wide. So we drive on the
        newest incoming message's text, but attach the active reply keyboard if
        that message has none of its own — first from a nearby message, otherwise
        from the last keyboard we remembered (it persists in Telegram even if it
        was set long ago). Inline keyboards stay attached to their own message.
        """
        incoming = [m for m in messages if not m.outgoing]
        if not incoming:
            return None
        latest = max(incoming, key=lambda m: m.message_id)
        if latest.has_buttons:
            return latest
        # borrow the most recent reply keyboard from any nearby message
        for candidate in sorted(messages, key=lambda m: m.message_id, reverse=True):
            if candidate.rows and not candidate.is_inline:
                return self._with_rows(latest, candidate.rows)
        # fall back to the persistent keyboard we remembered earlier
        if self.state.last_keyboard:
            rows = [[Button(text=text, kind="text") for text in row] for row in self.state.last_keyboard]
            return self._with_rows(latest, rows)
        return latest

    @staticmethod
    def _with_rows(message: BotMessage, rows: list[list[Button]]) -> BotMessage:
        return BotMessage(
            message_id=message.message_id,
            text=message.text,
            rows=rows,
            outgoing=False,
            entity_links=message.entity_links,
            mention_user_ids=message.mention_user_ids,
            raw=message.raw,
        )

    async def _gate_or_sleep(self) -> str:
        """Decide what the swiper may do right now.

        Returns one of:
          * "sleep"   — do nothing this round (daily lead cap / outside hours);
          * "passive" — like-лимит на паузе: проактивно листать анкеты НЕЛЬЗЯ, но
            входящие лайки («ты понравилась — показать?») и взаимные матчи квоту НЕ
            тратят, поэтому их продолжаем ловить и заводить в воронку;
          * "go"      — обычный полный тик.
        """
        now = datetime.now(self.tz)
        self._reset_leads_if_new_day(now)
        if self.state.leads_today >= self.cfg.daivinchik_daily_lead_limit:
            self._daily_cap_reached = True
            wait = self._seconds_until_window(now)
            logger.info(
                "daily lead cap reached (%d/%d); sleeping until tomorrow's window",
                self.state.leads_today,
                self.cfg.daivinchik_daily_lead_limit,
            )
            await asyncio.sleep(min(wait, 600))
            return "sleep"
        if not self._within_working_hours(now):
            wait = self._seconds_until_window(now)
            logger.info("outside working window; sleeping %ds", min(wait, 600))
            await asyncio.sleep(min(wait, 600))
            return "sleep"
        if self.state.paused_until and now < self.state.paused_until.astimezone(self.tz):
            remaining = (self.state.paused_until.astimezone(self.tz) - now).total_seconds()
            logger.info("paused (like-limit) for %.0fs more — watching for incoming likes/matches", remaining)
            # Мягкая каденция опроса во время паузы: проверяем входящие лайки ~раз в
            # минуту, не тратя квоту на проактивные свайпы.
            await asyncio.sleep(min(remaining, 60.0))
            return "passive"
        return "go"

    def _within_working_hours(self, now: datetime) -> bool:
        start = self.cfg.daivinchik_active_hours_start
        end = self.cfg.daivinchik_active_hours_end
        return start <= now.hour < end

    def _seconds_until_window(self, now: datetime) -> int:
        start = self.cfg.daivinchik_active_hours_start
        target = now.replace(hour=start, minute=0, second=0, microsecond=0)
        if now.hour >= self.cfg.daivinchik_active_hours_end or now.hour >= start:
            target = target + timedelta(days=1)
        if target <= now:
            target = target + timedelta(days=1)
        return max(1, int((target - now).total_seconds()))

    def _today_str(self, now: datetime | None = None) -> str:
        return (now or datetime.now(self.tz)).strftime("%Y-%m-%d")

    def _reset_leads_if_new_day(self, now: datetime) -> None:
        today = self._today_str(now)
        if self.state.leads_date != today:
            self.state.leads_date = today
            self.state.leads_today = 0
            self._daily_cap_reached = False
            self.state.save()

    async def _tick(
        self,
        connector: CRMChatConnector,
        context: CRMChatBootstrapContext,
        peer: dict[str, Any],
        *,
        passive: bool = False,
    ) -> None:
        # Курсор покрытия: самый старый из «уже обработанных» указателей. Листаем
        # историю назад до него, чтобы ни одно сообщение (особенно матч из пачки)
        # не проскочило мимо между тиками.
        cursors = [c for c in (self.state.last_match_id, self.state.last_acted_id) if c > 0]
        coverage_min_id = min(cursors) if cursors else 0
        messages = await self._fetch_messages(
            connector,
            context,
            peer,
            limit=self.cfg.daivinchik_history_limit,
            min_id=coverage_min_id,
        )
        self._remember_keyboard(messages)

        # Sweep the whole window for mutual matches first. Дайвинчик delivers the
        # match notification ("Начинай общаться 👉 …") in the middle of a burst
        # (profile -> match -> back to menu), so driving only on the newest message
        # loses it. This captures every not-yet-seen match so leads are never missed.
        await self._sweep_matches(messages, connector)
        if self._daily_cap_reached:
            return

        effective = self._effective_message(messages)
        if effective is None:
            await asyncio.sleep(self.cfg.daivinchik_poll_interval_seconds)
            return

        decision = self._decide(effective)

        # Во время паузы like-лимита НЕ листаем анкеты проактивно (это и есть то, что
        # упёрлось в лимит), но входящие лайки/матчи/навигацию обрабатываем — они
        # квоту не тратят, а матч уже сведён свипом выше.
        if passive and decision.intent == "rate":
            await asyncio.sleep(self.cfg.daivinchik_poll_interval_seconds)
            return

        # Matches are handled exclusively by the sweep above — don't double-capture.
        if decision.intent == "match":
            await asyncio.sleep(self.cfg.daivinchik_poll_interval_seconds)
            return

        # Already acted on this exact card -> the bot just hasn't advanced yet.
        if _is_actionable(decision) and effective.message_id == self.state.last_acted_id:
            await asyncio.sleep(self.cfg.daivinchik_poll_interval_seconds)
            return

        self._update_liker_mode(decision)
        logger.info(
            "msg %s -> %s | %s%s",
            effective.message_id,
            decision.intent,
            decision.note,
            " [liker_mode]" if self.state.liker_mode else "",
        )

        if not _is_actionable(decision):
            # Nothing to press (empty media / unrecognized). Do NOT advance the
            # acted pointer, otherwise a card that becomes actionable later would
            # be skipped. Log review at most once per message id.
            if decision.review and effective.message_id != self._last_reviewed_id:
                self._log_review(effective, decision)
                self._last_reviewed_id = effective.message_id
            await asyncio.sleep(self.cfg.daivinchik_poll_interval_seconds)
            return

        await self._execute(connector, context, peer, effective, decision)
        self._actions_done += 1
        self.state.last_acted_id = effective.message_id
        self.state.save()
        await asyncio.sleep(self._human_delay())

    async def _sweep_matches(self, messages: list[BotMessage], connector: CRMChatConnector | None = None) -> None:
        """Capture every not-yet-seen mutual match in the fetched window.

        Scans incoming messages with id > ``last_match_id`` in ascending order,
        captures a lead for each one the classifier marks as a match, and advances
        ``last_match_id`` so a match is processed exactly once. Stops early if the
        daily lead cap is hit mid-sweep (leaving later matches for the next day).
        """
        incoming = sorted(
            (m for m in messages if not m.outgoing), key=lambda m: m.message_id
        )
        highest = self.state.last_match_id
        for message in incoming:
            if message.message_id <= self.state.last_match_id:
                continue
            decision = self._decide(message)
            if decision.intent == "match" and decision.lead is not None:
                logger.info(
                    "sweep match msg %s -> %s",
                    message.message_id,
                    decision.note,
                )
                if decision.review:
                    self._log_review(message, decision)
                await self._capture_lead(decision.lead, message, connector)
                self._actions_done += 1
                highest = message.message_id
                if self._daily_cap_reached:
                    break
            else:
                highest = message.message_id
        if highest > self.state.last_match_id:
            self.state.last_match_id = highest
            self.state.save()

    def _update_liker_mode(self, decision: Decision) -> None:
        if decision.intent == "incoming_like_show":
            self.state.liker_mode = True
        elif decision.intent in {"menu", "match", "limit"}:
            self.state.liker_mode = False

    async def _execute(
        self,
        connector: CRMChatConnector,
        context: CRMChatBootstrapContext,
        peer: dict[str, Any],
        message: BotMessage,
        decision: Decision,
    ) -> None:
        if decision.review:
            self._log_review(message, decision)

        if decision.lead is not None:
            await self._capture_lead(decision.lead, message, connector)
            if self._daily_cap_reached:
                # Hit the daily lead cap on this very match — stop here, don't
                # browse further. _gate_or_sleep will hold until tomorrow.
                return

        if decision.pause_minutes is not None or decision.intent == "limit":
            minutes = decision.pause_minutes or self.cfg.daivinchik_limit_pause_minutes
            self.state.paused_until = datetime.now(UTC) + timedelta(minutes=minutes)
            self._limit_reached = True
            logger.info("limit reached; pausing %d min", minutes)
            return

        ws, acc = context.workspace.id, context.telegram_account.id
        # mark read so the account looks like a real, attentive user
        try:
            await connector.read_history(ws, acc, peer, max_id=message.message_id)
        except Exception:  # noqa: BLE001
            logger.debug("read_history failed (non-fatal)", exc_info=True)

        button = decision.press
        if button is not None and button.kind == "callback" and button.data:
            await connector.get_bot_callback_answer(ws, acc, peer, message.message_id, button.data)
            logger.info("pressed inline button %r", button.text)
            return
        text_to_send = button.text if button is not None else decision.send_text
        if text_to_send:
            await connector.send_message(ws, acc, peer, text_to_send, random_id=self.rng.getrandbits(63))
            logger.info("sent %r", text_to_send)

    async def _capture_lead(
        self, lead: LeadCapture, message: BotMessage, connector: CRMChatConnector | None = None
    ) -> None:
        source = self.cfg.daivinchik_lead_source
        lead, access_hash = self._enrich_lead_from_history_users(lead)
        # Always persist the contact to the durable leads table first, so a match
        # is never lost even if the DB intake can't run.
        self._log_lead(lead, message)

        # Count it against today's cap (reset per calendar day in _gate_or_sleep).
        self._reset_leads_if_new_day(datetime.now(self.tz))
        self.state.leads_today += 1
        self.state.save()
        logger.info(
            "lead #%d/%d today: %s",
            self.state.leads_today,
            self.cfg.daivinchik_daily_lead_limit,
            lead.telegram_username or lead.link or lead.external_id,
        )
        if self.state.leads_today >= self.cfg.daivinchik_daily_lead_limit:
            self._daily_cap_reached = True
            logger.info("daily lead cap reached — will stop until tomorrow")

        if not lead.telegram_username and not (lead.telegram_user_id and access_hash):
            # Дайвинчик linked the person by user-id only (no public @handle) И
            # accessHash не нашёлся в истории — написать физически нечем, only manual.
            logger.warning("match without username/peer: %s (link=%s)", lead.external_id, lead.link)
            await self.notifier.notify_intake_blocked(
                telegram_username=lead.link or lead.external_id,
                source=source,
                reason="Мэтч без @username и без peer — собран в daivinchik_leads.jsonl, обработай вручную",
            )
            return

        try:
            async with AsyncSessionLocal() as session:
                result = await LeadIntakeService(session, connector=connector).enqueue_lead(
                    source=source,
                    external_lead_id=lead.external_id,
                    telegram_username=lead.telegram_username,
                    # Прямой peer (id + accessHash из истории): открывает интейк
                    # мэтчам без @handle и экономит resolveUsername остальным.
                    telegram_user_id=lead.telegram_user_id,
                    telegram_access_hash=access_hash,
                    # Bind the lead to THIS swiper's account — the one that got the
                    # mutual match — so the funnel replies from the same account.
                    account_id=str(self.account.id) if self.account is not None else None,
                    payload={
                        "display_name": lead.display_name,
                        "profile_text": lead.profile_text,
                        "telegram_user_id": lead.telegram_user_id,
                        "telegram_access_hash": access_hash,
                        "link": lead.link,
                        "match_message_id": message.message_id,
                    },
                )
            logger.info(
                "lead captured %s (idempotent=%s)",
                lead.telegram_username or lead.external_id,
                result.idempotent,
            )
        except Exception as exc:  # noqa: BLE001
            # DB unavailable etc. The contact is already safe in the leads file,
            # so never let a match stall the swipe loop — log and move on.
            logger.warning("DB intake failed for %s: %s", lead.telegram_username or lead.external_id, exc)
            await self.notifier.notify_intake_blocked(
                telegram_username=lead.telegram_username or lead.link or lead.external_id,
                source=source,
                reason=f"БД недоступна, лид сохранён в {self.leads_path.name}: {type(exc).__name__}",
            )

    def _log_lead(self, lead: LeadCapture, message: BotMessage) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "telegram_username": lead.telegram_username,
            "telegram_user_id": lead.telegram_user_id,
            "link": lead.link,
            "display_name": lead.display_name,
            "external_id": lead.external_id,
            "profile_text": lead.profile_text,
            "match_message_id": message.message_id,
        }
        try:
            with self.leads_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001
            logger.warning("could not write leads table: %s", exc)

    def _enrich_lead_from_history_users(self, lead: LeadCapture) -> tuple[LeadCapture, str | None]:
        """Дотянуть username/accessHash мэтча из user-объектов getHistory.

        Telegram отдаёт вместе с историей полные user-объекты всех, кто упомянут
        в сообщениях — включая мэтча, на имени которого висит mention. Отсюда
        берём публичный @handle (если он есть, но не попал в текст) и accessHash,
        по которому можно писать даже без @handle."""
        if not lead.telegram_user_id:
            return lead, None
        user = self._users_by_id.get(str(lead.telegram_user_id))
        if not user:
            return lead, None
        access_hash = user.get("accessHash") or user.get("access_hash")
        username = user.get("username")
        if not lead.telegram_username and username:
            lead = replace(lead, telegram_username=f"@{username}")
        return lead, (str(access_hash) if access_hash is not None else None)

    # Кэш живёт в 24/7-процессе и без потолка растёт бесконечно. accessHash нужен
    # только в окне «мэтч → первое сообщение» (тот же или соседний цикл), поэтому
    # держим последних N и вытесняем самых старых (dict сохраняет порядок вставки).
    _USERS_CACHE_MAX = 500

    def _remember_users(self, payload: Any) -> None:
        """Скопить user-объекты из getHistory: там лежит accessHash собеседников,
        упомянутых в сообщениях, — единственный способ написать мэтчу без @handle."""
        try:
            users = payload.get("users") or []
        except AttributeError:
            return
        if not users:
            return
        for user in users:
            try:
                user_id = user.get("id")
            except AttributeError:
                continue
            if user_id is not None:
                key = str(user_id)
                self._users_by_id.pop(key, None)
                self._users_by_id[key] = dict(user)
        while len(self._users_by_id) > self._USERS_CACHE_MAX:
            self._users_by_id.pop(next(iter(self._users_by_id)))

    async def _resolve_bot_peer(
        self, connector: CRMChatConnector, context: CRMChatBootstrapContext
    ) -> dict[str, Any]:
        resolved = await connector.resolve_username(
            context.workspace.id,
            context.telegram_account.id,
            self.cfg.daivinchik_bot_username,
        )
        return dict(build_input_peer_from_resolve_username(resolved))

    async def _fetch_messages(
        self,
        connector: CRMChatConnector,
        context: CRMChatBootstrapContext,
        peer: dict[str, Any],
        *,
        limit: int,
        min_id: int = 0,
    ) -> list[BotMessage]:
        """Прочитать последние сообщения диалога с ботом.

        Если задан ``min_id`` (курсор последнего обработанного), листаем историю
        назад страницами, пока не покроем ВСЁ, что пришло после курсора. Это спасает
        от потери матчей, когда Дайвинчик вываливает пачку сообщений (открыли
        «взаимные симпатии» — прилетает десятки карточек разом): без пагинации матч,
        выпавший за окно ``limit``, уезжал ниже last_match_id и пропадал навсегда.
        """
        collected: dict[int, BotMessage] = {}
        offset_id = 0
        pages = 0
        max_pages = max(1, self.cfg.daivinchik_history_max_pages)
        while True:
            payload = await connector.get_history(
                context.workspace.id,
                context.telegram_account.id,
                peer,
                limit=limit,
                offset_id=offset_id,
            )
            self._remember_users(payload)
            snapshots = normalize_messages_response(payload)
            if not snapshots:
                break
            page_min_id = None
            for snapshot in snapshots:
                if snapshot.raw is None:
                    continue
                parsed = parse_bot_message(snapshot.raw)
                if parsed is None:
                    continue
                collected[parsed.message_id] = parsed
                if page_min_id is None or parsed.message_id < page_min_id:
                    page_min_id = parsed.message_id
            pages += 1
            # Дальше листаем, только если просили покрыть курсор и ещё не дошли до него.
            if page_min_id is None or min_id <= 0 or page_min_id <= min_id:
                break
            if pages >= max_pages:
                logger.warning(
                    "history pagination hit cap (%d pages); oldest id %s still > cursor %s",
                    max_pages,
                    page_min_id,
                    min_id,
                )
                break
            offset_id = page_min_id
        return sorted(collected.values(), key=lambda m: m.message_id)

    def _human_delay(self) -> float:
        return self.rng.uniform(
            self.cfg.daivinchik_min_action_delay_seconds,
            self.cfg.daivinchik_max_action_delay_seconds,
        )

    def _log_review(self, message: BotMessage, decision: Decision) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "intent": decision.intent,
            "note": decision.note,
            "message_id": message.message_id,
            "text": message.text,
            "rows": [
                [{"text": b.text, "kind": b.kind, "data": b.data, "url": b.url} for b in row]
                for row in message.rows
            ],
        }
        try:
            with self.review_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001
            logger.warning("could not write review log: %s", exc)


def _is_actionable(decision: Decision) -> bool:
    """A decision is actionable if it makes us do something to the chat."""
    return bool(
        decision.press is not None
        or decision.send_text
        or decision.lead is not None
        or decision.intent == "limit"
        or decision.pause_minutes is not None
    )
