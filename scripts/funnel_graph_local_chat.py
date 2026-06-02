from __future__ import annotations

import argparse
import asyncio
import sys
import threading

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
    parser.add_argument(
        "--debounce-seconds",
        type=float,
        default=1.0,
        help="Interactive quiet window before processing a candidate message batch. Default: 1.0.",
    )
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
    return await handle_messages(graph, repository, state, candidate_id, [user_text])


async def run_messages(
    graph,
    repository: TerminalFunnelRepository,
    candidate_id: str,
    user_texts: list[str],
) -> FunnelGraphState:
    texts = [text.strip() for text in user_texts if text.strip()]
    combined = "\n".join(texts)
    turn_state = {
        **repository.load_or_create(candidate_id),
        "incoming_message": combined,
        "message_batch": [
            {"direction": "inbound", "sender_type": "lead", "body": text}
            for text in texts
        ],
    }
    return await run_turn(graph, turn_state)


async def handle_messages(
    graph,
    repository: TerminalFunnelRepository,
    state: FunnelGraphState,
    candidate_id: str,
    user_texts: list[str],
) -> FunnelGraphState:
    texts = [text.strip() for text in user_texts if text.strip()]
    state = await run_messages(graph, repository, candidate_id, texts)
    print_bot_messages(state)
    print_status(state)
    repository.save_turn(candidate_id, state, incoming_message=texts)
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

    await interactive_loop(
        graph=graph,
        repository=repository,
        state=state,
        candidate_id=args.candidate_id,
        debounce_seconds=max(0.0, float(args.debounce_seconds)),
    )
    return


async def interactive_loop(
    *,
    graph,
    repository: TerminalFunnelRepository,
    state: FunnelGraphState,
    candidate_id: str,
    debounce_seconds: float,
) -> None:
    print("\nTerminal HR funnel. Commands: /restart, /timeout, /exit")
    print(f"[live batching: debounce={debounce_seconds:.1f}s; new input before reply restarts the batch]")
    print_status(state)

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    stop_event = threading.Event()
    loop = asyncio.get_running_loop()
    threading.Thread(target=input_reader, args=(loop, queue, stop_event), daemon=True).start()

    pending_texts: list[str] = []
    generation = 0
    active_task: asyncio.Task[FunnelGraphState] | None = None

    async def schedule_batch(batch: list[str], batch_generation: int) -> FunnelGraphState:
        if debounce_seconds:
            await asyncio.sleep(debounce_seconds)
        if batch_generation != generation:
            return state
        print(f"\n[processing batch: {len(batch)} message(s)]")
        result = await run_messages(graph, repository, candidate_id, batch)
        if batch_generation != generation:
            metadata = dict(result.get("metadata") or {})
            metadata["terminal_run_superseded"] = True
            result["metadata"] = metadata
            result["outgoing_messages"] = []
            result["pending_actions"] = []
            result["send_reply"] = False
            return result
        print_bot_messages(result)
        print_status(result)
        repository.save_turn(candidate_id, result, incoming_message=batch)
        return result

    try:
        while True:
            if state.get("stage") in TERMINAL_STAGES and active_task is None:
                print_final_state(state)
                return

            input_task = asyncio.create_task(queue.get())
            wait_for: set[asyncio.Task] = {input_task}
            if active_task is not None:
                wait_for.add(active_task)
            done, pending = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
            if input_task not in done:
                input_task.cancel()

            if active_task is not None and active_task in done:
                try:
                    state = active_task.result()
                    pending_texts = []
                except asyncio.CancelledError:
                    pass
                active_task = None

            if input_task in done:
                raw = input_task.result()
                if raw is None:
                    if active_task is not None and not active_task.done():
                        active_task.cancel()
                    print_final_state(state)
                    return

                user_text = raw.strip()
                lowered = user_text.lower()
                if lowered in {"/exit", "/quit"}:
                    if active_task is not None and not active_task.done():
                        active_task.cancel()
                    print_final_state(state)
                    return
                if lowered in {"/restart", "restart"}:
                    if active_task is not None and not active_task.done():
                        active_task.cancel()
                    pending_texts = []
                    generation += 1
                    repository.reset(candidate_id)
                    state = await start_if_needed(graph, repository, candidate_id)
                    print_status(state)
                    active_task = None
                    continue
                if lowered == "/timeout":
                    if active_task is not None and not active_task.done():
                        active_task.cancel()
                    pending_texts = []
                    generation += 1
                    state = await handle_timeout(graph, repository, candidate_id)
                    active_task = None
                    continue
                if not user_text:
                    continue

                pending_texts.append(user_text)
                generation += 1
                if active_task is not None and not active_task.done():
                    active_task.cancel()
                    print(f"[new message before reply: restarting analysis for {len(pending_texts)} messages]")
                active_task = asyncio.create_task(schedule_batch(list(pending_texts), generation))
    finally:
        stop_event.set()


def input_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str | None], stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            line = input("\nUSER: ")
        except EOFError:
            loop.call_soon_threadsafe(queue.put_nowait, None)
            return
        loop.call_soon_threadsafe(queue.put_nowait, line)
        if line.strip().lower() in {"/exit", "/quit"}:
            return


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
