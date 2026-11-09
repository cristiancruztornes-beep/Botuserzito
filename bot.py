import os
import re
import time
from datetime import datetime, timezone
import random
import difflib
import asyncio
from telethon import TelegramClient, events, utils, errors, types
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetRecentStickersRequest, GetFavedStickersRequest, SendVoteRequest
from telethon.tl.types import DocumentAttributeSticker, InputMessagesFilterVideo, InputMessagesFilterDocument
from telethon.errors import FloodWaitError
from google import genai
from aiohttp import web, ClientSession

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
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, loop=loop)

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
        muestra = random.sample(EJEMPLOS, min(len(EJEMPLOS), 25))  # otra tanda cada vez, para no repetirse
        p += "\n".join(f"- {e}" for e in muestra)
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


async def cargar_ejemplos():
    EJEMPLOS.clear()
    chats = GRUPOS[:]
    if not chats:
        chats = [d.id async for d in client.iter_dialogs(limit=40) if d.is_group]
    limite = datetime.fromisoformat(ANTES_DE).replace(tzinfo=timezone.utc)
    candidatos = set()
    propios = []
    for chat in chats[:10]:
        try:
            async for m in client.iter_messages(chat, from_user="me", limit=200, offset_date=limite):
                if m.sticker:
                    propios.append(m.sticker)
                    continue
                t = (m.raw_text or "").strip()
                if 3 <= len(t) <= 200 and "http" not in t and not t.startswith("/"):
                    candidatos.add(t)
        except Exception as e:
            print(f"No pude leer {chat}: {e}")
    candidatos = list(candidatos)
    random.shuffle(candidatos)
    EJEMPLOS.extend(candidatos[:150])
    print(f"🧠 Ejemplos de tu estilo cargados: {len(EJEMPLOS)}")
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


async def generar(contenido, sistema=None, max_tokens=1000, temperatura=None):
    reintentos = 0
    while True:
        try:
            return await ai_client.aio.models.generate_content(
                model=MODELO_ACTIVO,
                contents=contenido,
                config={
                    "system_instruction": sistema or prompt_sistema(),
                    "max_output_tokens": max_tokens,
                    **({"temperature": temperatura} if temperatura is not None else {}),
                },
            )
        except Exception as e:
            msg = str(e)
            if "429" in msg and "PerDay" in msg:
                if await cambiar_modelo_agotado():
                    continue
                raise
            reintentos += 1
            if reintentos >= 3 or not any(c in msg for c in ("503", "429")):
                raise
            await asyncio.sleep(3 * reintentos)


AUTO = True       # /auto on|off → detectar cuándo te hablan sin mencionarte
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
    if PAUSA:
        return
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
        ultima_respuesta[event.chat_id] = time.time()
        nota = MEMORIA.get(getattr(emisor, "id", None), {}).get("notas") if MEMORIA_ON else None
        extra_mem = (
            f"\nLo que sabes de {autor} (úsalo solo si viene al caso, sin forzarlo): {nota}" if nota else ""
        )
        previas_mias = RECIENTES.get(event.chat_id, [])[-8:]
        evitar = (
            "\n\nTus últimas respuestas en este chat (NO repitas sus frases, muletillas, emojis ni su estructura):\n"
            + "\n".join(f"- {r}" for r in previas_mias)
        ) if previas_mias else ""
        contenido = (
            "Conversación reciente:\n" + "\n".join(lineas)
            + f'\n\nÚltimo mensaje, de {autor}: "{texto}"{extra}{extra_mem}{evitar}'
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
            await event.reply(file=random.choice(opciones))
            print(f"Sticker enviado ({emo})")
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
            # en grupos la primera parte cita el mensaje; en privado casi nunca (como hace la gente)
            citar = (i == 0) and (not event.is_private or random.random() < 0.2)
            await enviar(event, parte, citar)
            print(f"Respuesta enviada: {parte}")
        registro.append(" / ".join(partes))
        del registro[:-10]
        if MEMORIA_ON and emisor is not None and len(texto) >= 4:
            asyncio.create_task(actualizar_memoria(emisor.id, autor, texto, " / ".join(partes), lineas))
    except Exception as e:
        print(f"Error al generar respuesta con IA: {e}")


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
MEM_ESPERA = 90             # segundos mínimos entre actualizaciones de notas de una misma persona
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


def texto_ayuda():
    return (
        "🤖 Userbot activo\n"
        f"Modelo: {MODELO_ACTIVO}\n"
        f"Ejemplos de tu estilo: {len(EJEMPLOS)}\n\n"
        "Comandos (escríbelos aquí, en Mensajes guardados):\n"
        "/modelo → ver todos los modelos\n"
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
    if _guardado is not None:
        ESTILO = _guardado
    _tg = await leer_trivia_guardada()
    if _tg is not None:
        TRIVIA_CHATS = _tg
    _ag = await leer_auto_guardado()
    if _ag is not None:
        AUTO_CHATS = _ag
    _rv = await leer_reenvio_guardado()
    if _rv is not None:
        ORIGENES, DESTINO, SOLO_VIDEO = _rv
    await leer_estado_guardado()
    await cargar_memoria()
    await leer_cursores_guardados()
    try:
        await client.get_dialogs(limit=300)  # para que reconozca los chats de reenvío
    except Exception as e:
        print(f"No pude cargar los chats: {e}")
    await cargar_ejemplos()
    try:  # deja el menú de comandos fijado en Mensajes guardados
        await client.unpin_message("me")
        menu = await client.send_message("me", texto_ayuda())
        await client.pin_message("me", menu, notify=False)
    except Exception as e:
        print(f"No pude fijar el menú: {e}")
    if not SESSION_STRING:
        print("\nTu SESSION_STRING (guárdala para no pedir código la próxima vez):\n")
        print(client.session.save(), "\n")
    print(f"¡Userbot conectado como {yo.first_name}! Deja esta celda ejecutándose.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    loop.run_until_complete(main())
