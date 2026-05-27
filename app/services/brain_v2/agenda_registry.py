from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class AgendaItemSpec:
    item_key: str
    stage: str
    priority: int
    required: bool
    completion_rule: str
    default_question: str
    slot_key: str | None
    next_stage_hint: str | None


STANDARD_AGENDA: tuple[AgendaItemSpec, ...] = (
    AgendaItemSpec(
        "trust.answer_source",
        "trust",
        10,
        True,
        "candidate understands why the agent contacted them",
        "Я коротко объясню: нашла тебя по профилю и решила предложить формат работы, который может подойти. Ок?",
        "trust_source_answered",
        "trust",
    ),
    AgendaItemSpec(
        "trust.answer_basic_suspicion",
        "trust",
        20,
        True,
        "candidate has no unresolved basic scam/safety objection",
        "Понимаю осторожность. Что именно смущает: откуда контакт, формат работы или безопасность?",
        "trust_basic_suspicion_resolved",
        "age_gate",
    ),
    AgendaItemSpec(
        "age.confirm_18",
        "age_gate",
        30,
        True,
        "candidate confirms they are 18 or older",
        "Скажи, пожалуйста, тебе уже есть 18?",
        "age",
        "qualification",
    ),
    AgendaItemSpec(
        "qualification.check_english",
        "qualification",
        40,
        True,
        "candidate confirms English level or says it is weak",
        "Как у тебя с английским: спокойно читаешь/пишешь или пока базово?",
        "english_level",
        "qualification",
    ),
    AgendaItemSpec(
        "qualification.check_equipment",
        "qualification",
        50,
        True,
        "candidate confirms phone/computer/internet equipment",
        "Для работы нужен стабильный интернет и телефон/ноутбук. С этим всё ок?",
        "equipment_status",
        "qualification",
    ),
    AgendaItemSpec(
        "qualification.check_availability",
        "qualification",
        60,
        True,
        "candidate shares availability",
        "Сколько времени в день тебе комфортно уделять работе?",
        "availability",
        "qualification",
    ),
    AgendaItemSpec(
        "qualification.check_boundaries",
        "qualification",
        70,
        True,
        "candidate confirms boundaries and acceptable format",
        "Важно: без нюдсов и без личных встреч. Такой формат тебе подходит?",
        "boundaries_ok",
        "personalization",
    ),
    AgendaItemSpec(
        "personalization.collect_hobbies",
        "personalization",
        80,
        False,
        "candidate shares interests or preferred themes",
        "Чтобы предложить более подходящий формат, чем ты обычно увлекаешься?",
        "hobbies",
        "personalization",
    ),
    AgendaItemSpec(
        "personalization.suggest_theme",
        "personalization",
        90,
        False,
        "agent suggests a relevant work theme",
        "Могу подобрать тему под твой стиль общения, чтобы было естественнее. Хочешь?",
        "suggested_theme",
        "interview_close",
    ),
    AgendaItemSpec(
        "interview.offer",
        "interview_close",
        100,
        True,
        "candidate agrees to interview or next human step",
        "Если тебе в целом ок, могу передать тебя менеджеру на короткое интервью. Подойдёт?",
        "interview_interest",
        "contact_collection",
    ),
    AgendaItemSpec(
        "contact.collect_name_phone",
        "contact_collection",
        110,
        True,
        "candidate provides name and phone/contact",
        "Оставь, пожалуйста, имя и номер телефона, чтобы менеджер мог связаться.",
        "contact",
        "scheduling",
    ),
    AgendaItemSpec(
        "schedule.collect_time",
        "scheduling",
        120,
        True,
        "candidate provides convenient time",
        "В какое время тебе удобнее, чтобы менеджер написал или позвонил?",
        "preferred_time",
        "handoff",
    ),
    AgendaItemSpec(
        "handoff.prepare_summary",
        "handoff",
        130,
        True,
        "lead summary is ready for human handoff",
        "Спасибо, я передам всё менеджеру, чтобы он написал тебе уже по делу.",
        "handoff_summary",
        "closed",
    ),
)


PROFITCAST_STAGE_MAP = {
    "first_touch": "first_touch_sent",
    "trust_source": "trust",
    "basic_info_pack": "basic_info_pack",
    "deep_info_pack": "qualification",
    "format_boundaries": "qualification",
    "qualification_faq": "qualification_faq",
    "equipment_check": "qualification",
    "company_info": "company",
    "try_close": "interview_close",
    "post_schedule_support": "post_schedule_support",
}

CANONICAL_STAGES = {
    "lead_created",
    "waiting_first_reply",
    "first_touch_sent",
    "lead_replied",
    "info_messages_sent",
    "interest_triage",
    "trust",
    "age_gate",
    "basic_info_pack",
    "qualification_faq",
    "company",
    "qualification",
    "personalization",
    "interview_close",
    "pre_schedule_questions",
    "contact_collection",
    "scheduling",
    "confirmation",
    "post_schedule_support",
    "handoff",
    "closed",
}


class AgendaRegistry:
    @staticmethod
    def standard_items() -> tuple[AgendaItemSpec, ...]:
        bundle_items = load_bundle_agenda_items()
        if not bundle_items:
            return STANDARD_AGENDA
        merged: list[AgendaItemSpec] = []
        seen: set[str] = set()
        for item in [*bundle_items, *STANDARD_AGENDA]:
            if item.item_key in seen:
                continue
            merged.append(item)
            seen.add(item.item_key)
        return tuple(merged)

    @staticmethod
    def first_pending_for_stage(items: list[dict], stage: str) -> dict | None:
        candidates = [
            item
            for item in items
            if item.get("stage") == stage and item.get("status") in {"pending", "active", "blocked"}
        ]
        candidates.sort(key=lambda item: int(item.get("priority") or 100))
        return candidates[0] if candidates else None


def load_profitcast_agenda_items() -> tuple[AgendaItemSpec, ...]:
    path = Path(__file__).resolve().parents[3] / "data" / "profitcast" / "agenda_template_profitcast_v1.json"
    return load_agenda_items_from_path(path)


def load_bundle_agenda_items() -> tuple[AgendaItemSpec, ...]:
    data_dir = Path(__file__).resolve().parents[3] / "data"
    preferred = [
        data_dir / "profitcast" / "agenda_template_profitcast_v1.json",
        data_dir / "nastya" / "agenda_additions_nastya_v1.json",
    ]
    paths: list[Path] = []
    seen: set[Path] = set()
    for path in preferred:
        if path.exists():
            paths.append(path)
            seen.add(path.resolve())
    for path in sorted(data_dir.glob("*/agenda_*.json")):
        resolved = path.resolve()
        if resolved not in seen:
            paths.append(path)
            seen.add(resolved)

    items: list[AgendaItemSpec] = []
    for path in paths:
        items.extend(load_agenda_items_from_path(path))
    return tuple(items)


def load_agenda_items_from_path(path: Path) -> tuple[AgendaItemSpec, ...]:
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, list):
        return ()
    items: list[AgendaItemSpec] = []
    for raw in payload:
        if not isinstance(raw, dict) or not raw.get("item_key"):
            continue
        default_question = candidate_facing_default_question(raw)
        completion_rule = completion_rule_from_raw(raw)
        items.append(
            AgendaItemSpec(
                item_key=str(raw["item_key"]),
                stage=canonical_agenda_stage(raw.get("stage")),
                priority=int(raw.get("priority") or 100),
                required=bool(raw.get("required", True)),
                completion_rule=completion_rule,
                default_question=default_question,
                slot_key=str(raw["slot_key"]) if raw.get("slot_key") else None,
                next_stage_hint=str(raw["next_stage_hint"]) if raw.get("next_stage_hint") else None,
            )
        )
    return tuple(items)


def candidate_facing_default_question(raw: dict) -> str:
    explicit = raw.get("default_question")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    item_key = str(raw.get("item_key") or "")
    fallback_by_key = {
        "trust.provide_company_links": "Могу скинуть сайт и официальный Telegram-канал, чтобы ты спокойно проверила.",
        "trust.answer_source": "Коротко объясню: нашла твой профиль и решила предложить формат работы, который может подойти. Ок?",
        "trust.answer_basic_suspicion": "Понимаю осторожность. Что именно смущает: откуда контакт, формат работы или безопасность?",
        "company.resend_telegram_link": "Сейчас продублирую ссылку отдельно.",
        "company.explain_verification": "Можешь спокойно проверить компанию и канал, я никуда не тороплю.",
        "schedule.handle_timezone_difference": "Подскажи, пожалуйста, какой у тебя часовой пояс или город?",
        "support.resolve_zoom_link_issue": "Zoom удобнее открыть через приложение. Получилось скачать или открыть ссылку?",
        "support.resolve_access_issue": "Напиши, пожалуйста, что именно не открывается, и я подскажу следующий шаг.",
    }
    if item_key in fallback_by_key:
        return fallback_by_key[item_key]
    if item_key.startswith("age."):
        return "Скажи, пожалуйста, тебе уже есть 18?"
    if item_key.startswith("qualification."):
        return "Уточню один момент, чтобы правильно сориентировать тебя по формату."
    if item_key.startswith("personalization."):
        return "Чтобы предложить более подходящий формат, чем ты обычно увлекаешься?"
    if item_key.startswith("interview."):
        return "Если тебе в целом ок, могу передать тебя менеджеру на короткое интервью. Подойдет?"
    if item_key.startswith("contact."):
        return "Оставь, пожалуйста, имя и номер телефона, чтобы менеджер мог связаться."
    if item_key.startswith("schedule."):
        return "В какое время тебе удобнее, чтобы менеджер написал или позвонил?"
    if item_key.startswith("handoff."):
        return "Спасибо, я передам все менеджеру, чтобы он написал тебе уже по делу."
    return "Уточни, пожалуйста, что именно тебе важно понять?"


def completion_rule_from_raw(raw: dict) -> str:
    signals = raw.get("completion_signal")
    if isinstance(signals, list):
        compact_signals = [str(signal).strip() for signal in signals if str(signal).strip()]
        if compact_signals:
            return ", ".join(compact_signals)
    explicit_rule = raw.get("completion_rule")
    if isinstance(explicit_rule, str) and explicit_rule.strip():
        return explicit_rule.strip()
    default_goal = raw.get("default_goal")
    if isinstance(default_goal, str) and default_goal.strip():
        return default_goal.strip()
    default_action = raw.get("default_action")
    if isinstance(default_action, str) and default_action.strip():
        return default_action.strip()
    return str(raw["item_key"])


def canonical_agenda_stage(stage: object) -> str:
    raw_stage = str(stage or "qualification")
    mapped = PROFITCAST_STAGE_MAP.get(raw_stage, raw_stage)
    return mapped if mapped in CANONICAL_STAGES else "qualification"
