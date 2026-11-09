# Ejecuta esto UNA vez (en tu PC, Termux o Colab) para obtener tu SESSION_STRING.
# pip install telethon
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: "))
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\nTu SESSION_STRING (no la compartas con nadie):\n")
    print(c.session.save())
