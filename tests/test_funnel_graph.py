import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.human_handoff import HumanHandoff
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
    normalize_reply_message_text,
    reply_llm_payload,
    sanitize_reply_result,
)
from app.services.funnel_graph.semantic import (
    SemanticResult,
    deterministic_semantic,
    extract_birthday_18_at,
    is_turning_18_soon,
    merge_deterministic_facts,
)
from app.services.funnel_graph.turn_buffer import FunnelTurnBufferService, is_before_reset_baseline
from scripts.annotate_dialogue_examples import parse_dialogues


def test_funnel_starts_with_first_touch_message() -> None:
    state = run_graph(initial_state())

    assert state["stage"] == "interest_check"
    variants = {normalize_reply_message_text(v) for v in StaticFunnelKnowledgeBase().first_touch_variants()}
    first_message = text_messages(state)[0]
    assert first_message in variants
    assert "onlyfans" in first_message
    assert state["metadata"]["last_graph_node"] == "save_state"


def test_inbound_first_question_enters_warmup_not_opener() -> None:
    # Девочка написала ПЕРВОЙ и спрашивает — не вываливаем опенер-предложение,
    # а заходим в тёплый разговор (inbound_warmup).
    state = initial_state(stage="interest_check")
    state["recent_messages"] = []  # мы ещё ни разу не писали
    result = run_graph(state, "привет, а как вы меня нашли?")

    assert result["stage"] == "inbound_warmup"
    opener_variants = {
        normalize_reply_message_text(v) for v in StaticFunnelKnowledgeBase().first_touch_variants()
    }
    assert not (set(text_messages(result)) & opener_variants)


def test_inbound_warmup_pitches_when_chat_lulls() -> None:
    # После нескольких ходов тёплой беседы (кап = 4 хода) роняем питч про стриминг
    # и переходим в interest_check; канонный вопрос стадии при этом подавлен.
    state = initial_state(stage="inbound_warmup")
    state["recent_messages"] = [{"direction": "outbound", "sender_type": "agent", "body": "привет)"}]
    state["metadata"] = {"warmup_turns": 3}
    result = run_graph(state, "ага, поняла")

    assert result["stage"] == "interest_check"
    assert result["candidate_profile"]["warmup_pitched"] is True
    joined = " ".join(text_messages(result))
    assert "стриминге" in joined
    assert "если интересно — расскажу, что за работа и как всё устроено 🙂" not in joined


def test_collapse_redundant_questions_keeps_one_question_no_dupes() -> None:
    from app.services.funnel_graph.graph import collapse_redundant_questions

    # Воспроизводит баг @springvood: мостик + две версии вопроса про возраст подряд.
    outgoing = [
        {"type": "text", "text": "расскажу всё подробно, но давай сначала эту формальность закроем)"},
        {"type": "text", "text": "сколько тебе лет?"},
        {"type": "text", "text": "давай для начала уточним небольшую формальность, сколько тебе лет?"},
    ]
    result = collapse_redundant_questions(outgoing)
    texts = [m["text"] for m in result]
    # Мостик (не вопрос) остаётся, вопрос ровно один — первый.
    assert texts == [
        "расскажу всё подробно, но давай сначала эту формальность закроем)",
        "сколько тебе лет?",
    ]
    # Точные повторы и второй вопрос вычищены.
    assert sum(1 for t in texts if t.rstrip().endswith("?")) == 1


def test_collapse_redundant_questions_preserves_voice_and_dedupes_text() -> None:
    from app.services.funnel_graph.graph import collapse_redundant_questions

    outgoing = [
        {"type": "voice_pack", "voice_pack_id": "vp1"},
        {"type": "text", "text": "да, общаешься с людьми и за это деньги 💖"},
        {"type": "text", "text": "да, общаешься с людьми и за это деньги 💖"},  # точный повтор
        {"type": "text", "text": "если интересна наша сфера, рассказать про зп?"},
    ]
    result = collapse_redundant_questions(outgoing)
    assert result[0]["type"] == "voice_pack"
    texts = [m["text"] for m in result if m.get("type") == "text"]
    assert texts == [
        "да, общаешься с людьми и за это деньги 💖",
        "если интересна наша сфера, рассказать про зп?",
    ]


def test_dedupe_cross_turn_drops_repeated_stage_question_after_answer() -> None:
    from app.services.funnel_graph.graph import dedupe_cross_turn_questions

    # Прошлый ход уже спрашивали этот вопрос (канон). В этот ход ответили по делу и
    # снова тянем его ВАРИАНТ — должен выпасть (баг @hunt_pavluck: «что-то ещё?» ×10).
    meta = {"last_asked_question_canonical": "остались ли у тебя какие-нибудь ещё вопросики?"}
    outgoing = [
        {"type": "text", "text": "работа удалённая, можно стримить из дома)"},
        {"type": "text", "text": "что-то ещё осталось непонятным?"},
    ]
    result = dedupe_cross_turn_questions(outgoing, meta)
    assert [m["text"] for m in result] == ["работа удалённая, можно стримить из дома)"]


def test_dedupe_cross_turn_keeps_question_when_nothing_else_to_say() -> None:
    from app.services.funnel_graph.graph import dedupe_cross_turn_questions

    # Содержательного ответа нет — значит модель не ответила, переспросить МОЖНО.
    meta = {"last_asked_question_canonical": "остались ли у тебя какие-нибудь ещё вопросики?"}
    outgoing = [{"type": "text", "text": "что-то ещё осталось непонятным?"}]
    result = dedupe_cross_turn_questions(outgoing, meta)
    assert [m["text"] for m in result] == ["что-то ещё осталось непонятным?"]


def test_dedupe_cross_turn_keeps_new_stage_question() -> None:
    from app.services.funnel_graph.graph import dedupe_cross_turn_questions

    # Новый вопрос (другая стадия) — задаём, даже если есть содержательный текст.
    meta = {"last_asked_question_canonical": "остались ли у тебя какие-нибудь ещё вопросики?"}
    outgoing = [
        {"type": "text", "text": "супер"},
        {"type": "text", "text": "какая у тебя моделька телефончика?"},
    ]
    result = dedupe_cross_turn_questions(outgoing, meta)
    assert [m["text"] for m in result] == ["супер", "какая у тебя моделька телефончика?"]
    assert meta["last_asked_question_canonical"] == "какая у тебя моделька телефончика?"


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
    assert "если интересно — расскажу, что за работа и как всё устроено 🙂" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True
    assert state["metadata"]["interrupt_followup_question"] == "если интересно — расскажу, что за работа и как всё устроено 🙂"
    delayed = delayed_followup_actions(state)
    assert len(delayed) == 1
    assert delayed[0]["delay_seconds"] == 120
    assert delayed[0]["text"] == "если интересно — расскажу, что за работа и как всё устроено 🙂"


def test_interest_job_question_does_not_move_to_age() -> None:
    state = run_graph(initial_state(stage="interest_check"), "Что за предложение?")

    assert state["stage"] == "interest_check"
    assert state["candidate_profile"]["interest_confirmed"] is None
    assert "Сколько тебе лет?" not in state["reply_text"]
    assert "если интересно — расскажу, что за работа и как всё устроено 🙂" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_interest_agreement_moves_to_age_check() -> None:
    state = run_graph(initial_state(stage="interest_check"), "Ну хорошо, расскажи")

    assert state["stage"] == "age_check"
    assert state["candidate_profile"]["interest_confirmed"] is True
    assert text_messages(state) == ["давай для начала уточним небольшую формальность, сколько тебе лет?"]


def test_phone_model_number_at_age_check_is_not_read_as_underage() -> None:
    state = run_graph(
        initial_state(stage="age_check", profile={"interest_confirmed": True}),
        "щас 14 про макс, но скоро поменяю на последний прошку",
    )

    assert state["stage"] == "age_check"
    assert state["status"] != "closed"
    assert state["candidate_profile"]["age"] is None
    assert state["candidate_profile"]["qualification_status"] != "underage"


def test_turning_18_soon_detection_and_birthday_parsing() -> None:
    from datetime import UTC, datetime

    assert is_turning_18_soon("через несколько дней 18") is True
    assert is_turning_18_soon("скоро будет 18") is True
    assert is_turning_18_soon("мне 25") is False

    now = datetime(2026, 6, 7, tzinfo=UTC)
    assert extract_birthday_18_at("через 3 дня", now).date().isoformat() == "2026-06-10"
    by_month = extract_birthday_18_at("15 июня", now)
    assert (by_month.month, by_month.day) == (6, 15)
    # дата уже прошла в этом году -> переносится на следующий
    assert extract_birthday_18_at("1 января", now).year == 2027


def test_age_17_routes_to_pending_18_and_asks_birthday() -> None:
    state = run_graph(
        initial_state(stage="age_check", profile={"interest_confirmed": True}),
        "мне 17, но скоро будет 18",
    )

    assert state["stage"] == "age_pending_18"
    assert state["candidate_profile"]["age_confirmed"] is False
    assert "день рождения" in (state["reply_text"] or "")


def test_age_pending_18_captures_birthday_and_schedules_followups() -> None:
    state = run_graph(
        initial_state(stage="age_pending_18", profile={"interest_confirmed": True}),
        "15 июня",
    )

    assert state["stage"] == "scheduled_until_18"
    assert state["candidate_profile"]["birthday_18_at"]
    reply = (state["reply_text"] or "").lower()
    assert "др" in reply or "поздравл" in reply
    schedule = [a for a in (state.get("pending_actions") or []) if a.get("type") == "schedule_birthday_followup"]
    assert len(schedule) == 1
    assert schedule[0]["birthday_at"]


def test_age_under_17_still_goes_to_lost() -> None:
    state = run_graph(
        initial_state(stage="age_check", profile={"interest_confirmed": True}),
        "мне 15",
    )

    assert state["stage"] == "lost"


def test_age_answer_sends_work_intro_pack_and_salary_offer() -> None:
    state = run_graph(initial_state(stage="age_check", profile={"interest_confirmed": True}), "18")

    assert state["stage"] == "salary_schedule_offer"
    assert state["candidate_profile"]["age_confirmed"] is True
    assert state["sent_voice_packs"] == ["work_intro"]
    assert voice_packs(state) == ["work_intro"]
    assert text_messages(state) == ["если интересна наша сфера, давай расскажу про зп и график 🐬"]


def test_salary_offer_question_is_interrupt() -> None:
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Это OnlyFans и оголёнка?",
    )

    assert state["stage"] == "salary_schedule_offer"
    assert state["candidate_profile"]["salary_schedule_interest"] is None
    assert "не OnlyFans" in state["reply_text"]
    assert "если интересна наша сфера, давай расскажу про зп и график 🐬" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_not_interested_has_job_is_rebutted_once_not_lost() -> None:
    # «не интересует, у меня есть работа» раньше падало в unclear/мгновенный отказ.
    # Теперь это возражение already_employed: отрабатываем углом совмещения, диалог
    # остаётся на месте, а не уходит в lost.
    result = deterministic_semantic(
        initial_state(stage="interest_check") | {"incoming_message": "не интересует, у меня есть работа"}
    )

    assert result.message_type == "objection"
    assert result.interrupt_topic == "already_employed"
    assert result.has_unresolved_interrupt is True


def test_not_interested_short_is_rebutted_once() -> None:
    result = deterministic_semantic(
        initial_state(stage="interest_check") | {"incoming_message": "не интересует"}
    )

    assert result.message_type == "objection"
    assert result.interrupt_topic == "soft_decline_income"


def test_repeated_decline_after_rebuttal_goes_to_refusal() -> None:
    result = deterministic_semantic(
        initial_state(stage="interest_check")
        | {"incoming_message": "всё равно не интересует", "metadata": {"soft_decline_rebutted": True}}
    )

    assert result.message_type == "hard_refusal"


def test_has_job_objection_runs_funnel_and_stays_then_lost_on_repeat() -> None:
    first = run_graph(initial_state(stage="interest_check"), "спасибо, у меня уже есть работа")
    assert first["stage"] == "interest_check"
    assert first["metadata"].get("soft_decline_rebutted") is True
    assert first["semantic_result"]["interrupt_topic"] == "already_employed"

    second = run_graph(
        initial_state(stage="interest_check", profile=None)
        | {"metadata": {"soft_decline_rebutted": True}},
        "нет, не интересно",
    )
    assert second["stage"] == "lost"


def test_meet_in_person_request_is_answered_not_deflected() -> None:
    state = run_graph(
        initial_state(stage="interest_check"),
        "А ты не хочешь встретиться в реальности и поговорить об этом?",
    )

    assert state["stage"] == "interest_check"
    assert state["semantic_result"]["interrupt_topic"] == "meet_in_person"
    reply = (state["reply_text"] or "").lower()
    assert "вживую" in reply or "онлайн" in reply
    assert "точных данных" not in reply


def test_interested_but_english_concern_is_not_refusal() -> None:
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Мне интересно, но с английским беда",
    )

    assert state["stage"] == "salary_schedule_offer"
    assert state["semantic_result"]["message_type"] == "mixed"
    assert state["semantic_result"]["interrupt_topic"] == "english_level"
    assert "английский не обязателен" in state["reply_text"]


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
    assert text_messages(state) == ["остались ли у тебя какие-нибудь ещё вопросики?"]


def _salary_offer_awaiting_state():
    state = initial_state(
        stage="salary_schedule_offer",
        profile={"interest_confirmed": True, "age_confirmed": True},
    )
    state["metadata"] = {
        "awaiting_interrupt_followup": True,
        "interrupt_followup_stage": "salary_schedule_offer",
        "interrupt_followup_question": "если интересна наша сфера, давай расскажу про зп и график 🐬",
    }
    return state


def test_salary_offer_question_is_answered_and_waits() -> None:
    # First interrupt turn: answer the question, stay on the offer, arm the follow-up.
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "а сколько по деньгам платят?",
    )

    assert state["stage"] == "salary_schedule_offer"
    assert state["candidate_profile"]["salary_schedule_interest"] is None
    assert voice_packs(state) == []
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_salary_offer_no_more_questions_sends_pack() -> None:
    # After we answered, a neutral "понятно" means no more questions -> deliver voices.
    state = run_graph(_salary_offer_awaiting_state(), "понятно")

    assert state["stage"] == "post_equipment_questions_check"
    assert state["candidate_profile"]["salary_schedule_interest"] is True
    assert voice_packs(state) == ["salary_schedule"]
    assert text_messages(state)[-1] == "остались ли у тебя какие-нибудь ещё вопросики?"
    assert not state["metadata"].get("awaiting_interrupt_followup")


def test_salary_offer_followup_timeout_sends_pack() -> None:
    # No reply within the window: the follow-up timeout delivers the voices.
    state = _salary_offer_awaiting_state()
    state["timeout_event"] = "interrupt_followup"
    state = run_graph(state)

    assert state["stage"] == "post_equipment_questions_check"
    assert state["candidate_profile"]["salary_schedule_interest"] is True
    assert voice_packs(state) == ["salary_schedule"]


def test_salary_offer_new_question_after_answer_keeps_waiting() -> None:
    # A fresh question while awaiting must be answered, not skipped to the voices.
    state = run_graph(_salary_offer_awaiting_state(), "а что за платформа?")

    assert state["stage"] == "salary_schedule_offer"
    assert voice_packs(state) == []


def test_action_stage_sends_voice_even_when_llm_reply_send_is_false() -> None:
    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Хорошо, слушаю",
    )

    assert state["stage"] == "post_equipment_questions_check"
    assert voice_packs(state) == ["salary_schedule"]
    assert text_messages(state) == ["остались ли у тебя какие-нибудь ещё вопросики?"]


def test_equipment_does_not_close_phone_model_requirement() -> None:
    state = run_graph(initial_state(stage="equipment_phone_check"), "У меня есть стриминговое оборудование")

    assert state["stage"] == "equipment_phone_check"
    assert state["candidate_profile"]["equipment_available"] is True
    assert state["candidate_profile"]["phone_model"] is None
    assert text_messages(state) == ["о, круто! а чтобы мы точно всё настроили, какая у тебя моделька телефончика?"]


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
        "о, круто! а чтобы мы точно всё настроили, какая у тебя моделька телефончика?"
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
    assert text_messages(state) == ["нам подходит", "тогда можем записаться на собеседование?"]


def test_interview_offer_accepts_go_zapishimsya_and_asks_contact() -> None:
    state = run_graph(initial_state(stage="interview_offer"), "норм, ну го запишимся на собеседование")

    assert state["stage"] == "contact_collection"
    assert state["candidate_profile"]["interview_interest"] is True
    assert text_messages(state) == ["для записи мне нужно твоё имя и номер телефончика"]


def test_no_questions_moves_to_profile_context() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "Пока вопросов нет")

    assert state["stage"] == "profile_theme_check"
    assert state["candidate_profile"]["questions_resolved"] is True
    assert text_messages(state) == [
        "давай я уточню у тебя несколько деталей, и далее мы с тобой запишемся на собеседование",
        "расскажи немного о себе, учишься/работаешь? чем любишь заниматься в свободное время? помогу подобрать тематику для стримов 🐬",
    ]


def test_post_equipment_short_faq_topic_answers_before_repeating_question() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "Оплата")

    assert state["stage"] == "post_equipment_questions_check"
    assert state["candidate_profile"]["questions_resolved"] is None
    assert "стажировочных днях" in state["reply_text"]
    assert "остались ли у тебя какие-нибудь ещё вопросики?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True
    assert state["metadata"]["interrupt_followup_question"] == "остались ли у тебя какие-нибудь ещё вопросики?"


def test_booking_intent_after_materials_moves_to_next_required_question() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "норм, ну го запишимся на собеседование")

    assert state["stage"] == "profile_theme_check"
    assert state["candidate_profile"]["questions_resolved"] is True
    assert state["candidate_profile"]["interview_interest"] is True
    assert text_messages(state) == [
        "давай я уточню у тебя несколько деталей, и далее мы с тобой запишемся на собеседование",
        "расскажи немного о себе, учишься/работаешь? чем любишь заниматься в свободное время? помогу подобрать тематику для стримов 🐬",
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
    assert "у тебя есть комната" in state["reply_text"]


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
    assert "английский не обязателен" in state["reply_text"]
    assert "переводчиком" in state["reply_text"]
    assert "остались ли у тебя какие-нибудь ещё вопросики?" not in state["reply_text"]
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
    assert "английский не обязателен" in state["reply_text"]
    assert "чтобы не грузить всем сразу" in state["reply_text"]
    assert "если интересно — расскажу, что за работа и как всё устроено 🙂" not in state["reply_text"]


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
    assert "если интересно — расскажу, что за работа и как всё устроено 🙂" not in state["reply_text"]


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
    # The broad job_description blob is demoted once a specific concern (nudity) is
    # present, so the reply stays focused on her actual question instead of dumping
    # the whole job description on top.
    assert "job_description" not in state["semantic_result"]["retrieval_topics"]
    assert "не OnlyFans" in state["reply_text"]


def test_specific_question_demotes_broad_job_description() -> None:
    """Asking a pointed question whose wording merely mentions streaming must answer
    the specific topic, not blend in the whole job_description blob (regression:
    «на каких платформах стримы будут проходить» → only platform_info)."""
    state = initial_state(
        stage="salary_schedule_offer",
        profile={"interest_confirmed": True, "age_confirmed": True},
    )
    state["message_batch"] = [
        {"direction": "inbound", "sender_type": "lead", "body": "а на каких платформах стримы будут проходить?"},
    ]

    state = run_graph(state)

    topics = state["semantic_result"]["retrieval_topics"]
    assert "platform_info" in topics
    assert "job_description" not in topics
    assert "Dacast" in state["reply_text"] or "Restream" in state["reply_text"]
    # the job-description blob must NOT be appended
    assert "разговорные эфиры" not in state["reply_text"]


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
    assert "остались ли у тебя какие-нибудь ещё вопросики?" not in state["reply_text"]


def test_friend_and_platform_batch_is_question_not_objection() -> None:
    state = initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True})
    state["message_batch"] = [
        {"direction": "inbound", "sender_type": "lead", "body": "а можно с подругой?"},
        {"direction": "inbound", "sender_type": "lead", "body": "и что за платформа?"},
    ]

    state = run_graph(state)

    # First interrupt turn: answer the questions and wait; do not deliver yet.
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
        "поняла) тогда уточню: остались ли у тебя ещё вопросики по условиям, оплате или формату?"
    ]


def test_social_only_greeting_does_not_create_interrupt_or_repeat_greeting() -> None:
    state = run_graph(initial_state(stage="interest_check"), "Привет")

    assert state["stage"] == "interest_check"
    assert state["semantic_result"]["has_unresolved_interrupt"] is False
    assert text_messages(state) == ["если интересно — расскажу, что за работа и как всё устроено 🙂"]


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
    # Весь пак планируется на один момент (один батч/цикл отправки) — реалистичную
    # паузу записи между голосовыми даёт сам outbound-воркер (recording_delay при
    # отправке), а не разнос по scheduled_at. Хвостовой текст — туда же.
    assert [action["delay_seconds"] for action in actions[:3]] == [0, 0, 0]
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
    assert "какая у тебя моделька телефончика?" not in state["reply_text"]
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
    assert text_messages(state) == ["хорошо, буду ждать"]


def test_profile_no_experience_objection_does_not_close_profile_stage() -> None:
    state = run_graph(
        initial_state(stage="profile_theme_check"),
        "Но я на таких платформах не работала, мой уровень это снять видео в тик ток и все",
    )

    assert state["stage"] == "profile_theme_check"
    assert state["candidate_profile"]["profile_info"] is None
    assert "Опыт не обязателен" in state["reply_text"]
    assert "расскажи немного о себе" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True
    assert state["metadata"]["interrupt_followup_question"] == "расскажи немного о себе, учишься/работаешь? чем любишь заниматься в свободное время? помогу подобрать тематику для стримов 🐬"


def test_interrupt_timeout_returns_to_active_question_after_wait() -> None:
    interrupted = run_graph(initial_state(stage="post_equipment_questions_check"), "Оплата")

    timeout_state = run_graph(
        {
            **next_state(interrupted),
            "timeout_event": "interrupt_followup",
        }
    )

    assert timeout_state["stage"] == "post_equipment_questions_check"
    assert text_messages(timeout_state) == ["что-то ещё осталось непонятным?"]
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
    assert text_messages(timeout_state) == ["что-то ещё осталось непонятным?"]


def test_new_message_during_interrupt_wait_is_processed_without_timeout_repeat() -> None:
    interrupted = run_graph(initial_state(stage="post_equipment_questions_check"), "Оплата")

    state = run_graph(next_state(interrupted), "А договор есть?")

    assert state["stage"] == "post_equipment_questions_check"
    assert "ГПХ" in state["reply_text"]
    assert "остались ли у тебя какие-нибудь ещё вопросики?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_profile_extracts_not_working_without_marking_as_working() -> None:
    state = run_graph(initial_state(stage="profile_theme_check"), "Да учусь, не работаю, люблю тик ток")

    assert state["stage"] == "room_available_check"
    assert state["candidate_profile"]["profile_info"]
    assert "не работаю" in state["candidate_profile"]["work_or_study"]
    assert len(text_messages(state)) == 2
    assert "у тебя есть комната" in text_messages(state)[1]


def test_contextual_negative_profile_answer_advances_to_next_question() -> None:
    state = run_graph(initial_state(stage="profile_theme_check"), "я же говорил что нет")

    assert state["stage"] == "room_available_check"
    assert state["candidate_profile"]["profile_info"] == "я же говорил что нет"
    assert state["candidate_profile"]["work_or_study"] == "не учится и не работает"
    assert "учишься/работаешь" not in state["reply_text"]
    assert "у тебя есть комната" in state["reply_text"]


def test_no_current_activity_profile_answer_does_not_require_hobbies() -> None:
    state = run_graph(initial_state(stage="profile_theme_check"), "в целом ничего не делаю")

    assert state["stage"] == "room_available_check"
    assert state["candidate_profile"]["profile_info"] == "в целом ничего не делаю"
    assert state["candidate_profile"]["work_or_study"] == "сейчас ничем не занимается"
    assert state["candidate_profile"]["hobbies"] is None
    assert "учишься/работаешь" not in state["reply_text"]
    assert "у тебя есть комната" in state["reply_text"]


def test_free_time_activity_profile_answers_advance_without_explicit_work_or_hobbies() -> None:
    for message in ("я тиктоки смотрю", "да я балду гоняю", "типо я в компе сижу"):
        state = run_graph(initial_state(stage="profile_theme_check"), message)

        assert state["stage"] == "room_available_check"
        assert state["candidate_profile"]["profile_info"] == message
        assert state["candidate_profile"]["hobbies"] is None
        assert "учишься/работаешь" not in state["reply_text"]
        assert "у тебя есть комната" in state["reply_text"]


def test_contact_collection_accepts_partial_then_missing_field() -> None:
    state = run_graph(initial_state(stage="contact_collection"), "79999999999")

    assert state["stage"] == "contact_collection"
    assert state["candidate_profile"]["phone_number"] == "79999999999"
    assert state["candidate_profile"]["candidate_name"] is None
    assert text_messages(state) == ["спасибо, номер получила) напиши, пожалуйста, имя"]


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
    assert text_messages(result_state) == ["завтра будет удобно провести собеседование?"]


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
    state["current_question"] = "расскажи немного о себе, учишься/работаешь? чем любишь заниматься в свободное время? помогу подобрать тематику для стримов 🐬"
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
    assert "расскажи немного о себе" in text


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
    assert text_messages(result_state) == ["завтра будет удобно провести собеседование?"]


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


def test_action_executor_handoff_notifies_once() -> None:
    dialog = Dialog(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        crmchat_dialog_id="d1",
        telegram_username="@lead",
    )
    lead = Lead(id=uuid.uuid4(), dialog_id=dialog.id)
    runtime = LeadFunnelRuntime(
        id=uuid.uuid4(),
        lead_id=lead.id,
        dialog_id=dialog.id,
        thread_id=str(dialog.id),
        metadata_json={"candidate_profile": {"candidate_name": "Аня", "phone_number": "+79990000000"}},
    )

    captured: list[dict] = []

    class _Notifier:
        handoff_enabled = True

        async def notify_handoff(self, **kwargs):
            captured.append(kwargs)

    class _HandoffSession:
        def __init__(self):
            self.added = []
            self.handoff = None

        async def execute(self, query):
            return FakeScalarResult(self.handoff)

        async def get(self, model, object_id):
            return None

        def add(self, instance):
            self.added.append(instance)
            if isinstance(instance, HumanHandoff):
                self.handoff = instance

        async def flush(self):
            pass

    session = _HandoffSession()
    executor = FunnelActionExecutor(session, notifier=_Notifier())
    action = {"type": "handoff", "reason": "funnel_requested_handoff"}

    asyncio.run(executor.execute(dialog=dialog, lead=lead, runtime=runtime, actions=[action]))
    # Повторный прогон (терминальная стадия переобрабатывается) не должен дублировать.
    asyncio.run(executor.execute(dialog=dialog, lead=lead, runtime=runtime, actions=[action]))

    handoffs = [item for item in session.added if isinstance(item, HumanHandoff)]
    assert len(handoffs) == 1
    assert runtime.stage == "human_handoff"
    assert len(captured) == 1
    assert captured[0]["telegram_username"] == "@lead"
    assert captured[0]["profile"]["candidate_name"] == "Аня"


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
        # Кандидат на interest_check и далее уже отвечает на наш first-touch опенер,
        # поэтому в истории есть прошлое агентское сообщение (иначе это «первый
        # контакт» и воронка по дизайну отдаёт опенер ещё раз).
        "recent_messages": [
            {"direction": "outbound", "sender_type": "agent", "body": "first-touch opener"}
        ],
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
        if action.get("type") == "send_text" and action.get("delay_seconds") == 120
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
