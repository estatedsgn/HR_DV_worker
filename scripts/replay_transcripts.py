"""W6 candidate-script replay eval.

Replays the curated per-persona candidate message sequences from
``data/funnel_eval/templates.json`` (derived from the real recruiter
transcripts) through the live LangGraph funnel and prints, per turn, the
candidate message and the BOT reply plus the funnel stage. Use it to spot where
the bot's meaning / dialogue logic diverges from the real recruiter so we know
exactly where few-shot examples (W3) are needed.

Run (UTF-8):
  PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/replay_transcripts.py --only vikusya
  PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/replay_transcripts.py --max 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import MemorySaver

from app.services.funnel_graph.funnel_policy import TERMINAL_STAGES
from app.services.funnel_graph.graph import build_funnel_graph
from app.services.funnel_graph.reply import ReplyOrchestrator
from app.services.funnel_graph.semantic import SemanticAnalyzer

DEFAULT_TEMPLATES = Path("data/funnel_eval/templates.json")


def bot_texts(state: dict) -> list[str]:
    if not state.get("send_reply", True):
        return []
    out: list[str] = []
    for message in state.get("outgoing_messages") or []:
        if message.get("type") == "voice_pack":
            out.append(f"[voice_pack: {message.get('voice_pack_id')}]")
        elif message.get("text"):
            out.append(message["text"])
    return out


async def run_turn(graph, state: dict) -> dict:
    return await graph.ainvoke(state, config={"configurable": {"thread_id": state["thread_id"]}})


async def replay_template(graph, template: dict, out: list[str]) -> str:
    name = template.get("name", "template")
    state = {
        "candidate_id": name,
        "lead_id": name,
        "dialog_id": name,
        "thread_id": f"{name}-{uuid.uuid4().hex[:6]}",
        "stage": "interest_check",
        "status": "active",
        "candidate_profile": {},
        "slots": {},
        "sent_voice_packs": [],
        "sent_templates": [],
        "recent_messages": [],
        "message_batch": [],
        "metadata": {},
    }
    recent: list[dict] = []

    state = await run_turn(graph, state)
    for text in bot_texts(state):
        out.append(f"BOT(first): {text}")
        recent.append({"direction": "outbound", "sender_type": "agent", "body": text})

    for message in template.get("seed_messages") or []:
        msg = str(message).strip()
        if not msg:
            continue
        if state.get("stage") in TERMINAL_STAGES:
            break
        recent.append({"direction": "inbound", "sender_type": "lead", "body": msg})
        turn = {
            **state,
            "incoming_message": msg,
            "message_batch": [{"direction": "inbound", "sender_type": "lead", "body": msg}],
            "recent_messages": recent[-24:],
        }
        state = await run_turn(graph, turn)
        bots = bot_texts(state)
        for text in bots:
            recent.append({"direction": "outbound", "sender_type": "agent", "body": text})
        out.append("")
        out.append(f"CAND: {msg}")
        out.append(f"BOT  [{state.get('stage')}]: {'  ||  '.join(bots) if bots else '(no reply)'}")

    return str(state.get("stage"))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates", type=Path, default=DEFAULT_TEMPLATES)
    parser.add_argument("--only", default=None, help="Substring filter on template name.")
    parser.add_argument("--max", type=int, default=6)
    parser.add_argument("--output", type=Path, default=Path("runtime_logs/transcript_replay.txt"))
    args = parser.parse_args()

    data = json.loads(args.templates.read_text(encoding="utf-8"))
    templates = data.get("templates") if isinstance(data, dict) else data
    if args.only:
        needle = args.only.lower()
        templates = [t for t in templates if needle in str(t.get("name")).lower()]
    templates = templates[: args.max]

    graph = build_funnel_graph(
        semantic_analyzer=SemanticAnalyzer(use_llm=True),
        reply_orchestrator=ReplyOrchestrator(use_llm=True),
    ).compile(checkpointer=MemorySaver())

    out: list[str] = []
    summary: list[str] = []
    for template in templates:
        out.append("=" * 72)
        out.append(f"PERSONA: {template.get('name')}")
        out.append("=" * 72)
        final_stage = await replay_template(graph, template, out)
        summary.append(f"{template.get('name')}: ended at {final_stage}")
        out.append("")

    out.append("=" * 72)
    out.append("SUMMARY")
    out.extend(summary)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(out), encoding="utf-8")
    print(f"written {args.output} ({len(templates)} persona(s))")
    for line in summary:
        print(" ", line)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
