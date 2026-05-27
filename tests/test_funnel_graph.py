import asyncio
import uuid

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.lead import Lead
from app.models.outbound_job import OutboundJob
from app.services.funnel_graph.actions import FunnelActionExecutor
from app.services.funnel_graph.checkpoint import psycopg_conn_string
from app.services.funnel_graph.gateway import LangGraphFunnelGateway
from app.services.funnel_graph.graph import build_funnel_graph, state_controller
from app.services.funnel_graph.knowledge import StaticFunnelKnowledgeBase
from app.services.funnel_graph.orchestrator import DialogueOrchestratorResult, result
from app.services.funnel_graph.reply import ReplyOutgoingMessage, ReplyResult, guard_reply_with_policy
from app.services.funnel_graph.semantic import deterministic_semantic, merge_deterministic_facts


def test_funnel_starts_with_first_touch_message() -> None:
    state = run_graph(initial_state())

    assert state["stage"] == "interest_check"
    assert text_messages(state) == [
        "привет! ты просто потрясающая! 🤩 у меня есть интересное предложение о работе стриминге на платформах подобных twitch. это не имеет отношения к вебкам или onlyfans."
    ]
    assert state["metadata"]["last_graph_node"] == "save_state"


def test_interest_question_is_interrupt_and_repeats_current_question() -> None:
    state = run_graph(
        initial_state(stage="interest_check"),
        "А откуда у вас мой контакт? И почему я заинтересовала вас?",
    )

    assert state["stage"] == "interest_check"
    assert state["candidate_profile"]["interest_confirmed"] is None
    assert "Контакт появился" in state["reply_text"]
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
    assert text_messages(state) == ["Сколько тебе лет?"]


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
    assert "не предлагаем OnlyFans" in state["reply_text"]
    assert "Если интересна наша сфера, давай расскажу про зп и график" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


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
    class FakeOrchestrator:
        async def run(self, state):
            return result(
                stage="salary_schedule_offer",
                target_stage="salary_schedule_delivery",
                action="send_voice_pack",
                state_patch={"salary_schedule_interest": True},
                reply_send=False,
                stage_completed=True,
            )

    state = run_graph(
        initial_state(stage="salary_schedule_offer", profile={"interest_confirmed": True, "age_confirmed": True}),
        "Хорошо, слушаю",
        orchestrator=FakeOrchestrator(),
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


def test_phone_model_moves_to_interview_offer() -> None:
    state = run_graph(initial_state(stage="equipment_phone_check"), "Samsung S25 Ultra")

    assert state["stage"] == "interview_offer"
    assert state["candidate_profile"]["phone_model"] == "Samsung S25 Ultra"
    assert text_messages(state) == ["Можем записаться на собеседование?"]


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
    assert "Порядок выплат" in state["reply_text"]
    assert "Остались ли у тебя какие-нибудь ещё вопросы?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True
    assert state["metadata"]["interrupt_followup_question"] == "Остались ли у тебя какие-нибудь ещё вопросы?"


def test_post_equipment_irrelevant_reply_uses_natural_followup() -> None:
    state = run_graph(initial_state(stage="post_equipment_questions_check"), "19")

    assert state["stage"] == "post_equipment_questions_check"
    assert text_messages(state) == [
        "Поняла. Тогда уточню: остались ли у тебя ещё вопросы по условиям, оплате или формату?"
    ]


def test_equipment_phone_stage_answers_faq_and_returns_to_phone_model() -> None:
    state = run_graph(initial_state(stage="equipment_phone_check"), "А договор?")

    assert state["stage"] == "equipment_phone_check"
    assert state["candidate_profile"]["phone_model"] is None
    assert "Формат оформления" in state["reply_text"]
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
    assert text_messages(state) == ["Хорошо, буду ждать."]


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
    assert "Формат оформления" in state["reply_text"]
    assert "Остались ли у тебя какие-нибудь ещё вопросы?" not in state["reply_text"]
    assert state["metadata"]["awaiting_interrupt_followup"] is True


def test_profile_extracts_not_working_without_marking_as_working() -> None:
    state = run_graph(initial_state(stage="profile_theme_check"), "Да учусь, не работаю, люблю тик ток")

    assert state["stage"] == "room_available_check"
    assert state["candidate_profile"]["profile_info"]
    assert "не работаю" in state["candidate_profile"]["work_or_study"]
    assert len(text_messages(state)) == 1
    assert "Есть ли у тебя комната" in text_messages(state)[0]


def test_contact_collection_accepts_partial_then_missing_field() -> None:
    state = run_graph(initial_state(stage="contact_collection"), "79999999999")

    assert state["stage"] == "contact_collection"
    assert state["candidate_profile"]["phone_number"] == "79999999999"
    assert state["candidate_profile"]["candidate_name"] is None
    assert text_messages(state) == ["Спасибо, номер получила. Напиши, пожалуйста, имя."]


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


def test_merge_deterministic_facts_accepts_phone_number_fact() -> None:
    llm_result = deterministic_semantic(initial_state(stage="contact_collection") | {"incoming_message": "непонятно"})
    fallback = deterministic_semantic(initial_state(stage="contact_collection") | {"incoming_message": "79999999999"})

    merged = merge_deterministic_facts(llm_result, fallback)

    assert merged.facts.phone_number == "79999999999"


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


def test_orchestrator_result_accepts_null_reply_text_from_alibaba() -> None:
    parsed = DialogueOrchestratorResult.model_validate(
        {
            "reply": {"send": True, "text": None},
        }
    )

    assert parsed.reply.send is True
    assert parsed.reply.text is None


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
                    "text": "hello",
                    "idempotency_key": "first-touch",
                    "delay_seconds": 0,
                }
            ],
        )
    )

    jobs = [item for item in session.added if isinstance(item, OutboundJob)]
    assert len(jobs) == 1
    assert jobs[0].media_metadata["source"] == "langgraph_funnel"
    assert jobs[0].media_metadata["funnel_idempotency_key"] == "first-touch"


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


class DeterministicGraphMode:
    use_llm = False
    fallback_on_llm_error = True
