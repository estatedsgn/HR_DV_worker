from __future__ import annotations

import argparse
import asyncio
import json

from app.db.session import AsyncSessionLocal
from app.models.brain_v2 import BrainRun
from app.services.brain_v2.executor import BrainExecutor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Approve, reject, or show a shadow Brain V2 run.")
    parser.add_argument("brain_run_id")
    parser.add_argument("--reject", action="store_true")
    parser.add_argument("--reason")
    parser.add_argument("--show", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        run = await session.get(BrainRun, args.brain_run_id)
        if run is None:
            print("brain run not found")
            return
        if args.show:
            print(json.dumps(run_payload(run), ensure_ascii=False, indent=2, default=str))
            return
        executor = BrainExecutor(session)
        if args.reject:
            run = await executor.reject(run, reason=args.reason)
        else:
            run = await executor.approve(run)
        await session.commit()
        print(json.dumps(run_payload(run), ensure_ascii=False, indent=2, default=str))


def run_payload(run: BrainRun) -> dict:
    return {
        "id": str(run.id),
        "status": run.status,
        "shadow_mode": run.shadow_mode,
        "dialog_id": str(run.dialog_id),
        "lead_id": str(run.lead_id),
        "stage_before": run.stage_before,
        "stage_after": run.stage_after,
        "dialogue_move": run.dialogue_move,
        "validator_verdict": run.validator_verdict,
        "response_text": run.response_text,
        "executor_action": run.executor_action,
        "error_message": run.error_message,
    }


if __name__ == "__main__":
    asyncio.run(main())
