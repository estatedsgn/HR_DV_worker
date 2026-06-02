from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


StageType = Literal["waiting", "action", "final"]


@dataclass(frozen=True, slots=True)
class StagePolicy:
    name: str
    goal: str
    current_question: str | None
    required_fields: tuple[str, ...]
    next_stage_if_completed: str | None
    allowed_transitions: tuple[str, ...]
    stage_type: StageType
    voice_pack_id: str | None = None
    template_id: str | None = None


CANDIDATE_PROFILE_FIELDS: tuple[str, ...] = (
    "interest_confirmed",
    "interest_status",
    "age",
    "age_confirmed",
    "salary_schedule_interest",
    "questions_resolved",
    "profile_info",
    "work_or_study",
    "hobbies",
    "smalltalk_done",
    "room_available",
    "room_note",
    "equipment_available",
    "phone_model",
    "interview_interest",
    "candidate_name",
    "phone_number",
    "interview_day_confirmed",
    "interview_day",
    "interview_time",
    "custom_interview_datetime",
    "qualification_status",
    "asked_questions",
    "objections",
)


STAGE_POLICIES: dict[str, StagePolicy] = {
    "interest_check": StagePolicy(
        name="interest_check",
        goal="Понять, есть ли у кандидатки интерес узнать подробности.",
        current_question="рассказать подробнее?",
        required_fields=("interest_confirmed",),
        next_stage_if_completed="age_check",
        allowed_transitions=("interest_check", "age_check", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "age_check": StagePolicy(
        name="age_check",
        goal="Узнать полный возраст кандидатки.",
        current_question="давай для начала уточним небольшую формальность, сколько тебе лет?",
        required_fields=("age_confirmed",),
        next_stage_if_completed="work_intro_delivery",
        allowed_transitions=("age_check", "work_intro_delivery", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "work_intro_delivery": StagePolicy(
        name="work_intro_delivery",
        goal="Отправить готовые голосовые о работе.",
        current_question=None,
        required_fields=(),
        next_stage_if_completed="salary_schedule_offer",
        allowed_transitions=("salary_schedule_offer",),
        stage_type="action",
        voice_pack_id="work_intro",
    ),
    "salary_schedule_offer": StagePolicy(
        name="salary_schedule_offer",
        goal="Получить согласие кандидатки узнать про зарплату и график.",
        current_question="если интересна наша сфера, давай расскажу про зп и график 🐬",
        required_fields=("salary_schedule_interest",),
        next_stage_if_completed="salary_schedule_delivery",
        allowed_transitions=("salary_schedule_offer", "salary_schedule_delivery", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "salary_schedule_delivery": StagePolicy(
        name="salary_schedule_delivery",
        goal="Отправить готовые материалы про зарплату и график.",
        current_question=None,
        required_fields=(),
        next_stage_if_completed="post_equipment_questions_check",
        allowed_transitions=("post_equipment_questions_check",),
        stage_type="action",
        voice_pack_id="salary_schedule",
    ),
    "post_equipment_questions_check": StagePolicy(
        name="post_equipment_questions_check",
        goal="Понять, остались ли у кандидатки вопросы после вводных материалов.",
        current_question="остались ли у тебя какие-нибудь ещё вопросики?",
        required_fields=("questions_resolved",),
        next_stage_if_completed="profile_theme_check",
        allowed_transitions=("post_equipment_questions_check", "profile_theme_check", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "profile_theme_check": StagePolicy(
        name="profile_theme_check",
        goal="Собрать базовую информацию: учёба, работа, интересы.",
        current_question="расскажи немного о себе, учишься/работаешь? чем любишь заниматься в свободное время? помогу подобрать тематику для стримов 🐬",
        required_fields=("profile_info",),
        next_stage_if_completed="support_smalltalk",
        allowed_transitions=("profile_theme_check", "support_smalltalk", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "support_smalltalk": StagePolicy(
        name="support_smalltalk",
        goal="Коротко живо отреагировать на рассказ кандидатки и перейти к квалификации.",
        current_question=None,
        required_fields=(),
        next_stage_if_completed="room_available_check",
        allowed_transitions=("room_available_check",),
        stage_type="action",
    ),
    "room_available_check": StagePolicy(
        name="room_available_check",
        goal="Уточнить, есть ли место/комната, где кандидатке никто не помешает.",
        current_question="есть ли у тебя комната, в которой тебе никто не помешает во время стримов?",
        required_fields=("room_available",),
        next_stage_if_completed="equipment_phone_check",
        allowed_transitions=("room_available_check", "equipment_phone_check", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "equipment_phone_check": StagePolicy(
        name="equipment_phone_check",
        goal="Уточнить модель телефона кандидатки.",
        current_question="какая у тебя моделька телефончика?",
        required_fields=("phone_model",),
        next_stage_if_completed="interview_offer",
        allowed_transitions=("equipment_phone_check", "interview_offer", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "interview_offer": StagePolicy(
        name="interview_offer",
        goal="Предложить записаться на собеседование.",
        current_question="мы можем с тобой записаться на собеседование?",
        required_fields=("interview_interest",),
        next_stage_if_completed="contact_collection",
        allowed_transitions=("interview_offer", "contact_collection", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "contact_collection": StagePolicy(
        name="contact_collection",
        goal="Собрать имя и номер телефона для записи.",
        current_question="для записи мне нужно твоё имя и номер телефончика",
        required_fields=("candidate_name", "phone_number"),
        next_stage_if_completed="interview_day_check",
        allowed_transitions=("contact_collection", "interview_day_check", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "interview_day_check": StagePolicy(
        name="interview_day_check",
        goal="Уточнить, удобно ли провести собеседование завтра.",
        current_question="завтра будет удобно провести собеседование?",
        required_fields=("interview_day_confirmed",),
        next_stage_if_completed="interview_time_check",
        allowed_transitions=("interview_day_check", "interview_time_check", "interview_custom_time", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "interview_time_check": StagePolicy(
        name="interview_time_check",
        goal="Выбрать время собеседования завтра в диапазоне 11:00-18:00.",
        current_question="с 11:00 по 18:00 по мск, в какое время будет удобнее?",
        required_fields=("interview_time",),
        next_stage_if_completed="human_handoff",
        allowed_transitions=("interview_time_check", "human_handoff", "lost", "do_not_contact"),
        stage_type="waiting",
    ),
    "interview_custom_time": StagePolicy(
        name="interview_custom_time",
        goal="Уточнить удобные дату и время, если завтра неудобно.",
        current_question="хорошо, когда тебе будет удобно пройти собеседование?",
        required_fields=("custom_interview_datetime",),
        next_stage_if_completed="human_handoff",
        allowed_transitions=("interview_custom_time", "human_handoff", "lost", "do_not_contact"),
        stage_type="waiting",
    ),
    "human_handoff": StagePolicy(
        name="human_handoff",
        goal="Передать диалог человеку с полным контекстом.",
        current_question=None,
        required_fields=(),
        next_stage_if_completed=None,
        allowed_transitions=("human_handoff",),
        stage_type="final",
    ),
    "lost": StagePolicy(
        name="lost",
        goal="Диалог закрыт отказом или неподходящей квалификацией.",
        current_question=None,
        required_fields=(),
        next_stage_if_completed=None,
        allowed_transitions=("lost",),
        stage_type="final",
    ),
    "do_not_contact": StagePolicy(
        name="do_not_contact",
        goal="Кандидатка попросила больше не писать.",
        current_question=None,
        required_fields=(),
        next_stage_if_completed=None,
        allowed_transitions=("do_not_contact",),
        stage_type="final",
    ),
    # Compatibility-only states from the previous funnel.
    "company_intro": StagePolicy(
        name="company_intro",
        goal="Совместимый action-state старой воронки.",
        current_question=None,
        required_fields=(),
        next_stage_if_completed="profile_theme_check",
        allowed_transitions=("profile_theme_check",),
        stage_type="action",
        template_id="company_intro_message",
    ),
    "try_interest_check": StagePolicy(
        name="try_interest_check",
        goal="Совместимый waiting-state старой воронки.",
        current_question="желаешь попробовать нашу сферу?",
        required_fields=("interest_confirmed",),
        next_stage_if_completed="profile_theme_check",
        allowed_transitions=("try_interest_check", "profile_theme_check", "lost", "do_not_contact", "human_handoff"),
        stage_type="waiting",
    ),
    "ready_for_interview": StagePolicy(
        name="ready_for_interview",
        goal="Совместимый финальный статус старой воронки.",
        current_question=None,
        required_fields=(),
        next_stage_if_completed=None,
        allowed_transitions=("ready_for_interview",),
        stage_type="final",
    ),
}


ACTION_STAGE_TO_WAITING_STAGE = {
    "work_intro_delivery": "salary_schedule_offer",
    "salary_schedule_delivery": "post_equipment_questions_check",
    "support_smalltalk": "room_available_check",
    "company_intro": "profile_theme_check",
}


TERMINAL_STAGES = {"human_handoff", "ready_for_interview", "lost", "do_not_contact"}


def get_stage_policy(stage: str | None) -> StagePolicy:
    return STAGE_POLICIES.get(stage or "interest_check") or STAGE_POLICIES["interest_check"]


def policy_state_patch(stage: str) -> dict[str, str | None]:
    policy = get_stage_policy(stage)
    return {
        "current_goal": policy.goal,
        "current_question": policy.current_question,
        "next_stage_if_completed": policy.next_stage_if_completed,
        "pending_question": policy.name if policy.current_question else None,
        "pending_question_text": policy.current_question,
    }


def empty_candidate_profile() -> dict[str, Any]:
    return {field: None for field in CANDIDATE_PROFILE_FIELDS}


def normalize_candidate_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    normalized = empty_candidate_profile()
    if profile:
        for field in CANDIDATE_PROFILE_FIELDS:
            if field in profile:
                normalized[field] = profile[field]
    if normalized.get("asked_questions") is None:
        normalized["asked_questions"] = []
    if normalized.get("objections") is None:
        normalized["objections"] = []
    return normalized


def can_transition(current_stage: str, target_stage: str) -> bool:
    return target_stage in get_stage_policy(current_stage).allowed_transitions


def stage_requirement_met(stage: str, profile: dict[str, Any]) -> bool:
    if stage == "interest_check":
        return profile.get("interest_confirmed") is True
    if stage == "age_check":
        return profile.get("age_confirmed") is True and int(profile.get("age") or 0) >= 18
    if stage == "salary_schedule_offer":
        return profile.get("salary_schedule_interest") is True
    if stage == "post_equipment_questions_check":
        return profile.get("questions_resolved") is True
    if stage == "profile_theme_check":
        return bool(profile.get("profile_info"))
    if stage == "room_available_check":
        return profile.get("room_available") is True
    if stage == "equipment_phone_check":
        return bool(profile.get("phone_model"))
    if stage == "interview_offer":
        return profile.get("interview_interest") is True
    if stage == "contact_collection":
        return bool(profile.get("candidate_name")) and bool(profile.get("phone_number"))
    if stage == "interview_day_check":
        return profile.get("interview_day_confirmed") is True or bool(profile.get("interview_day"))
    if stage == "interview_time_check":
        return bool(profile.get("interview_time"))
    if stage == "interview_custom_time":
        return bool(profile.get("custom_interview_datetime"))
    if stage == "try_interest_check":
        return profile.get("interest_confirmed") is True
    return True


def next_stage_if_requirement_met(stage: str, profile: dict[str, Any]) -> str:
    if not stage_requirement_met(stage, profile):
        return stage
    if stage == "interview_day_check" and profile.get("interview_day_confirmed") is not True:
        return "interview_custom_time"
    policy = get_stage_policy(stage)
    return policy.next_stage_if_completed or stage
