import os
import re
import time
import random
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from google import genai

# ==========================================
# VARIABLES DE ENTORNO
# ==========================================
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")   # se genera con generar_sesion.py
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
NOMBRE = os.getenv("NOMBRE", "Yo")
# IDs de grupos separados por coma, ej: -1001234567890,-1009876543210
GRUPOS = [int(x) for x in os.getenv("GRUPOS", "").split(",") if x.strip()]
COOLDOWN = int(os.getenv("COOLDOWN", 20))          # segundos mínimos entre respuestas por chat

ai_client = genai.Client(api_key=GEMINI_API_KEY)
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

SYSTEM_PROMPT = f"""
Eres {NOMBRE}, chateando en grupos de Telegram.
- Estilo: informal, directo, jerga relajada.
- Longitud: respuestas cortas, como un mensaje de texto normal.
- Si alguien te pregunta directamente si eres un bot o una IA, responde con honestidad.
"""

# ==========================================
# ESCANEO Y ELECCIÓN DE MODELO
# ==========================================
EXCLUIR = ("tts", "image", "live", "embedding", "audio", "lite",
           "robotics", "computer-use", "learnlm", "thinking")


def obtener_mejor_modelo() -> str:
    try:
        print("🔍 Escaneando modelos disponibles en la API de Gemini...")
        candidatos = []
        for m in ai_client.models.list():
            acciones = m.supported_actions or []
            if "generateContent" not in acciones:
                continue
            nombre = m.name.replace("models/", "")
            low = nombre.lower()
            if "flash" not in low or any(x in low for x in EXCLUIR):
                continue
            v = re.search(r"gemini-(\d+(?:\.\d+)?)-flash", low)
            version = float(v.group(1)) if v else 0.0
            estable = not any(x in low for x in ("preview", "exp", "latest"))
            candidatos.append((version, estable, nombre))

        if candidatos:
            # Mayor versión primero; a igualdad, prefiere la estable
            candidatos.sort(reverse=True)
            elegido = candidatos[0][2]
            print(f"✅ Modelo seleccionado: {elegido}")
            return elegido
    except Exception as e:
        print(f"❌ Error al escanear modelos: {e}")

    print("⚠️ Usando modelo de respaldo: gemini-2.5-flash")
    return "gemini-2.5-flash"


MODELO_ACTIVO = obtener_mejor_modelo()

# ==========================================
# TELEGRAM
# ==========================================
ultima_respuesta = {}  # chat_id -> timestamp
yo = None


@client.on(events.NewMessage(incoming=True, chats=GRUPOS or None))
async def handle_message(event):
    if not event.is_group:
        return

    # Solo responde si te mencionan o responden a un mensaje tuyo
    es_para_mi = bool(event.message.mentioned)
    if not es_para_mi and event.is_reply:
        citado = await event.get_reply_message()
        es_para_mi = bool(citado and citado.sender_id == yo.id)
    if not es_para_mi:
        return

    # Límite de frecuencia por chat
    ahora = time.time()
    if ahora - ultima_respuesta.get(event.chat_id, 0) < COOLDOWN:
        return
    ultima_respuesta[event.chat_id] = ahora

    texto = event.raw_text
    if not texto:
        return

    try:
        print(f"Mensaje recibido: {texto}")
        response = await ai_client.aio.models.generate_content(
            model=MODELO_ACTIVO,
            contents=f"Alguien en el grupo dijo: '{texto}'. Responde como {NOMBRE}:",
            config={
                "system_instruction": SYSTEM_PROMPT,
                "max_output_tokens": 150,
            },
        )
        respuesta = (response.text or "").strip()
        if not respuesta:
            return

        # Pausa y "escribiendo..." para que no sea instantáneo
        async with client.action(event.chat_id, "typing"):
            await asyncio.sleep(random.uniform(2, 6))
        await event.reply(respuesta)
        print(f"Respuesta enviada: {respuesta}")

    except Exception as e:
        print(f"Error al generar respuesta con IA: {e}")


# ==========================================
# FASTAPI (para health check / pinger)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global yo
    print("Iniciando cliente de Telegram...")
    await client.start()
    yo = await client.get_me()
    print(f"¡Userbot conectado como {yo.first_name}!")
    tarea = asyncio.create_task(client.run_until_disconnected())
    yield
    await client.disconnect()
    tarea.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    return {"status": "AI Userbot is running!", "model_in_use": MODELO_ACTIVO}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
