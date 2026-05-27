from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.knowledge import PROJECT_ROOT
from app.services.funnel_graph.semantic import is_recoverable_llm_format_error


PROMPT_PATH = PROJECT_ROOT / "prompts" / "funnel_quality_evaluator.md"


class QualityCriterionScore(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    score: int = Field(ge=0, le=10)
    comment: str = ""


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    severity: Literal["low", "medium", "high", "critical"] = "low"
    turn: int | None = None
    category: str = ""
    evidence: str = ""
    recommendation: str = ""


class DialogueQualityEvaluation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    overall_score: float = Field(ge=0, le=10)
    passed: bool = False
    final_stage_ok: bool = False
    criteria: list[QualityCriterionScore] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    issues: list[QualityIssue] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.0)


class DialogueQualityEvaluator:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        adapter: BrainLLMAdapter | None = None,
        prompt_path: Path | None = None,
        use_llm: bool = True,
        require_llm: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.adapter = adapter or BrainLLMAdapter(settings=self.settings)
        self.prompt_path = prompt_path or PROMPT_PATH
        self.use_llm = use_llm
        self.require_llm = require_llm

    async def evaluate(self, run: dict[str, Any]) -> DialogueQualityEvaluation:
        if self.use_llm:
            try:
                if not self.adapter.has_api_key("validator"):
                    raise BrainLLMError("Missing API key for quality evaluator")
                payload = await self.adapter.complete_json(
                    component="validator",
                    system_prompt=self.prompt_path.read_text(encoding="utf-8"),
                    user_payload=quality_payload(run),
                    response_model=DialogueQualityEvaluation,
                )
                return normalize_evaluation(DialogueQualityEvaluation.model_validate(payload))
            except BrainLLMError as exc:
                if self.require_llm and not is_recoverable_llm_format_error(exc):
                    raise
            except (ValidationError, ValueError, TypeError):
                if self.require_llm:
                    raise
        return heuristic_quality_evaluation(run)


def quality_payload(run: dict[str, Any]) -> dict[str, Any]:
    template = dict(run.get("template") or {})
    template.pop("seed_messages", None)
    turns = []
    for turn in run.get("turns") or []:
        technical = dict(turn.get("technical_log") or {})
        semantic = dict(technical.get("semantic_result") or turn.get("semantic_result") or {})
        turns.append(
            {
                "turn": turn.get("turn"),
                "stage_before": turn.get("stage_before"),
                "stage_after": turn.get("stage_after"),
                "candidate_text": turn.get("candidate_text"),
                "bot_messages": turn.get("bot_messages") or [],
                "semantic": {
                    "message_type": semantic.get("message_type"),
                    "current_goal_satisfied": semantic.get("current_goal_satisfied"),
                    "has_unresolved_interrupt": semantic.get("has_unresolved_interrupt"),
                    "interrupt_type": semantic.get("interrupt_type"),
                    "interrupt_topic": semantic.get("interrupt_topic"),
                    "retrieval_topics": semantic.get("retrieval_topics"),
                },
                "controller_decision": technical.get("controller_decision") or {},
                "error_report": turn.get("error_report") or technical.get("error_report") or {},
            }
        )
    return {
        "run_id": run.get("run_id"),
        "template": template,
        "final_stage": run.get("final_stage"),
        "final_status": run.get("final_status"),
        "final_profile": run.get("final_profile") or {},
        "transcript": run.get("transcript") or [],
        "turns": turns,
    }


def heuristic_quality_evaluation(run: dict[str, Any]) -> DialogueQualityEvaluation:
    turns = list(run.get("turns") or [])
    issues: list[QualityIssue] = []
    strengths: list[str] = []
    final_profile = dict(run.get("final_profile") or {})
    final_stage = str(run.get("final_stage") or "")
    final_stage_ok = final_stage in {"human_handoff", "ready_for_interview"}

    if final_stage_ok:
        strengths.append("Воронка дошла до передачи человеку/финального этапа.")
    else:
        issues.append(issue("high", None, "final_stage", f"Диалог завершился на {final_stage}.", "Проверить блокирующий turn и required fields."))

    technical_errors = []
    technical_warnings = []
    repeated_current_question = 0
    stale_contact_reply = False
    for turn in turns:
        report = turn.get("error_report") or {}
        if report.get("has_errors"):
            technical_errors.append(turn.get("turn"))
        if report.get("has_warnings"):
            technical_warnings.append(turn.get("turn"))
        bot_text = "\n".join(str(message.get("text") or "") for message in turn.get("bot_messages") or [])
        if "Остались ли у тебя какие-нибудь ещё вопросы?" in bot_text:
            repeated_current_question += 1
        if "Осталось только номер" in bot_text or "Остался только номер" in bot_text:
            stale_contact_reply = True

    if technical_errors:
        issues.append(issue("high", technical_errors[0], "technical_errors", f"Есть technical errors на turns: {technical_errors}.", "Разобрать parse_errors/error_report."))
    if technical_warnings:
        issues.append(issue("medium", technical_warnings[0], "technical_warnings", f"Есть warnings на turns: {technical_warnings}.", "Проверить warnings в technical log."))
    if repeated_current_question > 3:
        issues.append(issue("medium", None, "repetition", "Вопрос про оставшиеся вопросы повторяется слишком часто.", "Делать более мягкий возврат после нескольких FAQ-вопросов."))
    if stale_contact_reply:
        issues.append(issue("high", None, "stale_reply", "Бот попросил номер, когда номер уже был в профиле.", "Не давать reply-LLM отвечать на закрытый partial answer."))

    required = {
        "age": final_profile.get("age") or final_profile.get("age_confirmed"),
        "phone_model": final_profile.get("phone_model"),
        "room_available": final_profile.get("room_available"),
        "candidate_name": final_profile.get("candidate_name"),
        "phone_number": final_profile.get("phone_number"),
        "interview_time": final_profile.get("interview_time") or final_profile.get("custom_interview_datetime"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        issues.append(issue("high", None, "missing_required_data", f"Не собраны поля: {', '.join(missing)}.", "Не переводить к handoff без полной квалификации."))
    else:
        strengths.append("Ключевые поля квалификации и записи собраны.")

    criteria = [
        criterion("transition_safety", score_from_issues(issues, {"final_stage", "missing_required_data", "technical_errors"}), "Проверка переходов и required fields."),
        criterion("interrupt_handling", 8 if repeated_current_question <= 3 else 6, "Ответы на вопросы с возвратом к текущей цели."),
        criterion("knowledge_grounding", 8, "Ответы строятся по найденным FAQ/objections; неизвестные темы требуют ручной проверки."),
        criterion("naturalness", max(5, 9 - repeated_current_question), "Живость, отсутствие механических повторов и противоречий."),
        criterion("data_collection", 10 if not missing else 6, "Сбор возраста, комнаты, телефона, имени, номера и времени."),
        criterion("technical_health", 10 if not technical_errors and not technical_warnings else (6 if not technical_errors else 3), "Ошибки, warnings, parse_errors."),
    ]
    overall = round(sum(item.score for item in criteria) / len(criteria), 1)
    return normalize_evaluation(
        DialogueQualityEvaluation(
            overall_score=overall,
            passed=overall >= 8 and final_stage_ok and not any(item.severity in {"high", "critical"} for item in issues),
            final_stage_ok=final_stage_ok,
            criteria=criteria,
            strengths=strengths,
            issues=issues,
            recommendations=[item.recommendation for item in issues if item.recommendation],
            confidence=0.72,
        )
    )


def normalize_evaluation(evaluation: DialogueQualityEvaluation) -> DialogueQualityEvaluation:
    if not evaluation.criteria:
        evaluation.criteria = [criterion("overall", int(round(evaluation.overall_score)), "Общая оценка.")]
    if not evaluation.recommendations:
        evaluation.recommendations = [item.recommendation for item in evaluation.issues if item.recommendation]
    return evaluation


def criterion(name: str, score: int, comment: str) -> QualityCriterionScore:
    return QualityCriterionScore(name=name, score=max(0, min(10, int(score))), comment=comment)


def issue(severity: str, turn: int | None, category: str, evidence: str, recommendation: str) -> QualityIssue:
    return QualityIssue(
        severity=severity,  # type: ignore[arg-type]
        turn=turn,
        category=category,
        evidence=evidence,
        recommendation=recommendation,
    )


def score_from_issues(issues: list[QualityIssue], categories: set[str]) -> int:
    score = 10
    for item in issues:
        if item.category not in categories:
            continue
        score -= {"low": 1, "medium": 2, "high": 4, "critical": 7}.get(item.severity, 1)
    return max(0, score)


def evaluation_to_json(evaluation: DialogueQualityEvaluation) -> str:
    return json.dumps(evaluation.model_dump(), ensure_ascii=False, indent=2, default=str)
