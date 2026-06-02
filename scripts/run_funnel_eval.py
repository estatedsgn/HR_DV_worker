from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.services.brain_v2.llm_provider import BrainLLMError
from app.services.funnel_graph.eval_runner import DEFAULT_EVAL_DIR, FunnelEvalConfig, FunnelEvalRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run synthetic candidate conversations against the LangGraph HR funnel."
    )
    parser.add_argument("--templates", type=Path, help="Path to JSON templates with seed conversations.")
    parser.add_argument("--runs", type=int, default=20, help="Number of conversations to generate.")
    parser.add_argument("--max-turns", type=int, default=30, help="Max candidate turns per conversation.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_EVAL_DIR, help="Directory for JSON and report.md.")
    parser.add_argument(
        "--no-agent-llm",
        action="store_true",
        help="Use deterministic agent orchestrator fallback instead of the configured LLM node.",
    )
    parser.add_argument(
        "--no-candidate-llm",
        action="store_true",
        help="Use deterministic candidate simulator instead of LLM-generated candidate replies.",
    )
    parser.add_argument(
        "--require-agent-llm",
        action="store_true",
        help="Fail if the agent LLM path cannot be used.",
    )
    parser.add_argument(
        "--require-candidate-llm",
        action="store_true",
        help="Fail if the candidate simulator LLM path cannot be used.",
    )
    parser.add_argument(
        "--candidate-provider",
        help="Override provider only for the LLM candidate simulator.",
    )
    parser.add_argument(
        "--candidate-model",
        help="Override model only for the LLM candidate simulator.",
    )
    parser.add_argument(
        "--skip-llm-preflight",
        action="store_true",
        help="Do not run a fail-fast LLM connectivity check before the conversations.",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        help="Override BRAIN_LLM_TIMEOUT_SECONDS for this eval run.",
    )
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        help="Override BRAIN_LLM_MAX_RETRIES for this eval run.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic variation choices.")
    parser.add_argument(
        "--with-quality-evaluator",
        action="store_true",
        help="Run a dialogue quality evaluator and include scores in report.md/quality_report.md.",
    )
    parser.add_argument(
        "--no-quality-evaluator-llm",
        action="store_true",
        help="Use deterministic quality scoring instead of the configured evaluator LLM.",
    )
    parser.add_argument(
        "--require-quality-evaluator-llm",
        action="store_true",
        help="Fail if the quality evaluator LLM cannot be used.",
    )
    parser.add_argument(
        "--quality-evaluator-provider",
        help="Override provider only for the quality evaluator.",
    )
    parser.add_argument(
        "--quality-evaluator-model",
        help="Override model only for the quality evaluator.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    config = FunnelEvalConfig(
        templates_path=args.templates,
        output_dir=args.output_dir,
        runs=args.runs,
        max_turns=args.max_turns,
        agent_llm=not args.no_agent_llm,
        candidate_llm=not args.no_candidate_llm,
        require_agent_llm=args.require_agent_llm,
        require_candidate_llm=args.require_candidate_llm,
        candidate_llm_provider=args.candidate_provider,
        candidate_llm_model=args.candidate_model,
        llm_preflight=not args.skip_llm_preflight,
        llm_timeout_seconds=args.llm_timeout_seconds,
        llm_max_retries=args.llm_max_retries,
        random_seed=args.seed,
        quality_evaluator=args.with_quality_evaluator,
        quality_evaluator_llm=not args.no_quality_evaluator_llm,
        require_quality_evaluator_llm=args.require_quality_evaluator_llm,
        quality_evaluator_provider=args.quality_evaluator_provider,
        quality_evaluator_model=args.quality_evaluator_model,
    )
    try:
        result = await FunnelEvalRunner(config=config).run()
    except BrainLLMError as exc:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": datetime.now(UTC).isoformat(),
            "error_type": "BrainLLMError",
            "error": str(exc),
            "config": {
                "templates_path": str(config.templates_path) if config.templates_path else None,
                "runs": config.runs,
                "max_turns": config.max_turns,
                "agent_llm": config.agent_llm,
                "candidate_llm": config.candidate_llm,
                "require_agent_llm": config.require_agent_llm,
                "require_candidate_llm": config.require_candidate_llm,
                "candidate_llm_provider": config.candidate_llm_provider,
                "candidate_llm_model": config.candidate_llm_model,
                "llm_timeout_seconds": config.llm_timeout_seconds,
                "llm_max_retries": config.llm_max_retries,
                "quality_evaluator": config.quality_evaluator,
                "quality_evaluator_llm": config.quality_evaluator_llm,
                "require_quality_evaluator_llm": config.require_quality_evaluator_llm,
                "quality_evaluator_provider": config.quality_evaluator_provider,
                "quality_evaluator_model": config.quality_evaluator_model,
            },
        }
        (config.output_dir / "api_error.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (config.output_dir / "api_error_report.md").write_text(
            render_api_error_report(payload),
            encoding="utf-8",
        )
        print(f"api_error={config.output_dir / 'api_error.json'}")
        print(f"api_error_report={config.output_dir / 'api_error_report.md'}")
        raise SystemExit(1) from exc
    summary = result["summary"]
    print(
        f"runs={summary['total_runs']} "
        f"human_handoff={summary.get('human_handoff', 0)} "
        f"ready_for_interview={summary.get('ready_for_interview', 0)} "
        f"errors={summary['errors']}"
    )
    print(f"json={config.output_dir / 'all_runs.json'}")
    print(f"report={config.output_dir / 'report.md'}")
    print(f"transcripts={config.output_dir / 'transcripts.md'}")
    print(f"turn_logs={config.output_dir / 'turn_logs.jsonl'}")
    if config.quality_evaluator:
        print(f"quality_report={config.output_dir / 'quality_report.md'}")


def render_api_error_report(payload: dict) -> str:
    return "\n".join(
        [
            "# Funnel Eval API Error",
            "",
            f"Created: {payload.get('created_at')}",
            "",
            f"Error type: `{payload.get('error_type')}`",
            "",
            "```text",
            str(payload.get("error") or ""),
            "```",
            "",
            "## Config",
            "",
            "```json",
            json.dumps(payload.get("config") or {}, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
