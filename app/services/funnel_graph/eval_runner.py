from __future__ import annotations

import json
import random
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.evaluator import DialogueQualityEvaluator
from app.services.funnel_graph.funnel_policy import ACTION_STAGE_TO_WAITING_STAGE, TERMINAL_STAGES
from app.services.funnel_graph.graph import build_funnel_graph
from app.services.funnel_graph.knowledge import PROJECT_ROOT
from app.services.funnel_graph.reply import ReplyOrchestrator
from app.services.funnel_graph.semantic import SemanticAnalyzer
from app.services.funnel_graph.repository import TerminalFunnelRepository
from app.services.funnel_graph.state import FunnelGraphState


DEFAULT_EVAL_DIR = PROJECT_ROOT / "runtime_logs" / "funnel_eval"


CANDIDATE_SIMULATOR_PROMPT = """Ты симулируешь кандидатку в переписке с HR-ботом.

Ты не оцениваешь бота и не объясняешь свои действия. Ты пишешь только следующее сообщение кандидата.

Требования:
- Пиши как обычная девушка в мессенджере: коротко, естественно, иногда неидеально.
- Следуй характеру и примерам из template.
- Иногда задавай вопросы и сомнения из template, чтобы проверить, возвращается ли бот к воронке.
- Если бот задал конкретный вопрос, обычно отвечай на него, но можешь сначала задать один уточняющий вопрос.
- Не пиши за бота.
- Не используй Markdown.
- Верни только JSON.

Формат:
{
  "text": "следующее сообщение кандидата",
  "intent": "answer | question | objection | pause | refuse | mixed",
  "rationale": "коротко почему выбрано это сообщение"
}
"""


class CandidateReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    intent: str = "answer"
    rationale: str = ""


class LLMPreflightReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ok: bool


class EvalTemplate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    persona: str = "Обычная девушка, осторожная, но в целом готова узнать условия."
    goal: str = "Проверить, доведёт ли агент кандидатку до записи на собеседование."
    seed_messages: list[str] = Field(default_factory=list)
    notes: str = ""


@dataclass(slots=True)
class FunnelEvalConfig:
    templates_path: Path | None = None
    output_dir: Path = DEFAULT_EVAL_DIR
    runs: int = 20
    max_turns: int = 30
    agent_llm: bool = True
    candidate_llm: bool = True
    require_agent_llm: bool = False
    require_candidate_llm: bool = False
    llm_preflight: bool = True
    llm_timeout_seconds: float | None = None
    llm_max_retries: int | None = None
    random_seed: int = 42
    quality_evaluator: bool = False
    quality_evaluator_llm: bool = True
    require_quality_evaluator_llm: bool = False


class CandidateSimulator:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        adapter: BrainLLMAdapter | None = None,
        use_llm: bool = True,
        require_llm: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.adapter = adapter or BrainLLMAdapter(settings=self.settings)
        self.use_llm = use_llm
        self.require_llm = require_llm

    async def next_reply(
        self,
        *,
        template: EvalTemplate,
        run_index: int,
        turn_index: int,
        transcript: list[dict[str, Any]],
        state: FunnelGraphState,
    ) -> CandidateReply:
        if self.use_llm:
            try:
                if not self.adapter.has_api_key("dialogue_brain"):
                    raise BrainLLMError("Missing API key for candidate simulator")
                payload = await self.adapter.complete_json(
                    component="dialogue_brain",
                    system_prompt=CANDIDATE_SIMULATOR_PROMPT,
                    user_payload={
                        "template": template.model_dump(),
                        "run_index": run_index,
                        "turn_index": turn_index,
                        "current_stage": state.get("stage"),
                        "candidate_profile": state.get("candidate_profile") or {},
                        "last_bot_messages": last_bot_texts(transcript),
                        "transcript": transcript[-16:],
                        "instruction": variation_instruction(run_index),
                    },
                    response_model=CandidateReply,
                )
                reply = CandidateReply.model_validate(payload)
                if reply.text.strip():
                    return reply
            except BrainLLMError:
                if self.require_llm:
                    raise
            except (ValueError, TypeError):
                if self.require_llm:
                    raise
        return deterministic_candidate_reply(
            template=template,
            run_index=run_index,
            turn_index=turn_index,
            transcript=transcript,
            state=state,
        )


class FunnelEvalRunner:
    def __init__(
        self,
        *,
        config: FunnelEvalConfig,
        settings: Settings | None = None,
        candidate_simulator: CandidateSimulator | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or get_settings()
        if config.llm_timeout_seconds is not None:
            self.settings.brain_llm_timeout_seconds = config.llm_timeout_seconds
        if config.llm_max_retries is not None:
            self.settings.brain_llm_max_retries = config.llm_max_retries
        self.candidate_simulator = candidate_simulator or CandidateSimulator(
            settings=self.settings,
            use_llm=config.candidate_llm,
            require_llm=config.require_candidate_llm,
        )
        self.quality_evaluator = (
            DialogueQualityEvaluator(
                settings=self.settings,
                use_llm=config.quality_evaluator_llm,
                require_llm=config.require_quality_evaluator_llm,
            )
            if config.quality_evaluator
            else None
        )
        self.repository = TerminalFunnelRepository(config.output_dir / "state")

    async def run(self) -> dict[str, Any]:
        random.seed(self.config.random_seed)
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        templates = load_eval_templates(self.config.templates_path)
        await self.preflight_llm_if_required()
        runs = []
        for run_index in range(1, self.config.runs + 1):
            template = templates[(run_index - 1) % len(templates)]
            runs.append(await self.run_one(template=template, run_index=run_index))
            payload = self.build_payload(runs, completed=False)
            self.write_outputs(payload)
        payload = self.build_payload(runs, completed=True)
        self.write_outputs(payload)
        return payload

    def build_payload(self, runs: list[dict[str, Any]], *, completed: bool) -> dict[str, Any]:
        return {
            "created_at": datetime.now(UTC).isoformat(),
            "completed": completed,
            "config": {
                "runs": self.config.runs,
                "completed_runs": len(runs),
                "max_turns": self.config.max_turns,
                "agent_llm": self.config.agent_llm,
                "candidate_llm": self.config.candidate_llm,
                "quality_evaluator": self.config.quality_evaluator,
                "quality_evaluator_llm": self.config.quality_evaluator_llm,
                "templates_path": str(self.config.templates_path) if self.config.templates_path else None,
            },
            "summary": build_eval_summary(runs),
            "runs": runs,
            "turn_logs_path": str(self.config.output_dir / "turn_logs.jsonl"),
        }

    def write_outputs(self, payload: dict[str, Any]) -> None:
        turn_logs = flatten_turn_logs(list(payload.get("runs") or []))
        (self.config.output_dir / "all_runs.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        with (self.config.output_dir / "turn_logs.jsonl").open("w", encoding="utf-8") as stream:
            for entry in turn_logs:
                stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        (self.config.output_dir / "transcripts.md").write_text(render_transcripts(payload), encoding="utf-8")
        (self.config.output_dir / "report.md").write_text(render_markdown_report(payload), encoding="utf-8")
        if self.config.quality_evaluator:
            (self.config.output_dir / "quality_report.md").write_text(render_quality_report(payload), encoding="utf-8")

    async def preflight_llm_if_required(self) -> None:
        if not self.config.llm_preflight:
            return
        if not (
            (self.config.agent_llm and self.config.require_agent_llm)
            or (self.config.candidate_llm and self.config.require_candidate_llm)
        ):
            return
        adapter = BrainLLMAdapter(settings=self.settings)
        llm_config = adapter.config_for("dialogue_brain", LLMPreflightReply)
        if not adapter.has_api_key("dialogue_brain"):
            raise BrainLLMError(f"LLM preflight failed: missing API key for provider {llm_config.provider}")
        try:
            await adapter.complete_json(
                component="dialogue_brain",
                system_prompt='Return exactly JSON matching the schema: {"ok": true}.',
                user_payload={"ping": "ok"},
                response_model=LLMPreflightReply,
            )
        except Exception as exc:
            raise BrainLLMError(
                "LLM preflight failed for "
                f"provider={llm_config.provider} model={llm_config.model} "
                f"timeout={llm_config.timeout_seconds}s retries={llm_config.retry_policy.max_retries}: {exc}"
            ) from exc

    async def run_one(self, *, template: EvalTemplate, run_index: int) -> dict[str, Any]:
        candidate_id = f"eval_{run_index:03d}_{slug(template.name)}"
        self.repository.reset(candidate_id)
        graph = build_funnel_graph(
            semantic_analyzer=SemanticAnalyzer(
                settings=self.settings,
                use_llm=self.config.agent_llm,
                fallback_on_llm_error=not self.config.require_agent_llm,
            ),
            reply_orchestrator=ReplyOrchestrator(
                settings=self.settings,
                use_llm=self.config.agent_llm,
                fallback_on_llm_error=not self.config.require_agent_llm,
            )
        ).compile(checkpointer=MemorySaver())

        state = self.repository.load_or_create(candidate_id)
        transcript: list[dict[str, Any]] = []
        state = await invoke_graph(graph, state)
        append_bot_messages(transcript, state)
        self.repository.save_turn(candidate_id, state, incoming_message=None)

        turns = []
        error: str | None = None
        for turn_index in range(1, self.config.max_turns + 1):
            if state.get("stage") in TERMINAL_STAGES:
                break
            try:
                candidate = await self.candidate_simulator.next_reply(
                    template=template,
                    run_index=run_index,
                    turn_index=turn_index,
                    transcript=transcript,
                    state=state,
                )
            except Exception as exc:
                error = f"candidate_simulator_error: {exc}"
                break
            candidate_text = candidate.text.strip()
            if not candidate_text:
                error = "candidate_simulator_empty_reply"
                break

            stage_before = state.get("stage")
            transcript.append(
                {
                    "role": "candidate",
                    "text": candidate_text,
                    "intent": candidate.intent,
                    "rationale": candidate.rationale,
                }
            )
            turn_state = {
                **state_for_next_turn(state),
                "incoming_message": candidate_text,
                "message_batch": [{"direction": "inbound", "sender_type": "lead", "body": candidate_text}],
            }
            state = await invoke_graph(graph, turn_state)
            agent_run = deepcopy(state.get("agent_run") or {})
            error_report = build_turn_error_report(state)
            append_bot_messages(transcript, state)
            self.repository.save_turn(candidate_id, state, incoming_message=candidate_text)
            turns.append(
                {
                    "turn": turn_index,
                    "candidate_text": candidate_text,
                    "candidate_intent": candidate.intent,
                    "stage_before": stage_before,
                    "stage_after": state.get("stage"),
                    "status_after": state.get("status"),
                    "bot_messages": bot_messages_for_state(state),
                    "profile": simplified_profile(state),
                    "orchestrator_summary": (
                        (state.get("orchestrator_result") or {}).get("understanding") or {}
                    ).get("summary"),
                    "parse_errors": state.get("parse_errors") or [],
                    "technical_log": build_technical_turn_log(
                        run_id=candidate_id,
                        template_name=template.name,
                        turn_index=turn_index,
                        candidate=candidate,
                        agent_run=agent_run,
                        state=state,
                        error_report=error_report,
                    ),
                    "error_report": error_report,
                }
            )

        run = {
            "run_id": candidate_id,
            "template": template.model_dump(),
            "final_stage": state.get("stage"),
            "final_status": state.get("status"),
            "final_profile": simplified_profile(state),
            "turn_count": len(turns),
            "error": error,
            "transcript": transcript,
            "turns": turns,
            "state_file": str(self.repository.state_path(candidate_id)),
        }
        if self.quality_evaluator is not None:
            run["quality_evaluation"] = (await self.quality_evaluator.evaluate(run)).model_dump()
        return run


async def invoke_graph(graph, state: FunnelGraphState) -> FunnelGraphState:
    return await graph.ainvoke(state, config={"configurable": {"thread_id": state["thread_id"]}})


def load_eval_templates(path: Path | None) -> list[EvalTemplate]:
    if path is None or not path.exists():
        return [default_template()]
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_templates = data.get("templates") if isinstance(data, dict) else data
    if not isinstance(raw_templates, list) or not raw_templates:
        return [default_template()]
    return [normalize_template(item, index) for index, item in enumerate(raw_templates, start=1)]


def normalize_template(item: Any, index: int) -> EvalTemplate:
    if isinstance(item, str):
        return EvalTemplate(name=f"template_{index}", seed_messages=[item])
    if not isinstance(item, dict):
        return EvalTemplate(name=f"template_{index}")
    messages = item.get("seed_messages") or item.get("messages") or []
    seed_messages = []
    for message in messages:
        if isinstance(message, str):
            seed_messages.append(message)
        elif isinstance(message, dict):
            role = str(message.get("role") or message.get("sender") or "").lower()
            text = str(message.get("text") or message.get("body") or "").strip()
            if text and role in {"user", "candidate", "lead", ""}:
                seed_messages.append(text)
    return EvalTemplate(
        name=str(item.get("name") or f"template_{index}"),
        persona=str(item.get("persona") or "Обычная девушка, осторожная, но готова узнать условия."),
        goal=str(item.get("goal") or "Проверить прохождение HR-воронки."),
        seed_messages=seed_messages,
        notes=str(item.get("notes") or ""),
    )


def default_template() -> EvalTemplate:
    return EvalTemplate(
        name="default_cautious_candidate",
        persona="Девушка 18-24, осторожная, спрашивает про безопасность, оплату и формат, но в целом открыта к диалогу.",
        seed_messages=[
            "А что за работа?",
            "Ну хорошо, расскажи",
            "18",
            "Это не OnlyFans?",
            "Хорошо, слушаю",
            "У меня есть оборудование, но телефон Samsung",
            "А как оплата?",
            "Пока вопросов нет",
            "Да, можно попробовать",
            "Учусь, подрабатываю, люблю рисовать",
            "Хорошо",
            "79999999999",
            "Диана",
            "Да",
            "В 17:00",
        ],
    )


def deterministic_candidate_reply(
    *,
    template: EvalTemplate,
    run_index: int,
    turn_index: int,
    transcript: list[dict[str, Any]],
    state: FunnelGraphState,
) -> CandidateReply:
    stage = str(state.get("stage") or "")
    used_candidate_texts = [item.get("text") for item in transcript if item.get("role") == "candidate"]
    scripted = next_unused_seed_for_stage(template.seed_messages, used_candidate_texts, stage)
    if scripted and should_use_seed(stage, turn_index):
        return CandidateReply(text=scripted, intent="scripted", rationale="template seed message")

    asked_source = any("откуда" in str(text).lower() for text in used_candidate_texts)
    asked_onlyfans = any("onlyfans" in str(text).lower() or "онлифанс" in str(text).lower() for text in used_candidate_texts)
    asked_payment = any("оплат" in str(text).lower() for text in used_candidate_texts)
    gave_equipment = any("оборуд" in str(text).lower() for text in used_candidate_texts)

    if stage == "interest_check":
        if not asked_source and run_index % 3 == 0:
            return CandidateReply(text="А откуда у вас мой контакт?", intent="question")
        return CandidateReply(text="Ну хорошо, расскажи", intent="answer")
    if stage == "age_check":
        return CandidateReply(text="18", intent="answer")
    if stage == "salary_schedule_offer":
        if not asked_onlyfans and run_index % 2 == 0:
            return CandidateReply(text="Это не OnlyFans и без оголёнки?", intent="objection")
        return CandidateReply(text="Хорошо, слушаю", intent="answer")
    if stage == "equipment_phone_check":
        if not gave_equipment and run_index % 2 == 1:
            return CandidateReply(text="У меня есть стриминговое оборудование", intent="answer")
        return CandidateReply(text="Samsung S25 Ultra", intent="answer")
    if stage == "post_equipment_questions_check":
        if not asked_payment:
            return CandidateReply(text="А как оплата?", intent="question")
        return CandidateReply(text="Пока вопросов нет", intent="answer")
    if stage == "try_interest_check":
        return CandidateReply(text="Да, я бы попробовала", intent="answer")
    if stage == "profile_theme_check":
        if run_index % 5 == 0 and not any("через час" in str(text).lower() for text in used_candidate_texts):
            return CandidateReply(text="Я отвечу через час, сейчас занята", intent="pause")
        return CandidateReply(text="Учусь, работаю, люблю рисовать", intent="answer")
    if stage == "room_available_check":
        return CandidateReply(text="Да, есть отдельная комната", intent="answer")
    if stage == "interview_offer":
        return CandidateReply(text="Хорошо", intent="answer")
    if stage == "contact_collection":
        profile = state.get("candidate_profile") or {}
        if not profile.get("phone_number") and not profile.get("candidate_name") and run_index % 2 == 0:
            return CandidateReply(text="Diana, 79999999999", intent="answer")
        if not profile.get("phone_number"):
            return CandidateReply(text="79999999999", intent="answer")
        return CandidateReply(text="Diana", intent="answer")
    if stage == "interview_day_check":
        return CandidateReply(text="Да", intent="answer")
    if stage == "interview_time_check":
        return CandidateReply(text="примерно в 17.00", intent="answer")
    if stage == "interview_custom_time":
        return CandidateReply(text="Послезавтра в 13:00", intent="answer")
    return CandidateReply(text="Хорошо", intent="answer")


def next_unused_seed(seed_messages: list[str], used_texts: list[str | None]) -> str | None:
    used = {str(text) for text in used_texts if text}
    for message in seed_messages:
        if message not in used:
            return message
    return None


def next_unused_seed_for_stage(seed_messages: list[str], used_texts: list[str | None], stage: str) -> str | None:
    used = {str(text) for text in used_texts if text}
    for message in seed_messages:
        if message in used:
            continue
        if seed_message_matches_stage(message, stage):
            return message
    return None


def seed_message_matches_stage(message: str, stage: str) -> bool:
    text = message.strip().lower()
    if not text:
        return False
    if stage == "interest_check":
        return any(marker in text for marker in ["привет", "расскажи", "подроб", "контакт", "аккаунт", "откуда"])
    if stage == "age_check":
        return text.isdigit() and 18 <= int(text) <= 60
    if stage == "salary_schedule_offer":
        return any(marker in text for marker in ["слушаю", "давай", "интерес", "onlyfans", "онлифанс", "огол"])
    if stage == "equipment_phone_check":
        return any(
            marker in text
            for marker in ["оборуд", "телефон", "айфон", "iphone", "samsung", "самсунг", "xiaomi", "сяоми"]
        )
    if stage == "post_equipment_questions_check":
        return any(marker in text for marker in ["оплат", "вопрос", "понятно", "нет", "договор", "график"])
    if stage == "try_interest_check":
        return any(marker in text for marker in ["попроб", "да", "соглас"])
    if stage == "profile_theme_check":
        return any(marker in text for marker in ["учусь", "работ", "люблю", "свобод", "хобби", "занята", "через час"])
    if stage == "room_available_check":
        return any(marker in text for marker in ["комната", "место", "никто", "помеш", "да", "есть", "нет"])
    if stage == "interview_offer":
        return any(marker in text for marker in ["хорошо", "давай", "супер", "можем", "да"])
    if stage == "contact_collection":
        return any(char.isdigit() for char in text) or text in {"диана", "арина", "тарина", "рената", "настя"}
    if stage == "interview_day_check":
        return any(marker in text for marker in ["да", "завтра", "послезавтра", "21", "нет"])
    if stage == "interview_time_check":
        return any(marker in text for marker in ["17", "13", "14", "15", "11", ":00", ".00"])
    if stage == "interview_custom_time":
        return any(marker in text for marker in ["послезавтра", "завтра", "21", "17", "13", "14", "15", ":00", ".00"])
    return False


def should_use_seed(stage: str, turn_index: int) -> bool:
    if turn_index <= 2:
        return True
    return stage in {"post_equipment_questions_check", "profile_theme_check"} and turn_index % 2 == 1


def append_bot_messages(transcript: list[dict[str, Any]], state: FunnelGraphState) -> None:
    for message in bot_messages_for_state(state):
        transcript.append({"role": "bot", **message})


def bot_messages_for_state(state: FunnelGraphState) -> list[dict[str, str]]:
    messages = []
    if not state.get("send_reply", True):
        return messages
    for message in state.get("outgoing_messages") or []:
        if message.get("type") == "voice_pack":
            messages.append({"type": "voice_pack", "text": f"[voice_pack: {message.get('voice_pack_id')}]"})
        elif message.get("text"):
            messages.append({"type": "text", "text": str(message["text"])})
    return messages


def last_bot_texts(transcript: list[dict[str, Any]]) -> list[str]:
    texts = [str(item.get("text") or "") for item in transcript if item.get("role") == "bot"]
    return texts[-4:]


def state_for_next_turn(previous: FunnelGraphState) -> FunnelGraphState:
    return {
        "candidate_id": previous["candidate_id"],
        "lead_id": previous["lead_id"],
        "dialog_id": previous["dialog_id"],
        "thread_id": previous["thread_id"],
        "stage": previous.get("stage") or "interest_check",
        "status": previous.get("status") or "active",
        "candidate_profile": dict(previous.get("candidate_profile") or {}),
        "slots": dict(previous.get("candidate_profile") or {}),
        "sent_voice_packs": list(previous.get("sent_voice_packs") or []),
        "sent_templates": list(previous.get("sent_templates") or []),
        "recent_messages": list(previous.get("recent_messages") or []),
        "message_batch": [],
        "metadata": dict(previous.get("metadata") or {}),
    }


def simplified_profile(state: FunnelGraphState) -> dict[str, Any]:
    profile = dict(state.get("candidate_profile") or {})
    return {key: value for key, value in profile.items() if value is not None and value != "" and value != []}


def build_eval_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    final_stage_counts: dict[str, int] = {}
    errors = 0
    quality_scores: list[float] = []
    quality_passed = 0
    quality_high_or_critical_issues = 0
    for run in runs:
        stage = str(run.get("final_stage") or "unknown")
        final_stage_counts[stage] = final_stage_counts.get(stage, 0) + 1
        if run.get("error"):
            errors += 1
        evaluation = run.get("quality_evaluation") or {}
        if evaluation:
            try:
                quality_scores.append(float(evaluation.get("overall_score")))
            except (TypeError, ValueError):
                pass
            if evaluation.get("passed"):
                quality_passed += 1
            for issue in evaluation.get("issues") or []:
                if str(issue.get("severity") or "") in {"high", "critical"}:
                    quality_high_or_critical_issues += 1
    summary = {
        "total_runs": len(runs),
        "final_stage_counts": final_stage_counts,
        "errors": errors,
        "ready_for_interview": final_stage_counts.get("ready_for_interview", 0),
        "human_handoff": final_stage_counts.get("human_handoff", 0),
    }
    if quality_scores:
        summary["quality_average_score"] = round(sum(quality_scores) / len(quality_scores), 2)
        summary["quality_passed"] = quality_passed
        summary["quality_high_or_critical_issues"] = quality_high_or_critical_issues
    return summary


def flatten_turn_logs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    logs = []
    for run in runs:
        for turn in run.get("turns") or []:
            technical_log = turn.get("technical_log")
            if technical_log:
                logs.append(technical_log)
    return logs


def build_technical_turn_log(
    *,
    run_id: str,
    template_name: str,
    turn_index: int,
    candidate: CandidateReply,
    agent_run: dict[str, Any],
    state: FunnelGraphState,
    error_report: dict[str, Any],
) -> dict[str, Any]:
    outgoing_messages = deepcopy(agent_run.get("outgoing_messages") or state.get("outgoing_messages") or [])
    pending_actions = deepcopy(agent_run.get("pending_actions") or state.get("pending_actions") or [])
    return {
        "run_id": run_id,
        "template": template_name,
        "turn": turn_index,
        "input": {
            "candidate_text": candidate.text,
            "candidate_intent": candidate.intent,
            "candidate_rationale": candidate.rationale,
        },
        "stage_before": agent_run.get("stage_before"),
        "state_before": deepcopy(agent_run.get("state_before") or {}),
        "retrieved_knowledge": deepcopy(agent_run.get("retrieved_knowledge") or {}),
        "llm_result": deepcopy(agent_run.get("orchestrator_result") or state.get("orchestrator_result") or {}),
        "semantic_result": deepcopy(agent_run.get("semantic_result") or state.get("semantic_result") or {}),
        "reply_result": deepcopy(agent_run.get("reply_result") or state.get("reply_result") or {}),
        "controller_decision": deepcopy(agent_run.get("controller_decision") or state.get("controller_decision") or {}),
        "state_after": deepcopy(agent_run.get("state_after") or {}),
        "sent_messages": outgoing_messages,
        "pending_actions": pending_actions,
        "parse_errors": deepcopy(agent_run.get("parse_errors") or state.get("parse_errors") or []),
        "error_report": error_report,
    }


def build_turn_error_report(state: FunnelGraphState) -> dict[str, Any]:
    metadata = dict(state.get("metadata") or {})
    orchestrator_result = dict(state.get("orchestrator_result") or {})
    outgoing_messages = list(state.get("outgoing_messages") or [])
    errors = []
    warnings = []

    parse_errors = list(state.get("parse_errors") or [])
    if parse_errors:
        errors.append({"type": "parse_or_runtime_error", "details": parse_errors})

    invalid_transition = metadata.get("controller_invalid_transition")
    if invalid_transition:
        errors.append({"type": "invalid_transition", "details": invalid_transition})

    if state.get("send_reply", True) and not outgoing_messages and state.get("stage") not in TERMINAL_STAGES:
        warnings.append({"type": "empty_bot_reply", "details": "send_reply=true but outgoing_messages is empty"})

    transition = dict(orchestrator_result.get("transition") or {})
    controller = dict(state.get("controller_decision") or {})
    target_stage = transition.get("target_stage") or controller.get("target_stage")
    if target_stage and target_stage != state.get("stage") and not is_expected_action_stage_hop(str(target_stage), str(state.get("stage") or "")):
        warnings.append(
            {
                "type": "llm_target_stage_adjusted",
                "details": {
                    "llm_target_stage": target_stage,
                    "actual_stage": state.get("stage"),
                },
            }
        )

    return {
        "has_errors": bool(errors),
        "has_warnings": bool(warnings),
        "errors": errors,
        "warnings": warnings,
    }


def is_expected_action_stage_hop(llm_target_stage: str, actual_stage: str) -> bool:
    if ACTION_STAGE_TO_WAITING_STAGE.get(llm_target_stage) == actual_stage:
        return True
    legacy_hops = {
        ("salary_schedule_delivery", "equipment_phone_check"),
        ("company_intro", "try_interest_check"),
        ("work_intro_delivery", "salary_schedule_offer"),
    }
    return (llm_target_stage, actual_stage) in legacy_hops


def render_markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Funnel Eval Report",
        "",
        f"Created: {payload.get('created_at')}",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(payload.get("summary") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    for run in payload.get("runs") or []:
        lines.extend(
            [
                f"## {run['run_id']}",
                "",
                f"Template: `{run['template']['name']}`",
                f"Final stage: `{run.get('final_stage')}`",
                f"Turns: `{run.get('turn_count')}`",
                f"Error: `{run.get('error')}`",
                "",
                "### Transcript",
                "",
            ]
        )
        for item in run.get("transcript") or []:
            role = "BOT" if item.get("role") == "bot" else "USER"
            lines.append(f"{role}: {item.get('text')}")
        lines.extend(["", "### Turn States", ""])
        for turn in run.get("turns") or []:
            lines.append(
                f"- {turn['turn']}: `{turn['stage_before']}` -> `{turn['stage_after']}`; "
                f"profile={json.dumps(turn.get('profile') or {}, ensure_ascii=False)}"
            )
        evaluation = run.get("quality_evaluation")
        if evaluation:
            lines.extend(
                [
                    "",
                    "### Quality Evaluation",
                    "",
                    f"Overall: `{evaluation.get('overall_score')}`; passed: `{evaluation.get('passed')}`",
                    "",
                    "```json",
                    json.dumps(evaluation, ensure_ascii=False, indent=2, default=str),
                    "```",
                    "",
                ]
            )
        lines.extend(["", "### Technical Logs", ""])
        for turn in run.get("turns") or []:
            log = turn.get("technical_log") or {}
            lines.extend(
                [
                    f"#### Turn {turn['turn']}",
                    "",
                    "```json",
                    json.dumps(
                        {
                            "input": log.get("input"),
                            "state_before": log.get("state_before"),
                            "semantic_result": log.get("semantic_result"),
                            "retrieved_knowledge": log.get("retrieved_knowledge"),
                            "reply_result": log.get("reply_result"),
                            "controller_decision": log.get("controller_decision"),
                            "state_after": log.get("state_after"),
                            "sent_messages": log.get("sent_messages"),
                            "pending_actions": log.get("pending_actions"),
                            "error_report": log.get("error_report"),
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    "```",
                    "",
                ]
            )
        lines.append("")
    return "\n".join(lines)


def render_quality_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Funnel Quality Report",
        "",
        f"Created: {payload.get('created_at')}",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(payload.get("summary") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    for run in payload.get("runs") or []:
        evaluation = run.get("quality_evaluation") or {}
        lines.extend(
            [
                f"## {run.get('run_id')}",
                "",
                f"Final stage: `{run.get('final_stage')}`",
                f"Overall score: `{evaluation.get('overall_score')}`",
                f"Passed: `{evaluation.get('passed')}`",
                "",
                "### Criteria",
                "",
            ]
        )
        for item in evaluation.get("criteria") or []:
            lines.append(f"- `{item.get('name')}`: {item.get('score')}/10 - {item.get('comment')}")
        lines.extend(["", "### Issues", ""])
        issues = evaluation.get("issues") or []
        if not issues:
            lines.append("- No issues.")
        for item in issues:
            turn = item.get("turn")
            turn_text = f"turn {turn}" if turn is not None else "run"
            lines.append(
                f"- `{item.get('severity')}` `{item.get('category')}` ({turn_text}): "
                f"{item.get('evidence')} Recommendation: {item.get('recommendation')}"
            )
        lines.extend(["", "### Recommendations", ""])
        recommendations = evaluation.get("recommendations") or []
        if not recommendations:
            lines.append("- No recommendations.")
        for recommendation in recommendations:
            lines.append(f"- {recommendation}")
        lines.append("")
    return "\n".join(lines)


def render_transcripts(payload: dict[str, Any]) -> str:
    lines = [
        "# Funnel Eval Transcripts",
        "",
        f"Created: {payload.get('created_at')}",
        "",
    ]
    for run in payload.get("runs") or []:
        lines.extend(
            [
                f"## {run['run_id']}",
                "",
                f"Template: `{run['template']['name']}`",
                f"Final stage: `{run.get('final_stage')}`",
                f"Error: `{run.get('error')}`",
                "",
            ]
        )
        for item in run.get("transcript") or []:
            role = "BOT" if item.get("role") == "bot" else "USER"
            text = str(item.get("text") or "").replace("\n", "\n  ")
            lines.append(f"{role}: {text}")
        lines.append("")
    return "\n".join(lines)


def variation_instruction(run_index: int) -> str:
    variants = [
        "Будь осторожной: спроси источник контакта или безопасность, но не срывай диалог.",
        "Будь заинтересованной и отвечай коротко, иногда проси уточнить условия.",
        "Задай вопрос про оплату/договор после технического блока.",
        "На этапе профиля сначала скажи, что ответишь позже, потом продолжи.",
        "Дай частичный контакт: сначала номер, потом имя.",
    ]
    return variants[(run_index - 1) % len(variants)]


def slug(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "_" for char in value)
    return "_".join(part for part in cleaned.split("_") if part)[:60] or "template"
