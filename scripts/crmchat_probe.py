from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.crmchat_connector import (
    CRMChatAPIError,
    CRMChatConnector,
    normalize_dialogs_response,
)
from app.services.crmchat_diagnostics import (
    build_input_peer,
    redact_value,
    summarize_dialogs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only CRMchat probe for validating API credentials and Telegram account access."
    )
    parser.add_argument(
        "--dialogs-limit",
        type=int,
        default=5,
        help="How many dialogs to request via messages.getDialogs.",
    )
    parser.add_argument(
        "--with-history",
        action="store_true",
        help="Also fetch recent history for normalized dialogs.",
    )
    parser.add_argument(
        "--history-limit",
        type=int,
        default=5,
        help="How many messages to request per dialog when --with-history is set.",
    )
    parser.add_argument(
        "--history-dialogs",
        type=int,
        default=1,
        help="How many dialogs to inspect when --with-history is set.",
    )
    parser.add_argument(
        "--save-redacted",
        type=Path,
        help="Optional path for a redacted JSON diagnostic snapshot. Prefer docs/samples/*.local.json.",
    )
    parser.add_argument(
        "--print-redacted-raw",
        action="store_true",
        help="Print redacted raw getDialogs payload to stdout.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    diagnostics: dict[str, Any] = {"dialogs": {}, "history": []}

    async with CRMChatConnector(settings=settings) as connector:
        print("CRMchat probe: starting read-only checks")
        print(f"Base URL: {settings.crmchat_api_base_url}")

        context = await connector.bootstrap()
        diagnostics["bootstrap"] = {
            "organization": context.organization.raw,
            "workspace": context.workspace.raw,
            "telegram_account": context.telegram_account.raw,
        }

        print("\nSelected CRMchat context")
        print(
            f"- organization: id={context.organization.id} name={context.organization.name}"
        )
        print(
            f"- workspace:    id={context.workspace.id} name={context.workspace.name}"
        )
        print(
            "- telegram:     "
            f"id={context.telegram_account.id} status={context.telegram_account.status} "
            f"username={context.telegram_account.username}"
        )

        workspaces = await connector.list_workspaces(context.organization.id)
        accounts = await connector.list_telegram_accounts(context.workspace.id)
        print(f"\nDiscovered workspaces: {len(workspaces)}")
        print(f"Discovered Telegram accounts in selected workspace: {len(accounts)}")

        dialogs_payload = await connector.get_dialogs(
            context.workspace.id,
            context.telegram_account.id,
            limit=args.dialogs_limit,
        )
        dialogs = normalize_dialogs_response(dialogs_payload)
        diagnostics["dialogs"] = {
            "raw": dialogs_payload,
            "summary": summarize_dialogs(dialogs),
        }

        print(f"\nRequested dialogs limit: {args.dialogs_limit}")
        print(f"Normalized dialogs: {len(dialogs)}")
        for index, dialog_summary in enumerate(summarize_dialogs(dialogs), start=1):
            print(
                f"{index}. peer={dialog_summary['peer_type']}:{dialog_summary['peer_id']} "
                f"username={dialog_summary['username']} "
                f"name={dialog_summary['display_name']} "
                f"top_message_id={dialog_summary['top_message_id']} "
                f"unread={dialog_summary['unread_count']} "
                f"has_access_hash={dialog_summary['has_access_hash']}"
            )

        if args.print_redacted_raw:
            print("\nRedacted raw dialogs payload:")
            print(
                json.dumps(redact_value(dialogs_payload), ensure_ascii=False, indent=2)
            )

        if args.with_history:
            print("\nFetching recent history for normalized dialogs")
            for dialog in dialogs[: args.history_dialogs]:
                try:
                    peer = build_input_peer(dialog.peer)
                except ValueError as exc:
                    print(
                        f"- skipped {dialog.peer.peer_type}:{dialog.peer.peer_id}: {exc}"
                    )
                    continue
                history_payload = await connector.get_history(
                    context.workspace.id,
                    context.telegram_account.id,
                    peer,
                    limit=args.history_limit,
                )
                diagnostics["history"].append(
                    {
                        "peer": summarize_dialogs([dialog])[0],
                        "raw": history_payload,
                    }
                )
                messages = history_payload.get("messages", [])
                messages_count = (
                    len(messages) if isinstance(messages, list) else "unknown"
                )
                print(
                    f"- {dialog.peer.peer_type}:{dialog.peer.peer_id}: messages={messages_count}"
                )

    if args.save_redacted:
        args.save_redacted.parent.mkdir(parents=True, exist_ok=True)
        args.save_redacted.write_text(
            json.dumps(redact_value(diagnostics), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nSaved redacted diagnostics to {args.save_redacted}")

    print("\nCRMchat probe: completed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except CRMChatAPIError as exc:
        print(f"CRMchat API error: {exc}")
        if exc.status_code:
            print(f"Status code: {exc.status_code}")
        raise SystemExit(1) from exc
