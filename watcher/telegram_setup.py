"""Print the chat id(s) that have messaged your bot, so you can set TELEGRAM_CHAT_ID.

Usage: send your bot any message in Telegram, then run
    TELEGRAM_BOT_TOKEN=... python -m watcher.telegram_setup
"""
import os
import sys

import httpx

token = os.environ.get("TELEGRAM_BOT_TOKEN") or sys.exit("set TELEGRAM_BOT_TOKEN first")
updates = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
chats = {u["message"]["chat"]["id"]: u["message"]["chat"] for u in updates.get("result", []) if "message" in u}
if not chats:
    sys.exit("No messages yet. Send your bot a message in Telegram, then run this again.")
for cid, chat in chats.items():
    name = chat.get("username") or chat.get("title") or chat.get("first_name", "")
    print(f"TELEGRAM_CHAT_ID={cid}   # {name}")
