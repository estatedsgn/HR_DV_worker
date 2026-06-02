import asyncio
import uuid
from pathlib import Path

from app.services.funnel_graph.eval_runner import (
    EvalTemplate,
    FunnelEvalConfig,
    FunnelEvalRunner,
    build_turn_error_report,
    deterministic_candidate_reply,
    normalize_template,
)


def test_funnel_eval_runner_writes_report_without_llm() -> None:
    output_dir = Path("runtime_logs") / f"test_funnel_eval_runner_{uuid.uuid4().hex}"
    config = FunnelEvalConfig(
        output_dir=output_dir,
        runs=1,
        max_turns=20,
        agent_llm=False,
        candidate_llm=False,
    )

    result = asyncio.run(FunnelEvalRunner(config=config).run())

    assert result["summary"]["total_runs"] == 1
    assert result["runs"][0]["final_stage"] == "human_handoff"
    assert (output_dir / "all_runs.json").exists()
    assert (output_dir / "report.md").exists()
    assert (output_dir / "turn_logs.jsonl").exists()
    assert (output_dir / "transcripts.md").exists()
    first_turn = result["runs"][0]["turns"][0]
    assert first_turn["technical_log"]["input"]["candidate_text"]
    assert first_turn["technical_log"]["state_before"]["stage"]
    assert first_turn["technical_log"]["semantic_result"]["message_type"]
    assert first_turn["technical_log"]["controller_decision"]["target_stage"]
    assert first_turn["technical_log"]["state_after"]["stage"]
    assert "error_report" in first_turn["technical_log"]


def test_funnel_eval_runner_writes_quality_report_without_llm() -> None:
    output_dir = Path("runtime_logs") / f"test_funnel_eval_runner_quality_{uuid.uuid4().hex}"
    config = FunnelEvalConfig(
        output_dir=output_dir,
        runs=1,
        max_turns=20,
        agent_llm=False,
        candidate_llm=False,
        quality_evaluator=True,
        quality_evaluator_llm=False,
    )

    result = asyncio.run(FunnelEvalRunner(config=config).run())

    evaluation = result["runs"][0]["quality_evaluation"]
    assert evaluation["overall_score"] >= 0
    assert evaluation["criteria"]
    assert "quality_average_score" in result["summary"]
    assert (output_dir / "quality_report.md").exists()


def test_eval_template_normalizes_seed_messages() -> None:
    template = normalize_template(
        {
            "name": "sample",
            "messages": [
                {"role": "candidate", "text": "А что за работа?"},
                {"role": "bot", "text": "ignored"},
                "Ну хорошо",
            ],
        },
        1,
    )

    assert template.name == "sample"
    assert template.seed_messages == ["А что за работа?", "Ну хорошо"]


def test_deterministic_candidate_uses_only_stage_matching_seed() -> None:
    reply = deterministic_candidate_reply(
        template=EvalTemplate(name="sample", seed_messages=["19", "А как оплата?", "Пока вопросов нет"]),
        run_index=1,
        turn_index=7,
        transcript=[],
        state={"stage": "post_equipment_questions_check", "candidate_profile": {}},
    )

    assert reply.text == "А как оплата?"


def test_expected_action_stage_hop_is_not_reported_as_warning() -> None:
    error_report = build_turn_error_report(
        {
            "stage": "equipment_phone_check",
            "send_reply": True,
            "outgoing_messages": [{"type": "voice_pack", "voice_pack_id": "salary_schedule"}],
            "metadata": {},
            "parse_errors": [],
            "orchestrator_result": {
                "transition": {
                    "target_stage": "salary_schedule_delivery",
                }
            },
        }
    )

    assert error_report["has_warnings"] is False

