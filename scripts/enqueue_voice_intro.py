from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.dialog import Dialog
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.services.campaign_sequence import build_peer_from_dialog


DEFAULT_MANIFEST = Path("data/voice_intro/voice_intro_manifest.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enqueue the 4 intro voice notes for a dialog.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--dialog-id")
    target.add_argument("--username")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--start-delay-seconds", type=int, default=0)
    parser.add_argument("--gap-seconds", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_dir = manifest_path.parent
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        dialog = await find_dialog(session, dialog_id=args.dialog_id, username=args.username)
        if dialog is None:
            raise SystemExit("Dialog not found")
        items = sorted(manifest.get("items") or [], key=lambda item: int(item.get("order") or 100))
        for index, item in enumerate(items):
            media_path = base_dir / str(item["file"])
            scheduled_at = now + timedelta(seconds=args.start_delay_seconds + index * args.gap_seconds)
            print(
                f"{'dry-run ' if args.dry_run else ''}voice#{item['order']} "
                f"dialog={dialog.id} file={media_path} scheduled_at={scheduled_at.isoformat()}"
            )
            if args.dry_run:
                continue
            message = Message(
                dialog_id=dialog.id,
                direction="outbound",
                sender_type="agent",
                body=f"[voice] {item.get('topic') or item['file']}",
                status="scheduled",
            )
            session.add(message)
            await session.flush()
            session.add(
                OutboundJob(
                    account_id=dialog.account_id,
                    dialog_id=dialog.id,
                    message_id=message.id,
                    target_username=dialog.telegram_username,
                    peer=build_peer_from_dialog(dialog),
                    job_type="voice",
                    text=message.body,
                    media_path=str(media_path),
                    media_mime_type="audio/ogg",
                    media_metadata={
                        "caption": item.get("caption") or "",
                        "duration_seconds": item.get("duration_seconds") or 0,
                        "recording_delay_seconds": item.get("recording_delay_seconds") or 50,
                        "voice_intro_order": item.get("order"),
                        "stage_hint": item.get("stage_hint"),
                        "topic": item.get("topic"),
                        "related_cards": item.get("related_cards") or [],
                    },
                    typing_action=manifest.get("recording_action") or "sendMessageRecordAudioAction",
                    status="queued",
                    scheduled_at=scheduled_at,
                    next_attempt_at=scheduled_at,
                )
            )
        if not args.dry_run:
            await session.commit()


async def find_dialog(session, *, dialog_id: str | None, username: str | None) -> Dialog | None:
    if dialog_id:
        return await session.get(Dialog, dialog_id)
    normalized = normalize_username(username or "")
    result = await session.execute(
        select(Dialog)
        .where(Dialog.telegram_username.in_([normalized, normalized.lstrip("@")]))
        .order_by(Dialog.updated_at.desc(), Dialog.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def normalize_username(value: str) -> str:
    username = value.strip()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username


if __name__ == "__main__":
    asyncio.run(main())
