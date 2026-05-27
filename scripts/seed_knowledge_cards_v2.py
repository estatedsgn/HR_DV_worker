from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.db.session import AsyncSessionLocal
from app.services.brain_v2.agenda_registry import canonical_agenda_stage
from app.services.brain_v2.knowledge_cards import KnowledgeCardService


DEFAULT_PATH = Path("data/knowledge_cards_v2_seed.jsonl")
DEFAULT_PROFITCAST_DIR = Path("data/profitcast")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed Brain V2 knowledge cards.")
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument(
        "--profitcast-dir",
        default=None,
        help="Import Profitcast JSON bundle with knowledge_cards_profitcast_v1.json and rag_seed_manifest_profitcast_v1.json.",
    )
    parser.add_argument(
        "--bundle-dir",
        default=None,
        help="Import a JSON RAG bundle with rag_seed_manifest_*.json, knowledge_cards, and optional dialogue_chunks.",
    )
    parser.add_argument("--embed", action="store_true", help="Embed imported cards after upsert.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    path = Path(args.path)
    created = updated = 0
    async with AsyncSessionLocal() as session:
        service = KnowledgeCardService(session)
        payloads = []
        if args.bundle_dir:
            payloads.extend(load_bundle_cards(Path(args.bundle_dir)))
        elif args.profitcast_dir:
            payloads.extend(load_profitcast_cards(Path(args.profitcast_dir)))
        else:
            payloads.extend(load_jsonl_cards(path))
        for payload in payloads:
            existing = await service.get_by_key(payload["card_key"])
            if existing is None:
                await service.create_knowledge_card(**payload)
                created += 1
            else:
                await service.update_knowledge_card(payload["card_key"], **payload)
                updated += 1
            if args.embed:
                await service.embed_knowledge_card(payload["card_key"])
        await session.commit()
    print(f"brain v2 knowledge cards seeded: created={created} updated={updated}")


def load_jsonl_cards(path: Path) -> list[dict]:
    payloads = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            payloads.append(json.loads(line))
    return payloads


def load_profitcast_cards(directory: Path) -> list[dict]:
    return load_bundle_cards(directory, default_source_bundle="profitcast_v1")


def load_bundle_cards(directory: Path, *, default_source_bundle: str | None = None) -> list[dict]:
    manifest = load_bundle_manifest(directory)
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    source_bundle = str(
        manifest.get("source_name") or default_source_bundle or f"{directory.name}_bundle"
    )
    template = manifest.get("embedding_text_template")
    fields = manifest.get("embedding_text_fields")
    payloads: list[dict] = []

    cards_file = (
        files.get("knowledge_cards")
        or manifest.get("knowledge_cards_file")
        or first_existing_name(directory, "knowledge_cards_*.json")
    )
    if cards_file:
        cards_path = directory / str(cards_file)
        cards = json.loads(cards_path.read_text(encoding="utf-8-sig"))
        payloads.extend(
            normalize_seed_card(card, source_bundle=source_bundle, template=template, fields=fields)
            for card in cards
        )

    chunks_file = files.get("dialogue_chunks") or first_existing_name(directory, "dialogue_chunks_*.json")
    if chunks_file:
        chunks_path = directory / str(chunks_file)
        chunks = json.loads(chunks_path.read_text(encoding="utf-8-sig"))
        payloads.extend(normalize_dialogue_chunk(chunk, source_bundle=source_bundle) for chunk in chunks)

    return payloads


def load_bundle_manifest(directory: Path) -> dict:
    manifest_paths = sorted(directory.glob("rag_seed_manifest_*.json"))
    if not manifest_paths:
        return {}
    return json.loads(manifest_paths[0].read_text(encoding="utf-8-sig"))


def first_existing_name(directory: Path, pattern: str) -> str | None:
    matches = sorted(directory.glob(pattern))
    return matches[0].name if matches else None


def normalize_seed_card(
    card: dict,
    *,
    source_bundle: str,
    template: list[str] | None = None,
    fields: list[str] | None = None,
) -> dict:
    metadata = dict(card.get("metadata") or {})
    metadata.update(
        {
            "source_bundle": source_bundle,
            "source_stage": card.get("stage"),
            "card_type": card.get("card_type"),
            "priority": card.get("priority"),
            "next_goal": card.get("next_goal"),
            "answer_style": card.get("answer_style"),
            "embedding_text": build_embedding_text(card, template=template, fields=fields),
        }
    )
    content_parts = [
        str(card.get("fact_text") or "").strip(),
        str(card.get("example_answer") or "").strip(),
    ]
    return {
        "card_key": str(card["card_key"]),
        "stage": canonical_agenda_stage(card.get("stage")),
        "topic": card.get("topic"),
        "content": "\n\n".join(part for part in content_parts if part),
        "triggers": {"items": card.get("triggers") or []},
        "tags": {"items": card.get("tags") or []},
        "verification_status": card.get("verification_status") or "approved",
        "active": bool(card.get("is_active", True)),
    } | {"metadata_json": metadata}


def normalize_dialogue_chunk(chunk: dict, *, source_bundle: str) -> dict:
    transcript = dialogue_chunk_transcript(chunk)
    retrieval_topics = chunk.get("retrieval_topics") or []
    lead_intents = chunk.get("lead_intents") or []
    metadata = {
        "source_bundle": source_bundle,
        "source_stage": chunk.get("stage"),
        "card_type": "dialogue_chunk",
        "chunk_id": chunk.get("chunk_id"),
        "order": chunk.get("order"),
        "title": chunk.get("title"),
        "lead_intents": lead_intents,
        "slot_patch": chunk.get("slot_patch") or {},
        "retrieval_topics": retrieval_topics,
        "next_goal": chunk.get("next_goal"),
        "messages": chunk.get("messages") or [],
        "embedding_text": "\n".join(
            part
            for part in [
                str(chunk.get("chunk_id") or ""),
                str(chunk.get("title") or ""),
                str(chunk.get("stage") or ""),
                ", ".join(retrieval_topics),
                ", ".join(lead_intents),
                transcript,
            ]
            if part
        ),
    }
    return {
        "card_key": f"dialogue_chunk.{chunk['chunk_id']}",
        "stage": canonical_agenda_stage(chunk.get("stage")),
        "topic": chunk.get("next_goal") or chunk.get("stage"),
        "content": transcript,
        "triggers": {"items": [*retrieval_topics, *lead_intents]},
        "tags": {"items": ["dialogue_chunk", source_bundle, *retrieval_topics]},
        "verification_status": "verified",
        "active": bool(chunk.get("use_for_rag", True)),
        "metadata_json": metadata,
    }


def dialogue_chunk_transcript(chunk: dict) -> str:
    lines = [str(chunk.get("title") or chunk.get("chunk_id") or "").strip()]
    for message in chunk.get("messages") or []:
        if not isinstance(message, dict):
            continue
        speaker = str(message.get("speaker") or "unknown").strip()
        text = str(message.get("text") or "").strip()
        message_type = str(message.get("message_type") or "text").strip()
        if text:
            lines.append(f"{speaker} ({message_type}): {text}")
    if chunk.get("next_goal"):
        lines.append(f"next_goal: {chunk['next_goal']}")
    return "\n".join(line for line in lines if line)


def build_embedding_text(
    card: dict,
    *,
    template: list[str] | None = None,
    fields: list[str] | None = None,
) -> str:
    if not template:
        if fields:
            return "\n".join(format_embedding_field(name, card.get(name)) for name in fields)
        return str(card.get("fact_text") or card.get("example_answer") or "")
    values = {
        "card_key": card.get("card_key"),
        "card_type": card.get("card_type"),
        "stage": card.get("stage"),
        "topic": card.get("topic"),
        "priority": card.get("priority"),
        "tags": ", ".join(card.get("tags") or []),
        "triggers": ", ".join(card.get("triggers") or []),
        "fact_text": card.get("fact_text"),
        "example_answer": card.get("example_answer"),
        "next_goal": card.get("next_goal"),
        "answer_style": card.get("answer_style"),
    }
    return "\n".join(line.format(**values) for line in template)


def format_embedding_field(name: str, value) -> str:
    if isinstance(value, list):
        formatted = ", ".join(str(item) for item in value)
    elif isinstance(value, dict):
        formatted = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        formatted = "" if value is None else str(value)
    return f"{name}: {formatted}"


if __name__ == "__main__":
    asyncio.run(main())
