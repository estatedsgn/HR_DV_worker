import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import Settings
from app.models.brain_v2 import BrainRun
from app.models.dialog import Dialog
from app.models.inbound_event import InboundEvent
from app.models.outbound_job import OutboundJob
import app.services.inbound_queue_worker as inbound_queue_worker
from app.services.brain_v2.agenda_registry import AgendaRegistry, candidate_facing_default_question, canonical_agenda_stage
from app.services.brain_v2.dialogue_brain import DialogueBrain
from app.services.brain_v2.executor import BrainExecutor
from app.services.brain_v2.gateway import combined_incoming_message
from app.services.brain_v2.knowledge_cards import (
    is_mentor_reference,
    merge_and_rerank,
    to_retrieved_card,
    trigger_score,
)
from app.services.brain_v2.llm_provider import (
    BrainLLMAdapter,
    api_key_for_provider,
    base_url_for_provider,
    component_model,
    component_provider,
    parse_json_content,
    provider_supports_json_schema_response_format,
)
from app.services.brain_v2.router import RouterExtractor
from app.services.brain_v2.schemas import BrainMessage, ExecutorAction
from app.services.brain_v2.state_manager import next_stage_after, normalize_stage
from app.services.brain_v2.turn_buffer import is_candidate_typing_event
from app.services.brain_v2.validator import BrainValidator
from app.models.brain_v2 import KnowledgeCard
from app.services.inbound_queue_worker import DeferredInboundEvent, InboundQueueWorker
from scripts.seed_knowledge_cards_v2 import load_bundle_cards, load_profitcast_cards


def test_agenda_registry_contains_required_items() -> None:
    items = AgendaRegistry.standard_items()
    keys = {item.item_key for item in items}

    assert "trust.answer_source" in keys
    assert "age.confirm_18" in keys
    assert "handoff.prepare_summary" in keys
    assert "trust.answer_contact_source" in keys
    assert "trust.provide_company_links" in keys
    assert "schedule.offer_available_time_window" in keys
    assert "qualification.clarify_confusion" in keys
    assert "support.resolve_zoom_link_issue" in keys
    assert {item.stage for item in items} <= {
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


def test_agenda_default_goal_is_not_candidate_text() -> None:
    question = candidate_facing_default_question(
        {
            "item_key": "trust.answer_contact_source",
            "stage": "trust_source",
            "default_goal": "Закрыть вопрос кандидата про источник контакта",
        }
    )

    assert "Закрыть вопрос" not in question
    assert question.endswith("?")


def test_state_helpers_keep_canonical_stage_order() -> None:
    assert normalize_stage("age_gate") == "age_gate"
    assert normalize_stage("basic_info_pack") == "basic_info_pack"
    assert normalize_stage("unknown") == "qualification"
    assert next_stage_after("qualification_faq") == "company"


def test_agenda_stage_mapping_preserves_canonical_funnel_stages() -> None:
    assert canonical_agenda_stage("basic_info_pack") == "basic_info_pack"
    assert canonical_agenda_stage("qualification_faq") == "qualification_faq"
    assert canonical_agenda_stage("company_info") == "company"
    assert canonical_agenda_stage("post_schedule_support") == "post_schedule_support"


def test_router_extracts_refusal_question_and_slots() -> None:
    router = RouterExtractor()

    refusal = asyncio.run(
        router.extract(
            incoming_message=BrainMessage(body="Не интересно, не пиши"),
            recent_messages=[],
            state_snapshot={"stage": "trust"},
        )
    )
    question = asyncio.run(
        router.extract(
            incoming_message=BrainMessage(body="А нюды нужны? Мне 19"),
            recent_messages=[],
            state_snapshot={"stage": "trust"},
        )
    )

    assert refusal.primary_intent == "not_interested"
    assert question.has_candidate_question is True
    assert question.slot_patch["age"] == 19
    assert "trust.no_nudity" in question.question_topics


def test_validator_revises_missing_send_text() -> None:
    validator = BrainValidator()

    result = asyncio.run(
        validator.validate(
            current_stage="trust",
            open_loop_before=None,
            retrieved_knowledge_cards=[],
            dialogue_decision=__import__(
                "app.services.brain_v2.schemas", fromlist=["DialogueBrainDecision"]
            ).DialogueBrainDecision(executor_action=ExecutorAction(type="send_message")),
            response_text=None,
            slot_patch={},
            state_patch={},
        )
    )

    assert result.verdict == "revise"


def test_knowledge_trigger_match_and_rerank() -> None:
    card = KnowledgeCard(
        id=uuid.uuid4(),
        card_key="trust.no_nudity",
        stage="trust",
        topic="trust.no_nudity",
        content="Без нюдсов и личных встреч.",
        triggers={"phrases": ["нюд", "интим"]},
        tags={"items": ["trust"]},
        verification_status="approved",
        active=True,
    )

    score, reasons = trigger_score(card, {"нужны", "нюд"}, {"trust.no_nudity"}, set())
    retrieved = merge_and_rerank([to_retrieved_card(card, score, reasons)], top_k=1)

    assert score > 0
    assert retrieved[0].card_key == "trust.no_nudity"


def test_mentor_reference_cards_get_small_rerank_boost() -> None:
    base = KnowledgeCard(
        id=uuid.uuid4(),
        card_key="platform.regular",
        stage="qualification",
        topic="platforms",
        content="Dacast Restream",
        triggers={"items": ["dacast"]},
        tags={"items": ["platforms"]},
        verification_status="verified",
        active=True,
        metadata_json={},
    )
    mentor = KnowledgeCard(
        id=uuid.uuid4(),
        card_key="mentor.platforms_streaming_platforms",
        stage="qualification",
        topic="platforms",
        content="Dacast Restream",
        triggers={"items": ["dacast"]},
        tags={"items": ["platforms"]},
        verification_status="verified",
        active=True,
        metadata_json={"source_bundle": "mentor_pavluck_bundle"},
    )

    base_score, _ = trigger_score(base, {"dacast"}, {"platforms"}, set())
    mentor_score, reasons = trigger_score(mentor, {"dacast"}, {"platforms"}, set())

    assert is_mentor_reference(mentor) is True
    assert mentor_score > base_score
    assert "mentor_reference" in reasons


def test_profitcast_seed_loader_normalizes_bundle() -> None:
    cards = load_profitcast_cards(Path("data/profitcast"))

    assert len(cards) == 26
    assert cards[0]["card_key"] == "first_touch.streamer_offer"
    assert cards[0]["active"] is True
    assert cards[0]["stage"] == "first_touch_sent"
    assert cards[0]["metadata_json"]["source_bundle"] == "profitcast_v1"
    assert cards[0]["metadata_json"]["source_stage"] == "first_touch"
    assert "embedding_text" in cards[0]["metadata_json"]


def test_nastya_bundle_loader_imports_cards_and_dialogue_chunks() -> None:
    cards = load_bundle_cards(Path("data/nastya"))
    keys = {card["card_key"] for card in cards}

    assert len(cards) == 19
    assert "company.profitcast_links_site_channel" in keys
    assert "dialogue_chunk.nastya_001_first_touch_interest" in keys
    chunk = next(card for card in cards if card["card_key"] == "dialogue_chunk.nastya_001_first_touch_interest")
    assert chunk["stage"] == "first_touch_sent"
    assert chunk["verification_status"] == "verified"
    assert chunk["metadata_json"]["card_type"] == "dialogue_chunk"
    assert chunk["metadata_json"]["source_bundle"] == "template_dialogue_003_nastya"


def test_rina_bundle_loader_imports_cards_without_manifest() -> None:
    cards = load_bundle_cards(Path("data/rina"))
    keys = {card["card_key"] for card in cards}

    assert len(cards) == 9
    assert "workflow.clarify_confusion_before_continue" in keys
    zoom = next(card for card in cards if card["card_key"] == "support.zoom_link_download_appstore")
    assert zoom["stage"] == "post_schedule_support"
    assert zoom["metadata_json"]["source_bundle"] == "rina_bundle"
    assert zoom["metadata_json"]["source_stage"] == "post_schedule_support"


def test_mentor_pavluck_bundle_loader_marks_reference_cards() -> None:
    cards = load_bundle_cards(Path("data/mentor_pavluck"))
    keys = {card["card_key"] for card in cards}

    assert len(cards) == 7
    assert "mentor.platforms_streaming_platforms" in keys
    first = next(card for card in cards if card["card_key"] == "mentor.platforms_streaming_platforms")
    assert first["stage"] == "qualification_faq"
    assert first["metadata_json"]["source_bundle"] == "mentor_pavluck_bundle"
    assert first["metadata_json"]["source_stage"] == "qualification_faq"


def test_combined_incoming_message_uses_candidate_batch() -> None:
    incoming = BrainMessage(id="3", body="и когда старт?")
    combined = combined_incoming_message(
        incoming,
        [
            BrainMessage(id="1", body="интересно"),
            BrainMessage(id="2", body="а что по оплате?"),
            incoming,
        ],
    )

    assert combined.body == "интересно\nа что по оплате?\nи когда старт?"


def test_typing_event_detection() -> None:
    assert is_candidate_typing_event({"event_type": "user_typing"}) is True
    assert is_candidate_typing_event({"action": {"_": "sendMessageTypingAction"}}) is True
    assert is_candidate_typing_event({"event_type": "message_new"}) is False


def test_llm_provider_config_resolves_component_env() -> None:
    settings = Settings(
        BRAIN_DEFAULT_PROVIDER="openrouter",
        BRAIN_ROUTER_MODEL="deepseek/test-router",
        OPENROUTER_API_KEY="or-key",
    )

    assert component_provider(settings, "router") == "openrouter"
    assert component_model(settings, "router") == "deepseek/test-router"
    assert api_key_for_provider(settings, "openrouter") == "or-key"
    assert base_url_for_provider("groq") == "https://api.groq.com/openai/v1"


def test_brain_llm_config_falls_back_to_generic_llm_settings() -> None:
    settings = Settings(
        LLM_PROVIDER="openrouter",
        LLM_MODEL="openai/gpt-4.1-mini",
        LLM_BASE_URL="https://openrouter.ai/api/v1/",
        LLM_API_KEY="or-key",
    )

    assert component_provider(settings, "dialogue_brain") == "openrouter"
    assert component_model(settings, "dialogue_brain") == "openai/gpt-4.1-mini"
    assert api_key_for_provider(settings, "openrouter") == "or-key"
    assert base_url_for_provider("openrouter", settings) == "https://openrouter.ai/api/v1"


def test_alibaba_provider_uses_plain_json_parsing_without_json_schema_response_format() -> None:
    settings = Settings(
        LLM_PROVIDER="alibaba",
        LLM_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        LLM_MODEL="qwen-plus",
        LLM_API_KEY="ali-key",
    )

    assert component_provider(settings, "dialogue_brain") == "alibaba"
    assert base_url_for_provider("alibaba", settings) == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert provider_supports_json_schema_response_format("alibaba") is False
    assert parse_json_content('```json\n{"ok": true}\n```') == {"ok": True}


def test_embedding_adapter_uses_default_embedding_provider() -> None:
    class FakeHTTPClient:
        def __init__(self):
            self.calls = []

        async def post(self, url, *, json, headers, timeout):
            self.calls.append((url, json, headers, timeout))
            return FakeHTTPResponse(
                {
                    "model": "text-embedding-3-small",
                    "data": [{"embedding": [0.1, 0.2, 0.3]}],
                    "usage": {"prompt_tokens": 3},
                }
            )

    class FakeHTTPResponse:
        is_error = False
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    settings = Settings(
        DEFAULT_EMBEDDING_PROVIDER="openai",
        DEFAULT_EMBEDDING_MODEL="text-embedding-3-small",
        OPENAI_API_KEY="openai-key",
    )
    client = FakeHTTPClient()
    adapter = BrainLLMAdapter(settings=settings, http_client=client)

    embeddings = asyncio.run(adapter.embed_texts(["hello"]))

    assert embeddings == [[0.1, 0.2, 0.3]]
    assert client.calls[0][0] == "https://api.openai.com/v1/embeddings"
    assert client.calls[0][1]["model"] == "text-embedding-3-small"
    assert adapter.telemetry[0].component == "embedding"


class FakeSession:
    def __init__(self, dialog=None):
        self.dialog = dialog
        self.added = []
        self.flushed = 0

    async def get(self, model, object_id):
        if model.__name__ == "Dialog":
            return self.dialog
        return None

    async def execute(self, query):
        return FakeScalarResult(None)

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        self.flushed += 1


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


def test_executor_shadow_does_not_enqueue_outbound_job() -> None:
    session = FakeSession()
    run = BrainRun(
        id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        dialog_id=uuid.uuid4(),
        shadow_mode=True,
        status="shadow_pending",
    )

    asyncio.run(
        BrainExecutor(session).execute_or_shadow(
            brain_run=run,
            dialog=Dialog(id=run.dialog_id, account_id=uuid.uuid4(), crmchat_dialog_id="d1"),
            action=ExecutorAction(type="send_message", text="hello"),
            shadow_mode=True,
        )
    )

    assert run.status == "shadow_pending"
    assert not any(isinstance(item, OutboundJob) for item in session.added)


def test_executor_approve_creates_outbound_job() -> None:
    dialog = Dialog(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        crmchat_dialog_id="d1",
        telegram_username="@iamnekiy",
    )
    run = BrainRun(
        id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        dialog_id=dialog.id,
        shadow_mode=True,
        status="shadow_pending",
        executor_action={"type": "send_message", "text": "Привет"},
    )
    session = FakeSession(dialog)

    asyncio.run(BrainExecutor(session).approve(run))

    assert run.status == "approved"
    assert any(isinstance(item, OutboundJob) for item in session.added)


def test_executor_send_message_has_no_extra_debounce_delay() -> None:
    dialog = Dialog(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        crmchat_dialog_id="d1",
        telegram_username="@iamnekiy",
    )
    run = BrainRun(
        id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        dialog_id=dialog.id,
        shadow_mode=True,
        status="shadow_pending",
        executor_action={"type": "send_message", "text": "Привет"},
    )
    session = FakeSession(dialog)
    before = datetime.now(UTC)

    asyncio.run(BrainExecutor(session, settings=Settings(BRAIN_INBOUND_DEBOUNCE_SECONDS=10)).approve(run))

    job = next(item for item in session.added if isinstance(item, OutboundJob))
    assert job.scheduled_at <= before + timedelta(seconds=1)


def test_replay_core_dialogue_scenarios() -> None:
    scenarios = json.loads(
        (Path(__file__).parent / "fixtures" / "brain_v2_replays.json").read_text(encoding="utf-8")
    )

    async def run_one(text: str, stage: str):
        router = RouterExtractor()
        router_result = await router.extract(
            incoming_message=BrainMessage(body=text),
            recent_messages=[],
            state_snapshot={"stage": stage},
        )
        agenda = [
            {
                "item_key": item.item_key,
                "stage": item.stage,
                "priority": item.priority,
                "required": item.required,
                "status": "pending",
                "completion_rule": item.completion_rule,
                "default_question": item.default_question,
                "slot_key": item.slot_key,
                "next_stage_hint": item.next_stage_hint,
            }
            for item in AgendaRegistry.standard_items()
        ]
        return await DialogueBrain().decide(
            current_stage=stage,
            current_goal="replay",
            open_loop={"question": "Скажи, пожалуйста, тебе уже есть 18?"},
            profile_slots={},
            agenda_items=agenda,
            recent_messages=[],
            last_lead_message=BrainMessage(body=text),
            router_result=router_result,
            retrieved_knowledge_cards=[],
        )

    for scenario in scenarios:
        decision = asyncio.run(run_one(scenario["message"], scenario["stage"]))
        assert decision.dialogue_move == scenario["expected_move"], scenario["name"]


def test_inbound_worker_runs_langgraph_gateway_only(monkeypatch) -> None:
    calls = []

    class FakeGateway:
        def __init__(self, session, *, settings=None):
            self.session = session

        async def decide_for_dialog_message(self, *, dialog_id, message_id):
            calls.append(("langgraph", dialog_id, message_id))

    monkeypatch.setattr(inbound_queue_worker, "LangGraphFunnelGateway", FakeGateway)
    monkeypatch.setattr(
        inbound_queue_worker,
        "get_settings",
        lambda: Settings(LANGGRAPH_FUNNEL_ENABLED=True, LANGGRAPH_CHECKPOINT_POSTGRES_ENABLED=False),
    )
    event = InboundEvent(
        id=uuid.uuid4(),
        source="test",
        dialog_id=uuid.uuid4(),
        payload={"db_message_id": str(uuid.uuid4())},
        status="queued",
    )

    asyncio.run(InboundQueueWorker(FakeSession())._process_event(event))

    assert [item[0] for item in calls] == ["langgraph"]


def test_inbound_worker_defers_until_candidate_quiet_window(monkeypatch) -> None:
    retry_at = datetime.now(UTC)

    class FakeTurnBuffer:
        def __init__(self, session, *, debounce_seconds):
            self.session = session

        async def prepare_inbound_turn(self, *, dialog, lead, runtime, message_id):
            return __import__("types").SimpleNamespace(
                ready=False,
                retry_at=retry_at,
                reason="waiting_for_candidate_quiet_window",
            )

    monkeypatch.setattr(inbound_queue_worker, "FunnelTurnBufferService", FakeTurnBuffer)
    monkeypatch.setattr(
        inbound_queue_worker,
        "get_settings",
        lambda: Settings(LANGGRAPH_FUNNEL_ENABLED=True, LANGGRAPH_CHECKPOINT_POSTGRES_ENABLED=False),
    )
    event = InboundEvent(
        id=uuid.uuid4(),
        source="test",
        dialog_id=uuid.uuid4(),
        payload={"db_message_id": str(uuid.uuid4())},
        status="queued",
    )
    session = FakeSession(Dialog(id=event.dialog_id, account_id=uuid.uuid4(), crmchat_dialog_id="d1"))

    try:
        asyncio.run(InboundQueueWorker(session)._process_event(event))
    except DeferredInboundEvent as exc:
        assert exc.retry_at == retry_at
        assert exc.reason == "waiting_for_candidate_quiet_window"
    else:
        raise AssertionError("expected DeferredInboundEvent")
