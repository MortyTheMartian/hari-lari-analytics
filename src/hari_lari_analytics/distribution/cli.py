"""`hari-lari-telegram`: helpers for setting up and testing the Telegram delivery."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hari_lari_analytics.distribution import settings
from hari_lari_analytics.distribution.telegram import TelegramClient, TelegramError


def chats_from_updates(updates: list[dict]) -> dict[int, dict]:
    """Every distinct chat the bot has seen, keyed by chat id."""
    chats: dict[int, dict] = {}
    for update in updates:
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    return chats


def describe_chat(chat: dict) -> str:
    name = chat.get("title") or " ".join(
        x for x in [chat.get("first_name"), chat.get("last_name")] if x
    )
    handle = f" @{chat['username']}" if chat.get("username") else ""
    return f"{chat['id']:>16}  {chat['type']:<11} {name}{handle}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hari-lari-telegram", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami", help="check the bot token and show the bot's name")
    sub.add_parser("chat-ids", help="list the chats that wrote to the bot or added it (4.5)")
    ping = sub.add_parser("ping", help="send a short test message")
    ping.add_argument("--to", choices=["admin", "group"], required=True)
    send = sub.add_parser("send-report", help="send a cover PNG and a PDF")
    send.add_argument("--to", choices=["admin", "group"], required=True)
    send.add_argument("--cover", type=Path, required=True)
    send.add_argument("--pdf", type=Path, required=True)
    send.add_argument("--caption", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = TelegramClient(settings.get_secret(settings.BOT_TOKEN))
        if args.command == "whoami":
            me = client.get_me()
            print(f"Bot: {me['first_name']} (@{me['username']}), can join groups: "
                  f"{me.get('can_join_groups')}")  # fmt: skip
        elif args.command == "chat-ids":
            chats = chats_from_updates(client.get_updates())
            if not chats:
                print("No chats yet. Send the bot a message (or add it to the group), "
                      "then run this again.")  # fmt: skip
            for chat in chats.values():
                print(describe_chat(chat))
        else:
            spec = settings.ADMIN_CHAT_ID if args.to == "admin" else settings.CHAT_ID
            chat_id = settings.get_secret(spec)
            if args.command == "ping":
                client.send_message(chat_id, "Hari Lari bot: test message, all good.")
            else:
                client.send_report(chat_id, args.cover, args.pdf, args.caption)
            print(f"Sent to the {args.to} chat.")
    except (TelegramError, settings.MissingSecret) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
