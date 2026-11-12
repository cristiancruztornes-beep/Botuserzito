import os
import re
import time
from datetime import datetime, timezone, timedelta
import random
import difflib
import asyncio
import json
from telethon import TelegramClient, events, utils, errors, types
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetRecentStickersRequest, GetFavedStickersRequest, SendVoteRequest
from telethon.tl.types import DocumentAttributeSticker, InputMessagesFilterVideo, InputMessagesFilterDocument
from telethon.errors import FloodWaitError
from google import genai
from aiohttp import web, ClientSession, ClientTimeout

# ==========================================
# DATOS (se piden al ejecutar la celda)
# ==========================================
def secreto(nombre, obligatorio=True):
    v = os.environ.get(nombre, "").strip()
    if not v and obligatorio:
        raise RuntimeError(f"Falta la variable de entorno {nombre} en Render.")
    return v


def norm_id(x):
    """Si escribes el ID de un grupo/canal sin el signo menos (1003743630322), lo corrige a -1003743630322."""
    return -x if x > 10**12 else x


API_ID = int(secreto("API_ID"))
API_HASH = secreto("API_HASH")
GEMINI_API_KEY = secreto("GEMINI_API_KEY")
NOMBRE = secreto("NOMBRE")
GRUPOS = [int(x) for x in secreto("GRUPOS", False).split(",") if x.strip()]  # vacío = todos los grupos
SESSION_STRING = secreto("SESSION_STRING")  # obligatoria en Render (genérala con gen_session.py)
COOLDOWN = 8  # segundos mínimos entre respuestas por chat
LECTURA = (3, 7)        # segundos que "tarda en leer" antes de empezar a escribir (solo mensajes de texto)
VEL_ESCRIBIR = 0.09     # segundos de "escribiendo..." por cada caracter de la respuesta
ESCRIBIR_MIN_MAX = (3, 20)  # límites del tiempo mostrando "escribiendo..."

ai_client = genai.Client(api_key=GEMINI_API_KEY)

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, loop=loop, flood_sleep_threshold=300)

ESTILO = secreto("ESTILO", False)  # opcional: notas libres, ej: "digo bro y dale pues, uso 💀"
# Solo aprende de mensajes tuyos anteriores a esta fecha (así no aprende de lo que escribió el bot)
ANTES_DE = secreto("ESTILO_ANTES_DE", False) or "2026-11-09"

BASE_PROMPT = f"""
Eres {NOMBRE}, una persona real chateando por Telegram desde el celular.
- Imita EXACTAMENTE la forma de escribir de los ejemplos reales: vocabulario, jerga, emojis, mayúsculas, signos y longitud.
- Los ejemplos sirven solo para copiar el ESTILO: NUNCA repitas un ejemplo literalmente.
- Tu respuesta debe tener sentido con lo que acaban de decir. Si no entiendes algo, reacciona corto y natural.
- Escribe solo el mensaje, sin comillas ni explicaciones.
- Sé espontáneo, como una persona: a veces solo reaccionas ("jaja", "uff", "no jodas"), a veces cuentas algo tuyo, a veces bromeas, a veces preguntas algo de vuelta, pero NO siempre lo mismo.
- Varía mucho la longitud: a veces una o dos palabras, a veces una frase. Casi nunca un párrafo.
- NO repitas frases, muletillas, emojis ni la misma forma de empezar o terminar que ya usaste en tus últimas respuestas. Cada mensaje debe sonar distinto.
- No saludes ni uses el nombre de la persona en cada mensaje. No termines siempre con una pregunta.
- Nunca hables como asistente ("claro", "por supuesto", "entiendo", "¿en qué te puedo ayudar?").
- Si quieres mandar dos mensajes cortos seguidos, como hace la gente, sepáralos con una línea que diga solo ||| (máximo dos veces, y no siempre).
- Si alguien te pregunta directamente si eres un bot o una IA, responde con honestidad.
"""

EJEMPLOS = []  # mensajes reales tuyos
STICKERS = []  # (sticker, emoji) que el bot puede mandar


def emoji_de(doc):
    for a in doc.attributes:
        if isinstance(a, DocumentAttributeSticker):
            return a.alt or ""
    return ""


def prompt_sistema():
    p = BASE_PROMPT
    if ESTILO:
        p += f"\nNotas de estilo: {ESTILO}\n"
    if STICKERS:
        emojis = " ".join(sorted({e for _, e in STICKERS if e}))
        p += (
            "\nPuedes responder con un sticker en vez de texto cuando sea la reacción natural "
            "(risa, sorpresa, acuerdo corto). Para eso escribe SOLO [STICKER:😂] con el emoji que "
            f"mejor encaje, de esta lista: {emojis}. Úsalo de vez en cuando, no en cada respuesta.\n"
        )
    if EJEMPLOS:
        p += "\nEjemplos de mensajes reales de " + NOMBRE + " (así escribe):\n"
        muestra = random.sample(EJEMPLOS, min(len(EJEMPLOS), 30))  # otra tanda cada vez, para no repetirse
        p += "\n".join(f"- {e}" for e in muestra)
    if PARES:
        p += "\n\nAsí le respondes tú a otros (mensaje de ellos → tu respuesta):\n"
        for quien, a, b in random.sample(PARES, min(len(PARES), 8)):
            p += f"- {quien}: {a} → {b}\n"
    if VIBE:
        p += (
            "\nAsí habla el resto del grupo (solo para entender la jerga y los temas; NO imites su forma "
            "de escribir, sigue escribiendo como " + NOMBRE + "):\n"
        )
        p += "\n".join(f"- {v}" for v in random.sample(VIBE, min(len(VIBE), 15)))
    return p

# ==========================================
# ESCANEO Y ELECCIÓN DE MODELO
# ==========================================
EXCLUIR = ("tts", "image", "live", "embedding", "audio", "lite",
           "robotics", "computer-use", "learnlm", "thinking")
MODELOS_DISP = []  # TODOS los modelos que devuelve la API
NO_TEXTO = set()   # los que no sirven para generar texto (se marcan con ⚠️)
CANDIDATOS = []    # modelos válidos ordenados de mejor a peor (los "lite" al final)
AGOTADOS = {}      # modelo -> hora en que se quedó sin cuota diaria


def obtener_mejor_modelo() -> str:
    try:
        print("🔍 Escaneando modelos disponibles en la API de Gemini...")
        MODELOS_DISP.clear()
        NO_TEXTO.clear()
        candidatos = []
        for m in ai_client.models.list():
            nombre = m.name.replace("models/", "")
            low = nombre.lower()
            MODELOS_DISP.append(nombre)
            if "generateContent" not in (m.supported_actions or []):
                NO_TEXTO.add(nombre)
                continue
            if "flash" not in low or any(x in low for x in EXCLUIR if x != "lite"):
                continue
            v = re.search(r"gemini-(\d+(?:\.\d+)?)-flash", low)
            version = float(v.group(1)) if v else 0.0
            estable = not any(x in low for x in ("preview", "exp", "latest"))
            candidatos.append(("lite" not in low, version, estable, nombre))
        MODELOS_DISP.sort()
        if candidatos:
            candidatos.sort(reverse=True)
            CANDIDATOS[:] = [c[3] for c in candidatos]
            elegido = next((m for m in CANDIDATOS if time.time() - AGOTADOS.get(m, 0) > 23 * 3600), CANDIDATOS[0])
            print(f"✅ Modelo seleccionado: {elegido}")
            return elegido
    except Exception as e:
        print(f"❌ Error al escanear modelos: {e}")
    if not CANDIDATOS:
        CANDIDATOS[:] = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    print("⚠️ Usando modelo de respaldo: gemini-2.5-flash")
    return "gemini-2.5-flash"


MODELO_ACTIVO = obtener_mejor_modelo()
MODELO_FIJO = secreto("MODELO", False)  # opcional: fuerza un modelo
if MODELO_FIJO:
    MODELO_ACTIVO = MODELO_FIJO
    print(f"📌 Modelo fijado por secreto: {MODELO_ACTIVO}")

# ==========================================
# TELEGRAM
# ==========================================
ultima_respuesta = {}
INICIO = time.time()
ERRORES = []      # (hora, texto) de los últimos fallos, para /estado
RESPUESTAS_DADAS = 0


def registrar_error(txt):
    ERRORES.append((time.time(), str(txt)[:160]))
    del ERRORES[:-5]
    try:
        s = stats_dia()
        s["errores"] += 1
        if str(txt).startswith("FloodWait"):
            s["floods"] += 1
    except Exception:
        pass


FLOOD_HASTA = 0   # si Telegram manda FloodWait, el bot se queda quieto hasta esta hora
yo = None


async def cargar_stickers(propios):
    STICKERS.clear()
    vistos = set()

    def add(doc):
        if doc.id not in vistos and len(STICKERS) < 60:
            vistos.add(doc.id)
            STICKERS.append((doc, emoji_de(doc)))

    for d in propios:
        add(d)
    for req in (GetFavedStickersRequest(hash=0), GetRecentStickersRequest(hash=0)):
        try:
            r = await client(req)
            for d in getattr(r, "stickers", []):
                add(d)
        except Exception as e:
            print(f"No pude leer tus stickers guardados/recientes: {e}")
    print(f"🎟️ Stickers disponibles: {len(STICKERS)}")


# ==========================================
# OTROS USERBOTS: máximo BOT_LIMITE respuestas, luego los ignora
# ==========================================
BOT_LIMITE = 2      # respuestas máximas a un mismo userbot
BOT_RESET = 1800    # segundos de silencio de ese userbot para volver a responderle
BOT_MENSAJE = secreto("BOT_MENSAJE", False) or "No hablo con otros bot colega"  # lo que le dice UNA vez al pasar el límite
BOT_RAPIDO = 15     # si alguien te contesta en menos de esto...
BOT_RACHA = 5       # ...esta cantidad de veces seguidas, se marca como posible userbot
OTROS_BOTS = {}     # clave ("id:123" o "u:nombre") -> {"nombre", "claves"}
NO_BOTS = set()     # claves que marcaste como humanas (no se vuelve a sospechar de ellas)
CONT_BOT = {}       # (chat_id, user_id) -> {"n": respuestas dadas, "t": hora de la última}
RACHA = {}          # (chat_id, user_id) -> contestaciones rápidas seguidas
ULT_ENVIO = {}      # chat_id -> hora de mi último mensaje enviado
ULTIMO_DESTINO = {} # chat_id -> id de la última persona a la que respondí


def claves_de(emisor):
    if emisor is None:
        return []
    ks = []
    if getattr(emisor, "id", None):
        ks.append(f"id:{emisor.id}")
    u = (getattr(emisor, "username", None) or "").lower()
    if u:
        ks.append(f"u:{u}")
    return ks


def es_userbot(emisor):
    return any(k in OTROS_BOTS for k in claves_de(emisor))


def marcar_envio(chat_id, emisor, contar, sticker=False):
    """Se llama tras responder: guarda cuándo y a quién, y cuenta la respuesta si es otro userbot."""
    global RESPUESTAS_DADAS
    ULT_ENVIO[chat_id] = time.time()
    if contar:
        RESPUESTAS_DADAS += 1
        s = stats_dia()
        s["resp"] += 1
        if sticker:
            s["stickers"] += 1
        s["chats"][str(chat_id)] = s["chats"].get(str(chat_id), 0) + 1
        nom = (getattr(emisor, "first_name", None) or getattr(emisor, "username", None) or "") if emisor else ""
        if nom:
            s["personas"][nom] = s["personas"].get(nom, 0) + 1
        try:
            asyncio.get_running_loop().create_task(guardar_dia())
        except RuntimeError:
            pass
    if emisor is None or not getattr(emisor, "id", None):
        return
    ULTIMO_DESTINO[chat_id] = emisor.id
    if contar and es_userbot(emisor):
        d = CONT_BOT.setdefault((chat_id, emisor.id), {"n": 0, "t": 0})
        d["n"] += 1
        d["t"] = time.time()


async def guardar_otrobot(estado, claves, nombre):
    nombre = (nombre or "?").replace("|", "/").replace("\n", " ")
    await client.send_message("me", f"/otrobotdato {estado}|{','.join(claves)}|{nombre}")


async def marcar_userbot(claves, nombre, aviso):
    d = {"nombre": nombre, "claves": list(claves)}
    for k in claves:
        OTROS_BOTS[k] = d
    NO_BOTS.difference_update(claves)
    try:
        await guardar_otrobot("bot", claves, nombre)
        if aviso:
            ref = next((k[2:] for k in claves if k.startswith("u:")), None)
            ref = f"@{ref}" if ref else next((k[3:] for k in claves if k.startswith("id:")), "")
            await client.send_message(
                "me",
                f"🤖 {nombre} parece otro userbot (te contestó rapidísimo {BOT_RACHA} veces seguidas). "
                f"Le responderé máximo {BOT_LIMITE} mensajes, luego le diré «{BOT_MENSAJE}» una vez y lo ignoro hasta que "
                f"pasen {BOT_RESET // 60} min de silencio suyo.\nSi es una persona: /otrobot quitar {ref}",
            )
    except Exception as e:
        print(f"No pude guardar el userbot: {e}")


async def cargar_otrobots():
    OTROS_BOTS.clear()
    NO_BOTS.clear()
    vistos = set()
    async for m in client.iter_messages("me", limit=300, search="/otrobotdato"):
        t = (m.raw_text or "").strip()
        if not t.startswith("/otrobotdato "):
            continue
        partes = t[len("/otrobotdato "):].split("|", 2)
        if len(partes) < 3:
            continue
        cl = [c for c in partes[1].split(",") if c]
        if not cl or any(c in vistos for c in cl):  # el más reciente manda
            continue
        vistos.update(cl)
        if partes[0] == "bot":
            d = {"nombre": partes[2], "claves": cl}
            for k in cl:
                OTROS_BOTS[k] = d
        else:
            NO_BOTS.update(cl)
    print(f"🤖 Userbots marcados: {len({id(d) for d in OTROS_BOTS.values()})}")


@client.on(events.NewMessage(outgoing=True, pattern=r"^/otrobot(?:\s+(quitar))?(?:\s+(@?\w+))?$"))
async def cmd_otrobot(event):
    if event.chat_id != yo.id:
        return
    quitar, ref = event.pattern_match.group(1), event.pattern_match.group(2)
    if not ref:
        if quitar:
            await event.reply("Uso: /otrobot quitar @usuario")
            return
        unicos = {id(d): d for d in OTROS_BOTS.values()}
        if not unicos:
            await event.reply(
                f"No hay userbots marcados. Si uno te contesta rapidísimo {BOT_RACHA} veces seguidas lo detecto solo.\n"
                "Para marcarlo a mano: /otrobot @usuario"
            )
            return
        lin = [f"• {d['nombre']} ({', '.join(d['claves'])})" for d in unicos.values()]
        await event.reply(
            f"🤖 Userbots marcados (les respondo máx {BOT_LIMITE} y luego los ignoro):\n" + "\n".join(lin)
            + "\n\n/otrobot @usuario → marcar · /otrobot quitar @usuario → desmarcar"
        )
        return
    k = f"id:{ref}" if ref.isdigit() else f"u:{ref.lstrip('@').lower()}"
    if quitar:
        d = OTROS_BOTS.get(k)
        cl = d["claves"] if d else [k]
        for c in cl:
            OTROS_BOTS.pop(c, None)
        NO_BOTS.update(cl)
        await guardar_otrobot("humano", cl, d["nombre"] if d else ref)
        await event.reply(f"👤 Listo, {ref} vuelve a contar como persona (ya no lo limito).")
        return
    await marcar_userbot([k], ref, False)
    await event.reply(f"🤖 Marcado. A {ref} le responderé máximo {BOT_LIMITE} mensajes, luego le diré «{BOT_MENSAJE}» y lo ignoro.")


# ==========================================
# APRENDER DE GRUPOS: tu estilo, cómo respondes a otros y la jerga del grupo
# ==========================================
APRENDER_CHATS = set()
PARES = []   # (quién, su mensaje, tu respuesta)
VIBE = []    # mensajes de otros del grupo (jerga y contexto)
APRENDER_LIMITE = 800   # mensajes recientes que lee por grupo (con pausa para no provocar FloodWait)


async def aprender_chat(chat, candidatos, propios, pares, vibe, limite):
    """Lee el historial reciente de un grupo: tus mensajes (estilo), tus respuestas a otros y lo que dicen los demás."""
    msgs = []
    async for m in client.iter_messages(chat, limit=APRENDER_LIMITE, wait_time=1.5):
        msgs.append(m)  # del más nuevo al más viejo
    por_id = {m.id: m for m in msgs}
    for i, m in enumerate(msgs):
        if m.sticker and m.out and m.date < limite:
            propios.append(m.sticker)
            continue
        t = (m.raw_text or "").strip()
        if not t or "http" in t or t.startswith("/"):
            continue
        if m.out:
            if m.date >= limite:  # lo reciente puede haberlo escrito el bot: no se aprende de ahí
                continue
            if 3 <= len(t) <= 200:
                candidatos.add(t)
            previo = por_id.get(m.reply_to.reply_to_msg_id) if m.reply_to else None
            if previo is None and i + 1 < len(msgs):
                cand = msgs[i + 1]
                if not cand.out and (m.date - cand.date).total_seconds() < 120:
                    previo = cand
            if previo is not None and not previo.out:
                pt = (previo.raw_text or "").strip()
                if 3 <= len(pt) <= 200 and "http" not in pt and not getattr(previo.sender, "bot", False):
                    s = previo.sender
                    quien = getattr(s, "first_name", None) or getattr(s, "title", None) or "alguien"
                    pares.append((quien, pt, t))
        else:
            s = m.sender
            if getattr(s, "bot", False) or es_userbot(s):
                continue
            if 3 <= len(t) <= 120:
                vibe.add(t)


async def cargar_ejemplos():
    EJEMPLOS.clear()
    PARES.clear()
    VIBE.clear()
    chats = GRUPOS[:]
    if not chats:
        chats = [d.id async for d in client.iter_dialogs(limit=40) if d.is_group]
    limite = datetime.fromisoformat(ANTES_DE).replace(tzinfo=timezone.utc)
    candidatos = set()
    propios = []
    pares = []
    vibe = set()
    for chat in chats[:10]:
        if chat in APRENDER_CHATS:
            continue  # ese se lee completo más abajo
        try:
            async for m in client.iter_messages(chat, from_user="me", limit=200, offset_date=limite, wait_time=1.5):
                if m.sticker:
                    propios.append(m.sticker)
                    continue
                t = (m.raw_text or "").strip()
                if 3 <= len(t) <= 200 and "http" not in t and not t.startswith("/"):
                    candidatos.add(t)
        except FloodWaitError as e:
            print(f"⏳ FloodWait leyendo {chat}: espero {min(e.seconds, 120)}s")
            await asyncio.sleep(min(e.seconds, 120))
        except Exception as e:
            print(f"No pude leer {chat}: {e}")
        await asyncio.sleep(2)
    for chat in list(APRENDER_CHATS):
        try:
            await aprender_chat(chat, candidatos, propios, pares, vibe, limite)
        except FloodWaitError as e:
            print(f"⏳ FloodWait aprendiendo de {chat}: espero {min(e.seconds, 120)}s")
            await asyncio.sleep(min(e.seconds, 120))
        except Exception as e:
            print(f"No pude aprender de {chat}: {e}")
        await asyncio.sleep(2)
    candidatos = list(candidatos)
    random.shuffle(candidatos)
    EJEMPLOS.extend(candidatos[:300])
    random.shuffle(pares)
    PARES.extend(pares[:200])
    vibe = list(vibe)
    random.shuffle(vibe)
    VIBE.extend(vibe[:200])
    print(f"🧠 Ejemplos de tu estilo: {len(EJEMPLOS)} · respuestas tuyas a otros: {len(PARES)} · mensajes del grupo: {len(VIBE)}")
    await cargar_stickers(propios)


async def cambiar_modelo_agotado():
    """El modelo activo se quedó sin cuota diaria: pasa al siguiente que aún tenga."""
    global MODELO_ACTIVO
    ahora = time.time()
    previo = MODELO_ACTIVO
    AGOTADOS[previo] = ahora
    for m in CANDIDATOS:
        if m != previo and ahora - AGOTADOS.get(m, 0) > 23 * 3600:
            MODELO_ACTIVO = m
            print(f"🔄 {previo} sin cuota diaria → cambié a {m}")
            try:
                await client.send_message("me", f"⚠️ {previo} se quedó sin cuota diaria. Cambié a {m}.")
            except Exception:
                pass
            return True
    print("❌ Todos los modelos se quedaron sin cuota diaria.")
    return False


async def generar_gemini(contenido, sistema=None, max_tokens=1000, temperatura=None):
    reintentos = 0
    while True:
        modelo_usado = MODELO_ACTIVO
        try:
            resp = await ai_client.aio.models.generate_content(
                model=modelo_usado,
                contents=contenido,
                config={
                    "system_instruction": sistema or prompt_sistema(),
                    "max_output_tokens": max_tokens,
                    **({"temperature": temperatura} if temperatura is not None else {}),
                },
            )
            registrar_uso(modelo_usado, resp)
            return resp
        except Exception as e:
            msg = str(e)
            if "429" in msg:
                registrar_uso(modelo_usado, error429=True)
            if "429" in msg and "PerDay" in msg:
                if await cambiar_modelo_agotado():
                    continue
                raise
            reintentos += 1
            if reintentos >= 3 or not any(c in msg for c in ("503", "429")):
                raise
            await asyncio.sleep(3 * reintentos)


async def generar(contenido, sistema=None, max_tokens=1000, temperatura=None):
    return await generar_gemini(contenido, sistema, max_tokens, temperatura)


AUTO = True       # /auto on|off → detectar cuándo te hablan sin mencionarte
SILENCIO_MANUAL = int(secreto("SILENCIO_MANUAL", False) or 120)  # segundos que el bot se calla en un chat cuando hablas TÚ (0 = desactivado)
MANUAL = {}        # chat_id -> hora del último mensaje escrito por ti (no por el bot)
BOT_TEXTOS = {}    # chat_id -> [(hora, texto normalizado)] de lo último que mandó el bot
BOT_STICKER = {}   # chat_id -> hora del último sticker que mandó el bot


def _norm(s):
    return re.sub(r"\W+", "", (s or "").lower())


def anotar_envio_bot(chat_id, texto=None, sticker=False):
    """El bot escribe con TU cuenta: se anota lo que manda para no confundirlo contigo."""
    ahora = time.time()
    if sticker:
        BOT_STICKER[chat_id] = ahora
    if texto:
        lst = [(t, x) for t, x in BOT_TEXTOS.get(chat_id, []) if ahora - t < 90]
        lst.append((ahora, _norm(texto)))
        BOT_TEXTOS[chat_id] = lst[-10:]


def es_del_bot(event):
    ahora = time.time()
    n = _norm(event.raw_text)
    if not n:
        return bool(getattr(event.message, "sticker", None)) and ahora - BOT_STICKER.get(event.chat_id, 0) < 15
    return any(ahora - t < 90 and x == n for t, x in BOT_TEXTOS.get(event.chat_id, []))


def usuario_hablo(chat_id, desde):
    return bool(SILENCIO_MANUAL) and MANUAL.get(chat_id, 0) > desde


VENTANA = 180     # segundos después de que hables en un chat durante los que escucha la charla
ULTIMA_MIA = {}   # chat_id -> hora de tu último mensaje (tuyo o del bot)
# Grupo(s) donde detecta solo cuándo le hablan (sin mención). En los demás solo responde si te señalan.
AUTO_CHATS = {int(x) for x in secreto("GRUPO_AUTO", False).split(",") if x.strip()}


async def leer_auto_guardado():
    async for m in client.iter_messages("me", limit=200, search="/auto grupo"):
        t = (m.raw_text or "").strip()
        if t.startswith("/auto grupo "):
            v = t[len("/auto grupo "):].strip().lower()
            if v == "borrar":
                return set()
            try:
                return {norm_id(int(v))}
            except ValueError:
                continue
    return None

ultimo_clasif = {}
ALIAS_EXTRA = [a.strip() for a in secreto("ALIAS", False).split(",") if a.strip()]


@client.on(events.NewMessage(outgoing=True))
async def registrar_actividad(event):
    if event.is_group:
        ULTIMA_MIA[event.chat_id] = time.time()
    if (yo is not None and event.chat_id == yo.id) or not (event.is_group or event.is_private):
        return
    if (event.raw_text or "").strip().startswith("/"):
        return  # tus comandos no cuentan como conversación
    if es_del_bot(event):
        return  # lo mandó el bot con tu cuenta
    MANUAL[event.chat_id] = time.time()


def alias_propios():
    nombres = set(NOMBRE.split()) | {NOMBRE, getattr(yo, "first_name", None), getattr(yo, "username", None)}
    nombres |= set(ALIAS_EXTRA)
    return [n for n in nombres if n and len(n) >= 3]


def menciona_mi_nombre(texto):
    t = texto.lower()
    return any(re.search(rf"(?<!\w){re.escape(a.lower())}(?!\w)", t) for a in alias_propios())


async def armar_contexto(event, citado, emisor):
    # En grupos con temas (foros) lee solo el tema actual, no todo el grupo
    rt = event.message.reply_to
    hilo = None
    if rt:
        hilo = getattr(rt, "reply_to_top_id", None) or (
            rt.reply_to_msg_id if getattr(rt, "forum_topic", False) else None
        )
    try:
        if hilo:
            historial = await client.get_messages(event.chat_id, limit=10, reply_to=hilo)
        else:
            historial = await client.get_messages(event.chat_id, limit=10)
    except Exception:
        historial = await client.get_messages(event.chat_id, limit=10)
    lineas = []
    for m in reversed(historial):
        if not m.raw_text:
            continue
        quien = NOMBRE if m.out else (getattr(m.sender, "first_name", None) or "Alguien")
        lineas.append(f"{quien}: {m.raw_text}")
    autor = getattr(emisor, "first_name", None) or "Alguien"
    extra = ""
    if citado and citado.raw_text:
        extra = f'\nEse mensaje es una respuesta a este mensaje anterior de {NOMBRE}: "{citado.raw_text}"'
    return lineas, extra, autor


async def va_para_mi(lineas, autor, texto):
    try:
        r = await generar(
            "Conversación reciente:\n" + "\n".join(lineas)
            + f'\n\nÚltimo mensaje, de {autor}: "{texto}"\n\n'
            f"¿Ese último mensaje va dirigido a {NOMBRE} o espera respuesta suya? "
            f"Responde SI si le hablan a {NOMBRE} directamente, le preguntan algo, o continúan una charla "
            f"que {NOMBRE} acaba de tener con esa persona. Responde NO si es un comentario general al grupo "
            "o va dirigido a otra persona. Responde SOLO SI o NO.",
            sistema="Clasificas mensajes de chats grupales. Respondes únicamente SI o NO.",
            max_tokens=500,
        )
        t = (r.text or "").strip().upper()
        return t.startswith("SI") or t.startswith("SÍ")
    except Exception as e:
        print(f"No pude clasificar el mensaje: {e}")
        return False


RECIENTES = {}  # chat_id -> últimas respuestas del bot (para no repetirse)


def es_repetida(texto, chat_id):
    t = texto.lower().strip()
    for r in RECIENTES.get(chat_id, [])[-8:]:
        if difflib.SequenceMatcher(None, t, r.lower()).ratio() > 0.8:
            return True
    return False


async def enviar(event, texto, citar):
    """Envía como respuesta citada o como mensaje suelto (respetando el tema en grupos con foros)."""
    anotar_envio_bot(event.chat_id, texto)
    if citar:
        return await event.reply(texto)
    rt = event.message.reply_to
    top = None
    if rt:
        top = getattr(rt, "reply_to_top_id", None) or (
            rt.reply_to_msg_id if getattr(rt, "forum_topic", False) else None
        )
    if top:
        return await client.send_message(event.chat_id, texto, reply_to=top)
    return await event.respond(texto)


PRIVADOS = secreto("PRIVADOS", False).lower() != "off"  # responder mensajes privados (pon PRIVADOS=off para desactivar)
SOLO_CONTACTOS = secreto("SOLO_CONTACTOS", False).lower() == "on"  # on = en privado solo responde a tus contactos


@client.on(events.NewMessage(incoming=True))
async def handle_message(event):
    global FLOOD_HASTA
    if PAUSA or time.time() < FLOOD_HASTA:
        return
    if SILENCIO_MANUAL and time.time() - MANUAL.get(event.chat_id, 0) < SILENCIO_MANUAL:
        return  # estás hablando tú en este chat: el bot se calla
    if event.is_group:
        if GRUPOS and event.chat_id not in GRUPOS:
            return
    elif event.is_private:
        if not PRIVADOS:
            return
    else:
        return  # canales
    texto = event.raw_text
    if not texto:
        return

    emisor = await event.get_sender()
    if emisor is not None and getattr(emisor, "bot", False):
        return  # evita charlas infinitas con otros bots
    if emisor is not None and getattr(emisor, "id", None):
        cl = claves_de(emisor)
        llave = (event.chat_id, emisor.id)
        if any(k in OTROS_BOTS for k in cl):  # otro userbot: máximo BOT_LIMITE respuestas
            d = CONT_BOT.get(llave)
            if d and time.time() - d["t"] > BOT_RESET:
                CONT_BOT.pop(llave, None)
                d = None
            if d and d["n"] >= BOT_LIMITE:
                d["t"] = time.time()  # mientras siga hablando, no se reinicia el conteo
                if not d.get("aviso"):  # se despide UNA sola vez y luego lo ignora
                    d["aviso"] = True
                    try:
                        await asyncio.sleep(random.uniform(2, 4))
                        await enviar(event, BOT_MENSAJE, not event.is_private)
                        marcar_envio(event.chat_id, emisor, False)
                        print(f"🤖 Le dije a otro userbot: {BOT_MENSAJE}")
                        stats_dia()["bots"] += 1
                    except FloodWaitError as e:
                        FLOOD_HASTA = time.time() + e.seconds + 5
                    except Exception as e:
                        print(f"No pude avisarle al userbot: {e}")
                else:
                    print("🤖 Otro userbot sigue hablando, lo ignoro.")
                return
        elif not any(k in NO_BOTS for k in cl):  # ¿contesta demasiado rápido para ser persona?
            if ULTIMO_DESTINO.get(event.chat_id) == emisor.id and time.time() - ULT_ENVIO.get(event.chat_id, 0) < BOT_RAPIDO:
                RACHA[llave] = RACHA.get(llave, 0) + 1
            else:
                RACHA[llave] = 0
            if RACHA[llave] >= BOT_RACHA:
                RACHA[llave] = 0
                nombre = getattr(emisor, "first_name", None) or getattr(emisor, "username", None) or str(emisor.id)
                asyncio.create_task(marcar_userbot(cl, nombre, True))
    if event.is_private and SOLO_CONTACTOS and not getattr(emisor, "contact", False):
        return

    citado = await event.get_reply_message() if event.is_reply else None
    directo = (
        event.is_private  # en privado todo mensaje va dirigido a ti
        or bool(event.message.mentioned)
        or bool(citado and citado.sender_id == yo.id)
        or menciona_mi_nombre(texto)
    )
    # Charla en curso: tú (o el bot) hablaste hace poco en este chat
    en_charla = AUTO and event.chat_id in AUTO_CHATS
    if AUSENTE:
        if directo:
            await responder_ausente(event, emisor)
        return
    if not directo and not en_charla:
        return
    if not directo:
        # filtros baratos antes de gastar una llamada a Gemini
        if citado and citado.sender_id != yo.id:
            return  # respuesta a otra persona
        if re.search(r"@\w+", texto):
            return  # menciona a otro usuario
        if len(texto) < 4:
            return

    ahora = time.time()
    if ahora - ultima_respuesta.get(event.chat_id, 0) < COOLDOWN:
        return
    if not directo and ahora - ultimo_clasif.get(event.chat_id, 0) < 3:
        return

    try:
        print(f"Mensaje recibido: {texto}")
        lineas, extra, autor = await armar_contexto(event, citado, emisor)
        if not directo:
            ultimo_clasif[event.chat_id] = ahora
            if not await va_para_mi(lineas, autor, texto):
                print("↪️ No iba dirigido a mí, lo ignoro.")
                return
        inicio_resp = time.time()
        ultima_respuesta[event.chat_id] = inicio_resp
        nota = MEMORIA.get(getattr(emisor, "id", None), {}).get("notas") if MEMORIA_ON else None
        extra_mem = (
            f"\nLo que sabes de {autor} (úsalo solo si viene al caso, sin forzarlo): {nota}" if nota else ""
        )
        extra_amigo = ""
        uname = (getattr(emisor, "username", None) or "").lower()
        am = AMIGOS.get(uname) if uname else None
        if am:
            extra_amigo = (
                f"\nSobre {autor} (es tu amigo; esto lo sabes de él/ella desde hace tiempo): {am['gustos']}. "
                "Tenlo de fondo; lo principal es responder con coherencia a lo que dice."
            )
            if random.random() < AMIGO_PROB and ahora - ULTIMA_MENCION.get(uname, 0) > AMIGO_ESPERA:
                ULTIMA_MENCION[uname] = ahora
                extra_amigo += (
                    " Esta vez, SOLO si encaja de forma natural con lo que está diciendo, deja caer UNO de esos "
                    "gustos como lo haría un amigo (un comentario o chiste corto). Si no encaja, no lo menciones. "
                    "Nunca digas que lo tienes apuntado ni suenes a ficha."
                )
        extra_modo = ""
        md = MODOS.get(event.chat_id)
        if md and md["modo"] != "auto":
            extra_modo = f"\nTono que debes usar en ESTE chat ({md['modo']}): {md['texto']}"
        elif (md and md["modo"] == "auto") or (MODO_AUTO and event.is_group and not md):
            tono = await tono_para(event.chat_id)
            if tono:
                extra_modo = (
                    f"\nAmbiente de ESTE chat (detectado de sus mensajes): {tono} "
                    f"Ajusta SOLO cuánto bromeas, qué tan serio o relajado y qué tan breve eres a ese ambiente, pero escribe "
                    f"SIEMPRE exactamente a tu estilo de {NOMBRE} (tus muletillas, tu forma de escribir y tus ejemplos); "
                    "no imites la forma de escribir de los demás."
                )
        previas_mias = RECIENTES.get(event.chat_id, [])[-8:]
        evitar = (
            "\n\nTus últimas respuestas en este chat (NO repitas sus frases, muletillas, emojis ni su estructura):\n"
            + "\n".join(f"- {r}" for r in previas_mias)
        ) if previas_mias else ""
        contenido = (
            "Conversación reciente:\n" + "\n".join(lineas)
            + f'\n\nÚltimo mensaje, de {autor}: "{texto}"{extra}{extra_mem}{extra_amigo}{extra_modo}{evitar}'
            + f"\n\nResponde como {NOMBRE} a ESE último mensaje, de forma coherente con la conversación. "
            "Escribe solo el mensaje."
        )
        response = await generar(contenido, temperatura=1.2)
        respuesta = (response.text or "").strip()
        if not respuesta:
            return
        if es_repetida(respuesta, event.chat_id):
            response = await generar(
                contenido + "\n\nOJO: tu borrador se parece demasiado a una respuesta reciente. "
                "Escribe algo claramente distinto.",
                temperatura=1.4,
            )
            respuesta = (response.text or "").strip() or respuesta
        registro = RECIENTES.setdefault(event.chat_id, [])
        st = re.search(r"\[STICKER:?\s*([^\]\n]*)\]?", respuesta)
        if st and STICKERS:
            emo = st.group(1).strip()
            opciones = [d for d, e in STICKERS if e == emo] or [d for d, _ in STICKERS]
            await asyncio.sleep(random.uniform(0.5, 1.5))
            if usuario_hablo(event.chat_id, inicio_resp):
                print("🤫 Hablaste tú mientras preparaba el sticker, no lo mando.")
                return
            anotar_envio_bot(event.chat_id, sticker=True)
            await event.reply(file=random.choice(opciones))
            print(f"Sticker enviado ({emo})")
            marcar_envio(event.chat_id, emisor, True, sticker=True)
            registro.append(f"[sticker {emo}]")
            del registro[:-10]
            if MEMORIA_ON and emisor is not None and len(texto) >= 4:
                asyncio.create_task(actualizar_memoria(emisor.id, autor, texto, f"(mandó un sticker {emo})", lineas))
            return
        respuesta = re.sub(r"\[STICKER[^\]\n]*\]?", "", respuesta).strip()
        if not respuesta:
            return
        partes = [x.strip() for x in re.split(r"\s*\|\|\|\s*", respuesta) if x.strip()][:3]
        for i, parte in enumerate(partes):
            if i == 0:
                await asyncio.sleep(random.uniform(*LECTURA))  # primero "lee" el mensaje
            else:
                await asyncio.sleep(random.uniform(0.5, 2))  # pausa corta entre mensajes seguidos
            escribiendo = len(parte) * VEL_ESCRIBIR * random.uniform(0.8, 1.3)
            escribiendo = max(ESCRIBIR_MIN_MAX[0], min(escribiendo, ESCRIBIR_MIN_MAX[1]))
            if i > 0:
                escribiendo = max(1.5, escribiendo * 0.6)
            async with client.action(event.chat_id, "typing"):  # luego se ve "escribiendo..."
                await asyncio.sleep(escribiendo)
            if usuario_hablo(event.chat_id, inicio_resp):
                print("🤫 Hablaste tú mientras escribía, cancelo el resto de la respuesta.")
                return
            # en grupos la primera parte cita el mensaje; en privado casi nunca (como hace la gente)
            citar = (i == 0) and (not event.is_private or random.random() < 0.2)
            await enviar(event, parte, citar)
            print(f"Respuesta enviada: {parte}")
            marcar_envio(event.chat_id, emisor, False)
        registro.append(" / ".join(partes))
        del registro[:-10]
        marcar_envio(event.chat_id, emisor, True)
        if MEMORIA_ON and emisor is not None and len(texto) >= 4:
            asyncio.create_task(actualizar_memoria(emisor.id, autor, texto, " / ".join(partes), lineas))
    except FloodWaitError as e:
        FLOOD_HASTA = time.time() + e.seconds + 5
        print(f"⏳ Telegram pide esperar {e.seconds}s ({type(e.request).__name__}). Pauso las respuestas hasta entonces.")
        registrar_error(f"FloodWait {e.seconds}s ({type(e.request).__name__})")
    except Exception as e:
        print(f"Error al generar respuesta con IA: {e}")
        registrar_error(f"IA/respuesta: {e}")


@client.on(events.NewMessage(outgoing=True, pattern=r"^/modelo(?:\s+(\S+))?$"))
async def cambiar_modelo(event):
    global MODELO_ACTIVO
    if event.chat_id != yo.id:  # solo funciona en "Mensajes guardados"
        return
    arg = event.pattern_match.group(1)
    if not arg:
        lineas = [
            f"{i}. {m}{' ✅' if m == MODELO_ACTIVO else ''}{' ⚠️' if m in NO_TEXTO else ''}"
            for i, m in enumerate(MODELOS_DISP, 1)
        ]
        trozos, actual = [], f"Modelo actual: {MODELO_ACTIVO}\n\n"
        for linea in lineas:
            if len(actual) + len(linea) > 3500:
                trozos.append(actual)
                actual = ""
            actual += linea + "\n"
        actual += "\nCambia con /modelo <nombre o número>\n/modelo auto = elegir solo\n⚠️ = no genera texto"
        trozos.append(actual)
        for t in trozos:
            await event.reply(t)
        return
    if arg == "auto":
        MODELO_ACTIVO = obtener_mejor_modelo()
        aviso = ""
    elif arg.isdigit() and 1 <= int(arg) <= len(MODELOS_DISP):
        MODELO_ACTIVO = MODELOS_DISP[int(arg) - 1]
        aviso = ""
    else:
        MODELO_ACTIVO = arg.replace("models/", "")
        aviso = "" if MODELO_ACTIVO in MODELOS_DISP else "\n(ese nombre no está en la lista, puede dar error)"
    await event.reply(f"✅ Modelo cambiado a: {MODELO_ACTIVO}{aviso}")


def ia_activa():
    return f"Gemini ({MODELO_ACTIVO})"




TRIVIA = True  # /trivia on|off en Mensajes guardados
# Grupo(s) donde juega trivia. Se elige con /trivia aqui (escrito dentro del grupo) o /trivia grupo <id>
TRIVIA_CHATS = {int(x) for x in secreto("GRUPO_TRIVIA", False).split(",") if x.strip()}


def _txt(x):
    return x if isinstance(x, str) else getattr(x, "text", str(x))


@client.on(events.NewMessage(incoming=True))
async def votar_trivia(event):
    if PAUSA or not TRIVIA or event.chat_id not in TRIVIA_CHATS or not event.is_group or not event.poll:
        return
    encuesta = event.poll.poll
    if not encuesta.quiz or encuesta.closed or encuesta.multiple_choice:
        return
    pregunta = _txt(encuesta.question)
    opciones = [_txt(a.text) for a in encuesta.answers]
    lista = "\n".join(f"{i}. {o}" for i, o in enumerate(opciones, 1))
    try:
        print(f"🎲 Trivia: {pregunta}")
        r = await generar(
            f"Pregunta de trivia: {pregunta}\nOpciones:\n{lista}\n\n"
            "Responde SOLO con el número de la opción correcta.",
            sistema="Eres experto en videojuegos, cine, series, anime y cultura general. "
                    "Respondes únicamente con el número de la opción correcta, sin texto adicional.",
            max_tokens=1000,
        )
        n = re.search(r"\d+", r.text or "")
        if not n or not (1 <= int(n.group()) <= len(opciones)):
            print(f"No pude decidir la respuesta: {r.text}")
            return
        elegida = int(n.group())
        await asyncio.sleep(random.uniform(5, 15))
        await client(SendVoteRequest(
            peer=await event.get_input_chat(),
            msg_id=event.id,
            options=[encuesta.answers[elegida - 1].option],
        ))
        print(f"🗳️ Voté: {elegida}. {opciones[elegida - 1]}")
    except Exception as e:
        print(f"Error en la trivia: {e}")


async def leer_trivia_guardada():
    """Busca en Mensajes guardados tu último '/trivia grupo <id>' (así se conserva al reiniciar)."""
    async for m in client.iter_messages("me", limit=200, search="/trivia grupo"):
        t = (m.raw_text or "").strip()
        if t.startswith("/trivia grupo "):
            v = t[len("/trivia grupo "):].strip().lower()
            if v == "borrar":
                return set()
            try:
                return {norm_id(int(v))}
            except ValueError:
                continue
    return None


@client.on(events.NewMessage(outgoing=True, pattern=r"^/trivia(?:\s+(.+))?$"))
async def cmd_trivia(event):
    global TRIVIA, TRIVIA_CHATS
    arg = (event.pattern_match.group(1) or "").strip().lower()

    # Escrito DENTRO del grupo: "/trivia aqui" → ahí jugará (y borra el mensaje)
    if arg in ("aqui", "aquí"):
        if not event.is_group:
            return
        TRIVIA_CHATS = {event.chat_id}
        await event.delete()
        await client.send_message("me", f"/trivia grupo {event.chat_id}")  # se guarda como memoria
        return

    if event.chat_id != yo.id:  # lo demás solo en "Mensajes guardados"
        return
    if arg.startswith("grupo"):
        valor = arg[5:].strip()
        if valor == "borrar":
            TRIVIA_CHATS = set()
        else:
            try:
                TRIVIA_CHATS = {norm_id(int(valor))}
            except ValueError:
                await event.reply("ID no válido. Usa /trivia grupo <id>, o escribe /trivia aqui dentro del grupo.")
                return
    elif arg in ("on", "off"):
        TRIVIA = (arg == "on")

    nombres = []
    for cid in TRIVIA_CHATS:
        try:
            nombres.append(getattr(await client.get_entity(cid), "title", str(cid)))
        except Exception:
            nombres.append(str(cid))
    await event.reply(
        f"🎲 Trivia: {'activada' if TRIVIA else 'desactivada'}\n"
        f"Grupo: {', '.join(nombres) if nombres else 'ninguno (no jugará en ningún grupo)'}"
    )


@client.on(events.NewMessage(outgoing=True, pattern=r"^/auto(?:\s+(on|off|aqui|aquí|borrar|grupo\s+-?\d+))?$"))
async def cmd_auto(event):
    global AUTO, AUTO_CHATS
    arg = event.pattern_match.group(1)
    # Escrito DENTRO del grupo: "/auto aqui" → solo ahí detecta sin que lo señalen
    if arg in ("aqui", "aquí"):
        if not event.is_group:
            return
        AUTO_CHATS = {event.chat_id}
        await event.delete()
        await client.send_message("me", f"/auto grupo {event.chat_id}")  # se conserva al reiniciar
        return
    if event.chat_id != yo.id:  # lo demás solo en "Mensajes guardados"
        return
    if arg == "borrar":
        AUTO_CHATS = set()
        await client.send_message("me", "/auto grupo borrar")
    elif arg and arg.startswith("grupo"):
        AUTO_CHATS = {norm_id(int(arg.split()[1]))}  # este mismo mensaje queda guardado y se conserva al reiniciar
    elif arg in ("on", "off"):
        AUTO = (arg == "on")
    await event.reply(
        f"👂 Detección automática: {'activada' if AUTO else 'desactivada'}\n"
        f"Grupos con detección: {len(AUTO_CHATS)} (en los demás solo responde si te señalan)"
    )


async def leer_estilo_guardado():
    """Busca en Mensajes guardados tu último '/estilo <texto>' (así se conserva al reiniciar)."""
    async for m in client.iter_messages("me", limit=200, search="/estilo"):
        t = (m.raw_text or "").strip()
        if t.startswith("/estilo "):
            txt = t[len("/estilo "):].strip()
            return "" if txt.lower() == "borrar" else txt
    return None


@client.on(events.NewMessage(outgoing=True, pattern=r"^/estilo(?:\s+([\s\S]+))?$"))
async def recargar_estilo(event):
    global ESTILO
    if event.chat_id != yo.id:  # solo en "Mensajes guardados"
        return
    nuevo = event.pattern_match.group(1)
    if nuevo:  # /estilo <texto>  → guarda tus notas de estilo / jerga
        nuevo = nuevo.strip()
        ESTILO = "" if nuevo.lower() == "borrar" else nuevo
    else:  # /estilo → recarga
        guardado = await leer_estilo_guardado()
        ESTILO = guardado if guardado is not None else secreto("ESTILO", False)
    await cargar_ejemplos()
    await event.reply(
        f"🧠 Aprendí de {len(EJEMPLOS)} mensajes tuyos"
        + (" + tus notas de estilo" if ESTILO else "")
        + f" y {len(STICKERS)} stickers."
    )


# ==========================================
# REENVIAR mensajes de grupos/canales (incluso privados) a otro chat
# ==========================================
REENVIAR = True
ORIGENES = {int(x) for x in secreto("REENVIAR_ORIGENES", False).split(",") if x.strip()}
DESTINO = int(secreto("REENVIAR_DESTINO", False) or 0) or None
SOLO_VIDEO = True  # True = solo reenvía videos (incluye archivos .mkv, .mp4, etc.)


def es_video(msg):
    f = msg.file
    if not f or msg.gif or msg.sticker:
        return False
    if (f.mime_type or "").startswith("video/"):
        return True
    return (f.ext or "").lower() in (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv", ".wmv")


async def guardar_reenvio():
    await client.send_message("me", f"/reenviar config {','.join(map(str, ORIGENES))}|{DESTINO or ''}|{'video' if SOLO_VIDEO else 'todo'}")


async def leer_reenvio_guardado():
    async for m in client.iter_messages("me", limit=200, search="/reenviar config"):
        t = (m.raw_text or "").strip()
        if t.startswith("/reenviar config"):
            partes = t[len("/reenviar config"):].strip().split("|")
            o, d = partes[0], (partes[1] if len(partes) > 1 else "")
            video = not (len(partes) > 2 and partes[2].strip() == "todo")
            try:
                return {norm_id(int(x)) for x in o.split(",") if x.strip()}, (norm_id(int(d)) if d.strip() else None), video
            except ValueError:
                continue
    return None


async def nombre_chat(cid):
    if cid == yo.id:
        return "Mensajes guardados"
    try:
        return getattr(await client.get_entity(cid), "title", str(cid))
    except Exception:
        return str(cid)


@client.on(events.NewMessage(incoming=True))
async def reenviar_mensajes(event):
    if PAUSA or not REENVIAR or not DESTINO or event.chat_id not in ORIGENES or event.chat_id == DESTINO:
        return
    if SOLO_VIDEO and not es_video(event.message):
        return
    try:
        await client.forward_messages(DESTINO, event.message)
    except Exception as e:
        print(f"No pude reenviar (¿contenido restringido?): {e}")
        try:  # plan B: copiar solo el texto
            if event.raw_text and not SOLO_VIDEO:
                quien = getattr(await event.get_sender(), "first_name", None) or "Alguien"
                await client.send_message(DESTINO, f"{quien}: {event.raw_text}")
        except Exception as e2:
            print(f"Tampoco pude copiar el texto: {e2}")


CURSORES = {}      # chat_id -> id del video más viejo ya reenviado (0 = empezar desde los más recientes)
TRAYENDO = set()   # grupos con un /traer en curso


async def guardar_cursor(cid, mid):
    await client.send_message("me", f"/reenviar cursor {cid} {mid}")  # se conserva al reiniciar


async def leer_cursores_guardados():
    async for m in client.iter_messages("me", limit=500, search="/reenviar cursor"):
        partes = (m.raw_text or "").split()
        if len(partes) == 4 and partes[0] == "/reenviar" and partes[1] == "cursor":
            try:
                cid, mid = int(partes[2]), int(partes[3])
            except ValueError:
                continue
            CURSORES.setdefault(cid, mid)  # el primero que aparece es el más reciente
    print(f"📍 Grupos con progreso guardado: {len(CURSORES)}")


async def traer_videos(cid, n):
    """Reenvía los n videos más recientes de ese grupo que aún no se han traído. Devuelve (cuántos, error)."""
    destino = DESTINO or yo.id  # sin destino configurado → Mensajes guardados
    cursor = CURSORES.get(cid) or 0
    ids = set()
    for filtro in (InputMessagesFilterVideo(), InputMessagesFilterDocument()):
        hallados = 0
        async for m in client.iter_messages(cid, filter=filtro, offset_id=cursor):
            if es_video(m):
                ids.add(m.id)
                hallados += 1
                if hallados >= n:
                    break
    elegidos = sorted(sorted(ids, reverse=True)[:n])  # los n más nuevos, reenviados en orden cronológico
    if not elegidos:
        return 0, None
    while True:  # un solo reenvío: o salen todos o ninguno, así el progreso nunca queda a medias
        try:
            await client.forward_messages(destino, elegidos, from_peer=cid)
            break
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            print(f"/traer: no pude reenviar: {e}")
            return 0, ("restringido" if "RESTRICTED" in str(e).upper() else str(e))
    CURSORES[cid] = elegidos[0]
    await guardar_cursor(cid, elegidos[0])
    return len(elegidos), None


@client.on(events.NewMessage(outgoing=True, pattern=r"^/(?:traer|historial)(?:\s+(reiniciar))?(?:\s+(-?\d+|@\w+))?(?:\s+(\d+))?$"))
async def cmd_traer(event):
    if event.chat_id != yo.id:
        return
    reiniciar, ref, cant = event.pattern_match.group(1), event.pattern_match.group(2), event.pattern_match.group(3)
    if ref:
        try:
            cid = norm_id(int(ref)) if not ref.startswith("@") else utils.get_peer_id(await client.get_entity(ref))
        except Exception:
            await event.reply("No encontré ese chat. Usa /chats para ver su ID.")
            return
    elif len(ORIGENES) == 1:
        cid = next(iter(ORIGENES))
    else:
        await event.reply("Dime el grupo: /traer <id>  (usa /chats para ver los IDs).")
        return
    nombre = await nombre_chat(cid)
    if reiniciar:
        CURSORES[cid] = 0
        await guardar_cursor(cid, 0)
        await event.reply(f"🔄 Empezaré de nuevo desde los videos más recientes de {nombre}.")
        return
    if cid in TRAYENDO:
        await event.reply("Ya estoy trayendo videos de ese grupo, espera a que termine.")
        return
    n = max(1, min(int(cant), 100)) if cant else 50
    TRAYENDO.add(cid)
    try:
        await event.reply(f"🔎 Buscando los siguientes {n} videos de {nombre}…")
        k, error = await traer_videos(cid, n)
    except Exception as e:
        k, error = 0, str(e)
    finally:
        TRAYENDO.discard(cid)
    if error == "restringido":
        await event.reply(f"🚫 {nombre} tiene el contenido restringido: Telegram no deja reenviar.")
    elif error:
        await event.reply(f"❌ No pude terminar: {error}")
    elif k == 0:
        await event.reply(f"✅ Ya no hay más videos en {nombre}. Ya te reenvié todos.")
    elif k < n:
        await event.reply(f"✅ Reenvié {k} videos. Con esto ya no queda ninguno más en {nombre}.")
    else:
        await event.reply(f"✅ Reenvié {k} videos de {nombre}. Escribe /traer otra vez para los siguientes {n}.")


@client.on(events.NewMessage(outgoing=True, pattern=r"^/reenviar(?:\s+(origen|destino|quitar|on|off|borrar|video|todo))?(?:\s+(-?\d+|@\w+))?$"))
async def cmd_reenviar(event):
    global REENVIAR, ORIGENES, DESTINO, SOLO_VIDEO
    arg = (event.pattern_match.group(1) or "").lower()
    cid_arg = event.pattern_match.group(2)
    en_guardados = event.chat_id == yo.id
    if arg in ("origen", "quitar", "destino") and cid_arg:
        # Forma segura: se escribe en Mensajes guardados con el ID, sin tocar el grupo
        if not en_guardados:
            return
        try:
            cid = int(cid_arg) if not cid_arg.startswith("@") else utils.get_peer_id(await client.get_entity(cid_arg))
            cid = norm_id(cid)
        except Exception:
            await event.reply("No encontré ese chat. Usa /chats para ver su ID.")
            return
        if arg == "origen":
            ORIGENES.add(cid)
        elif arg == "quitar":
            ORIGENES.discard(cid)
        else:
            DESTINO = cid
        await guardar_reenvio()
    elif arg in ("origen", "quitar"):
        if en_guardados:
            await event.reply("Escribe /reenviar " + arg + " DENTRO del grupo o canal de origen.")
            return
        if arg == "origen":
            ORIGENES.add(event.chat_id)
        else:
            ORIGENES.discard(event.chat_id)
        await event.delete()
        await guardar_reenvio()
        return
    elif arg == "destino":
        DESTINO = event.chat_id  # el chat donde lo escribes (o Mensajes guardados)
        await guardar_reenvio()
        if not en_guardados:
            await event.delete()
            return
    if not en_guardados:
        return
    if arg == "borrar":
        ORIGENES, DESTINO = set(), None
        await guardar_reenvio()
    elif arg in ("on", "off"):
        REENVIAR = (arg == "on")
    elif arg in ("video", "todo"):
        SOLO_VIDEO = (arg == "video")
        await guardar_reenvio()
    origenes = [await nombre_chat(c) for c in ORIGENES]
    await event.reply(
        f"📨 Reenvío: {'activado' if REENVIAR else 'desactivado'}\n"
        f"Origen: {', '.join(origenes) if origenes else 'ninguno'}\n"
        f"Destino: {await nombre_chat(DESTINO) if DESTINO else 'ninguno'}\n"
        f"Contenido: {'solo videos' if SOLO_VIDEO else 'todo'}"
    )


@client.on(events.NewMessage(outgoing=True, pattern=r"^/chats(?:\s+(.+))?$"))
async def cmd_chats(event):
    if event.chat_id != yo.id:
        return
    filtro = (event.pattern_match.group(1) or "").strip().lower()
    lineas = []
    async for d in client.iter_dialogs(limit=300):
        es_bot = d.is_user and getattr(d.entity, "bot", False)
        if (d.is_group or d.is_channel or es_bot) and filtro in (d.name or "").lower():
            lineas.append(f"{'🤖 ' if es_bot else ''}{d.name} → {d.id}")
        if len(lineas) >= 40:
            break
    if not lineas:
        await event.reply("No encontré chats con ese nombre.")
        return
    texto, trozos = "", []
    for l in lineas:
        if len(texto) + len(l) > 3500:
            trozos.append(texto)
            texto = ""
        texto += l + "\n"
    trozos.append(texto)
    for t in trozos:
        await event.reply(t)


# ==========================================
# PAUSA, MODO AUSENTE Y MEMORIA POR PERSONA
# ==========================================
PAUSA = False
AUSENTE = False
AUSENTE_MSG = "Ahorita no estoy por aquí, apenas pueda te respondo 🙏"
AUSENTE_ESPERA = 3 * 3600   # no repite el aviso a la misma persona antes de 3 h
ULTIMO_AVISO = {}           # (chat_id, user_id) -> hora del último aviso
MEMORIA_ON = secreto("MEMORIA", False).lower() != "off"
MEMORIA = {}                # user_id -> {"nombre", "notas", "msg_id"}
MEM_ESPERA = 300            # segundos mínimos entre actualizaciones de notas de una misma persona
ULTIMA_NOTA = {}


async def leer_estado_guardado():
    """Recupera pausa y modo ausente desde Mensajes guardados (así se conservan al reiniciar)."""
    global PAUSA, AUSENTE, AUSENTE_MSG
    async for m in client.iter_messages("me", limit=100, search="/pausa"):
        x = (m.raw_text or "").strip().lower()
        if x in ("/pausa on", "/pausa off"):
            PAUSA = (x == "/pausa on")
            break
    estado = texto = False
    async for m in client.iter_messages("me", limit=200, search="/ausente"):
        x = (m.raw_text or "").strip()
        low = x.lower()
        if not estado and low in ("/ausente on", "/ausente off"):
            AUSENTE = (low == "/ausente on")
            estado = True
        elif not texto and low.startswith("/ausente texto "):
            AUSENTE_MSG = x[len("/ausente texto "):].strip() or AUSENTE_MSG
            texto = True
        if estado and texto:
            break


@client.on(events.NewMessage(outgoing=True, pattern=r"^/pausa(?:\s+(on|off))?$"))
async def cmd_pausa(event):
    global PAUSA
    if event.chat_id != yo.id:
        return
    arg = event.pattern_match.group(1)
    if arg:
        PAUSA = (arg == "on")
    await event.reply(
        "⏸️ Pausa ACTIVADA: no responde, no vota la trivia y no reenvía." if PAUSA
        else "▶️ Pausa desactivada: el bot funciona normal."
    )


async def responder_ausente(event, emisor):
    clave = (event.chat_id, getattr(emisor, "id", None))
    ahora = time.time()
    if ahora - ULTIMO_AVISO.get(clave, 0) < AUSENTE_ESPERA:
        return
    ULTIMO_AVISO[clave] = ahora
    await asyncio.sleep(random.uniform(1, 3))
    anotar_envio_bot(event.chat_id, AUSENTE_MSG)
    if event.is_private:
        await event.respond(AUSENTE_MSG)
    else:
        await event.reply(AUSENTE_MSG)


@client.on(events.NewMessage(outgoing=True, pattern=r"^/ausente(?:\s+(on|off|texto\s+[\s\S]+))?$"))
async def cmd_ausente(event):
    global AUSENTE, AUSENTE_MSG
    if event.chat_id != yo.id:
        return
    arg = (event.pattern_match.group(1) or "").strip()
    if arg.lower() in ("on", "off"):
        AUSENTE = (arg.lower() == "on")
    elif arg.lower().startswith("texto"):
        AUSENTE_MSG = arg[5:].strip() or AUSENTE_MSG
        ULTIMO_AVISO.clear()
    await event.reply(
        f"🌙 Modo ausente: {'ACTIVADO' if AUSENTE else 'desactivado'}\n"
        f"Mensaje: {AUSENTE_MSG}\n"
        "Cuando está activo, el bot no usa la IA: solo avisa con ese mensaje (una vez cada 3 h por persona) "
        "a quien te escriba en privado o te mencione."
    )


async def cargar_memoria():
    MEMORIA.clear()
    async for m in client.iter_messages("me", limit=500, search="/memoria"):
        t = (m.raw_text or "").strip()
        if not t.startswith("/memoria "):
            continue
        partes = t[len("/memoria "):].split("|", 2)
        if len(partes) != 3:
            continue
        try:
            uid = int(partes[0])
        except ValueError:
            continue
        if uid not in MEMORIA:  # el más reciente manda
            MEMORIA[uid] = {"nombre": partes[1], "notas": partes[2], "msg_id": m.id}
    print(f"🗂️ Personas en memoria: {len(MEMORIA)}")


async def actualizar_memoria(uid, nombre, texto, respuesta, lineas=None):
    ahora = time.time()
    if ahora - ULTIMA_NOTA.get(uid, 0) < MEM_ESPERA:
        return
    ULTIMA_NOTA[uid] = ahora
    try:
        previas = MEMORIA.get(uid, {}).get("notas", "")
        charla = "\n".join(lineas[-8:]) if lineas else f'{nombre}: {texto}\n{NOMBRE}: {respuesta}'
        r = await generar(
            f"Notas actuales sobre {nombre}: {previas or '(ninguna todavía)'}\n\n"
            f"Conversación reciente:\n{charla}\n{NOMBRE}: {respuesta}\n\n"
            f"Devuelve la ficha ACTUALIZADA de {nombre} en UNA sola línea de máximo 350 caracteres, "
            "combinando lo que ya había con lo nuevo. Apunta todo lo que se pueda aprender de la persona: "
            "cómo le dicen o le gusta que le digan, gustos, trabajo o estudios, ciudad o país, planes, gente que "
            "menciona, temas de los que habla, cómo se lleva con "
            f"{NOMBRE}. Es mejor apuntar algo pequeño que no apuntar nada. No guardes datos sensibles "
            "(salud, dinero, contraseñas, teléfonos, direcciones). Solo si de verdad no hay ningún dato nuevo "
            "responde exactamente SIN CAMBIOS.",
            sistema="Mantienes fichas breves y útiles sobre las personas con las que chatea el usuario.",
            max_tokens=2000,
        )
        notas = (r.text or "").strip()
        if not notas:
            print(f"🗂️ La IA no devolvió notas para {nombre} (respuesta vacía)")
            return
        if notas.upper().startswith("SIN CAMBIOS"):
            print(f"🗂️ Nada nuevo que apuntar de {nombre}")
            return
        notas = notas.replace("\n", " ").replace("|", "/")[:400]
        nombre = nombre.replace("|", "/")
        guardado = f"/memoria {uid}|{nombre}|{notas}"
        d = MEMORIA.get(uid)
        msg_id = None
        if d and d.get("msg_id"):
            try:
                await client.edit_message("me", d["msg_id"], guardado)
                msg_id = d["msg_id"]
            except Exception as e:
                if "not modified" in str(e).lower():
                    msg_id = d["msg_id"]
        if msg_id is None:
            msg_id = (await client.send_message("me", guardado)).id
        MEMORIA[uid] = {"nombre": nombre, "notas": notas, "msg_id": msg_id}
        print(f"🗂️ Notas de {nombre} actualizadas")
    except Exception as e:
        print(f"No pude actualizar la memoria: {e}")
        registrar_error(f"memoria: {e}")


@client.on(events.NewMessage(outgoing=True, pattern=r"^/memoria(?:\s+(ver|borrar)\s+(-?\d+))?$"))
async def cmd_memoria(event):
    if event.chat_id != yo.id:
        return
    accion, uid = event.pattern_match.group(1), event.pattern_match.group(2)
    if accion:
        d = MEMORIA.get(int(uid))
        if not d:
            await event.reply("No tengo notas de esa persona.")
            return
        if accion == "ver":
            await event.reply(f"{d['nombre']} ({uid}):\n{d['notas']}")
            return
        MEMORIA.pop(int(uid), None)
        try:
            await client.delete_messages("me", d["msg_id"])
        except Exception:
            pass
        await event.reply(f"🗑️ Borré las notas de {d['nombre']}.")
        return
    if not MEMORIA:
        await event.reply("Todavía no tengo notas de nadie.")
        return
    texto, trozos = "", []
    for u, d in MEMORIA.items():
        linea = f"{d['nombre']} → {u}"
        if len(texto) + len(linea) > 3500:
            trozos.append(texto)
            texto = ""
        texto += linea + "\n"
    trozos.append(texto + "\n/memoria ver <id> · /memoria borrar <id>")
    for t in trozos:
        await event.reply(t)


# ==========================================
# AMIGOS: gustos que TÚ le cuentas al bot (/amigo @usuario gustos)
# ==========================================
AMIGOS = {}              # username en minúsculas -> {"gustos": str, "msg_id": int}
AMIGO_PROB = 0.35        # probabilidad de que en una respuesta "se acuerde" de un gusto suyo
AMIGO_ESPERA = 600       # segundos mínimos entre una mención y la siguiente a la misma persona
ULTIMA_MENCION = {}      # username -> hora de la última vez que se le empujó a mencionar un gusto


async def cargar_amigos():
    AMIGOS.clear()
    async for m in client.iter_messages("me", limit=500, search="/amigodato"):
        t = (m.raw_text or "").strip()
        if not t.startswith("/amigodato "):
            continue
        partes = t[len("/amigodato "):].split("|", 1)
        if len(partes) < 2:
            continue
        u = partes[0].strip().lstrip("@").lower()
        if u and u not in AMIGOS:  # el más reciente manda
            AMIGOS[u] = {"gustos": partes[1].strip(), "msg_id": m.id}
    print(f"👥 Amigos con gustos guardados: {len(AMIGOS)}")


async def guardar_amigo(u, gustos):
    guardado = f"/amigodato @{u}|{gustos}"
    d = AMIGOS.get(u)
    msg_id = None
    if d and d.get("msg_id"):
        try:
            await client.edit_message("me", d["msg_id"], guardado)
            msg_id = d["msg_id"]
        except Exception as e:
            if "not modified" in str(e).lower():
                msg_id = d["msg_id"]
    if msg_id is None:
        msg_id = (await client.send_message("me", guardado)).id
    AMIGOS[u] = {"gustos": gustos, "msg_id": msg_id}


@client.on(events.NewMessage(outgoing=True, pattern=r"^/amigo(?:\s+([\s\S]+))?$"))
async def cmd_amigo(event):
    if event.chat_id != yo.id:
        return
    arg = (event.pattern_match.group(1) or "").strip()
    if not arg:
        if not AMIGOS:
            await event.reply(
                "Todavía no tengo amigos guardados.\n\nEjemplo (puedes mandar varios, uno por línea):\n"
                "/amigo @juan le gusta el anime, Boca y el reggaetón\n"
                "@pedro: juega Valorant, odia madrugar"
            )
            return
        texto, trozos = "", []
        for u, d in AMIGOS.items():
            linea = f"@{u}: {d['gustos']}\n"
            if len(texto) + len(linea) > 3500:
                trozos.append(texto)
                texto = ""
            texto += linea
        trozos.append(texto + "\n/amigo @usuario gustos → añadir · /amigo borrar @usuario → quitar")
        for t in trozos:
            await event.reply(t)
        return
    mb = re.match(r"^borrar\s+@?(\w+)$", arg, re.I)
    if mb:
        u = mb.group(1).lower()
        d = AMIGOS.pop(u, None)
        if not d:
            await event.reply(f"No tengo a @{u} guardado.")
            return
        try:
            await client.delete_messages("me", d["msg_id"])
        except Exception:
            pass
        await event.reply(f"🗑️ Borré lo que sabía de @{u}.")
        return
    guardados, ignoradas = [], []
    for linea in arg.split("\n"):
        linea = linea.strip()
        if not linea:
            continue
        m = re.match(r"^@(\w+)\s*[:\-–—]?\s*(.+)$", linea)
        if not m:
            ignoradas.append(linea)
            continue
        u, nuevo = m.group(1).lower(), m.group(2).strip().replace("|", "/")
        previo = AMIGOS.get(u, {}).get("gustos", "")
        if previo and nuevo.lower() in previo.lower():
            gustos = previo
        else:
            gustos = f"{previo}; {nuevo}" if previo else nuevo
        gustos = gustos[-500:]
        await guardar_amigo(u, gustos)
        guardados.append(f"@{u}: {gustos}")
    if not guardados:
        await event.reply("No entendí. Usa: /amigo @usuario le gusta X, Y, Z (uno por línea si son varios).")
        return
    resp = "✅ Guardado:\n" + "\n".join(guardados)
    if ignoradas:
        resp += "\n\n⚠️ Ignoré (les falta el @usuario):\n" + "\n".join(ignoradas)
    await event.reply(resp)


# ==========================================
# USO DE GEMINI (/uso): lo que ha gastado ESTE bot hoy
# ==========================================
try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")  # la cuota diaria de Google se reinicia a medianoche hora del Pacífico
except Exception:
    _PT = timezone(timedelta(hours=-8))
LIMITE_RPD = int(secreto("LIMITE_RPD", False) or 0)  # opcional: tu límite diario de peticiones del modelo (míralo en AI Studio)
USO = {"dia": "", "modelos": {}}   # modelo -> {"reqs","in","out","e429"}
USO_ULT_GUARDADO = 0
USO_MSG_ID = None
USO_ULT_TEXTO = ""


def registrar_uso(modelo, resp=None, error429=False):
    hoy = datetime.now(_PT).strftime("%Y-%m-%d")
    if USO["dia"] != hoy:
        USO["dia"] = hoy
        USO["modelos"] = {}
    d = USO["modelos"].setdefault(modelo, {"reqs": 0, "in": 0, "out": 0, "e429": 0})
    if error429:
        d["e429"] += 1
    else:
        d["reqs"] += 1
        um = getattr(resp, "usage_metadata", None)
        if um:
            entrada = getattr(um, "prompt_token_count", 0) or 0
            total = getattr(um, "total_token_count", 0) or 0
            salida = (total - entrada) if total else (getattr(um, "candidates_token_count", 0) or 0)
            d["in"] += entrada
            d["out"] += max(0, salida)
    try:
        asyncio.get_running_loop().create_task(guardar_uso())
    except RuntimeError:
        pass


async def guardar_uso():
    """Guarda el contador en Mensajes guardados (como máximo cada 2 min) para que sobreviva a reinicios."""
    global USO_ULT_GUARDADO, USO_MSG_ID
    global USO_ULT_TEXTO
    if time.time() - USO_ULT_GUARDADO < 600:
        return
    texto = "/usodato " + json.dumps(USO, separators=(",", ":"))
    if texto == USO_ULT_TEXTO:
        return
    USO_ULT_GUARDADO = time.time()
    USO_ULT_TEXTO = texto
    try:
        if USO_MSG_ID:
            await client.edit_message("me", USO_MSG_ID, texto)
        else:
            USO_MSG_ID = (await client.send_message("me", texto)).id
    except Exception as e:
        if "not modified" not in str(e).lower():
            USO_MSG_ID = None  # si lo borraste, la próxima vez crea otro
            print(f"No pude guardar el uso: {e}")


async def cargar_uso():
    global USO_MSG_ID
    async for m in client.iter_messages("me", limit=50, search="/usodato"):
        t = (m.raw_text or "").strip()
        if t.startswith("/usodato "):
            try:
                USO.update(json.loads(t[len("/usodato "):]))
                USO_MSG_ID = m.id
                print("📊 Contador de uso recuperado")
            except Exception:
                pass
            break


def _n(x):
    return f"{x:,}".replace(",", ".")


@client.on(events.NewMessage(outgoing=True, pattern=r"^/uso$"))
async def cmd_uso(event):
    if event.chat_id != yo.id:
        return
    ahora = datetime.now(_PT)
    hoy = ahora.strftime("%Y-%m-%d")
    modelos = USO["modelos"] if USO.get("dia") == hoy else {}
    reinicio = (ahora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - ahora
    h, m = divmod(int(reinicio.total_seconds()) // 60, 60)
    d = modelos.get(MODELO_ACTIVO, {"reqs": 0, "in": 0, "out": 0, "e429": 0})
    t = [f"📊 Uso de Gemini hoy (día en hora del Pacífico: {hoy})",
         f"🔄 La cuota diaria se reinicia en {h} h {m} min", "",
         f"Modelo activo: {MODELO_ACTIVO}",
         f"• Peticiones: {_n(d['reqs'])}"]
    if LIMITE_RPD:
        t[-1] += f" de {_n(LIMITE_RPD)} → te quedan ~{_n(max(0, LIMITE_RPD - d['reqs']))}"
    t.append(f"• Tokens: {_n(d['in'])} entrada · {_n(d['out'])} salida")
    if d["reqs"]:
        t.append(f"• Promedio: ~{_n((d['in'] + d['out']) // d['reqs'])} tokens por petición")
    if d["e429"]:
        t.append(f"• Avisos de límite (429): {d['e429']}")
    otros = [(k, v) for k, v in modelos.items() if k != MODELO_ACTIVO]
    if otros:
        t += ["", "Otros modelos usados hoy:"]
        t += [f"• {k}: {_n(v['reqs'])} peticiones · {_n(v['in'] + v['out'])} tokens" for k, v in otros]
    agot = [(k, v) for k, v in AGOTADOS.items() if time.time() - v < 23 * 3600]
    if agot:
        t += ["", "⚠️ Sin cuota diaria (ya agotados):"]
        t += [f"• {k} (hace {int((time.time() - v) // 3600)} h)" for k, v in agot]
    t += ["", "ℹ️ Google no informa por API cuánto te queda; esto es lo que ha gastado este bot "
              "(las pruebas de otros programas con tu misma clave no cuentan). Lo real: aistudio.google.com/usage"]
    if not LIMITE_RPD:
        t.append("Para ver cuántas te quedan, pon en Render la variable LIMITE_RPD con el límite diario de tu modelo.")
    await event.reply("\n".join(t))


async def cargar_aprender():
    APRENDER_CHATS.clear()
    vistos = set()
    async for m in client.iter_messages("me", limit=300, search="/aprender"):
        t = (m.raw_text or "").strip()
        mt = re.match(r"^/aprender\s+(grupo\s+|quitar\s+)?(-?\d+)$", t)
        if not mt:
            continue
        cid = norm_id(int(mt.group(2)))
        if cid in vistos:  # el más reciente manda
            continue
        vistos.add(cid)
        if not (mt.group(1) or "").startswith("quitar"):
            APRENDER_CHATS.add(cid)
    print(f"📚 Grupos de los que aprende: {len(APRENDER_CHATS)}")


@client.on(events.NewMessage(outgoing=True, pattern=r"^/aprender(?:\s+(aqui|aquí|grupo|quitar|ahora))?(?:\s+(-?\d+))?$"))
async def cmd_aprender(event):
    arg, num = event.pattern_match.group(1), event.pattern_match.group(2)
    # Escrito DENTRO del grupo: "/aprender aqui"
    if arg in ("aqui", "aquí"):
        if not event.is_group:
            return
        APRENDER_CHATS.add(event.chat_id)
        await event.delete()
        await client.send_message("me", f"/aprender grupo {event.chat_id}")  # se conserva al reiniciar
        return
    if event.chat_id != yo.id:
        return
    if arg == "ahora":
        asyncio.create_task(cargar_ejemplos())
        await event.reply("📚 Releyendo los grupos… (tarda un poco porque lo hago despacio para evitar FloodWait)")
        return
    if num and arg in (None, "grupo"):
        APRENDER_CHATS.add(norm_id(int(num)))
        asyncio.create_task(cargar_ejemplos())
        await event.reply(
            f"📚 Listo, aprenderé de ese grupo (hasta {APRENDER_LIMITE} mensajes recientes: los tuyos de antes del "
            f"{ANTES_DE}, cómo respondes a otros y lo que dicen los demás). Tarda un poco: lo leo despacio."
        )
        return
    if num and arg == "quitar":
        APRENDER_CHATS.discard(norm_id(int(num)))
        await event.reply("🗑️ Ya no aprenderé de ese grupo.")
        return
    if arg:  # "grupo" o "quitar" sin número
        await event.reply("Falta el ID del grupo. Ej: /aprender -1001234567890 (o escribe /aprender aqui dentro del grupo)")
        return
    nombres = []
    for cid in APRENDER_CHATS:
        try:
            nombres.append(f"• {getattr(await client.get_entity(cid), 'title', cid)} ({cid})")
        except Exception:
            nombres.append(f"• {cid}")
    await event.reply(
        f"📚 Aprendo de {len(APRENDER_CHATS)} grupo(s):\n" + ("\n".join(nombres) if nombres else "(ninguno)")
        + f"\n\nEjemplos de tu estilo: {len(EJEMPLOS)} · tus respuestas a otros: {len(PARES)} · mensajes del grupo: {len(VIBE)}\n"
        "/aprender aqui (dentro del grupo) · /aprender <id> · /aprender quitar <id> · /aprender ahora"
    )


# ==========================================
# MODO POR CHAT: un tono distinto en cada chat (/modo)
# ==========================================
MODOS = {}  # chat_id -> {"modo", "texto", "msg_id"}
PRESETS_MODO = {
    "serio": "Tono serio y educado: claro y directo, sin chistes, sin exceso de jerga y casi sin emojis.",
    "suelto": "Tono muy relajado y cercano: jerga, bromas ligeras, como hablando con tus panas.",
    "bromista": "Tono bromista con humor suave y algo de sarcasmo cuando encaje, sin pasarte ni ofender a nadie.",
    "corto": "Respuestas ultra breves: una frase corta, a veces solo una o dos palabras.",
    "largo": "Respuestas un poco más desarrolladas (2 o 3 frases) cuando el tema lo pida, sin enrollarte.",
}


async def _nombre(cid):
    try:
        e = await client.get_entity(int(cid))
        return getattr(e, "title", None) or getattr(e, "first_name", None) or getattr(e, "username", None) or str(cid)
    except Exception:
        return str(cid)


async def cargar_modos():
    MODOS.clear()
    async for m in client.iter_messages("me", limit=300, search="/mododato"):
        t = (m.raw_text or "").strip()
        if not t.startswith("/mododato "):
            continue
        partes = t[len("/mododato "):].split("|", 2)
        if len(partes) < 3:
            continue
        try:
            cid = int(partes[0])
        except ValueError:
            continue
        if cid not in MODOS:  # el más reciente manda
            MODOS[cid] = {"modo": partes[1], "texto": partes[2], "msg_id": m.id}
    print(f"🎭 Chats con modo propio: {len(MODOS)}")


async def guardar_modo(cid, modo, texto):
    guardado = f"/mododato {cid}|{modo}|{texto.replace('|', '/').replace(chr(10), ' ')}"
    d = MODOS.get(cid)
    msg_id = None
    if d and d.get("msg_id"):
        try:
            await client.edit_message("me", d["msg_id"], guardado)
            msg_id = d["msg_id"]
        except Exception as e:
            if "not modified" in str(e).lower():
                msg_id = d["msg_id"]
    if msg_id is None:
        msg_id = (await client.send_message("me", guardado)).id
    MODOS[cid] = {"modo": modo, "texto": texto, "msg_id": msg_id}


# ==========================================
# TONO AUTOMÁTICO: lee cada grupo, detecta su ambiente y te adaptas, pero siempre a TU estilo
# ==========================================
MODO_AUTO = True          # /modo auto todos on|off → aplica a todos los grupos sin modo manual
MODO_AUTO_MSG_ID = None
TONOS = {}                # chat_id -> {"texto", "ts", "msg_id"}
TONO_VIGENCIA = 7 * 86400  # cada cuánto se vuelve a leer un grupo
TONO_INTENTO = {}         # chat_id -> hora del último intento (espera 30 min entre intentos)
TONO_ANALISIS = []        # horas de los últimos análisis (máximo 4 por hora, para no gastar cuota ni provocar FloodWait)
TONO_LOCK = asyncio.Lock()


async def cargar_tonos():
    global MODO_AUTO, MODO_AUTO_MSG_ID
    TONOS.clear()
    async for m in client.iter_messages("me", limit=300, search="/tonodato"):
        t = (m.raw_text or "").strip()
        if not t.startswith("/tonodato "):
            continue
        partes = t[len("/tonodato "):].split("|", 2)
        if len(partes) < 3:
            continue
        try:
            cid, ts = int(partes[0]), int(partes[1])
        except ValueError:
            continue
        if cid not in TONOS:  # el más reciente manda
            TONOS[cid] = {"texto": partes[2], "ts": ts, "msg_id": m.id}
    await asyncio.sleep(1.2)
    async for m in client.iter_messages("me", limit=20, search="/modoautodato"):
        t = (m.raw_text or "").strip()
        if t.startswith("/modoautodato "):
            MODO_AUTO = t.split(" ", 1)[1].strip().lower() != "off"
            MODO_AUTO_MSG_ID = m.id
            break
    print(f"🎚️ Tonos de grupos guardados: {len(TONOS)} · tono automático global: {'sí' if MODO_AUTO else 'no'}")


async def guardar_modo_auto_cfg():
    global MODO_AUTO_MSG_ID
    texto = f"/modoautodato {'on' if MODO_AUTO else 'off'}"
    try:
        if MODO_AUTO_MSG_ID:
            await client.edit_message("me", MODO_AUTO_MSG_ID, texto)
        else:
            MODO_AUTO_MSG_ID = (await client.send_message("me", texto)).id
    except Exception as e:
        if "not modified" not in str(e).lower():
            MODO_AUTO_MSG_ID = None
            print(f"No pude guardar el ajuste de tono automático: {e}")


async def analizar_tono(cid, forzar=False):
    """Lee mensajes recientes del chat y pide a Gemini una descripción corta de su tono. Devuelve el texto guardado."""
    async with TONO_LOCK:
        previo = TONOS.get(cid, {}).get("texto")
        ahora = time.time()
        if not forzar:
            TONO_ANALISIS[:] = [t for t in TONO_ANALISIS if ahora - t < 3600]
            if ahora - TONO_INTENTO.get(cid, 0) < 1800 or len(TONO_ANALISIS) >= 4:
                return previo
        TONO_INTENTO[cid] = ahora
        TONO_ANALISIS.append(ahora)
        limite = datetime.fromisoformat(ANTES_DE).replace(tzinfo=timezone.utc)
        otros, mios = [], []
        try:
            async for m in client.iter_messages(cid, limit=150, wait_time=1.5):
                t = (m.raw_text or "").strip()
                if m.out or not t or t.startswith("/") or "http" in t or not (2 <= len(t) <= 160):
                    continue  # lo "mío" reciente puede ser del bot: no cuenta
                s = m.sender
                if getattr(s, "bot", False) or es_userbot(s):
                    continue
                otros.append(t)
            async for m in client.iter_messages(cid, from_user="me", limit=40, offset_date=limite, wait_time=1.5):
                t = (m.raw_text or "").strip()
                if 3 <= len(t) <= 160 and "http" not in t and not t.startswith("/"):
                    mios.append(t)
        except FloodWaitError as e:
            registrar_error(f"FloodWait {e.seconds}s (leyendo tono de {cid})")
            return previo
        except Exception as e:
            print(f"No pude leer el chat {cid} para el tono: {e}")
            return previo
        if len(otros) + len(mios) < 8:
            print(f"🎚️ Muy pocos mensajes en {cid} para detectar su tono")
            return previo
        otros = list(reversed(otros[:60]))  # de más viejo a más nuevo
        try:
            r = await generar(
                "Mensajes recientes del grupo (otras personas):\n" + "\n".join(f"- {x}" for x in otros)
                + (f"\n\nMensajes de {NOMBRE} en este grupo (cómo suena aquí):\n" + "\n".join(f"- {x}" for x in mios[:25]) if mios else "")
                + f"\n\nDescribe en 1 a 3 frases (máximo 280 caracteres) el TONO de este grupo y el registro en el que suena {NOMBRE} aquí: "
                "qué tan bromista o serio, qué tan relajado o formal, si los mensajes suelen ser cortos o largos y el nivel de jerga o emojis. "
                "Solo describe el ambiente y el registro; no copies frases ni menciones nombres o datos personales. "
                "Responde únicamente con la descripción.",
                sistema="Analizas el ambiente de grupos de chat de forma breve y objetiva.",
                max_tokens=2000,
                temperatura=0.3,
            )
        except Exception as e:
            registrar_error(f"tono: {e}")
            return previo
        texto = (r.text or "").strip().replace("\n", " ").replace("|", "/")[:300]
        if not texto:
            return previo
        guardado = f"/tonodato {cid}|{int(time.time())}|{texto}"
        d = TONOS.get(cid)
        msg_id = None
        try:
            if d and d.get("msg_id"):
                try:
                    await client.edit_message("me", d["msg_id"], guardado)
                    msg_id = d["msg_id"]
                except Exception as e:
                    if "not modified" in str(e).lower():
                        msg_id = d["msg_id"]
            if msg_id is None:
                msg_id = (await client.send_message("me", guardado)).id
        except Exception as e:
            print(f"No pude guardar el tono: {e}")
        TONOS[cid] = {"texto": texto, "ts": int(time.time()), "msg_id": msg_id}
        print(f"🎚️ Tono de {cid}: {texto}")
        return texto


async def tono_para(cid):
    """Tono detectado del chat: usa el guardado; si no hay, lo analiza (esperando poco) y si está viejo lo refresca de fondo."""
    d = TONOS.get(cid)
    if d:
        if time.time() - d["ts"] > TONO_VIGENCIA:
            asyncio.create_task(analizar_tono(cid))
        return d["texto"]
    tarea = asyncio.create_task(analizar_tono(cid))
    try:
        return await asyncio.wait_for(asyncio.shield(tarea), 25)
    except Exception:
        return None  # sigue analizando de fondo; la próxima respuesta ya lo tendrá


@client.on(events.NewMessage(outgoing=True, pattern=r"^/modo(?:\s+([\s\S]+))?$"))
async def cmd_modo(event):
    global MODO_AUTO
    arg = (event.pattern_match.group(1) or "").strip()
    if event.chat_id == yo.id:  # en Mensajes guardados: /modo <id del chat> <modo>
        responder = event.reply
        if arg.lower() in ("auto todos", "auto todos on", "auto todos off"):
            MODO_AUTO = not arg.lower().endswith("off")
            await guardar_modo_auto_cfg()
            await event.reply(
                f"🎚️ Tono automático en todos los grupos: {'activado' if MODO_AUTO else 'desactivado'}.\n"
                "(Los chats con modo manual no cambian.)"
            )
            return
        m = re.match(r"^(-?\d+)\s+([\s\S]+)$", arg)
        if not m:
            if arg:
                await event.reply("Falta el ID del chat: /modo <id> serio  (o escribe /modo serio dentro del chat)")
                return
            lin = [f"• {await _nombre(c)} ({c}): {d['modo']}" for c, d in MODOS.items()]
            tn = [f"• {await _nombre(c)}: {d['texto']}" for c, d in list(TONOS.items())[:10]]
            await event.reply(
                "🎭 Modos por chat:\n" + ("\n".join(lin) if lin else "(ninguno manual)")
                + f"\n\n🎚️ Tono automático en todos los grupos: {'sí' if MODO_AUTO else 'no'}"
                + ("\nTonos detectados:\n" + "\n".join(tn) if tn else "")
                + "\n\nModos: " + ", ".join(PRESETS_MODO) + ", auto\n"
                "Dentro del chat: /modo serio · /modo auto · /modo tono · /modo texto <instrucción> · /modo normal\n"
                "Desde aquí: /modo <id> serio · /modo <id> auto · /modo auto todos on|off"
            )
            return
        cid, resto = norm_id(int(m.group(1))), m.group(2).strip()
    else:  # dentro de un chat: se borra el comando y la respuesta te llega a Mensajes guardados
        cid, resto = event.chat_id, arg
        try:
            await event.delete()
        except Exception:
            pass

        async def responder(t):
            await client.send_message("me", t)
    low = resto.lower()
    nombre = await _nombre(cid)
    if not resto:
        d = MODOS.get(cid)
        await responder(f"🎭 Modo de {nombre}: {d['modo'] if d else 'normal'}\nModos: " + ", ".join(PRESETS_MODO) + " · texto <instrucción> · normal")
        return
    if low in ("normal", "quitar", "borrar"):
        d = MODOS.pop(cid, None)
        if d and d.get("msg_id"):
            try:
                await client.delete_messages("me", d["msg_id"])
            except Exception:
                pass
        await responder(f"🎭 {nombre}: vuelve al modo normal.")
        return
    if low.startswith("texto "):
        txt = resto[6:].strip()[:300]
        if not txt:
            await responder("Escribe la instrucción: /modo texto habla como si fueras muy tímido")
            return
        await guardar_modo(cid, "personalizado", txt)
        await responder(f"🎭 {nombre}: modo personalizado → {txt}")
        return
    if low in ("auto", "automatico", "automático"):
        await guardar_modo(cid, "auto", "")
        asyncio.create_task(analizar_tono(cid, True))
        await responder(
            f"🎭 {nombre}: modo automático. Leo el chat, detecto su tono y me adapto, siempre escribiendo a tu estilo. "
            "En un momento: /modo tono para ver lo que detecté."
        )
        return
    if low in ("tono", "auto ahora"):
        if low == "auto ahora":
            await responder(f"🎚️ Releyendo {nombre}…")
            await analizar_tono(cid, True)
        d = TONOS.get(cid)
        await responder(f"🎚️ Tono de {nombre}: {d['texto']}" if d else f"Todavía no detecté el tono de {nombre} (necesito unos cuantos mensajes en el chat).")
        return
    if low in PRESETS_MODO:
        await guardar_modo(cid, low, PRESETS_MODO[low])
        await responder(f"🎭 {nombre}: modo {low} → {PRESETS_MODO[low]}")
        return
    await responder("No conozco ese modo. Opciones: " + ", ".join(PRESETS_MODO) + ", auto · texto <instrucción> · normal")


# ==========================================
# RESUMEN DEL DÍA (/resumen)
# ==========================================
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
RESUMEN = {"on": True, "hora": 22, "zona": secreto("ZONA_HORARIA", False) or "America/Bogota", "ultimo": ""}
RESUMEN_MSG_ID = None
STATS = {"dia": "", "resp": 0, "stickers": 0, "errores": 0, "floods": 0, "bots": 0, "chats": {}, "personas": {}}
STATS_MSG_ID = None
STATS_ULT_GUARDADO = 0
STATS_ULT_TEXTO = ""


def zona_local():
    try:
        return ZoneInfo(RESUMEN["zona"])
    except Exception:
        return timezone.utc


def stats_dia():
    hoy = datetime.now(zona_local()).strftime("%Y-%m-%d")
    if STATS.get("dia") != hoy:
        STATS.update(dia=hoy, resp=0, stickers=0, errores=0, floods=0, bots=0, chats={}, personas={})
    return STATS


async def guardar_dia():
    """Guarda las estadísticas del día (como máximo cada 20 min) para que sobrevivan a un reinicio."""
    global STATS_MSG_ID, STATS_ULT_GUARDADO, STATS_ULT_TEXTO
    if time.time() - STATS_ULT_GUARDADO < 1200:
        return
    s = stats_dia()
    if len(s["personas"]) > 40:
        s["personas"] = dict(sorted(s["personas"].items(), key=lambda x: -x[1])[:30])
    if len(s["chats"]) > 30:
        s["chats"] = dict(sorted(s["chats"].items(), key=lambda x: -x[1])[:20])
    texto = "/diadato " + json.dumps(s, separators=(",", ":"), ensure_ascii=False)
    if texto == STATS_ULT_TEXTO or len(texto) > 4000:
        return
    STATS_ULT_GUARDADO = time.time()
    STATS_ULT_TEXTO = texto
    try:
        if STATS_MSG_ID:
            await client.edit_message("me", STATS_MSG_ID, texto)
        else:
            STATS_MSG_ID = (await client.send_message("me", texto)).id
    except Exception as e:
        if "not modified" not in str(e).lower():
            STATS_MSG_ID = None
            print(f"No pude guardar las estadísticas: {e}")


async def guardar_resumen_cfg():
    global RESUMEN_MSG_ID
    texto = "/resumendato " + json.dumps({k: RESUMEN[k] for k in ("on", "hora", "zona", "ultimo")})
    try:
        if RESUMEN_MSG_ID:
            await client.edit_message("me", RESUMEN_MSG_ID, texto)
        else:
            RESUMEN_MSG_ID = (await client.send_message("me", texto)).id
    except Exception as e:
        if "not modified" not in str(e).lower():
            RESUMEN_MSG_ID = None
            print(f"No pude guardar la config del resumen: {e}")


async def cargar_resumen():
    global RESUMEN_MSG_ID, STATS_MSG_ID
    async for m in client.iter_messages("me", limit=20, search="/resumendato"):
        t = (m.raw_text or "").strip()
        if t.startswith("/resumendato "):
            try:
                RESUMEN.update(json.loads(t[len("/resumendato "):]))
                RESUMEN_MSG_ID = m.id
            except Exception:
                pass
            break
    await asyncio.sleep(1.2)
    async for m in client.iter_messages("me", limit=20, search="/diadato"):
        t = (m.raw_text or "").strip()
        if t.startswith("/diadato "):
            try:
                STATS.update(json.loads(t[len("/diadato "):]))
                STATS_MSG_ID = m.id
            except Exception:
                pass
            break
    stats_dia()
    print(f"🌙 Resumen diario: {'activo' if RESUMEN['on'] else 'apagado'} a las {RESUMEN['hora']}:00 ({RESUMEN['zona']})")


async def texto_resumen():
    s = stats_dia()
    t = [f"🌙 Resumen del día ({s['dia']})", ""]
    if not s["resp"]:
        t.append("Hoy el bot no respondió ningún mensaje.")
    else:
        t.append(f"💬 {s['resp']} respuestas" + (f" (incluye {s['stickers']} stickers)" if s["stickers"] else ""))
        top = sorted(s["chats"].items(), key=lambda x: -x[1])[:3]
        t.append("📍 Chats más activos: " + ", ".join([f"{await _nombre(c)} ({n})" for c, n in top]))
        pers = sorted(s["personas"].items(), key=lambda x: -x[1])[:3]
        if pers:
            t.append("🗣️ Con quién más hablé: " + ", ".join(f"{n} ({c})" for n, c in pers))
    if USO.get("dia") == datetime.now(_PT).strftime("%Y-%m-%d") and USO["modelos"]:
        reqs = sum(v["reqs"] for v in USO["modelos"].values())
        toks = sum(v["in"] + v["out"] for v in USO["modelos"].values())
        t.append(f"🧠 Gemini: {_n(reqs)} peticiones · {_n(toks)} tokens (el día de la cuota va en hora del Pacífico)")
    if s["bots"]:
        t.append(f"🤖 Le avisé a {s['bots']} userbot(s) que no hablo con bots")
    t.append(f"⚠️ Errores: {s['errores']}" + (f" (FloodWait: {s['floods']})" if s["floods"] else "") if s["errores"] else "✅ Sin errores")
    return "\n".join(t)


async def bucle_resumen():
    """Cada 5 min mira si ya es la hora del resumen y lo manda a Mensajes guardados una vez al día."""
    await asyncio.sleep(90)
    while True:
        try:
            if RESUMEN["on"]:
                ahora = datetime.now(zona_local())
                hoy = ahora.strftime("%Y-%m-%d")
                if ahora.hour >= RESUMEN["hora"] and RESUMEN["ultimo"] != hoy:
                    RESUMEN["ultimo"] = hoy
                    await guardar_resumen_cfg()
                    await client.send_message("me", await texto_resumen())
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 300))
        except Exception as e:
            print(f"No pude mandar el resumen: {e}")
        await asyncio.sleep(300)


@client.on(events.NewMessage(outgoing=True, pattern=r"^/resumen(?:\s+(on|off|hora|zona|ahora))?(?:\s+(\S+))?$"))
async def cmd_resumen(event):
    if event.chat_id != yo.id:
        return
    arg, val = event.pattern_match.group(1), event.pattern_match.group(2)
    if arg in (None, "ahora"):
        await event.reply(await texto_resumen())
        return
    if arg in ("on", "off"):
        RESUMEN["on"] = (arg == "on")
    elif arg == "hora":
        if not (val and val.isdigit() and 0 <= int(val) <= 23):
            await event.reply("Uso: /resumen hora 22  (un número de 0 a 23, en tu zona horaria)")
            return
        RESUMEN["hora"] = int(val)
        RESUMEN["ultimo"] = ""  # si ya pasó esa hora hoy, lo manda en cuanto toque
    elif arg == "zona":
        try:
            ZoneInfo(val or "")
        except Exception:
            await event.reply("No reconozco esa zona. Ejemplos: America/Bogota, America/Mexico_City, America/Argentina/Buenos_Aires, Europe/Madrid\n(Si es válida y igual falla, añade tzdata a requirements.txt)")
            return
        RESUMEN["zona"] = val
    await guardar_resumen_cfg()
    await event.reply(
        f"🌙 Resumen diario: {'activo' if RESUMEN['on'] else 'apagado'} · a las {RESUMEN['hora']}:00 ({RESUMEN['zona']})\n"
        "/resumen → verlo ahora · /resumen on|off · /resumen hora <0-23> · /resumen zona <Zona>"
    )


async def leer_callar_guardado():
    """Recupera tu último '/callar ...' de Mensajes guardados (así se conserva al reiniciar)."""
    global SILENCIO_MANUAL
    async for m in client.iter_messages("me", limit=50, search="/callar"):
        mt = re.match(r"^/callar\s+(off|on|\d+)$", (m.raw_text or "").strip())
        if mt:
            a = mt.group(1)
            SILENCIO_MANUAL = 0 if a == "off" else (120 if a == "on" else max(10, min(int(a), 3600)))
            return


@client.on(events.NewMessage(outgoing=True, pattern=r"^/callar(?:\s+(off|on|\d+))?$"))
async def cmd_callar(event):
    global SILENCIO_MANUAL
    if event.chat_id != yo.id:
        return
    a = event.pattern_match.group(1)
    if a == "off":
        SILENCIO_MANUAL = 0
    elif a == "on":
        SILENCIO_MANUAL = 120
    elif a:
        SILENCIO_MANUAL = max(10, min(int(a), 3600))
    await event.reply(
        f"🤫 Cuando hablas tú en un chat, el bot se calla {SILENCIO_MANUAL} s." if SILENCIO_MANUAL
        else "🤫 Desactivado: el bot responde aunque estés hablando tú."
    )
    await event.reply("/callar <segundos> · /callar off · /callar on (120 s)") if not a else None


def _dur(seg):
    seg = int(seg)
    d, r = divmod(seg, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return (f"{d} d " if d else "") + (f"{h} h " if h or d else "") + f"{m} min"


@client.on(events.NewMessage(outgoing=True, pattern=r"^/estado$"))
async def cmd_estado(event):
    if event.chat_id != yo.id:
        return
    ahora = time.time()
    hoy = datetime.now(_PT).strftime("%Y-%m-%d")
    uso = USO["modelos"].get(MODELO_ACTIVO, {}) if USO.get("dia") == hoy else {}
    onoff = lambda x: "sí" if x else "no"
    t = [
        f"{'⏸️' if PAUSA else '🟢'} Estado del bot · {'EN PAUSA' if PAUSA else 'funcionando'}",
        f"⏱️ Encendido hace {_dur(ahora - INICIO)} · respuestas desde entonces: {RESPUESTAS_DADAS}",
        "",
        f"🧠 IA: {ia_activa()}",
        f"   hoy: {_n(uso.get('reqs', 0))} peticiones · {_n(uso.get('in', 0) + uso.get('out', 0))} tokens (detalle: /uso)",
    ]
    agot = [k for k, v in AGOTADOS.items() if ahora - v < 23 * 3600]
    if agot:
        t.append(f"   ⚠️ sin cuota diaria: {', '.join(agot)}")
    t += [
        "",
        f"💬 Ausente: {onoff(AUSENTE)} · Privados: {onoff(PRIVADOS)} · Solo contactos: {onoff(SOLO_CONTACTOS)}",
        f"👂 Detección auto: {onoff(AUTO)} ({len(AUTO_CHATS)} grupo(s)) · 🎲 Trivia: {onoff(TRIVIA)} ({len(TRIVIA_CHATS)} grupo(s))",
        f"📚 Estilo: {len(EJEMPLOS)} ejemplos · {len(PARES)} respuestas tuyas · {len(VIBE)} de jerga · aprende de {len(APRENDER_CHATS)} grupo(s)",
        f"👥 Amigos: {len(AMIGOS)} · 🗂️ Memoria: {len(MEMORIA)} persona(s){'' if MEMORIA_ON else ' (apagada)'}",
        f"🤖 Userbots marcados: {len({id(d) for d in OTROS_BOTS.values()})}",
        f"📤 Reenvío: {len(ORIGENES)} origen(es) → {'destino fijado' if DESTINO else 'sin destino'}",
        f"🤫 Silencio cuando hablas tú: {str(SILENCIO_MANUAL) + ' s' if SILENCIO_MANUAL else 'desactivado'}",
        f"🎚️ Tono automático: {'sí' if MODO_AUTO else 'no'} · {len(TONOS)} chat(s) analizados",
        f"🎭 Chats con modo propio: {len(MODOS)} · 🌙 Resumen diario: "
        + (f"{RESUMEN['hora']}:00 ({RESUMEN['zona']})" if RESUMEN["on"] else "apagado"),
    ]
    t.append("")
    if ahora < FLOOD_HASTA:
        t.append(f"⏳ FloodWait activo: sin responder {_dur(FLOOD_HASTA - ahora)} más")
    else:
        t.append("✅ Sin FloodWait ahora")
    if ERRORES:
        t.append("⚠️ Errores recientes:")
        t += [f"• hace {_dur(ahora - h)}: {e}" for h, e in reversed(ERRORES)]
    else:
        t.append("✅ Sin errores desde que arrancó")
    await event.reply("\n".join(t))


def texto_ayuda():
    return (
        "🤖 Userbot activo\n"
        f"IA activa: {ia_activa()}\n"
        f"Ejemplos de tu estilo: {len(EJEMPLOS)}\n\n"
        "Comandos (escríbelos aquí, en Mensajes guardados):\n"
        "/modo serio|suelto|bromista|corto|largo → (dentro de un chat) tono propio para ese chat · /modo texto <instrucción> · /modo normal\n"
        "/modo auto → (dentro de un chat) detecta solo su tono y se adapta a tu estilo · /modo tono → ver lo detectado · /modo auto ahora → releer\n"
        "/modo auto todos on|off → tono automático en todos los grupos sin modo manual\n"
        "/resumen → resumen del día (también te lo manda solo a la hora elegida) · /resumen on|off · hora <0-23> · zona <Zona>\n"
        "/callar <segundos> → el bot se calla en un chat mientras hablas tú (por defecto 120) · /callar off\n"
        "/estado → resumen de todo: IA, pausa, grupos, errores recientes y FloodWait\n"
        "/uso → cuántas peticiones y tokens ha gastado el bot hoy y cuándo se reinicia la cuota\n"
        "/aprender aqui → (dentro de un grupo) aprender de su historial: tu estilo, cómo respondes y su jerga\n"
        "/aprender → ver grupos · /aprender <id> · /aprender quitar <id> · /aprender ahora\n"
        "/otrobot → ver userbots marcados (máx 2 respuestas, luego les aviso y los ignoro) · /otrobot @usuario · /otrobot quitar @usuario\n"
        "/modelo → ver todos los modelos de Gemini\n"
        "/modelo <nombre o número> → cambiar de modelo\n"
        "/modelo auto → que elija solo\n"
        "/estilo → recargar tu estilo\n"
        "/estilo <texto> → guardar tus notas de estilo y jerga (se conservan)\n"
        "/estilo borrar → quitar esas notas\n"
        "/auto grupo <id> → grupo donde detecta cuándo le hablan sin señalarte (escríbelo aquí)\n"
        "/auto on|off → activar o apagar esa detección\n"
        "/auto borrar → quitar el grupo de detección\n"
        "/trivia grupo <id> → grupo donde juega la trivia (escríbelo aquí, sin tocar el grupo)\n"
        "/trivia on|off → activar o desactivar el juego\n"
        "/trivia grupo borrar → quitar el grupo de la trivia\n"
        "/chats <nombre> → ver el ID de tus grupos, canales y bots\n"
        "/reenviar origen <id o @usuario> → reenviar desde ese grupo, canal o bot (escríbelo aquí)\n"
        "/reenviar destino <id> → destino del reenvío (sin id = estos Guardados)\n"
        "/reenviar quitar <id> → dejar de reenviar desde ese grupo\n"
        "/traer <id> [n] → reenvía los 50 videos más recientes de ese grupo (o n, máx. 100). "
        "Cada vez que lo repites manda los 50 siguientes, sin repetir, hasta avisarte que ya no hay más\n"
        "/traer reiniciar <id> → volver a empezar desde los más recientes\n"
        "(/historial funciona igual que /traer)\n"
        "/reenviar video|todo → reenviar solo videos o todo el contenido (aquí)\n"
        "/reenviar on|off|borrar → activar, apagar o limpiar todo (aquí)\n"
        "/pausa on|off → apagar o encender todo el bot de golpe\n"
        "/ausente on|off → modo ausente (solo avisa que no estás, sin IA)\n"
        "/ausente texto <mensaje> → cambiar el aviso de ausente\n"
        "/memoria → ver de quién guarda notas (/memoria ver <id>, /memoria borrar <id>)\n"
        "/amigo @usuario gustos → contarle al bot qué le gusta a un amigo (varios: uno por línea)\n"
        "/amigo → ver tus amigos guardados · /amigo borrar @usuario\n"
        "/ayuda → ver este mensaje"
    )


@client.on(events.NewMessage(outgoing=True, pattern=r"^/ayuda$"))
async def ayuda(event):
    if event.chat_id != yo.id:
        return
    await event.reply(texto_ayuda())



# ==========================================
# SERVIDOR WEB MÍNIMO (Render exige un puerto abierto) + AUTO-PING
# ==========================================
async def iniciar_web():
    async def salud(request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", salud)
    runner = web.AppRunner(app)
    await runner.setup()
    puerto = int(os.environ.get("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", puerto).start()
    print(f"🌐 Servidor web en el puerto {puerto}")


async def auto_ping():
    """Se llama a sí mismo cada 10 min para que Render no lo duerma (Render define RENDER_EXTERNAL_URL)."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    await asyncio.sleep(60)
    while True:
        try:
            async with ClientSession() as s:
                await s.get(url, timeout=20)
        except Exception as e:
            print(f"auto-ping falló: {e}")
        await asyncio.sleep(600)


async def main():
    global yo, ESTILO, TRIVIA_CHATS, AUTO_CHATS, ORIGENES, DESTINO, SOLO_VIDEO
    await iniciar_web()
    asyncio.create_task(auto_ping())
    await client.start()
    yo = await client.get_me()
    _guardado = await leer_estilo_guardado()
    await asyncio.sleep(1.2)
    if _guardado is not None:
        ESTILO = _guardado
    _tg = await leer_trivia_guardada()
    await asyncio.sleep(1.2)
    if _tg is not None:
        TRIVIA_CHATS = _tg
    _ag = await leer_auto_guardado()
    await asyncio.sleep(1.2)
    if _ag is not None:
        AUTO_CHATS = _ag
    _rv = await leer_reenvio_guardado()
    await asyncio.sleep(1.2)
    if _rv is not None:
        ORIGENES, DESTINO, SOLO_VIDEO = _rv
    await leer_estado_guardado()
    await asyncio.sleep(1.2)
    await cargar_memoria()
    await asyncio.sleep(1.2)
    await cargar_amigos()
    await asyncio.sleep(1.2)
    await cargar_otrobots()
    await asyncio.sleep(1.2)
    await cargar_aprender()
    await asyncio.sleep(1.2)
    await cargar_modos()
    await asyncio.sleep(1.2)
    await cargar_tonos()
    await asyncio.sleep(1.2)
    await leer_callar_guardado()
    await asyncio.sleep(1.2)
    await cargar_resumen()
    await asyncio.sleep(1.2)
    await cargar_uso()
    await asyncio.sleep(1.2)
    await leer_cursores_guardados()
    await asyncio.sleep(1.2)
    if ORIGENES or DESTINO:
        try:
            await client.get_dialogs(limit=300)  # para que reconozca los chats de reenvío
        except Exception as e:
            print(f"No pude cargar los chats: {e}")
        await asyncio.sleep(1.2)
    await cargar_ejemplos()
    await asyncio.sleep(1.2)
    try:  # deja el menú de comandos fijado en Mensajes guardados
        menu_txt = texto_ayuda()
        fijados = await client.get_messages("me", limit=1, filter=types.InputMessagesFilterPinned)
        if fijados:  # edita el menú que ya está fijado en vez de borrar, mandar y fijar otro en cada arranque
            if (fijados[0].raw_text or "") != menu_txt:
                try:
                    await client.edit_message("me", fijados[0].id, menu_txt)
                except Exception as e:
                    if "not modified" not in str(e).lower():
                        raise
        else:
            menu = await client.send_message("me", menu_txt)
            await client.pin_message("me", menu, notify=False)
    except Exception as e:
        print(f"No pude fijar el menú: {e}")
    if not SESSION_STRING:
        print("\nTu SESSION_STRING (guárdala para no pedir código la próxima vez):\n")
        print(client.session.save(), "\n")
    asyncio.create_task(bucle_resumen())
    print(f"¡Userbot conectado como {yo.first_name}! Deja esta celda ejecutándose.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    loop.run_until_complete(main())
