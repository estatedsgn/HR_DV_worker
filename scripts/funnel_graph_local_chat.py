from __future__ import annotations

import argparse
import asyncio
import sys

from langgraph.checkpoint.memory import MemorySaver

from app.services.funnel_graph.funnel_policy import TERMINAL_STAGES
from app.services.funnel_graph.graph import build_funnel_graph
from app.services.funnel_graph.reply import ReplyOrchestrator
from app.services.funnel_graph.repository import TerminalFunnelRepository
from app.services.funnel_graph.semantic import SemanticAnalyzer
from app.services.funnel_graph.state import FunnelGraphState


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Terminal HR funnel runner without CRMchat/Telegram sends.")
    parser.add_argument("--candidate-id", default="test_1", help="Local candidate id. Default: test_1.")
    parser.add_argument("--script", nargs="*", help="Run these candidate messages non-interactively.")
    parser.add_argument("--reset", action="store_true", help="Reset the candidate state before starting.")
    parser.add_argument("--with-llm", action="store_true", help="Use the configured LLM adapter instead of local fallback.")
    return parser.parse_args()


async def run_turn(graph, state: FunnelGraphState) -> FunnelGraphState:
    return await graph.ainvoke(
        state,
        config={"configurable": {"thread_id": state["thread_id"]}},
    )


async def start_if_needed(graph, repository: TerminalFunnelRepository, candidate_id: str) -> FunnelGraphState:
    state = repository.load_or_create(candidate_id)
    if not state.get("recent_messages"):
        state = await run_turn(graph, state)
        print_bot_messages(state)
        repository.save_turn(candidate_id, state, incoming_message=None)
    return state


async def handle_message(
    graph,
    repository: TerminalFunnelRepository,
    state: FunnelGraphState,
    candidate_id: str,
    user_text: str,
) -> FunnelGraphState:
    turn_state = {
        **repository.load_or_create(candidate_id),
        "incoming_message": user_text,
        "message_batch": [{"direction": "inbound", "sender_type": "lead", "body": user_text}],
    }
    state = await run_turn(graph, turn_state)
    print_bot_messages(state)
    print_status(state)
    repository.save_turn(candidate_id, state, incoming_message=user_text)
    return state


async def handle_timeout(
    graph,
    repository: TerminalFunnelRepository,
    candidate_id: str,
) -> FunnelGraphState:
    turn_state = {
        **repository.load_or_create(candidate_id),
        "incoming_message": None,
        "message_batch": [],
        "timeout_event": "interrupt_followup",
    }
    state = await run_turn(graph, turn_state)
    print_bot_messages(state)
    print_status(state)
    repository.save_turn(candidate_id, state, incoming_message=None)
    return state


async def main() -> None:
    args = parse_args()
    repository = TerminalFunnelRepository()
    if args.reset:
        repository.reset(args.candidate_id)
    graph = build_funnel_graph(
        semantic_analyzer=SemanticAnalyzer(use_llm=args.with_llm),
        reply_orchestrator=ReplyOrchestrator(use_llm=args.with_llm),
    ).compile(
        checkpointer=MemorySaver()
    )
    state = await start_if_needed(graph, repository, args.candidate_id)
    if args.script:
        for message in args.script:
            if state.get("stage") in TERMINAL_STAGES:
                break
            if message.lower() == "/timeout":
                print("\n[TIMEOUT: interrupt_followup]")
                state = await handle_timeout(graph, repository, args.candidate_id)
                continue
            print(f"\nUSER: {message}")
            state = await handle_message(graph, repository, state, args.candidate_id, message)
        if state.get("stage") in TERMINAL_STAGES:
            print_final_state(state)
        return

    print("\nТерминальная HR-воронка. Команды: /restart, /timeout, /exit")
    print_status(state)
    while True:
        if state.get("stage") in TERMINAL_STAGES:
            print_final_state(state)
            return
        user_text = input("\nUSER: ").strip()
        if user_text.lower() in {"/exit", "/quit"}:
            print_final_state(state)
            return
        if user_text.lower() in {"/restart", "restart"}:
            repository.reset(args.candidate_id)
            state = await start_if_needed(graph, repository, args.candidate_id)
            print_status(state)
            continue
        if user_text.lower() == "/timeout":
            state = await handle_timeout(graph, repository, args.candidate_id)
            continue
        if not user_text:
            continue
        state = await handle_message(graph, repository, state, args.candidate_id, user_text)


def print_bot_messages(state: FunnelGraphState) -> None:
    if not state.get("send_reply", True):
        return
    for message in state.get("outgoing_messages") or []:
        if message.get("type") == "voice_pack":
            print(f"BOT: [voice_pack: {message.get('voice_pack_id')}]")
        elif message.get("text"):
            print(f"BOT: {message['text']}")


def print_status(state: FunnelGraphState) -> None:
    print(f"[stage={state.get('stage')} status={state.get('status')} profile={state.get('candidate_profile') or {}}]")


def print_final_state(state: FunnelGraphState) -> None:
    print("\nFINAL STATE")
    print_status(state)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
