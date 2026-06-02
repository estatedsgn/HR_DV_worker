from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.funnel_policy import next_stage_if_requirement_met, normalize_candidate_profile
from app.services.funnel_graph.knowledge import StaticFunnelKnowledgeBase
from app.services.funnel_graph.semantic import SemanticFacts, SemanticResult, deterministic_semantic, normalize_text


DEFAULT_INPUT_PATH = (
    Path.home()
    / "OneDrive"
    / "\u0420\u0430\u0431\u043e\u0447\u0438\u0439 \u0441\u0442\u043e\u043b"
    / "\u041f\u0440\u0438\u043c\u0435\u0440\u044b \u043f\u0435\u0440\u0435\u043f\u0438\u0441\u043e\u043a.txt"
)
DEFAULT_OUTPUT_ROOT = Path("runtime_logs")

CANONICAL_TOPICS = [
    "contact_source",
    "why_selected",
    "job_description",
    "nudity_onlyfans",
    "income",
    "schedule",
    "equipment",
    "phone_requirements",
    "payment_process",
    "contract_gph",
    "english_level",
    "company_info",
    "company_channels",
    "platform_info",
    "training_process",
    "friend_streaming",
    "theme_selection",
    "interview_process",
    "room",
    "timezone",
    "privacy_anonymity",
    "documents_privacy",
    "exit_policy",
    "trust_concern",
    "suspicious_or_scam",
    "no_experience",
    "new_sphere_uncertainty",
    "no_time",
]
GENERIC_TOPICS = {"unknown", "unclear", "parse_error", "none", "null"}


class DialogueLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    speaker: Literal["lead", "recruiter"]
    text: str
    line_index: int


class SemanticExpectation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_type: str = "unclear"
    current_goal_satisfied: bool = False
    has_unresolved_interrupt: bool = False
    interrupt_type: str = "none"
    interrupt_topic: str | None = None
    retrieval_topics: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0

    @field_validator("retrieval_topics", mode="before")
    @classmethod
    def normalize_topics(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            raw_items = re.split(r"[,;/]+", value)
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = [value]
        topics: list[str] = []
        for item in raw_items:
            topic = str(item or "").strip().lower().replace("-", "_").replace(" ", "_")
            if topic and topic not in topics:
                topics.append(topic)
        return topics[:8]


class KnowledgeNeed(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str
    kind: Literal["faq", "objection", "style", "stage_rule"] = "faq"
    candidate_phrases: list[str] = Field(default_factory=list)
    answer_facts: list[str] = Field(default_factory=list)
    reply_fragments: list[str] = Field(default_factory=list)


class PromptCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target_prompt: Literal["semantic_analyzer", "reply_orchestrator"]
    example: str
    reason: str = ""


class TurnChunkAnnotation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    turn_chunk_id: str
    inbound_messages: list[str] = Field(default_factory=list)
    expected_outbound_messages: list[str] = Field(default_factory=list)
    stage_before: str = "unknown"
    semantic_result_expected: SemanticExpectation = Field(default_factory=SemanticExpectation)
    knowledge_topics: list[str] = Field(default_factory=list)
    reply_intent: str = ""
    stage_after_expected: str = "unknown"
    chunk_boundary_reason: str = ""
    knowledge_needs: list[KnowledgeNeed] = Field(default_factory=list)
    prompt_candidates: list[PromptCandidate] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)

    @field_validator("semantic_result_expected", mode="before")
    @classmethod
    def normalize_semantic_result_expected(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"message_type": value}
        return value

    @field_validator("inbound_messages", "expected_outbound_messages", "review_flags", mode="before")
    @classmethod
    def normalize_string_list_fields(cls, value: Any) -> list[str]:
        return listify(value)

    @field_validator("knowledge_topics", mode="before")
    @classmethod
    def normalize_knowledge_topics(cls, value: Any) -> list[str]:
        return SemanticExpectation.normalize_topics(value)


class StageEpisodeAnnotation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stage_episode_id: str
    stage_name: str
    turn_chunk_ids: list[str] = Field(default_factory=list)
    candidate_goal: str = ""
    knowledge_needed: list[str] = Field(default_factory=list)
    working_reply_style: str = ""
    prompt_examples: list[str] = Field(default_factory=list)

    @field_validator("turn_chunk_ids", "knowledge_needed", "prompt_examples", mode="before")
    @classmethod
    def normalize_list_fields(cls, value: Any) -> list[str]:
        return listify(value)


class DialogueAnnotation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dialogue_id: str
    summary: str = ""
    turn_chunks: list[TurnChunkAnnotation] = Field(default_factory=list)
    stage_episodes: list[StageEpisodeAnnotation] = Field(default_factory=list)
    global_knowledge_needs: list[KnowledgeNeed] = Field(default_factory=list)
    global_prompt_candidates: list[PromptCandidate] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)


class AnnotationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dialogues: list[DialogueAnnotation] = Field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Annotate HR dialogue examples with Qwen into semantic chunks.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH, help="Path to raw dialogue examples.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Root directory for runtime output.")
    parser.add_argument("--output-dir", type=Path, help="Exact output directory. Defaults to timestamped runtime_logs dir.")
    parser.add_argument("--max-dialogues-per-call", type=int, default=1, help="Keep Qwen calls small and reviewable.")
    parser.add_argument(
        "--max-speaker-runs-per-call",
        type=int,
        default=12,
        help="Split large dialogues into compact Qwen parts. Use 0 to disable splitting.",
    )
    parser.add_argument("--llm-timeout-seconds", type=float, help="Override BRAIN_LLM_TIMEOUT_SECONDS for annotation calls.")
    parser.add_argument(
        "--manual-reviewed",
        action="store_true",
        help="Build reviewed_annotations.json locally without a full Qwen annotation run.",
    )
    parser.add_argument("--no-llm", action="store_true", help="Only parse the input and write parsed_dialogues.json.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    output_dir = args.output_dir or args.output_root / f"knowledge_annotation_qwen37max_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_text = args.input.read_text(encoding="utf-8")
    parsed_dialogues = parse_dialogues(raw_text)
    write_json(output_dir / "parsed_dialogues.json", {"dialogues": parsed_dialogues})

    if args.manual_reviewed:
        reviewed = build_manual_reviewed_annotations(parsed_dialogues)
        write_json(output_dir / "reviewed_annotations.json", reviewed.model_dump())
        write_json(output_dir / "knowledge_suggestions.json", build_knowledge_suggestions(reviewed))
        (output_dir / "prompt_snippets.md").write_text(render_prompt_snippets(reviewed), encoding="utf-8")
        print(f"reviewed_annotations={output_dir / 'reviewed_annotations.json'}")
        print(f"knowledge_suggestions={output_dir / 'knowledge_suggestions.json'}")
        print(f"prompt_snippets={output_dir / 'prompt_snippets.md'}")
        return

    if args.no_llm:
        print(f"parsed_dialogues={output_dir / 'parsed_dialogues.json'}")
        return

    settings = get_settings()
    if args.llm_timeout_seconds:
        settings = settings.model_copy(update={"brain_llm_timeout_seconds": float(args.llm_timeout_seconds)})
    adapter = BrainLLMAdapter(settings=settings)
    if not adapter.has_api_key("dialogue_brain"):
        raise SystemExit("Missing API key for dialogue_brain provider; parsed_dialogues.json was written.")

    llm_dialogues = split_dialogues_for_llm(parsed_dialogues, max_runs=int(args.max_speaker_runs_per_call))
    write_json(output_dir / "llm_dialogue_parts.json", {"dialogues": llm_dialogues})

    raw_annotations: list[dict[str, Any]] = []
    reviewed_dialogues: list[DialogueAnnotation] = []
    batches = chunked(llm_dialogues, max(1, int(args.max_dialogues_per_call)))
    for index, batch in enumerate(batches, start=1):
        payload = {
            "annotation_contract": annotation_contract(),
            "canonical_topics": CANONICAL_TOPICS,
            "dialogues": [compact_dialogue_for_llm(item) for item in batch],
        }
        try:
            response = await adapter.complete_json(
                component="dialogue_brain",
                system_prompt=annotation_prompt(),
                user_payload=payload,
                response_model=AnnotationResponse,
            )
        except Exception as exc:
            write_json(output_dir / "raw_annotations.json", {"batches": raw_annotations})
            write_json(
                output_dir / "annotation_errors.json",
                {
                    "failed_batch_index": index,
                    "dialogue_ids": [item.get("dialogue_id") for item in batch],
                    "error_type": type(exc).__name__,
                    "error": str(exc) or repr(exc),
                    "telemetry": [asdict(item) for item in adapter.telemetry],
                },
            )
            raise
        raw_annotations.append({"batch_index": index, "response": response})
        parsed = AnnotationResponse.model_validate(response)
        reviewed_dialogues.extend(normalize_annotation(parsed.dialogues, batch))

    reviewed = AnnotationResponse(dialogues=reviewed_dialogues)
    write_json(output_dir / "raw_annotations.json", {"batches": raw_annotations})
    write_json(output_dir / "reviewed_annotations.json", reviewed.model_dump())
    write_json(output_dir / "knowledge_suggestions.json", build_knowledge_suggestions(reviewed))
    (output_dir / "prompt_snippets.md").write_text(render_prompt_snippets(reviewed), encoding="utf-8")

    print(f"raw_annotations={output_dir / 'raw_annotations.json'}")
    print(f"reviewed_annotations={output_dir / 'reviewed_annotations.json'}")
    print(f"knowledge_suggestions={output_dir / 'knowledge_suggestions.json'}")
    print(f"prompt_snippets={output_dir / 'prompt_snippets.md'}")


def parse_dialogues(raw_text: str) -> list[dict[str, Any]]:
    dialogues: list[dict[str, Any]] = []
    current_id: str | None = "dialogue_1"
    current_lines: list[DialogueLine] = []
    line_index = 0
    current_speaker: Literal["lead", "recruiter"] | None = None
    message_buffer: list[str] = []

    def flush_message() -> None:
        nonlocal current_id, current_lines, current_speaker, message_buffer, line_index
        if current_speaker is None:
            message_buffer = []
            return
        text = "\n".join(part for part in message_buffer if part.strip()).strip()
        if not text:
            current_speaker = None
            message_buffer = []
            return
        if is_new_dialogue_opener(current_speaker, text, current_lines):
            dialogues.append(dialogue_payload(current_id or f"dialogue_{len(dialogues) + 1}", current_lines))
            current_id = f"dialogue_{len(dialogues) + 1}"
            current_lines = []
            line_index = 0
        line_index += 1
        current_lines.append(DialogueLine(speaker=current_speaker, text=text, line_index=line_index))
        current_speaker = None
        message_buffer = []

    def flush_dialogue(label: str | None = None) -> None:
        nonlocal current_id, current_lines, line_index
        flush_message()
        if current_lines:
            dialogues.append(dialogue_payload(current_id or f"dialogue_{len(dialogues) + 1}", current_lines))
            current_lines = []
            line_index = 0
        current_id = normalize_dialogue_id(label or f"dialogue_{len(dialogues) + 1}", len(dialogues) + 1)

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        exported = telegram_export_payload(line)
        from_export = exported is not None
        if exported is not None:
            line = exported.strip()
            if not line:
                continue
        role_match = re.match(r"^(Лид|Кандидатка|Кандидат|Рекрутер)\s*:\s*(.*)$", line, flags=re.IGNORECASE)
        if role_match:
            flush_message()
            current_speaker = speaker_for_role(role_match.group(1))
            tail = role_match.group(2).strip()
            message_buffer = [tail] if tail else []
            continue
        if is_dialogue_separator(line):
            flush_message()
            continue
        if current_speaker is not None:
            message_buffer.append(line)
            continue
        if from_export and current_lines and not is_dialogue_separator(line):
            current_speaker = "recruiter"
            message_buffer = [line]
            continue
        flush_dialogue(line)
    flush_message()
    if current_id and current_lines:
        dialogues.append(dialogue_payload(current_id, current_lines))
    return dialogues


def speaker_for_role(role: str) -> Literal["lead", "recruiter"]:
    normalized = normalize_text(role)
    return "recruiter" if "рекрутер" in normalized else "lead"


def telegram_export_payload(line: str) -> str | None:
    match = re.match(r"^\[[^\]]+\]\s+[^:]{1,80}:\s*(.*)$", line)
    return match.group(1) if match else None


def is_dialogue_separator(line: str) -> bool:
    normalized = normalize_text(line)
    if not normalized:
        return True
    if re.fullmatch(r"[⸻—\-]+", line):
        return True
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    if re.fullmatch(rf"\d{{1,2}}\s+({'|'.join(months)})(?:\s+\d{{4}})?", normalized):
        return True
    if normalized in {"добрый день", "доброй ночи"}:
        return False
    return False


def is_new_dialogue_opener(speaker: str, text: str, existing_lines: list[DialogueLine]) -> bool:
    if speaker != "recruiter" or not existing_lines:
        return False
    normalized = normalize_text(text)
    opener_markers = (
        "интересное предложение",
        "предложение о работе",
        "предложение работы",
        "вакансия контент",
        "работы контент моделью",
        "работа контент моделью",
        "ты выглядишь",
        "ты просто великолеп",
        "выглядищь великолеп",
        "выглядишь великолеп",
    )
    funnel_markers = ("onlyfans", "онлиф", "вебкам", "стрим", "twitch", "твич", "от 18")
    return has_any(normalized, opener_markers) and has_any(normalized, funnel_markers)


def dialogue_payload(dialogue_id: str, lines: list[DialogueLine]) -> dict[str, Any]:
    return {
        "dialogue_id": dialogue_id,
        "lines": [line.model_dump() for line in lines],
        "speaker_runs": speaker_runs(lines),
    }


def compact_dialogue_for_llm(dialogue: dict[str, Any]) -> dict[str, Any]:
    return {
        "dialogue_id": dialogue.get("dialogue_id"),
        "source_dialogue_id": dialogue.get("source_dialogue_id") or dialogue.get("dialogue_id"),
        "part_index": dialogue.get("part_index"),
        "part_count": dialogue.get("part_count"),
        "run_offset": dialogue.get("run_offset", 0),
        "speaker_runs": dialogue.get("speaker_runs") or [],
    }


def split_dialogues_for_llm(dialogues: list[dict[str, Any]], *, max_runs: int) -> list[dict[str, Any]]:
    if max_runs <= 0:
        return [compact_dialogue_for_llm(item) for item in dialogues]
    result: list[dict[str, Any]] = []
    for dialogue in dialogues:
        runs = list(dialogue.get("speaker_runs") or [])
        if len(runs) <= max_runs:
            result.append(compact_dialogue_for_llm(dialogue))
            continue
        parts = [runs[index : index + max_runs] for index in range(0, len(runs), max_runs)]
        for part_index, part_runs in enumerate(parts, start=1):
            result.append(
                {
                    "dialogue_id": f"{dialogue.get('dialogue_id')}__part_{part_index:02d}",
                    "source_dialogue_id": dialogue.get("dialogue_id"),
                    "part_index": part_index,
                    "part_count": len(parts),
                    "run_offset": (part_index - 1) * max_runs,
                    "speaker_runs": part_runs,
                }
            )
    return result


def build_manual_reviewed_annotations(parsed_dialogues: list[dict[str, Any]]) -> AnnotationResponse:
    store = StaticFunnelKnowledgeBase()
    dialogues: list[DialogueAnnotation] = []
    for parsed in parsed_dialogues:
        dialogue_id = str(parsed.get("dialogue_id") or "dialogue")
        stage = "interest_check"
        profile = normalize_candidate_profile({})
        turn_chunks: list[TurnChunkAnnotation] = []
        stage_episodes: dict[str, StageEpisodeAnnotation] = {}
        runs = list(parsed.get("speaker_runs") or [])
        chunk_number = 0
        for index, run in enumerate(runs):
            if run.get("speaker") != "lead":
                continue
            previous_outbound = []
            if index > 0 and runs[index - 1].get("speaker") == "recruiter":
                previous_outbound = [redact_text(item) for item in runs[index - 1].get("messages") or [] if str(item or "").strip()]
            inbound = [redact_text(item) for item in run.get("messages") or [] if str(item or "").strip()]
            if not inbound:
                continue
            outbound = []
            if index + 1 < len(runs) and runs[index + 1].get("speaker") == "recruiter":
                outbound = [redact_text(item) for item in runs[index + 1].get("messages") or [] if str(item or "").strip()]
            chunk_number += 1
            incoming = "\n".join(inbound)
            stage_before = infer_stage_before(stage, previous_outbound)
            state = {
                "stage": stage_before,
                "candidate_profile": profile,
                "incoming_message": incoming,
                "message_batch": [
                    {"direction": "inbound", "sender_type": "lead", "body": message}
                    for message in inbound
                ],
            }
            semantic = semantic_with_manual_overrides(stage_before, deterministic_semantic(state), inbound, previous_outbound)
            retrieved = store.retrieve(
                incoming_message=incoming,
                current_stage=stage_before,
                retrieval_query=semantic.retrieval_query,
                retrieval_topics=semantic.retrieval_topics,
            )
            topics = prioritize_manual_topics(compact_topics([*list(semantic.retrieval_topics or []), semantic.interrupt_topic or ""]), incoming)
            if semantic.has_unresolved_interrupt and topics:
                semantic = semantic.model_copy(update={"retrieval_topics": topics, "interrupt_topic": topics[0]})
            stage_after = infer_stage_after(stage_before, profile, semantic, outbound)
            knowledge_needs = knowledge_needs_for_topics(topics, retrieved, inbound, outbound)
            chunk = TurnChunkAnnotation(
                turn_chunk_id=f"{dialogue_id}_turn_{chunk_number:02d}",
                inbound_messages=inbound,
                expected_outbound_messages=outbound,
                stage_before=stage_before,
                semantic_result_expected=SemanticExpectation.model_validate(semantic.model_dump()),
                knowledge_topics=topics,
                reply_intent=reply_intent_for_semantic(semantic.message_type, topics),
                stage_after_expected=stage_after,
                chunk_boundary_reason="contiguous lead speaker run paired with following recruiter speaker run",
                knowledge_needs=knowledge_needs,
                prompt_candidates=prompt_candidates_for_chunk(inbound, outbound, semantic.message_type, topics),
                review_flags=[],
            )
            turn_chunks.append(chunk)
            episode_key = stage_episode_key(stage_before, topics)
            episode = stage_episodes.setdefault(
                episode_key,
                StageEpisodeAnnotation(
                    stage_episode_id=f"{dialogue_id}_{episode_key}",
                    stage_name=stage_before,
                    candidate_goal=candidate_goal_for_topics(topics, stage_before),
                    knowledge_needed=[],
                    working_reply_style="Коротко отвечать на смысловой batch, не терять вопросы из середины пачки и не давить.",
                    prompt_examples=[],
                ),
            )
            episode.turn_chunk_ids.append(chunk.turn_chunk_id)
            for topic in topics:
                append_unique(episode.knowledge_needed, topic)
            for candidate in chunk.prompt_candidates[:1]:
                append_unique(episode.prompt_examples, candidate.example)

            profile.update({key: value for key, value in semantic.facts.model_dump().items() if value is not None})
            stage = stage_after
        dialogues.append(
            DialogueAnnotation(
                dialogue_id=dialogue_id,
                summary=f"Manual reviewed semantic chunk annotation for {dialogue_id}.",
                turn_chunks=turn_chunks,
                stage_episodes=list(stage_episodes.values()),
                global_knowledge_needs=global_knowledge_needs_from_chunks(turn_chunks),
                global_prompt_candidates=global_prompt_candidates_from_chunks(turn_chunks),
                review_flags=["manual_reviewed_by_codex", "qwen_spot_check_optional"],
            )
        )
    return AnnotationResponse(dialogues=dialogues)


def manual_next_stage(stage: str, profile: dict[str, Any], semantic: Any) -> str:
    if semantic.has_unresolved_interrupt or semantic.message_type in {"empty", "unclear", "partial_answer", "pause"}:
        return stage
    merged_profile = {**profile, **{key: value for key, value in semantic.facts.model_dump().items() if value is not None}}
    next_stage = next_stage_if_requirement_met(stage, merged_profile)
    if next_stage == "work_intro_delivery":
        return "salary_schedule_offer"
    if next_stage == "salary_schedule_delivery":
        return "post_equipment_questions_check"
    if next_stage == "support_smalltalk":
        return "room_available_check"
    return next_stage


def infer_stage_before(current_stage: str, previous_outbound: list[str]) -> str:
    return infer_stage_from_recruiter_messages(previous_outbound) or current_stage


def infer_stage_after(
    stage_before: str,
    profile: dict[str, Any],
    semantic: SemanticResult,
    outbound: list[str],
) -> str:
    inferred_from_recruiter = infer_stage_from_recruiter_messages(outbound)
    if inferred_from_recruiter:
        return inferred_from_recruiter
    return manual_next_stage(stage_before, profile, semantic)


def infer_stage_from_recruiter_messages(messages: list[str]) -> str | None:
    text = normalize_text("\n".join(messages))
    if not text:
        return None
    if has_any(text, ("уже 18", "есть 18", "18 есть", "совершеннолет")):
        return "age_check"
    if has_all(text, ("сколько", "лет")):
        return "age_check"
    if has_all(text, ("11:00", "18:00")) and has_any(text, ("время", "удобнее")):
        return "interview_time_check"
    if has_any(text, ("на какое число", "когда смож", "когда тебе будет удобно")):
        return "interview_custom_time"
    if has_all(text, ("завтра", "удобно")) and has_any(text, ("собесед", "провести")):
        return "interview_day_check"
    if has_any(text, ("номер телефон", "номер телефона", "номер телефончика", "имя и номер", "номер и имя")):
        return "contact_collection"
    if has_all(text, ("для записи", "номер")):
        return "contact_collection"
    if has_any(text, ("записаться на собеседование", "записаться на интервью")) or has_all(text, ("можем", "записаться")):
        return "interview_offer"
    if has_all(text, ("модель", "телефон")) or has_any(text, ("моделька телефон", "модель телефона")):
        return "equipment_phone_check"
    if has_any(text, ("комната", "свободная", "никто не помеш", "место")) and has_any(text, ("стрим", "трансляц", "помеш")):
        return "room_available_check"
    if has_any(text, ("расскажи немного о себе", "учишься/работаешь", "учишься", "работаешь")) and has_any(text, ("свободное время", "заниматься", "темати")):
        return "profile_theme_check"
    if has_any(text, ("желаешь попробовать", "попробовать себя", "попробовать нашу сферу")) or has_all(text, ("насколько", "интерес")):
        return "try_interest_check"
    if has_any(text, ("остались", "больше вопросов", "вопросиков")) and "вопрос" in text:
        return "post_equipment_questions_check"
    if has_any(text, ("про зп", "зарплат", "выплат")) and has_any(text, ("график", "слушаю", "интересна")):
        return "salary_schedule_offer"
    if has_any(text, ("рассказать подробнее", "расскажу подробнее")):
        return "interest_check"
    return None


def semantic_with_manual_overrides(
    stage: str,
    semantic: SemanticResult,
    inbound: list[str],
    previous_outbound: list[str] | None = None,
) -> SemanticResult:
    text = "\n".join(inbound)
    normalized = normalize_text(text)
    previous_prompt = normalize_text("\n".join(previous_outbound or []))
    if stage == "contact_collection" and ("[номер]" in normalized or re.search(r"\d[\d\s().-]{8,}\d", text)):
        facts = semantic.facts.model_dump()
        facts["phone_number"] = facts.get("phone_number") or "[номер]"
        name = extract_placeholder_name(text)
        if name:
            facts["candidate_name"] = facts.get("candidate_name") or name
        complete = bool(facts.get("phone_number") and facts.get("candidate_name"))
        return SemanticResult(
            message_type="stage_answer" if complete else "partial_answer",
            summary="contact data provided in reviewed dialogue",
            current_goal_satisfied=complete,
            has_unresolved_interrupt=False,
            interrupt_type="none",
            facts=SemanticFacts.model_validate(facts),
            retrieval_query="",
            retrieval_topics=[],
            evidence="manual reviewed contact placeholder",
            confidence=0.95,
        )
    if stage == "equipment_phone_check" and looks_like_phone_model(normalized):
        facts = semantic.facts.model_dump()
        facts["phone_model"] = facts.get("phone_model") or text
        return SemanticResult(
            message_type="stage_answer",
            summary="phone model provided in reviewed dialogue",
            current_goal_satisfied=True,
            has_unresolved_interrupt=False,
            interrupt_type="none",
            facts=SemanticFacts.model_validate(facts),
            retrieval_query="",
            retrieval_topics=[],
            evidence="manual reviewed phone model",
            confidence=0.9,
        )
    if is_generic_unresolved(semantic) and is_link_or_meeting_access_issue(normalized, previous_prompt):
        return SemanticResult(
            message_type="interrupt_question",
            summary="candidate has a link or meeting access issue",
            current_goal_satisfied=False,
            has_unresolved_interrupt=True,
            interrupt_type="question",
            interrupt_topic="company_channels",
            interrupt_text=text,
            facts=semantic.facts,
            retrieval_query=text,
            retrieval_topics=["company_channels", "interview_process"],
            evidence="manual reviewed link/meeting access issue",
            confidence=0.85,
        )
    normalized_msk_time = msk_daytime_short_hour(normalized)
    if stage == "interview_time_check" and normalized_msk_time:
        facts = semantic.facts.model_dump()
        facts["interview_time"] = normalized_msk_time
        return stage_answer_override(semantic, facts, "interview time selected in reviewed dialogue")
    if stage == "interview_time_check" and is_timezone_offset_correction(normalized, previous_prompt):
        return SemanticResult(
            message_type="objection",
            summary="candidate corrects timezone offset",
            current_goal_satisfied=False,
            has_unresolved_interrupt=True,
            interrupt_type="objection",
            interrupt_topic="timezone",
            interrupt_text=text,
            facts=semantic.facts,
            retrieval_query=text,
            retrieval_topics=["timezone"],
            evidence="manual reviewed timezone correction",
            confidence=0.85,
        )
    has_topics = has_semantic_topics(semantic)
    if (
        semantic.message_type == "stage_answer"
        and not has_topics
        and is_positive_stage_answer(normalized)
        and previous_prompt
        and not previous_prompt_matches_stage(stage, previous_prompt)
    ):
        return neutral_no_interrupt_override(semantic, "short answer to recruiter small talk, not active funnel stage")
    if is_phone_or_equipment_chitchat(normalized, semantic):
        return neutral_no_interrupt_override(semantic, "phone/equipment small talk in reviewed dialogue")
    positive_matches_stage = not previous_prompt or previous_prompt_matches_stage(stage, previous_prompt)
    if stage in {"interest_check", "try_interest_check"} and is_positive_stage_answer(normalized) and not has_topics and positive_matches_stage:
        return stage_answer_override(semantic, {"interest_confirmed": True, "interest_status": "interested"}, "interest confirmed in reviewed dialogue")
    if stage == "salary_schedule_offer" and is_positive_stage_answer(normalized) and not has_topics and positive_matches_stage:
        return stage_answer_override(semantic, {"salary_schedule_interest": True}, "salary/schedule info accepted in reviewed dialogue")
    if stage == "post_equipment_questions_check" and is_no_more_questions_answer(normalized):
        return stage_answer_override(semantic, {"questions_resolved": True}, "questions resolved in reviewed dialogue")
    if stage == "try_interest_check" and is_positive_stage_answer(normalized) and not has_topics and positive_matches_stage:
        return stage_answer_override(semantic, {"interest_confirmed": True, "interest_status": "interested"}, "try interest confirmed in reviewed dialogue")
    if stage == "room_available_check" and is_positive_stage_answer(normalized) and not has_topics and positive_matches_stage:
        return stage_answer_override(semantic, {"room_available": True, "room_note": text}, "room availability confirmed in reviewed dialogue")
    if stage == "interview_offer" and is_positive_stage_answer(normalized) and not has_topics and positive_matches_stage:
        return stage_answer_override(semantic, {"interview_interest": True}, "interview interest confirmed in reviewed dialogue")
    if stage == "interview_day_check" and is_positive_stage_answer(normalized) and not has_topics and positive_matches_stage:
        return stage_answer_override(semantic, {"interview_day_confirmed": True, "interview_day": "завтра"}, "interview day confirmed in reviewed dialogue")
    if stage == "interview_custom_time" and has_any(normalized, ("послезавтра", "на 21", "число", "завтра", "временной отрезок")):
        facts = semantic.facts.model_dump()
        facts["custom_interview_datetime"] = facts.get("custom_interview_datetime") or text
        return stage_answer_override(semantic, facts, "custom interview datetime provided in reviewed dialogue")
    if stage in {"interview_day_check", "interview_time_check"} and has_any(normalized, ("не могу", "неудобно", "не удобно")) and not has_topics:
        return partial_no_interrupt_override(semantic, "candidate cannot use proposed interview slot")
    if is_generic_unresolved(semantic) and previous_prompt and has_any(previous_prompt, ("вопрос", "понятно")) and is_no_more_questions_answer(normalized):
        return stage_answer_override(semantic, {"questions_resolved": True}, "no more questions in reviewed dialogue")
    if is_generic_unresolved(semantic) and (is_positive_stage_answer(normalized) or is_ack_or_short_neutral(normalized)):
        return neutral_no_interrupt_override(semantic, "short neutral acknowledgement in reviewed dialogue")
    return normalize_manual_interrupt_taxonomy(semantic, normalized)


def stage_answer_override(semantic: SemanticResult, facts_update: dict[str, Any], summary: str) -> SemanticResult:
    facts = semantic.facts.model_dump()
    facts.update({key: value for key, value in facts_update.items() if value is not None})
    return SemanticResult(
        message_type="stage_answer",
        summary=summary,
        current_goal_satisfied=True,
        has_unresolved_interrupt=False,
        interrupt_type="none",
        facts=SemanticFacts.model_validate(facts),
        retrieval_query="",
        retrieval_topics=[],
        evidence="manual reviewed stage answer",
        confidence=max(float(semantic.confidence or 0.0), 0.9),
    )


def neutral_no_interrupt_override(semantic: SemanticResult, summary: str) -> SemanticResult:
    return SemanticResult(
        message_type="unclear",
        summary=summary,
        current_goal_satisfied=False,
        has_unresolved_interrupt=False,
        interrupt_type="none",
        facts=semantic.facts,
        retrieval_query="",
        retrieval_topics=[],
        evidence="manual reviewed neutral turn",
        confidence=max(float(semantic.confidence or 0.0), 0.85),
    )


def partial_no_interrupt_override(semantic: SemanticResult, summary: str) -> SemanticResult:
    return SemanticResult(
        message_type="partial_answer",
        summary=summary,
        current_goal_satisfied=False,
        has_unresolved_interrupt=False,
        interrupt_type="none",
        facts=semantic.facts,
        retrieval_query="",
        retrieval_topics=[],
        evidence="manual reviewed partial stage answer",
        confidence=max(float(semantic.confidence or 0.0), 0.85),
    )


def normalize_manual_interrupt_taxonomy(semantic: SemanticResult, text: str) -> SemanticResult:
    if not semantic.has_unresolved_interrupt:
        return semantic
    if semantic.interrupt_type != "objection" and semantic.message_type != "objection":
        return semantic
    topic = semantic.interrupt_topic or (semantic.retrieval_topics[0] if semantic.retrieval_topics else None)
    strict_objection_topics = {
        "nudity_onlyfans",
        "no_experience",
        "new_sphere_uncertainty",
        "trust_concern",
        "suspicious_or_scam",
        "no_time",
    }
    if topic in strict_objection_topics:
        return semantic
    if topic == "english_level" and has_any(text, ("плохо", "беда", "слаб", "не знаю", "проблем")):
        return semantic
    if has_any(text, ("боюсь", "не хочу", "не буду", "сомневаюсь", "опасно", "скам", "развод")):
        return semantic
    message_type = "mixed" if semantic.message_type == "mixed" else "interrupt_question"
    return semantic.model_copy(update={"message_type": message_type, "interrupt_type": "question"})


def is_generic_unresolved(semantic: SemanticResult) -> bool:
    return semantic.has_unresolved_interrupt and not compact_topics([*semantic.retrieval_topics, semantic.interrupt_topic or ""])


def has_semantic_topics(semantic: SemanticResult) -> bool:
    return bool(compact_topics([*semantic.retrieval_topics, semantic.interrupt_topic or ""]))


def previous_prompt_matches_stage(stage: str, previous_prompt: str) -> bool:
    inferred = infer_stage_from_recruiter_messages([previous_prompt])
    return inferred == stage


def is_phone_or_equipment_chitchat(text: str, semantic: SemanticResult) -> bool:
    topics = set(compact_topics([*semantic.retrieval_topics, semantic.interrupt_topic or ""]))
    if not topics.intersection({"equipment", "phone_requirements"}):
        return False
    if "?" in text:
        return False
    if has_any(text, ("нужно", "надо", "обязательно", "нет компьютера", "нет ноута", "какая модель", "модель")):
        return False
    return has_any(text, ("айфон", "самсунг", "камера", "андроид", "вкусовщина", "советую", "нравил"))


def prioritize_manual_topics(topics: list[str], text: str) -> list[str]:
    normalized = normalize_text(text)
    priority: list[str] = []
    if has_any(normalized, ("подруг", "вместе", "вдвоем", "вдвоём")):
        priority.append("friend_streaming")
    if has_any(normalized, ("оборуд", "компьютер", "ноут", "камера", "свет", "микрофон")):
        priority.append("equipment")
    if has_any(normalized, ("критерии", "телефон", "айфон", "iphone", "samsung", "самсунг", "модель")):
        priority.append("phone_requirements")
    if has_any(normalized, ("оплат", "выплат", "карта", "деньги")):
        priority.append("payment_process")
    if has_any(normalized, ("договор", "гпх", "официаль")):
        priority.append("contract_gph")
    if has_any(normalized, ("узнают", "друзья", "знакомые", "аноним", "снг аудитори")):
        priority.append("privacy_anonymity")
    if has_any(normalized, ("паспорт", "личные документы", "личные данные", "вложен", "дата рождения")):
        priority.append("documents_privacy")
    if has_any(normalized, ("отработ", "отказаться", "передум", "на год", "невидимым текстом")):
        priority.append("exit_policy")
    if has_any(normalized, ("откуда", "нашла", "нашли", "контакт", "аккаунт")):
        priority.append("contact_source")
    if has_any(normalized, ("почему", "заинтересовала", "выбрали", "подошла")):
        priority.append("why_selected")

    result: list[str] = []
    for topic in [*priority, *topics]:
        if topic in CANONICAL_TOPICS and topic in topics and topic not in result:
            result.append(topic)
    return result[:8]


def is_positive_stage_answer(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE).strip()
    tokens = [token for token in cleaned.split() if token]
    if not tokens:
        return False
    positive_phrases = (
        "да",
        "давай",
        "хорошо",
        "слушаю",
        "расскажи",
        "супер",
        "отлично",
        "готова",
        "готов",
        "можно",
        "интересно",
        "попробовала",
        "попробовать",
        "подойдет",
        "подойдёт",
    )
    if len(tokens) <= 4 and has_any(cleaned, positive_phrases):
        return True
    return has_any(text, ("я согласна", "мне интересно", "мне это интересно", "это интересно", "готова послушать", "можем записаться", "да, давай"))


def is_no_more_questions_answer(text: str) -> bool:
    return has_any(
        text,
        (
            "вопросов нет",
            "нет",
            "пока нет",
            "больше вопросов",
            "вроде нет",
            "все понятно",
            "всё понятно",
            "по ходу разбер",
            "потом появ",
        ),
    )


def is_ack_or_short_neutral(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE).strip()
    tokens = [token for token in cleaned.split() if token]
    if not tokens or len(tokens) > 5:
        return False
    return has_any(cleaned, ("поняла", "понял", "понятно", "хорошо", "ясно", "ага", "ок", "окей", "супер", "отлично", "уже да", "очень"))


def looks_like_phone_model(text: str) -> bool:
    return bool(
        re.search(r"\b(1[1-9]|2[0-9])\s*(pro|max|про|промакс|про макс|прош)", text)
        or has_any(text, ("айфон", "iphone", "samsung", "самсунг", "android", "андроид", "последний прош"))
    )


def is_link_or_meeting_access_issue(text: str, previous_prompt: str) -> bool:
    if not has_any(text, ("не открывается", "не могу перейти", "не могу открыть", "ссылка не работает", "зум не работает")):
        return False
    return has_any(text + "\n" + previous_prompt, ("ссыл", "zoom", "зум", "appstore", "собесед"))


def is_timezone_offset_correction(text: str, previous_prompt: str) -> bool:
    return bool(re.fullmatch(r"на\s+\d{1,2}", text)) and has_any(previous_prompt, ("час", "моск", "мск", "время"))


def msk_daytime_short_hour(text: str) -> str | None:
    if not has_any(text, ("по москов", "по мск", "мск")):
        return None
    match = re.search(r"\b(?:в|на|к)\s+([1-6])\b", text)
    if not match:
        return None
    hour = int(match.group(1)) + 12
    return f"{hour:02d}:00"


def has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def has_all(text: str, markers: tuple[str, ...]) -> bool:
    return all(marker in text for marker in markers)


def extract_placeholder_name(text: str) -> str | None:
    cleaned = text.replace("Лид:", " ")
    cleaned = re.sub(r"\[номер\]|\+?\d[\d\s().-]{8,}\d", " ", cleaned, flags=re.IGNORECASE)
    words = re.findall(r"\b[А-ЯЁ][а-яё]{1,24}\b", cleaned)
    ignored = {"Привет", "Да", "Нет", "Хорошо", "Лид"}
    for word in words:
        if word not in ignored:
            return word
    return None


def knowledge_needs_for_topics(
    topics: list[str],
    retrieved: dict[str, Any],
    inbound: list[str],
    outbound: list[str],
) -> list[KnowledgeNeed]:
    by_topic: dict[str, KnowledgeNeed] = {}
    for topic in topics:
        if not topic:
            continue
        by_topic[topic] = KnowledgeNeed(
            topic=topic,
            kind="objection" if topic in {"nudity_onlyfans", "no_experience", "new_sphere_uncertainty", "trust_concern", "suspicious_or_scam", "no_time", "privacy_anonymity", "documents_privacy", "exit_policy"} else "faq",
            candidate_phrases=[shorten_text(message) for message in inbound],
            answer_facts=[],
            reply_fragments=[shorten_text(message) for message in outbound],
        )
    for item in list(retrieved.get("faq_context") or []) + list(retrieved.get("objection_context") or []):
        topic = str(item.get("topic") or "").strip()
        if not topic or topic not in by_topic:
            continue
        answer = str(item.get("answer") or item.get("content") or "").strip()
        if answer:
            append_unique(by_topic[topic].answer_facts, shorten_text(answer, limit=220))
    return list(by_topic.values())


def prompt_candidates_for_chunk(
    inbound: list[str],
    outbound: list[str],
    message_type: str,
    topics: list[str],
) -> list[PromptCandidate]:
    if not topics and message_type not in {"mixed", "partial_answer", "pause"}:
        return []
    inbound_text = " / ".join(shorten_text(item, limit=90) for item in inbound)
    outbound_text = " / ".join(shorten_text(item, limit=120) for item in outbound[:3])
    semantic_example = (
        f'message_batch=[{inbound_text}] -> message_type="{message_type}", '
        f"retrieval_topics={topics}, analyze as one semantic turn."
    )
    reply_example = (
        f"USER batch: {inbound_text}\n"
        f"Good reply shape: {outbound_text or 'answer the whole batch briefly and do not jump stage'}"
    )
    return [
        PromptCandidate(target_prompt="semantic_analyzer", example=semantic_example, reason="multi-message semantic chunk"),
        PromptCandidate(target_prompt="reply_orchestrator", example=reply_example, reason="tone and multi-message reply shape"),
    ]


def global_knowledge_needs_from_chunks(chunks: list[TurnChunkAnnotation]) -> list[KnowledgeNeed]:
    result: dict[str, KnowledgeNeed] = {}
    for chunk in chunks:
        for need in chunk.knowledge_needs:
            topic = need.topic
            target = result.setdefault(topic, KnowledgeNeed(topic=topic, kind=need.kind))
            for field in ("candidate_phrases", "answer_facts", "reply_fragments"):
                for value in getattr(need, field):
                    append_unique(getattr(target, field), value)
    return list(result.values())


def global_prompt_candidates_from_chunks(chunks: list[TurnChunkAnnotation]) -> list[PromptCandidate]:
    result: list[PromptCandidate] = []
    seen: set[str] = set()
    priority_topics = {
        "contact_source",
        "why_selected",
        "nudity_onlyfans",
        "equipment",
        "payment_process",
        "contract_gph",
        "english_level",
        "platform_info",
        "friend_streaming",
        "timezone",
        "no_experience",
        "privacy_anonymity",
        "documents_privacy",
        "exit_policy",
    }
    for chunk in chunks:
        if not priority_topics.intersection(chunk.knowledge_topics):
            continue
        for candidate in chunk.prompt_candidates:
            key = f"{candidate.target_prompt}:{candidate.example}"
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
            if len(result) >= 12:
                return result
    return result


def stage_episode_key(stage: str, topics: list[str]) -> str:
    if topics:
        return f"{stage}_{topics[0]}"
    return stage


def candidate_goal_for_topics(topics: list[str], stage: str) -> str:
    if topics:
        return "Уточнить: " + ", ".join(topics)
    return f"Ответить на активный вопрос стадии {stage}."


def reply_intent_for_semantic(message_type: str, topics: list[str]) -> str:
    if topics:
        return "answer_knowledge_topics:" + ",".join(topics)
    if message_type == "stage_answer":
        return "let_controller_advance"
    if message_type == "partial_answer":
        return "ask_missing_field"
    return "clarify_or_wait"


def redact_text(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\+?\d[\d\s().-]{8,}\d", "[номер]", text)
    text = re.sub(r"https?://\S+", "[ссылка]", text)
    return text


def shorten_text(value: str, *, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def speaker_runs(lines: list[DialogueLine]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    current_speaker: str | None = None
    current_messages: list[str] = []
    current_indexes: list[int] = []
    for line in lines:
        if line.speaker != current_speaker and current_messages:
            runs.append({"speaker": current_speaker, "messages": current_messages, "line_indexes": current_indexes})
            current_messages = []
            current_indexes = []
        current_speaker = line.speaker
        current_messages.append(line.text)
        current_indexes.append(line.line_index)
    if current_messages:
        runs.append({"speaker": current_speaker, "messages": current_messages, "line_indexes": current_indexes})
    return runs


def normalize_dialogue_id(value: str, index: int) -> str:
    cleaned = re.sub(r"^\d+[.)]\s*", "", value).strip()
    cleaned = re.sub(r"\s+", "_", cleaned.lower())
    cleaned = re.sub(r"[^0-9a-zа-яё_/-]+", "", cleaned, flags=re.IGNORECASE)
    return cleaned or f"dialogue_{index}"


def annotation_prompt() -> str:
    return """
Ты размечаешь реальные HR-переписки для LangGraph semantic/reply knowledge layer.

Главное: не размечай механически "1 строка лида -> 1 строка рекрутера".
Размечай по смысловым кускам:
- подряд идущие сообщения одного автора можно объединять, если это один смысл;
- turn_chunk может быть 3 входящих сообщения -> 2 исходящих, 1 -> 4, 4 -> 1 и так далее;
- expected_outbound_messages должны отражать все сообщения рекрутера, которые вместе отвечают на этот входящий смысл;
- если в одной стадии несколько turn_chunk уточняют один вопрос, объедини их сверху в один stage_episode;
- если кандидатка задает вопрос/сомнение/возражение, это interrupt и stage не закрывается, пока вопрос не обработан;
- не добавляй персональные данные в prompt_candidates.

Верни только JSON по схеме. Не используй markdown.
""".strip()


def annotation_contract() -> dict[str, Any]:
    return {
        "goal": "Segment dialogues into variable-size semantic chunks and stage episodes.",
        "turn_chunk_required_fields": [
            "turn_chunk_id",
            "inbound_messages",
            "expected_outbound_messages",
            "stage_before",
            "semantic_result_expected",
            "knowledge_topics",
            "reply_intent",
            "stage_after_expected",
            "chunk_boundary_reason",
        ],
        "stage_episode_required_fields": [
            "stage_episode_id",
            "stage_name",
            "turn_chunk_ids",
            "candidate_goal",
            "knowledge_needed",
            "working_reply_style",
            "prompt_examples",
        ],
        "semantic_message_types": [
            "empty",
            "stage_answer",
            "interrupt_question",
            "objection",
            "mixed",
            "partial_answer",
            "soft_refusal",
            "hard_refusal",
            "do_not_contact",
            "unclear",
            "pause",
        ],
    }


def normalize_annotation(dialogues: list[DialogueAnnotation], source_batch: list[dict[str, Any]]) -> list[DialogueAnnotation]:
    source_ids = {str(item.get("dialogue_id")) for item in source_batch}
    result: list[DialogueAnnotation] = []
    for dialogue in dialogues:
        if dialogue.dialogue_id not in source_ids and len(source_ids) == 1:
            dialogue = dialogue.model_copy(update={"dialogue_id": next(iter(source_ids))})
        chunks: list[TurnChunkAnnotation] = []
        for chunk_index, chunk in enumerate(dialogue.turn_chunks, start=1):
            chunk_id = chunk.turn_chunk_id or f"{dialogue.dialogue_id}_turn_{chunk_index}"
            topics = compact_topics([*chunk.knowledge_topics, *chunk.semantic_result_expected.retrieval_topics])
            semantic = chunk.semantic_result_expected.model_copy(update={"retrieval_topics": topics})
            chunks.append(chunk.model_copy(update={"turn_chunk_id": chunk_id, "knowledge_topics": topics, "semantic_result_expected": semantic}))
        result.append(dialogue.model_copy(update={"turn_chunks": chunks}))
    return result


def compact_topics(topics: list[str]) -> list[str]:
    result: list[str] = []
    for raw_topic in topics:
        topic = str(raw_topic or "").strip().lower().replace("-", "_").replace(" ", "_")
        if topic and topic not in GENERIC_TOPICS and topic not in result:
            result.append(topic)
    return result[:8]


def build_knowledge_suggestions(response: AnnotationResponse) -> dict[str, Any]:
    by_topic: dict[str, dict[str, Any]] = {}
    for dialogue in response.dialogues:
        for source in [*dialogue.global_knowledge_needs, *(need for chunk in dialogue.turn_chunks for need in chunk.knowledge_needs)]:
            topic = source.topic.strip().lower().replace("-", "_").replace(" ", "_")
            if not topic or topic in GENERIC_TOPICS:
                continue
            item = by_topic.setdefault(
                topic,
                {"topic": topic, "kinds": [], "candidate_phrases": [], "answer_facts": [], "reply_fragments": []},
            )
            append_unique(item["kinds"], source.kind)
            for key in ("candidate_phrases", "answer_facts", "reply_fragments"):
                for value in getattr(source, key):
                    append_unique(item[key], value)
    return {"topics": list(by_topic.values())}


def render_prompt_snippets(response: AnnotationResponse) -> str:
    lines = ["# Prompt Snippets From Reviewed Annotation", ""]
    for dialogue in response.dialogues:
        snippets = [*dialogue.global_prompt_candidates, *(candidate for chunk in dialogue.turn_chunks for candidate in chunk.prompt_candidates)]
        if not snippets:
            continue
        lines.extend([f"## {dialogue.dialogue_id}", ""])
        for snippet in snippets:
            lines.extend([f"### {snippet.target_prompt}", "", snippet.example.strip(), "", f"Reason: {snippet.reason}", ""])
    return "\n".join(lines).strip() + "\n"


def append_unique(values: list[str], value: str) -> None:
    cleaned = str(value or "").strip()
    if cleaned and cleaned not in values:
        values.append(cleaned)


def listify(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [str(value).strip()]


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except (BrainLLMError, ValidationError) as exc:
        raise SystemExit(f"Annotation failed: {exc}") from exc
