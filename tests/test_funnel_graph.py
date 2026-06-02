import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.lead import Lead
from app.models.outbound_job import OutboundJob
from app.services.funnel_graph.actions import FunnelActionExecutor
from app.services.funnel_graph.checkpoint import psycopg_conn_string
from app.services.funnel_graph.gateway import LangGraphFunnelGateway
from app.services.funnel_graph.graph import build_funnel_graph, pending_actions_from_outgoing, state_controller
from app.services.funnel_graph.knowledge import StaticFunnelKnowledgeBase
from app.services.funnel_graph.reply import (
    ReplyOutgoingMessage,
    ReplyResult,
    deterministic_reply,
    guard_reply_with_policy,
    reply_llm_payload,
    sanitize_reply_result,
)
from app.services.funnel_graph.semantic import SemanticResult, deterministic_semantic, merge_deterministic_facts
from app.services.funnel_graph.turn_buffer import FunnelTurnBufferService, is_before_reset_baseline
from scripts.annotate_dialogue_examples import parse_dialogues


def test_funnel_starts_with_first_touch_message() -> None:
    state = run_graph(initial_state())

    assert state["stage"] == "interest_check"
    assert text_messages(state) == [
        "привет! ты просто потрясающая! 🤩 у меня есть интересное предложение о работе стриминге на платформах подобных twitch. это не имеет отношения к вебкам или onlyfans"
    ]
    assert state["metadata"]["last_graph_node"] == "save_state"


def test_annotation_parser_supports_candidate_multiline_batches() -> None:
    parsed = parse_dialogues(
        """
        Рекрутер:
        привет, расскажу про работу

        Кандидатка:
        привет
        а как вы меня нашли?
        и почему я подошла?

        Рекрутер:
        нашла контакт в открытых каналах
        по профилю ты подошла под формат
        """
    )

    assert len(parsed) == 1
    runs = parsed[0]["speaker_runs"]
    assert runs[1]["speaker"] == "lead"
    assert runs[1]["messages"] == ["привет\nа как вы меня нашли?\nи почему я подошла?"]
    assert runs[2]["speaker"] == "recruiter"
    assert len(runs[2]["messages"]) == 1


def test_interest_question_is_interrupt_and_repeats_current_question() -> None:
    state = run_graph(
        initial_state(stage="interest_check"),
        "А откуда у вас мой контакт? И почему я заинтересовала вас?",
    )

    assert state["stage"] == "interest_check"
    assert state["candidate_profile"]["interest_confirmed"] is None
    assert "Контакт мог" in state["reply_text"]
    assert "Рассказать подробнее?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True
    assert state["metadata"]["interrupt_followup_question"] == "Рассказать подробнее?"
    delayed = delayed_followup_actions(state)
    assert len(delayed) == 1
    assert delayed[0]["delay_seconds"] == 60
    assert delayed[0]["text"] == "Хочешь, расскажу подробнее?"


def test_interest_job_question_does_not_move_to_age() -> None:
    state = run_graph(initial_state(stage="interest_check"), "Что за предложение?")

    assert state["stage"] == "interest_check"
    assert state["candidate_profile"]["interest_confirmed"] is None
    assert "Сколько тебе лет?" not in state["reply_text"]
    assert "Рассказать подробнее?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_interest_agreement_moves_to_age_check() -> None:
    state = run_graph(initial_state(stage="interest_check"), "Ну хорошо, расскажи")

    assert state["stage"] == "age_check"
    assert state["candidate_profile"]["interest_confirmed"] is True
    assert text_messages(state) == ["Для начала скажи, сколько тебе лет?"]


def test_age_answer_sends_work_intro_pack_and_salary_offer() -> None:
    state = run_graph(initial_state(stage="age_check", profile={"interest_confirmed": True}), "18")

    assert state["stage"] == "salary_schedule_offer"
    assert state["candidate_profile"]["age_confirmed"] is True
    assert state["sent_voice_packs"] == ["work_intro"]
    assert voice_packs(state) == ["work_intro"]
    assert text_messages(state) == ["Если интересна наша сфера, давай расскажу про зп и график"]


def test_salary_offer_question_is_interrupt() -> None:
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Это OnlyFans и оголёнка?",
    )

    assert state["stage"] == "salary_schedule_offer"
    assert state["candidate_profile"]["salary_schedule_interest"] is None
    assert "не OnlyFans" in state["reply_text"]
    assert "Если интересна наша сфера, давай расскажу про зп и график" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_interested_but_english_concern_is_not_refusal() -> None:
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Мне интересно, но с английским беда",
    )

    assert state["stage"] == "salary_schedule_offer"
    assert state["semantic_result"]["message_type"] == "mixed"
    assert state["semantic_result"]["interrupt_topic"] == "english_level"
    assert "Английский не обязателен" in state["reply_text"]


def test_privacy_and_english_batch_uses_new_knowledge_topics() -> None:
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Это будет в зарубежной сфере? Не будет опасности, что меня узнают друзья? Нужно ли знание другого языка? Да, очень интересно",
    )

    assert state["stage"] == "salary_schedule_offer"
    assert state["semantic_result"]["message_type"] == "mixed"
    assert state["semantic_result"]["interrupt_type"] == "objection"
    assert {"privacy_anonymity", "english_level"}.issubset(set(state["semantic_result"]["retrieval_topics"]))
    assert "Английский не обязателен" in state["reply_text"]
    assert "зарубеж" in state["reply_text"]


def test_documents_and_scam_concern_get_relevant_knowledge() -> None:
    state = run_graph(
        initial_state(
            stage="post_equipment_questions_check",
            profile={"interest_confirmed": True, "age_confirmed": True, "salary_schedule_interest": True},
        ),
        "Как я могу удостовериться, что это правда и не скам? Нужно паспорт отправлять или вложения?",
    )

    assert state["stage"] == "post_equipment_questions_check"
    assert state["semantic_result"]["interrupt_type"] == "objection"
    assert {"documents_privacy", "suspicious_or_scam"}.issubset(set(state["semantic_result"]["retrieval_topics"]))
    assert "Вложения" in state["reply_text"]
    assert "паспорт" in state["reply_text"]


def test_exit_policy_concern_does_not_advance_stage() -> None:
    state = run_graph(
        initial_state(
            stage="post_equipment_questions_check",
            profile={"interest_confirmed": True, "age_confirmed": True, "salary_schedule_interest": True},
        ),
        "Опасаюсь договора, вдруг там написано, что я обязана работать год и будет отработка?",
    )

    assert state["stage"] == "post_equipment_questions_check"
    assert state["semantic_result"]["interrupt_type"] == "objection"
    assert "exit_policy" in state["semantic_result"]["retrieval_topics"]
    assert "Отработки" in state["reply_text"] or "отработки" in state["reply_text"]


def test_salary_agreement_sends_salary_pack_and_asks_any_questions() -> None:
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Хорошо, слушаю",
    )

    assert state["stage"] == "post_equipment_questions_check"
    assert state["candidate_profile"]["salary_schedule_interest"] is True
    assert voice_packs(state) == ["salary_schedule"]
    assert text_messages(state) == ["Остались ли у тебя какие-нибудь ещё вопросы?"]


def test_action_stage_sends_voice_even_when_llm_reply_send_is_false() -> None:
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Хорошо, слушаю",
    )

    assert state["stage"] == "post_equipment_questions_check"
    assert voice_packs(state) == ["salary_schedule"]
    assert text_messages(state) == ["Остались ли у тебя какие-нибудь ещё вопросы?"]


def test_equipment_does_not_close_phone_model_requirement() -> None:
    state = run_graph(initial_state(stage="equipment_phone_check"), "У меня есть стриминговое оборудование")

    assert state["stage"] == "equipment_phone_check"
    assert state["candidate_profile"]["equipment_available"] is True
    assert state["candidate_profile"]["phone_model"] is None
    assert text_messages(state) == ["О, круто! А чтобы мы точно всё настроили — какая у тебя модель телефона?"]


def test_equipment_question_is_interrupt_not_equipment_available() -> None:
    state = run_graph(initial_state(stage="equipment_phone_check"), "А оборудование нужно?")

    assert state["stage"] == "equipment_phone_check"
    assert state["candidate_profile"]["equipment_available"] is None
    assert state["candidate_profile"]["phone_model"] is None
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_reply_guard_uses_policy_followup_for_equipment_partial() -> None:
    state = initial_state(stage="equipment_phone_check")
    state["semantic_result"] = {
        "message_type": "partial_answer",
        "current_goal_satisfied": False,
        "has_unresolved_interrupt": False,
        "facts": {"equipment_available": True},
    }
    parsed = ReplyResult(
        outgoing_messages=[
            ReplyOutgoingMessage(type="text", text="О, круто! Но всё равно уточню: какая модель телефона?"),
        ],
    )

    guarded = guard_reply_with_policy(state, parsed)

    assert guarded is not None
    assert [message.text for message in guarded.outgoing_messages] == [
        "О, круто! А чтобы мы точно всё настроили — какая у тебя модель телефона?"
    ]


def test_merge_deterministic_partial_equipment_overrides_llm_interrupt() -> None:
    fallback = deterministic_semantic(
        initial_state(stage="equipment_phone_check") | {"incoming_message": "У меня есть стриминговое оборудование"}
    )
    llm = deterministic_semantic(
        initial_state(stage="equipment_phone_check") | {"incoming_message": "А оборудование нужно?"}
    )

    merged = merge_deterministic_facts(llm, fallback)

    assert merged.message_type == "partial_answer"
    assert merged.has_unresolved_interrupt is False
    assert merged.facts.equipment_available is True


def test_phone_brand_without_model_is_partial_and_asks_for_model() -> None:
    state = run_graph(initial_state(stage="equipment_phone_check"), "айфон")

    assert state["stage"] == "equipment_phone_check"
    assert state["semantic_result"]["message_type"] == "partial_answer"
    assert state["candidate_profile"]["phone_model"] is None
    assert "модель" in (state["reply_text"] or "").lower()


def test_llm_goal_flag_is_checked_against_active_required_fields() -> None:
    state = initial_state(stage="equipment_phone_check")
    llm = SemanticResult(
        message_type="stage_answer",
        current_goal_satisfied=True,
        has_unresolved_interrupt=False,
        facts={"interest_confirmed": True},
        confidence=0.8,
    )
    fallback = SemanticResult(
        message_type="unclear",
        current_goal_satisfied=False,
        has_unresolved_interrupt=False,
        confidence=0.6,
    )

    merged = merge_deterministic_facts(llm, fallback, state=state)

    assert merged.current_goal_satisfied is False


def test_retrieval_query_is_empty_when_topics_are_empty() -> None:
    result = deterministic_semantic(initial_state(stage="profile_theme_check") | {"incoming_message": "ок"})

    assert result.retrieval_topics == []
    assert result.retrieval_query == ""


def test_phone_model_moves_to_interview_offer() -> None:
    state = run_graph(initial_state(stage="equipment_phone_check"), "Samsung S25 Ultra")

    assert state["stage"] == "interview_offer"
    assert state["candidate_profile"]["phone_model"] == "Samsung S25 Ultra"
    assert text_messages(state) == ["Можем записаться на собеседование?"]


def test_interview_offer_accepts_go_zapishimsya_and_asks_contact() -> None:
    state = run_graph(initial_state(stage="interview_offer"), "норм, ну го запишимся на собеседование")

    assert state["stage"] == "contact_collection"
    assert state["candidate_profile"]["interview_interest"] is True
    assert text_messages(state) == ["Для записи мне нужен твой номер телефона и имя"]


def test_no_questions_moves_to_profile_context() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "Пока вопросов нет")

    assert state["stage"] == "profile_theme_check"
    assert state["candidate_profile"]["questions_resolved"] is True
    assert text_messages(state) == [
        "Расскажи немного о себе: учишься/работаешь? Чем любишь заниматься в свободное время?"
    ]


def test_post_equipment_short_faq_topic_answers_before_repeating_question() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "Оплата")

    assert state["stage"] == "post_equipment_questions_check"
    assert state["candidate_profile"]["questions_resolved"] is None
    assert "стажировочных днях" in state["reply_text"]
    assert "Остались ли у тебя какие-нибудь ещё вопросы?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True
    assert state["metadata"]["interrupt_followup_question"] == "Остались ли у тебя какие-нибудь ещё вопросы?"


def test_booking_intent_after_materials_moves_to_next_required_question() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "норм, ну го запишимся на собеседование")

    assert state["stage"] == "profile_theme_check"
    assert state["candidate_profile"]["questions_resolved"] is True
    assert state["candidate_profile"]["interview_interest"] is True
    assert text_messages(state) == [
        "Расскажи немного о себе: учишься/работаешь? Чем любишь заниматься в свободное время?"
    ]


def test_no_questions_and_sobes_booking_skips_completed_profile_question() -> None:
    state = run_graph(
        initial_state(stage="post_equipment_questions_check", profile={"profile_info": "не работаю, люблю тикток"}),
        "вопросов нет, хочу на собес",
    )

    assert state["stage"] == "room_available_check"
    assert state["candidate_profile"]["questions_resolved"] is True
    assert state["candidate_profile"]["interview_interest"] is True
    assert "учишься/работаешь" not in state["reply_text"]
    assert "Есть ли у тебя комната" in state["reply_text"]


def test_neutral_ack_after_faq_waits_without_reply() -> None:
    interrupted = run_graph(initial_state(stage="post_equipment_questions_check"), "Оплата")

    state = run_graph(next_state(interrupted), "спасибо")

    assert state["stage"] == "post_equipment_questions_check"
    assert state["send_reply"] is False
    assert text_messages(state) == []
    assert state["reply_result"]["send_reply"] is False
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_english_question_gets_short_relevant_answer() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "А английский нужен?")

    assert state["stage"] == "post_equipment_questions_check"
    assert "Английский не обязателен" in state["reply_text"]
    assert "переводчиком" in state["reply_text"]
    assert "Остались ли у тебя какие-нибудь ещё вопросы?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_repeated_interrupts_softly_return_to_goal_after_fourth_question() -> None:
    state = initial_state(stage="interest_check")
    for message in [
        "А откуда у вас мой контакт?",
        "А что за работа?",
        "А договор есть?",
        "А английский нужен?",
    ]:
        state = run_graph(next_state(state), message)

    assert state["stage"] == "interest_check"
    assert "Английский не обязателен" in state["reply_text"]
    assert "Чтобы не грузить всем сразу" in state["reply_text"]
    assert "Рассказать подробнее?" not in state["reply_text"]


def test_mixed_interest_question_preserves_interest_fact() -> None:
    state = run_graph(initial_state(stage="interest_check"), "Интересно, а договор есть?")

    assert state["stage"] == "interest_check"
    assert state["semantic_result"]["message_type"] == "mixed"
    assert state["candidate_profile"]["interest_confirmed"] is True
    assert state["candidate_profile"]["interest_status"] == "interested"
    assert "ГПХ" in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_multi_message_interest_source_and_selection_interrupt() -> None:
    state = initial_state(stage="interest_check")
    state["message_batch"] = [
        {"direction": "inbound", "sender_type": "lead", "body": "привет"},
        {"direction": "inbound", "sender_type": "lead", "body": "а откуда у вас мой контакт?"},
        {"direction": "inbound", "sender_type": "lead", "body": "и вообще по какой причине я заинтересовала вас?"},
    ]

    state = run_graph(state)

    assert state["stage"] == "interest_check"
    assert state["candidate_profile"]["interest_confirmed"] is None
    assert state["semantic_result"]["retrieval_topics"][:2] == ["contact_source", "why_selected"]
    assert "Контакт мог" in state["reply_text"]
    assert "Жёстких критериев" in state["reply_text"]
    assert "Рассказать подробнее?" not in state["reply_text"]


def test_multi_message_nudity_batch_is_one_objection() -> None:
    state = initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True})
    state["message_batch"] = [
        {"direction": "inbound", "sender_type": "lead", "body": "хорошо, это конечно классно, но я так и не поняла"},
        {"direction": "inbound", "sender_type": "lead", "body": "что значит уделять внимание? Это онлифанс тема, оголенка?"},
        {"direction": "inbound", "sender_type": "lead", "body": "или что-то другое?"},
    ]

    state = run_graph(state)

    assert state["stage"] == "salary_schedule_offer"
    assert state["semantic_result"]["has_unresolved_interrupt"] is True
    assert "nudity_onlyfans" in state["semantic_result"]["retrieval_topics"]
    assert "job_description" in state["semantic_result"]["retrieval_topics"]
    assert "не OnlyFans" in state["reply_text"]
    assert "прямые трансляции" in state["reply_text"]


def test_multi_topic_question_batch_answers_all_relevant_knowledge() -> None:
    state = initial_state(stage="post_equipment_questions_check")
    state["message_batch"] = [
        {
            "direction": "inbound",
            "sender_type": "lead",
            "body": (
                "звучит классно. вопросы: оборудование, которое я должна иметь, "
                "критерии, оплата и договор ГПХ. Работа не официальная?"
            ),
        }
    ]

    state = run_graph(state)

    assert state["stage"] == "post_equipment_questions_check"
    assert {"equipment", "payment_process", "contract_gph"} <= set(state["semantic_result"]["retrieval_topics"])
    assert "оборудование" in state["reply_text"]
    assert "стажировочных днях" in state["reply_text"]
    assert "ГПХ" in state["reply_text"]
    assert "Остались ли у тебя какие-нибудь ещё вопросы?" not in state["reply_text"]


def test_friend_and_platform_batch_is_question_not_objection() -> None:
    state = initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True})
    state["message_batch"] = [
        {"direction": "inbound", "sender_type": "lead", "body": "а можно с подругой?"},
        {"direction": "inbound", "sender_type": "lead", "body": "и что за платформа?"},
    ]

    state = run_graph(state)

    assert state["stage"] == "salary_schedule_offer"
    assert state["semantic_result"]["message_type"] == "interrupt_question"
    assert state["semantic_result"]["interrupt_type"] == "question"
    assert {"friend_streaming", "platform_info"} <= set(state["semantic_result"]["retrieval_topics"])
    assert "Подругу привести можно" in state["reply_text"]
    assert "Dacast" in state["reply_text"]
    assert "Понимаю сомнение" not in state["reply_text"]
    payload_topics = {item["topic"] for item in reply_llm_payload(state)["knowledge_options"]}
    assert {"friend_streaming", "platform_info"} <= payload_topics
    assert "trust_concern" not in payload_topics


def test_post_equipment_irrelevant_reply_uses_natural_followup() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "19")

    assert state["stage"] == "post_equipment_questions_check"
    assert text_messages(state) == [
        "Поняла. Тогда уточню: остались ли у тебя ещё вопросы по условиям, оплате или формату?"
    ]


def test_social_only_greeting_does_not_create_interrupt_or_repeat_greeting() -> None:
    state = run_graph(initial_state(stage="interest_check"), "Привет")

    assert state["stage"] == "interest_check"
    assert state["semantic_result"]["has_unresolved_interrupt"] is False
    assert text_messages(state) == ["Рассказать подробнее?"]


def test_actionable_topic_answers_knowledge_instead_of_repeating_question() -> None:
    state = initial_state(stage="post_equipment_questions_check")
    state["incoming_message"] = "я раздеваться не буду"
    state["semantic_result"] = {
        "message_type": "unclear",
        "current_goal_satisfied": False,
        "has_unresolved_interrupt": True,
        "interrupt_type": "unclear",
        "interrupt_topic": "nudity_onlyfans",
        "interrupt_text": "я раздеваться не буду",
        "retrieval_topics": ["nudity_onlyfans"],
        "facts": {},
    }
    state["faq_context"] = [
        {
            "topic": "nudity_onlyfans",
            "answer": "Раздеваться не нужно, это не OnlyFans и не вебкам.",
        }
    ]

    reply = deterministic_reply(state)
    text = reply.outgoing_messages[0].text or ""

    assert "Раздеваться не нужно" in text
    assert "остались ли" not in text.lower()


def test_reply_llm_payload_gives_final_layer_dialogue_and_knowledge_options() -> None:
    state = initial_state(stage="post_equipment_questions_check")
    state["incoming_message"] = "а точно без раздевания?"
    state["recent_messages"] = [
        {"direction": "outbound", "sender_type": "agent", "body": "Остались ли вопросы?"},
        {"direction": "inbound", "sender_type": "lead", "body": "а точно без раздевания?"},
    ]
    state["conversation_history"] = [
        {"direction": "outbound", "body": "привет! есть предложение"},
        {"direction": "inbound", "body": "да"},
    ]
    state["semantic_result"] = {
        "message_type": "objection",
        "current_goal_satisfied": False,
        "has_unresolved_interrupt": True,
        "interrupt_type": "objection",
        "interrupt_topic": "nudity_onlyfans",
        "retrieval_topics": ["nudity_onlyfans"],
        "facts": {},
    }
    state["faq_context"] = [
        {"topic": "nudity_onlyfans", "answer": "Раздеваться не нужно."},
    ]
    state["objection_context"] = [
        {"topic": "nudity_concern", "content": "Формат без ню и OnlyFans."},
    ]

    payload = reply_llm_payload(state)

    assert payload["role_contract"]["layer"] == "final_reply_generator"
    assert payload["role_contract"]["semantic_is_classifier_only"] is True
    assert payload["reply_mode_contract"]["max_text_messages"] == 3
    assert payload["reply_mode_contract"]["max_total_reply_sentences"] == 3
    assert payload["reply_mode_contract"]["max_sentences_per_text_message"] == 2
    assert payload["stage_contract"]["current_state"] == "post_equipment_questions_check"
    assert payload["dialogue_context"]["recent_messages"][-1]["body"] == "а точно без раздевания?"
    assert payload["dialogue_context"]["conversation_history"][0]["body"] == "привет! есть предложение"
    assert [item["content"] for item in payload["knowledge_options"]] == [
        "Раздеваться не нужно.",
        "Формат без ню и OnlyFans.",
    ]


def test_delay_then_answer_creates_delayed_pending_action() -> None:
    state = initial_state(stage="interest_check")
    parsed = ReplyResult(
        reply_mode="delay_then_answer",
        outgoing_messages=[ReplyOutgoingMessage(type="text", text="Сейчас поясню.", delay_seconds=100)],
    )

    sanitized = sanitize_reply_result(state, parsed)
    outgoing = [message.model_dump() for message in sanitized.outgoing_messages]
    actions = pending_actions_from_outgoing(state | {"send_reply": True}, outgoing)

    assert sanitized.outgoing_messages[0].delay_seconds == 45
    assert actions[0]["delay_seconds"] == 45


def test_sanitize_reply_result_limits_text_messages_to_three_and_strips_final_periods() -> None:
    state = initial_state(stage="interest_check")
    parsed = ReplyResult(
        outgoing_messages=[
            ReplyOutgoingMessage(type="text", text="Первое."),
            ReplyOutgoingMessage(type="text", text="Второе."),
            ReplyOutgoingMessage(type="text", text="Третье."),
            ReplyOutgoingMessage(type="text", text="Четвертое."),
            ReplyOutgoingMessage(type="text", text="Пятое."),
        ],
    )

    sanitized = sanitize_reply_result(state, parsed)
    outgoing = [message.model_dump() for message in sanitized.outgoing_messages]
    actions = pending_actions_from_outgoing(state | {"send_reply": True}, outgoing)

    assert [message.text for message in sanitized.outgoing_messages] == ["Первое", "Второе", "Третье"]
    assert [action["text"] for action in actions[:3]] == ["Первое", "Второе", "Третье"]


def test_multi_message_outbound_actions_share_reply_group_id() -> None:
    state = initial_state(stage="post_equipment_questions_check")
    state["incoming_message"] = "оборудование, оплата и договор?"
    outgoing = [
        {"type": "text", "text": "Для старта достаточно телефона."},
        {"type": "text", "text": "Оплату объясняют до старта."},
        {"type": "text", "text": "ГПХ можно подписать по желанию."},
    ]

    actions = pending_actions_from_outgoing(state | {"send_reply": True}, outgoing)
    group_ids = {action.get("reply_group_id") for action in actions[:3]}

    assert len(group_ids) == 1
    assert None not in group_ids
    assert [action["reply_group_index"] for action in actions[:3]] == [1, 2, 3]
    assert all(action["reply_group_size"] == 3 for action in actions[:3])
    assert [action["idempotency_key"].rsplit(":", 2)[-2] for action in actions[:3]] == ["0", "1", "2"]


def test_voice_pack_expands_to_recorded_voice_jobs_before_text() -> None:
    state = initial_state(stage="salary_schedule_delivery")
    state["voice_packs"] = {
        "salary_schedule": [
            {
                "id": "salary_1",
                "media_path": "data/voice_intro/voice_intro_03_platform_process.ogg",
                "caption": "[voice] salary_1",
                "recording_delay_seconds": 40,
                "duration_seconds": 0,
            },
            {
                "id": "salary_2",
                "media_path": "data/voice_intro/voice_intro_04_next_steps.ogg",
                "caption": "[voice] salary_2",
                "recording_delay_seconds": 40,
                "duration_seconds": 0,
            },
        ]
    }
    outgoing = [
        {"type": "voice_pack", "voice_pack_id": "salary_schedule"},
        {"type": "text", "text": "РћСЃС‚Р°Р»РёСЃСЊ РІРѕРїСЂРѕСЃС‹?"},
    ]

    actions = pending_actions_from_outgoing(state | {"send_reply": True}, outgoing)

    assert [action["type"] for action in actions[:3]] == ["send_voice", "send_voice", "send_text"]
    assert actions[0]["media_path"].endswith("voice_intro_03_platform_process.ogg")
    assert actions[1]["media_path"].endswith("voice_intro_04_next_steps.ogg")
    assert actions[0]["recording_delay_seconds"] == 40
    assert actions[1]["recording_delay_seconds"] == 40
    assert [action["delay_seconds"] for action in actions[:3]] == [0, 40, 80]
    assert [action["reply_group_index"] for action in actions[:3]] == [1, 2, 3]
    assert all(action["reply_group_size"] == 3 for action in actions[:3])


def test_multi_message_outbound_group_is_cancelled_together_on_new_inbound() -> None:
    group_id = "thread:stage:reply:abc"
    jobs = [
        SimpleNamespace(status="queued", next_attempt_at="set", lease_owner="worker", lease_expires_at="set", error_message=None, message_id=None, media_metadata={"reply_group_id": group_id}),
        SimpleNamespace(status="retry", next_attempt_at="set", lease_owner="worker", lease_expires_at="set", error_message=None, message_id=None, media_metadata={"reply_group_id": group_id}),
    ]
    session = FakeCancelSession(jobs)

    cancelled = asyncio.run(
        FunnelTurnBufferService(session, debounce_seconds=0).cancel_pending_outbound(
            "dialog-id",
            reason="new inbound before langgraph reply",
            message_id="inbound-1",
        )
    )

    assert cancelled == 2
    assert {job.status for job in jobs} == {"cancelled"}
    assert all(job.next_attempt_at is None for job in jobs)
    assert all("inbound-1" in job.error_message for job in jobs)


def test_turn_buffer_ignores_inbound_sent_before_reset_baseline() -> None:
    reset_at = datetime.now(UTC)
    runtime = LeadFunnelRuntime(
        id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        dialog_id=uuid.uuid4(),
        thread_id="thread",
        metadata_json={"reset_at": reset_at.isoformat()},
    )
    old_message = SimpleNamespace(sent_at=reset_at - timedelta(seconds=1))
    new_message = SimpleNamespace(sent_at=reset_at + timedelta(seconds=1))

    assert is_before_reset_baseline(runtime, old_message)
    assert not is_before_reset_baseline(runtime, new_message)


def test_unknown_interrupt_never_defers_to_manager() -> None:
    state = run_graph(initial_state(stage="interest_check"), "А где у вас офис на Марсе?")

    assert state["stage"] == "interest_check"
    assert "менеджер" not in (state["reply_text"] or "").lower()
    assert "собеседован" in (state["reply_text"] or "").lower()


def test_short_unclear_question_asks_to_clarify() -> None:
    state = run_graph(initial_state(stage="interest_check"), "что?")

    assert state["stage"] == "interest_check"
    assert "уточни" in (state["reply_text"] or "").lower()
    assert "собеседован" not in (state["reply_text"] or "").lower()


def test_equipment_phone_stage_answers_faq_and_returns_to_phone_model() -> None:
    state = run_graph(initial_state(stage="equipment_phone_check"), "А договор?")

    assert state["stage"] == "equipment_phone_check"
    assert state["candidate_profile"]["phone_model"] is None
    assert "ГПХ" in state["reply_text"]
    assert "Какая у тебя модель телефона?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_reply_guard_blocks_next_stage_question_during_interrupt() -> None:
    state = initial_state(stage="post_equipment_questions_check")
    state["faq_context"] = [
        {
            "topic": "phone_requirements",
            "answer": "Для старта достаточно современного смартфона с хорошей камерой и стабильным интернетом.",
        }
    ]
    state["semantic_result"] = {
        "message_type": "interrupt_question",
        "current_goal_satisfied": False,
        "has_unresolved_interrupt": True,
        "interrupt_type": "question",
        "interrupt_topic": "phone_requirements",
        "retrieval_topics": ["phone_requirements"],
        "facts": {},
    }
    parsed = ReplyResult(
        outgoing_messages=[
            ReplyOutgoingMessage(
                type="text",
                text=(
                    "Да, можно работать и с телефона — подойдёт современный смартфон. "
                    "Чтобы точнее понять, подойдёт ли твой — скажи, какой у тебя телефон?"
                ),
            )
        ],
    )

    guarded = guard_reply_with_policy(state, parsed)

    assert guarded is not None
    text = guarded.outgoing_messages[0].text or ""
    assert "Для старта достаточно" in text
    assert "какой у тебя телефон" not in text.lower()


def test_profile_later_keeps_stage() -> None:
    state = run_graph(initial_state(stage="profile_theme_check"), "Сделаю через час, сейчас занята")

    assert state["stage"] == "profile_theme_check"
    assert state["candidate_profile"]["profile_info"] is None
    assert text_messages(state) == ["Хорошо, буду ждать"]


def test_profile_no_experience_objection_does_not_close_profile_stage() -> None:
    state = run_graph(
        initial_state(stage="profile_theme_check"),
        "Но я на таких платформах не работала, мой уровень это снять видео в тик ток и все",
    )

    assert state["stage"] == "profile_theme_check"
    assert state["candidate_profile"]["profile_info"] is None
    assert "Опыт не обязателен" in state["reply_text"]
    assert "Расскажи немного о себе" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True
    assert state["metadata"]["interrupt_followup_question"] == "Расскажи немного о себе: учишься/работаешь? Чем любишь заниматься в свободное время?"


def test_interrupt_timeout_returns_to_active_question_after_wait() -> None:
    interrupted = run_graph(initial_state(stage="post_equipment_questions_check"), "Оплата")

    timeout_state = run_graph(
        {
            **next_state(interrupted),
            "timeout_event": "interrupt_followup",
        }
    )

    assert timeout_state["stage"] == "post_equipment_questions_check"
    assert text_messages(timeout_state) == ["Что-то ещё осталось непонятным?"]
    assert not timeout_state["metadata"].get("awaiting_interrupt_followup")
    assert not delayed_followup_actions(timeout_state)


def test_interrupt_timeout_ignores_stale_recent_inbound_text() -> None:
    interrupted = run_graph(initial_state(stage="post_equipment_questions_check"), "Оплата")
    payload = {
        **next_state(interrupted),
        "recent_messages": [
            {"direction": "inbound", "sender_type": "lead", "body": "Оплата"},
            {"direction": "outbound", "sender_type": "agent", "body": interrupted["reply_text"]},
        ],
        "timeout_event": "interrupt_followup",
    }

    timeout_state = run_graph(payload)

    assert timeout_state["semantic_result"]["message_type"] == "empty"
    assert text_messages(timeout_state) == ["Что-то ещё осталось непонятным?"]


def test_new_message_during_interrupt_wait_is_processed_without_timeout_repeat() -> None:
    interrupted = run_graph(initial_state(stage="post_equipment_questions_check"), "Оплата")

    state = run_graph(next_state(interrupted), "А договор есть?")

    assert state["stage"] == "post_equipment_questions_check"
    assert "ГПХ" in state["reply_text"]
    assert "Остались ли у тебя какие-нибудь ещё вопросы?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_profile_extracts_not_working_without_marking_as_working() -> None:
    state = run_graph(initial_state(stage="profile_theme_check"), "Да учусь, не работаю, люблю тик ток")

    assert state["stage"] == "room_available_check"
    assert state["candidate_profile"]["profile_info"]
    assert "не работаю" in state["candidate_profile"]["work_or_study"]
    assert len(text_messages(state)) == 1
    assert "Есть ли у тебя комната" in text_messages(state)[0]


def test_contextual_negative_profile_answer_advances_to_next_question() -> None:
    state = run_graph(initial_state(stage="profile_theme_check"), "я же говорил что нет")

    assert state["stage"] == "room_available_check"
    assert state["candidate_profile"]["profile_info"] == "я же говорил что нет"
    assert state["candidate_profile"]["work_or_study"] == "не учится и не работает"
    assert "учишься/работаешь" not in state["reply_text"]
    assert "Есть ли у тебя комната" in state["reply_text"]


def test_no_current_activity_profile_answer_does_not_require_hobbies() -> None:
    state = run_graph(initial_state(stage="profile_theme_check"), "в целом ничего не делаю")

    assert state["stage"] == "room_available_check"
    assert state["candidate_profile"]["profile_info"] == "в целом ничего не делаю"
    assert state["candidate_profile"]["work_or_study"] == "сейчас ничем не занимается"
    assert state["candidate_profile"]["hobbies"] is None
    assert "учишься/работаешь" not in state["reply_text"]
    assert "Есть ли у тебя комната" in state["reply_text"]


def test_free_time_activity_profile_answers_advance_without_explicit_work_or_hobbies() -> None:
    for message in ("я тиктоки смотрю", "да я балду гоняю", "типо я в компе сижу"):
        state = run_graph(initial_state(stage="profile_theme_check"), message)

        assert state["stage"] == "room_available_check"
        assert state["candidate_profile"]["profile_info"] == message
        assert state["candidate_profile"]["hobbies"] is None
        assert "учишься/работаешь" not in state["reply_text"]
        assert "Есть ли у тебя комната" in state["reply_text"]


def test_contact_collection_accepts_partial_then_missing_field() -> None:
    state = run_graph(initial_state(stage="contact_collection"), "79999999999")

    assert state["stage"] == "contact_collection"
    assert state["candidate_profile"]["phone_number"] == "79999999999"
    assert state["candidate_profile"]["candidate_name"] is None
    assert text_messages(state) == ["Спасибо, номер получила. Напиши, пожалуйста, имя"]


def test_contact_collection_combines_multiple_inbound_messages() -> None:
    state = initial_state(stage="contact_collection")
    state["message_batch"] = [
        {"direction": "inbound", "sender_type": "lead", "body": "Диана"},
        {"direction": "inbound", "sender_type": "lead", "body": "79999999999"},
    ]

    result_state = run_graph(state)

    assert result_state["stage"] == "interview_day_check"
    assert result_state["incoming_message"] == "Диана\n79999999999"
    assert result_state["candidate_profile"]["candidate_name"] == "Диана"
    assert result_state["candidate_profile"]["phone_number"] == "79999999999"
    assert text_messages(result_state) == ["Завтра будет удобно провести собеседование?"]


def test_reply_guard_suppresses_stale_partial_contact_reply_when_complete() -> None:
    state = initial_state(stage="contact_collection", profile={"phone_number": "79999999999"})
    state["semantic_result"] = {
        "message_type": "partial_answer",
        "current_goal_satisfied": False,
        "has_unresolved_interrupt": False,
        "facts": {"candidate_name": "Diana"},
    }
    parsed = ReplyResult(
        outgoing_messages=[
            ReplyOutgoingMessage(type="text", text="stale partial contact reply"),
        ],
    )

    guarded = guard_reply_with_policy(state, parsed)

    assert guarded is not None
    assert guarded.outgoing_messages == []


def test_reply_guard_replaces_booking_transition_with_missing_question() -> None:
    state = initial_state(stage="profile_theme_check")
    state["current_question"] = "Расскажи немного о себе: учишься/работаешь? Чем любишь заниматься в свободное время?"
    state["pending_question_text"] = state["current_question"]
    state["semantic_result"] = {
        "message_type": "partial_answer",
        "current_goal_satisfied": False,
        "has_unresolved_interrupt": False,
        "facts": {"interview_interest": True},
    }
    parsed = ReplyResult(
        outgoing_messages=[
            ReplyOutgoingMessage(type="text", text="Супер, сейчас уточню пару моментов и перейдем к записи."),
        ],
    )

    guarded = guard_reply_with_policy(state, parsed)

    assert guarded is not None
    text = guarded.outgoing_messages[0].text or ""
    assert "Расскажи немного о себе" in text


def test_controller_advances_when_required_fields_are_complete_even_if_llm_goal_flag_false() -> None:
    state = initial_state(stage="contact_collection", profile={"phone_number": "79999999999"})
    state.update(
        {
            "semantic_result": {
                "message_type": "stage_answer",
                "current_goal_satisfied": False,
                "has_unresolved_interrupt": False,
                "facts": {"candidate_name": "Diana"},
            },
            "reply_result": {
                "send_reply": True,
                "outgoing_messages": [{"type": "text", "text": "stale ask phone"}],
            },
        }
    )

    result_state = asyncio.run(state_controller(state))

    assert result_state["stage"] == "interview_day_check"
    assert text_messages(result_state) == ["Завтра будет удобно провести собеседование?"]


def test_controller_skips_age_question_when_age_collected_with_interest() -> None:
    state = initial_state(stage="interest_check")
    state.update(
        {
            "semantic_result": {
                "message_type": "stage_answer",
                "current_goal_satisfied": True,
                "has_unresolved_interrupt": False,
                "facts": {"interest_confirmed": True, "interest_status": "interested", "age": 19, "age_confirmed": True},
            },
            "reply_result": {
                "send_reply": True,
                "outgoing_messages": [],
            },
        }
    )

    result_state = asyncio.run(state_controller(state))

    assert result_state["stage"] == "work_intro_delivery"
    assert result_state["candidate_profile"]["age"] == 19
    assert text_messages(result_state) == []


def test_merge_deterministic_facts_accepts_phone_number_fact() -> None:
    llm_result = deterministic_semantic(initial_state(stage="contact_collection") | {"incoming_message": "непонятно"})
    fallback = deterministic_semantic(initial_state(stage="contact_collection") | {"incoming_message": "79999999999"})

    merged = merge_deterministic_facts(llm_result, fallback)

    assert merged.facts.phone_number == "79999999999"


def test_merge_keeps_specific_llm_interrupt_topic_when_fallback_is_unknown() -> None:
    llm_result = SemanticResult(
        message_type="unclear",
        summary="candidate asks whether this is an adult-content platform",
        has_unresolved_interrupt=True,
        interrupt_type="unclear",
        interrupt_topic="nudity_onlyfans",
        retrieval_topics=["nudity_onlyfans"],
        confidence=0.95,
    )
    fallback = SemanticResult(
        message_type="interrupt_question",
        summary="fallback saw a question but did not know the topic",
        has_unresolved_interrupt=True,
        interrupt_type="question",
        interrupt_topic="unknown",
        retrieval_topics=[],
        confidence=0.55,
    )

    merged = merge_deterministic_facts(llm_result, fallback)

    assert merged.message_type == "objection"
    assert merged.interrupt_type == "objection"
    assert merged.interrupt_topic == "nudity_onlyfans"
    assert merged.retrieval_topics == ["nudity_onlyfans"]


def test_full_scripted_funnel_reaches_human_handoff() -> None:
    state = initial_state()
    state = run_graph(state)
    for message in [
        "Ну хорошо, расскажи",
        "18",
        "Хорошо, слушаю",
        "Пока вопросов нет",
        "Учусь, работаю, люблю рисовать",
        "Да, есть отдельная комната",
        "Samsung S25 Ultra",
        "Хорошо",
        "79999999999",
        "Диана",
        "Да",
        "примерно в 17.00",
    ]:
        state = run_graph(next_state(state), message)

    assert state["stage"] == "human_handoff"
    assert state["status"] == "handoff"
    assert state["candidate_profile"]["candidate_name"] == "Диана"
    assert state["candidate_profile"]["interview_time"] == "17:00"
    assert "17:00" in text_messages(state)[0]


def test_handoff_normalizes_tomorrow_to_russian() -> None:
    state = run_graph(
        initial_state(
            stage="interview_time_check",
            profile={"interview_day_confirmed": True, "interview_day": "tomorrow"},
        ),
        "примерно в 17.00",
    )

    assert state["stage"] == "human_handoff"
    assert "завтра" in text_messages(state)[0]
    assert "tomorrow" not in text_messages(state)[0]


def test_interview_time_understands_short_msk_afternoon_hour() -> None:
    state = run_graph(
        initial_state(
            stage="interview_time_check",
            profile={"interview_day_confirmed": True, "interview_day": "28.04"},
        ),
        "тогда в 2 по московскому времени",
    )

    assert state["stage"] == "human_handoff"
    assert state["candidate_profile"]["interview_time"] == "14:00"
    assert "14:00" in text_messages(state)[0]


def test_reply_result_accepts_null_reply_text_from_alibaba() -> None:
    parsed = ReplyResult.model_validate(
        {
            "send_reply": True,
            "reply_text": None,
        }
    )

    assert parsed.send_reply is True
    assert parsed.reply_text is None


def test_do_not_contact_disables_reply() -> None:
    state = run_graph(initial_state(stage="interest_check"), "Не пишите мне больше")

    assert state["stage"] == "do_not_contact"
    assert state["send_reply"] is False
    assert state["outgoing_messages"] == []


def test_gateway_creates_runtime_with_interest_stage() -> None:
    dialog = Dialog(id=uuid.uuid4(), account_id=uuid.uuid4(), crmchat_dialog_id="d1")
    lead = Lead(id=uuid.uuid4(), dialog_id=dialog.id)
    session = FakeSession()
    gateway = LangGraphFunnelGateway(
        session,
        settings=Settings(LANGGRAPH_CHECKPOINT_POSTGRES_ENABLED=False),
        checkpointer=False,
    )

    runtime = asyncio.run(gateway.get_or_create_runtime(lead=lead, dialog=dialog))

    assert runtime.thread_id == str(dialog.id)
    assert runtime.stage == "interest_check"
    assert session.added == [runtime]


def test_action_executor_creates_text_job_with_idempotency_metadata() -> None:
    dialog = Dialog(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        crmchat_dialog_id="d1",
        telegram_username="@iamnekiy",
    )
    lead = Lead(id=uuid.uuid4(), dialog_id=dialog.id)
    runtime = LeadFunnelRuntime(
        id=uuid.uuid4(),
        lead_id=lead.id,
        dialog_id=dialog.id,
        thread_id=str(dialog.id),
    )
    session = FakeSession(existing_job=None)

    asyncio.run(
        FunnelActionExecutor(session).execute(
            dialog=dialog,
            lead=lead,
            runtime=runtime,
            actions=[
                {
                    "type": "send_text",
                    "text": "РџСЂРёРІРµС‚",
                    "idempotency_key": "first-touch",
                    "delay_seconds": 0,
                }
            ],
        )
    )

    jobs = [item for item in session.added if isinstance(item, OutboundJob)]
    assert len(jobs) == 1
    assert jobs[0].text == "Привет"
    assert jobs[0].media_metadata["source"] == "langgraph_funnel"
    assert jobs[0].media_metadata["funnel_idempotency_key"] == "first-touch"


def test_action_executor_creates_voice_job_with_recording_metadata() -> None:
    dialog = Dialog(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        crmchat_dialog_id="d1",
        telegram_username="@iamnekiy",
    )
    lead = Lead(id=uuid.uuid4(), dialog_id=dialog.id)
    runtime = LeadFunnelRuntime(
        id=uuid.uuid4(),
        lead_id=lead.id,
        dialog_id=dialog.id,
        thread_id=str(dialog.id),
    )
    session = FakeSession(existing_job=None)

    asyncio.run(
        FunnelActionExecutor(session).execute(
            dialog=dialog,
            lead=lead,
            runtime=runtime,
            actions=[
                {
                    "type": "send_voice",
                    "media_path": "data/voice_intro/voice_intro_01_offer_overview.ogg",
                    "caption": "[voice] intro",
                    "recording_delay_seconds": 40,
                    "duration_seconds": 0,
                    "idempotency_key": "voice-intro",
                }
            ],
        )
    )

    jobs = [item for item in session.added if isinstance(item, OutboundJob)]
    assert len(jobs) == 1
    assert jobs[0].job_type == "voice"
    assert jobs[0].media_path.replace("\\", "/") == "data/voice_intro/voice_intro_01_offer_overview.ogg"
    assert jobs[0].typing_action == "sendMessageRecordAudioAction"
    assert jobs[0].media_metadata["recording_delay_seconds"] == 40
    assert jobs[0].media_metadata["duration_seconds"] == 0


def test_psycopg_conn_string_converts_asyncpg_scheme() -> None:
    assert psycopg_conn_string("postgresql+asyncpg://u:p@localhost/db") == "postgresql://u:p@localhost/db"


def run_graph(state, message: str | None = None, orchestrator=None):
    if orchestrator is None:
        orchestrator = DeterministicGraphMode()
    elif not hasattr(orchestrator, "use_llm"):
        orchestrator.use_llm = False
        orchestrator.fallback_on_llm_error = True
    graph = build_funnel_graph(
        static_knowledge=StaticFunnelKnowledgeBase(),
        orchestrator=orchestrator,
    ).compile(checkpointer=MemorySaver())
    payload = dict(state)
    if message is not None:
        payload["incoming_message"] = message
        payload["message_batch"] = [{"direction": "inbound", "sender_type": "lead", "body": message}]
    return asyncio.run(
        graph.ainvoke(
            payload,
            config={"configurable": {"thread_id": payload["thread_id"]}},
        )
    )


def initial_state(stage="interest_check", profile=None):
    candidate_profile = {
        "interest_confirmed": None,
        "age_confirmed": None,
        "salary_schedule_interest": None,
        "phone_model": None,
        "equipment_available": None,
        "questions_resolved": None,
        "wants_to_try": None,
        "profile_info": None,
        "work_or_study": None,
        "hobbies": None,
        "interview_interest": None,
        "candidate_name": None,
        "phone_number": None,
        "interview_day_confirmed": None,
        "interview_day": None,
        "interview_time": None,
    }
    candidate_profile.update(profile or {})
    thread_id = str(uuid.uuid4())
    return {
        "candidate_id": "test_1",
        "lead_id": "test_1",
        "dialog_id": "test_1",
        "thread_id": thread_id,
        "stage": stage,
        "status": "active",
        "candidate_profile": candidate_profile,
        "slots": candidate_profile,
        "sent_voice_packs": [],
        "sent_templates": [],
        "recent_messages": [],
        "message_batch": [],
        "metadata": {},
    }


def next_state(previous):
    return {
        "candidate_id": previous["candidate_id"],
        "lead_id": previous["lead_id"],
        "dialog_id": previous["dialog_id"],
        "thread_id": previous["thread_id"],
        "stage": previous["stage"],
        "status": previous["status"],
        "candidate_profile": previous["candidate_profile"],
        "slots": previous["candidate_profile"],
        "sent_voice_packs": previous.get("sent_voice_packs") or [],
        "sent_templates": previous.get("sent_templates") or [],
        "recent_messages": previous.get("recent_messages") or [],
        "message_batch": [],
        "metadata": previous.get("metadata") or {},
    }


def text_messages(state):
    return [message["text"] for message in state.get("outgoing_messages") or [] if message.get("type") == "text"]


def voice_packs(state):
    return [
        message["voice_pack_id"]
        for message in state.get("outgoing_messages") or []
        if message.get("type") == "voice_pack"
    ]


def delayed_followup_actions(state):
    return [
        action
        for action in state.get("pending_actions") or []
        if action.get("type") == "send_text" and action.get("delay_seconds") == 60
    ]


class FakeSession:
    def __init__(self, existing_job=None):
        self.existing_job = existing_job
        self.added = []
        self.flushed = 0

    async def execute(self, query):
        return FakeScalarResult(self.existing_job)

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        self.flushed += 1


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeAllResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class FakeCancelSession:
    def __init__(self, jobs):
        self.jobs = jobs
        self.flushed = 0

    async def execute(self, query):
        return FakeAllResult(self.jobs)

    async def get(self, model, object_id):
        return None

    async def flush(self):
        self.flushed += 1


class DeterministicGraphMode:
    use_llm = False
    fallback_on_llm_error = True
