"""Download the channel history to history.jsonl. Run: python backfill.py [limit]"""
import os, json, sys
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()
NAME = os.getenv("CHANNEL_KEYWORD", "A. Pierce Capital").lower()
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
client = TelegramClient("session", int(os.getenv("TG_API_ID")), os.getenv("TG_API_HASH"))


async def main():
    async for dialog in client.iter_dialogs():
        if NAME in (dialog.name or "").lower():
            print("Found:", dialog.name)
            n = 0
            with open("history.jsonl", "w", encoding="utf-8") as f:
                async for m in client.iter_messages(dialog, limit=LIMIT):
                    f.write(json.dumps({
                        "id": m.id,
                        "reply_to": m.reply_to_msg_id,
                        "date": m.date.isoformat(),  # UTC
                        "has_media": bool(m.media),
                        "text": m.raw_text,
                    }, ensure_ascii=False) + "\n")
                    n += 1
            print(f"Saved {n} messages to history.jsonl")
            return
    print("Channel not found. Check CHANNEL_KEYWORD against the exact chat name.")


with client:
    client.loop.run_until_complete(main())
