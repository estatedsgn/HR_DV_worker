from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.eval_runner import (
    CandidateReply,
    CandidateSimulator,
    EvalTemplate,
    FunnelEvalConfig,
    FunnelEvalRunner,
    next_unused_seed_for_stage,
)
from app.services.funnel_graph.state import FunnelGraphState


GUIDED_CANDIDATE_PROMPT = """Ты симулируешь кандидатку в HR-переписке.

Пиши только следующее сообщение кандидатки.

Важное правило: используй next_seed_message как основу ответа. Можно слегка переформулировать живым языком, но не добавляй новые большие темы, если они не нужны.

Если next_seed_message пустой, ответь естественно на последний вопрос бота и помогай диалогу двигаться дальше.

Если бот просит имя и номер, дай реалистичные имя и номер в одном сообщении.
Если бот просит день собеседования, выбери завтра или послезавтра.
Если бот просит время, выбери время в диапазоне 11:00-18:00.
Если бот просит модель телефона, назови конкретную модель.

Стиль: коротко, как обычная переписка в Telegram. Без markdown. Не пиши за бота.

Верни строго JSON:
{
  "text": "сообщение кандидатки",
  "intent": "answer | question | objection | pause | refuse | mixed",
  "rationale": "почему так ответила"
}
"""


class GuidedQwenCandidateSimulator(CandidateSimulator):
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        adapter: BrainLLMAdapter | None = None,
        require_llm: bool = True,
    ) -> None:
        super().__init__(settings=settings, adapter=adapter, use_llm=True, require_llm=require_llm)
        self._seed_offsets: dict[str, int] = {}

    async def next_reply(
        self,
        *,
        template: EvalTemplate,
        run_index: int,
        turn_index: int,
        transcript: list[dict[str, Any]],
        state: FunnelGraphState,
    ) -> CandidateReply:
        used_candidate_texts = [item.get("text") for item in transcript if item.get("role") == "candidate"]
        stage = str(state.get("stage") or "")
        offset_key = f"{run_index}:{template.name}"
        next_seed, next_seed_index = self._next_seed(template.seed_messages, stage, offset_key)
        payload = await self.adapter.complete_json(
            component="dialogue_brain",
            system_prompt=GUIDED_CANDIDATE_PROMPT,
            user_payload={
                "template": template.model_dump(),
                "run_index": run_index,
                "turn_index": turn_index,
                "current_stage": stage,
                "candidate_profile": state.get("candidate_profile") or {},
                "next_seed_message": next_seed,
                "last_bot_messages": [str(item.get("text") or "") for item in transcript if item.get("role") == "bot"][-4:],
                "transcript": transcript[-12:],
            },
            response_model=CandidateReply,
        )
        reply = CandidateReply.model_validate(payload)
        if not reply.text.strip():
            raise BrainLLMError("Guided candidate simulator returned empty text")
        if next_seed_index is not None:
            self._seed_offsets[offset_key] = next_seed_index + 1
        return reply

    def _next_seed(self, seed_messages: list[str], stage: str, offset_key: str) -> tuple[str | None, int | None]:
        offset = self._seed_offsets.get(offset_key, 0)
        for index in range(offset, len(seed_messages)):
            candidate = seed_messages[index]
            if next_unused_seed_for_stage([candidate], [], stage):
                return candidate, index
        if offset < len(seed_messages):
            return seed_messages[offset], offset
        return None, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run guided LLM candidate eval against the funnel.")
    parser.add_argument("--templates", type=Path, default=Path("data/funnel_eval/templates.json"))
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--max-turns", type=int, default=18)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--agent-provider", default="alibaba")
    parser.add_argument("--agent-model", default="qwen-plus")
    parser.add_argument("--candidate-provider", default="alibaba")
    parser.add_argument("--candidate-model", default="qwen-plus")
    parser.add_argument("--llm-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--llm-max-retries", type=int, default=0)
    parser.add_argument("--skip-llm-preflight", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = get_settings().model_copy(
        update={
            "brain_dialogue_provider": args.agent_provider,
            "brain_dialogue_model": args.agent_model,
            "brain_llm_timeout_seconds": args.llm_timeout_seconds,
            "brain_llm_max_retries": args.llm_max_retries,
        }
    )
    candidate_settings = settings.model_copy(
        update={
            "brain_dialogue_provider": args.candidate_provider,
            "brain_dialogue_model": args.candidate_model,
        }
    )
    config = FunnelEvalConfig(
        templates_path=args.templates,
        output_dir=args.output_dir,
        runs=args.runs,
        max_turns=args.max_turns,
        agent_llm=True,
        candidate_llm=True,
        require_agent_llm=True,
        require_candidate_llm=True,
        candidate_llm_provider=args.candidate_provider,
        candidate_llm_model=args.candidate_model,
        llm_preflight=not args.skip_llm_preflight,
        llm_timeout_seconds=args.llm_timeout_seconds,
        llm_max_retries=args.llm_max_retries,
        quality_evaluator=False,
    )
    runner = FunnelEvalRunner(
        config=config,
        settings=settings,
        candidate_simulator=GuidedQwenCandidateSimulator(settings=candidate_settings, require_llm=True),
    )
    result = await runner.run()
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"json={args.output_dir / 'all_runs.json'}")
    print(f"report={args.output_dir / 'report.md'}")
    print(f"transcripts={args.output_dir / 'transcripts.md'}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
