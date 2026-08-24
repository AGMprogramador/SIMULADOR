import os
import json
import base64
import random
import logging
from flask import Flask, request
import requests
from telegram import KeyboardButton, ReplyKeyboardMarkup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # Formato: "usuario/repositorio"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_FILE_PATH = "preguntas.json"
ERRORES_FILE_PATH = "errores.json"
SESIONES_FILE_PATH = "sesiones.json"
VISTAS_FILE_PATH = "vistas.json"
RACHA_PARA_GRADUAR = 3  # aciertos consecutivos necesarios para "graduar" una pregunta del repaso

if not TOKEN:
    raise RuntimeError("Falta la variable de entorno TELEGRAM_TOKEN. El bot no puede iniciar sin ella.")

app = Flask(__name__)

# Ruta del webhook: usamos el TOKEN como path secreto (evita que cualquiera dispare tu bot).
# OJO: esta ruta y la que registramos con setWebhook() deben ser EXACTAMENTE la misma.
WEBHOOK_PATH = f"/{TOKEN}"

# Diccionario en memoria para gestionar el estado del usuario
user_states = {}


def get_keyboard():
    """Genera el teclado interactivo con botones fijos."""
    keyboard = [
        [KeyboardButton("🎯 Práctica Aleatoria (10)"), KeyboardButton("📚 Por Dominios")],
        [KeyboardButton("🔁 Repasar mis Errores"), KeyboardButton("📊 Mi Resumen / Reporte")],
        [KeyboardButton("❓ Ayuda")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_source_keyboard():
    """Genera los botones para elegir la fuente de las preguntas."""
    keyboard = [
        [KeyboardButton("🤖 Preguntas IA")],
        [KeyboardButton("🎓 Preguntas de Clase (Exámenes)")],
        [KeyboardButton("🔀 Mezcladas (IA + Clase)")],
        [KeyboardButton("🔙 Volver al Menú Principal")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_domain_keyboard():
    """Genera los botones para elegir un Dominio específico."""
    keyboard = [
        [KeyboardButton("1️⃣ Dominio I: Fundamentos")],
        [KeyboardButton("2️⃣ Dominio II: Independencia y Objetividad")],
        [KeyboardButton("3️⃣ Dominio III: Competencia y Cuidado")],
        [KeyboardButton("4️⃣ Dominio IV: Gestión del Riesgo y Control")],
        [KeyboardButton("5️⃣ Dominio V: Gobierno Corporativo y Ética")],
        [KeyboardButton("6️⃣ Dominio VI: Fraude y Comunicaciones")],
        [KeyboardButton("🔙 Volver al Menú Principal")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


SOURCE_LABELS = {
    "ia": "🤖 IA",
    "clase": "🎓 Clase",
}


def cargar_preguntas():
    """Descarga el preguntas.json actualizado desde GitHub."""
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Error al descargar preguntas.json: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"preguntas.json no es un JSON válido: {e}")
        return []


def filtrar_por_origen(preguntas, origen):
    """Filtra la lista de preguntas por su campo 'origen' ('ia' o 'clase').
    origen=None devuelve todas (mezcladas)."""
    if origen is None:
        return preguntas
    return [p for p in preguntas if p.get("origen") == origen]


def _github_contents_url(path):
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"


def cargar_json_github(path):
    """Descarga un JSON completo desde GitHub vía Contents API.
    Devuelve (dict, sha). Si el archivo no existe todavía, devuelve ({}, None)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning(f"Falta GITHUB_TOKEN o GITHUB_REPO: no se puede leer {path} desde GitHub.")
        return {}, None
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(_github_contents_url(path), headers=headers, timeout=10)
        if r.status_code == 404:
            return {}, None
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content), data["sha"]
    except requests.RequestException as e:
        logger.error(f"Error al descargar {path}: {e}")
        return {}, None
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"{path} no es válido: {e}")
        return {}, None


def guardar_json_github(path, data_dict, sha, mensaje="Actualizar estado del bot"):
    """Sube un JSON completo a GitHub. Reintenta una vez si el sha quedó desactualizado."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    content_str = json.dumps(data_dict, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
    payload = {"message": mensaje, "content": content_b64, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(_github_contents_url(path), headers=headers, json=payload, timeout=10)
        if r.status_code == 409:
            # sha desactualizado (otra escritura concurrente): reintenta una vez
            _, fresh_sha = cargar_json_github(path)
            payload["sha"] = fresh_sha
            r = requests.put(_github_contents_url(path), headers=headers, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Error al guardar {path}: {e}")
        return False


def cargar_errores_raw():
    return cargar_json_github(ERRORES_FILE_PATH)


def guardar_errores_raw(errores_dict, sha):
    return guardar_json_github(ERRORES_FILE_PATH, errores_dict, sha, "Actualizar registro de repaso de errores")


def registrar_respuesta_error(chat_id, pregunta, fue_correcta):
    """Actualiza el repaso de errores del usuario tras responder una pregunta.
    - Si falla: la agrega (o reinicia su racha a 0).
    - Si acierta y la pregunta estaba en su lista: suma 1 a la racha; si llega a
      RACHA_PARA_GRADUAR, la elimina (pregunta 'graduada').
    Devuelve True si la pregunta fue graduada en esta respuesta (para avisar al usuario)."""
    errores, sha = cargar_errores_raw()
    chat_key = str(chat_id)
    qid = str(pregunta["id"])
    usuario_errores = errores.setdefault(chat_key, {})

    graduada = False
    if fue_correcta:
        if qid in usuario_errores:
            usuario_errores[qid]["racha"] = usuario_errores[qid].get("racha", 0) + 1
            if usuario_errores[qid]["racha"] >= RACHA_PARA_GRADUAR:
                del usuario_errores[qid]
                graduada = True
    else:
        usuario_errores[qid] = {"racha": 0}

    if not usuario_errores:
        errores.pop(chat_key, None)

    guardar_errores_raw(errores, sha)
    return graduada


def obtener_preguntas_para_repaso(chat_id, preguntas):
    """Devuelve la lista de objetos de pregunta (del banco completo) que el usuario
    tiene pendientes de repasar, según errores.json."""
    errores, _ = cargar_errores_raw()
    usuario_errores = errores.get(str(chat_id), {})
    if not usuario_errores:
        return []
    ids_pendientes = set(usuario_errores.keys())
    return [p for p in preguntas if str(p.get("id")) in ids_pendientes]


def contar_totales_por_origen(preguntas):
    """Cuenta cuántas preguntas hay en el banco actual por cada origen."""
    return {
        "ia": sum(1 for p in preguntas if p.get("origen") == "ia"),
        "clase": sum(1 for p in preguntas if p.get("origen") == "clase"),
    }


def obtener_vistos_usuario(chat_id, vistas=None):
    """Devuelve {'ia': [...ids...], 'clase': [...ids...]} para este usuario.
    Si no se pasa 'vistas' ya cargado, lo descarga de GitHub."""
    if vistas is None:
        vistas, _ = cargar_json_github(VISTAS_FILE_PATH)
    return vistas.get(str(chat_id), {})


def filtrar_no_vistas(preguntas, chat_id):
    """De una lista de preguntas, devuelve solo las que este usuario todavía
    no ha visto (según vistas.json), agrupando por el origen propio de cada
    pregunta. Si el resultado queda vacío (ya vio el 100% de ese subconjunto),
    devuelve la lista completa sin filtrar."""
    vistos = obtener_vistos_usuario(chat_id)

    def ya_vista(p):
        origen_p = p.get("origen")
        return str(p.get("id")) in vistos.get(origen_p, [])

    no_vistas = [p for p in preguntas if not ya_vista(p)]
    return no_vistas if no_vistas else preguntas


def registrar_pregunta_vista(chat_id, pregunta, totales_por_origen=None):
    """Marca una pregunta como vista por el usuario en vistas.json.
    Devuelve True si con esta pregunta el usuario ACABA de completar el 100%
    de esa fuente (para poder felicitarlo), o False en cualquier otro caso."""
    origen = pregunta.get("origen")
    if origen not in ("ia", "clase"):
        return False

    vistas, sha = cargar_json_github(VISTAS_FILE_PATH)
    chat_key = str(chat_id)
    qid = str(pregunta.get("id"))
    usuario_vistas = vistas.setdefault(chat_key, {})
    lista_vistas = usuario_vistas.setdefault(origen, [])

    ya_estaba = qid in lista_vistas
    if not ya_estaba:
        lista_vistas.append(qid)
        guardar_json_github(VISTAS_FILE_PATH, vistas, sha, "Actualizar progreso de cobertura")

    if ya_estaba or not totales_por_origen:
        return False

    total = totales_por_origen.get(origen)
    return bool(total) and len(lista_vistas) == total


def reiniciar_progreso_cobertura(chat_id):
    """Borra el progreso de cobertura del usuario (todas las fuentes)."""
    vistas, sha = cargar_json_github(VISTAS_FILE_PATH)
    chat_key = str(chat_id)
    if chat_key in vistas:
        del vistas[chat_key]
        guardar_json_github(VISTAS_FILE_PATH, vistas, sha, "Reiniciar progreso de cobertura")


def guardar_sesion_activa(chat_id, state):
    """Persiste en GitHub la ronda de práctica en curso (lista de ids, índice y
    marcador), para poder reanudarla si el proceso del bot se reinicia entre
    pregunta y respuesta (p. ej. por inactividad o un redeploy en Railway)."""
    sesiones, sha = cargar_json_github(SESIONES_FILE_PATH)
    sesiones[str(chat_id)] = {
        "lista_ids": [p["id"] for p in state.get("preguntas_lista", [])],
        "indice_lista": state.get("indice_lista", 0),
        "score_correctas": state.get("score_correctas", 0),
        "total_respondidas": state.get("total_respondidas", 0),
    }
    guardar_json_github(SESIONES_FILE_PATH, sesiones, sha, "Actualizar sesión activa")


def borrar_sesion_activa(chat_id):
    """Elimina la sesión persistida (ronda terminada o reiniciada desde el menú)."""
    sesiones, sha = cargar_json_github(SESIONES_FILE_PATH)
    if str(chat_id) in sesiones:
        del sesiones[str(chat_id)]
        guardar_json_github(SESIONES_FILE_PATH, sesiones, sha, "Limpiar sesión activa")


def restaurar_sesion_activa(chat_id, preguntas, sesiones):
    """Intenta reconstruir el estado de una ronda de práctica en curso a partir de
    lo guardado en GitHub. Devuelve un dict de estado listo para usar, o None si
    no había ninguna sesión pendiente para este usuario."""
    sesion = sesiones.get(str(chat_id))
    if not sesion:
        return None

    preguntas_por_id = {str(p["id"]): p for p in preguntas}
    lista = [preguntas_por_id[i] for i in sesion.get("lista_ids", []) if i in preguntas_por_id]
    indice = sesion.get("indice_lista", 0)
    if not lista or indice >= len(lista):
        return None

    return {
        "pregunta_actual": lista[indice],
        "score_correctas": sesion.get("score_correctas", 0),
        "total_respondidas": sesion.get("total_respondidas", 0),
        "preguntas_lista": lista,
        "indice_lista": indice,
        "modo": "practicando",
        "origen_pendiente": None,
        "dominio_pendiente": None,
    }


def enviar_mensaje(chat_id, texto, reply_markup=None):
    """Envía un mensaje a Telegram con soporte para formato HTML."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup.to_dict()
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Error al enviar mensaje a Telegram: {e}")


@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    update = request.get_json(silent=True) or {}
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "").strip()

        if chat_id not in user_states:
            # El bot pudo haberse reiniciado (inactividad, redeploy en Railway, etc.)
            # perdiendo el estado en memoria. Antes de asumir "sin pregunta activa",
            # intenta recuperar una ronda de práctica que haya quedado a medias.
            restaurado = None
            sesiones_check, _ = cargar_json_github(SESIONES_FILE_PATH)
            if str(chat_id) in sesiones_check:
                preguntas_bank = cargar_preguntas()
                restaurado = restaurar_sesion_activa(chat_id, preguntas_bank, sesiones_check)

            user_states[chat_id] = restaurado or {
                "pregunta_actual": None,
                "score_correctas": 0,
                "total_respondidas": 0,
                "preguntas_lista": [],
                "indice_lista": 0,
                "modo": None,
                "origen_pendiente": None,   # "ia" | "clase" | None (mezclada) mientras se elige
                "dominio_pendiente": None,  # nombre del dominio elegido, mientras se elige origen
            }

        state = user_states[chat_id]

        # --- MENU PRINCIPAL ---
        if text in ["/start", "/menu", "🔙 Volver al Menú Principal"]:
            state["modo"] = None
            if state.get("pregunta_actual"):
                borrar_sesion_activa(chat_id)
            state["pregunta_actual"] = None
            msg = "👋 <b>¡Hola, Aldemar! Bienvenido a tu Bot Entrenador CIA Parte 1.</b>\n\nElige una opción en el menú inferior para empezar:"
            enviar_mensaje(chat_id, msg, get_keyboard())

        elif text in ["🎯 Práctica Aleatoria (10)", "/practica"]:
            state["modo"] = "esperando_origen_aleatoria"
            state["dominio_pendiente"] = None
            msg = "🎯 <b>¿Qué banco de preguntas quieres practicar?</b>"
            enviar_mensaje(chat_id, msg, get_source_keyboard())

        elif text in ["📚 Por Dominios", "/dominios"]:
            state["modo"] = "esperando_dominio"
            msg = "📚 <b>Selecciona el Dominio que deseas practicar:</b>"
            enviar_mensaje(chat_id, msg, get_domain_keyboard())

        elif text in ["🔁 Repasar mis Errores", "/repaso"]:
            preguntas = cargar_preguntas()
            if not preguntas:
                enviar_mensaje(chat_id, "⚠️ No se pudieron cargar las preguntas desde GitHub.", get_keyboard())
                return "ok", 200

            state["totales_origen"] = contar_totales_por_origen(preguntas)
            pendientes = obtener_preguntas_para_repaso(chat_id, preguntas)
            if not pendientes:
                enviar_mensaje(
                    chat_id,
                    "🎉 <b>¡No tienes preguntas pendientes de repaso!</b>\nCuando falles una pregunta, aparecerá aquí hasta que la aciertes 3 veces seguidas.",
                    get_keyboard()
                )
            else:
                random.shuffle(pendientes)
                state["preguntas_lista"] = pendientes
                state["indice_lista"] = 0
                state["modo"] = "practicando"
                enviar_mensaje(
                    chat_id,
                    f"🔁 <b>Repaso de errores</b> ({len(pendientes)} pregunta{'s' if len(pendientes) != 1 else ''} pendiente{'s' if len(pendientes) != 1 else ''}).\nNecesitas acertar cada una <b>{RACHA_PARA_GRADUAR} veces seguidas</b> para que se dé por dominada.",
                    get_keyboard()
                )
                lanzar_siguiente_pregunta(chat_id)

        elif state.get("modo") == "esperando_dominio":
            mapa_dominios = {
                "1️⃣ Dominio I: Fundamentos": "Dominio I",
                "2️⃣ Dominio II: Independencia y Objetividad": "Dominio II",
                "3️⃣ Dominio III: Competencia y Cuidado": "Dominio III",
                "4️⃣ Dominio IV: Gestión del Riesgo y Control": "Dominio IV",
                "5️⃣ Dominio V: Gobierno Corporativo y Ética": "Dominio V",
                "6️⃣ Dominio VI: Fraude y Comunicaciones": "Dominio VI"
            }

            dominio_codigo = mapa_dominios.get(text)
            if dominio_codigo:
                state["dominio_pendiente"] = (dominio_codigo, text)
                state["modo"] = "esperando_origen_dominio"
                msg = f"📚 <b>{text}</b>\n¿Qué banco de preguntas quieres usar?"
                enviar_mensaje(chat_id, msg, get_source_keyboard())
            else:
                enviar_mensaje(chat_id, "Por favor selecciona un dominio válido del menú.", get_domain_keyboard())

        elif state.get("modo") in ["esperando_origen_aleatoria", "esperando_origen_dominio"]:
            mapa_origen = {
                "🤖 Preguntas IA": "ia",
                "🎓 Preguntas de Clase (Exámenes)": "clase",
                "🔀 Mezcladas (IA + Clase)": None,
            }
            if text not in mapa_origen:
                enviar_mensaje(chat_id, "Por favor selecciona una opción válida del menú.", get_source_keyboard())
                return "ok", 200

            origen = mapa_origen[text]
            preguntas = cargar_preguntas()
            if not preguntas:
                enviar_mensaje(chat_id, "⚠️ No se pudieron cargar las preguntas desde GitHub. Revisa el archivo preguntas.json.", get_keyboard())
                state["modo"] = None
                return "ok", 200

            state["totales_origen"] = contar_totales_por_origen(preguntas)
            preguntas = filtrar_por_origen(preguntas, origen)

            if state["modo"] == "esperando_origen_aleatoria":
                if not preguntas:
                    enviar_mensaje(chat_id, "⚠️ No hay preguntas disponibles para esa fuente.", get_keyboard())
                    state["modo"] = None
                    return "ok", 200
                pool = filtrar_no_vistas(preguntas, chat_id)
                random.shuffle(pool)
                state["preguntas_lista"] = pool[:10]
                state["indice_lista"] = 0
                state["modo"] = "practicando"
                fuente_txt = SOURCE_LABELS.get(origen, "🔀 Mezcladas")
                enviar_mensaje(chat_id, f"🚀 <b>Iniciando ronda de 10 preguntas aleatorias ({fuente_txt}).</b> ¡Mucho éxito!", get_keyboard())
                lanzar_siguiente_pregunta(chat_id)
            else:  # esperando_origen_dominio
                dominio_codigo, dominio_texto = state["dominio_pendiente"]
                filtradas = [p for p in preguntas if p.get("dominio", "").startswith(dominio_codigo)]
                if not filtradas:
                    enviar_mensaje(chat_id, f"⚠️ No se encontraron preguntas registradas para el <b>{dominio_texto}</b> en esa fuente.", get_domain_keyboard())
                    state["modo"] = "esperando_dominio"
                else:
                    filtradas = filtrar_no_vistas(filtradas, chat_id)
                    random.shuffle(filtradas)
                    state["preguntas_lista"] = filtradas
                    state["indice_lista"] = 0
                    state["modo"] = "practicando"
                    fuente_txt = SOURCE_LABELS.get(origen, "🔀 Mezcladas")
                    enviar_mensaje(chat_id, f"🎯 <b>Practicando: {dominio_texto}</b> ({fuente_txt}, {len(filtradas)} preguntas encontradas).", get_keyboard())
                    lanzar_siguiente_pregunta(chat_id)

        elif text in ["📊 Mi Resumen / Reporte", "/resumen"]:
            total = state["total_respondidas"]
            correctas = state["score_correctas"]
            porcentaje = (correctas / total * 100) if total > 0 else 0

            reporte = (
                f"📊 <b>REPORTE DE RENDIMIENTO CIA - ALDEMAR</b>\n\n"
                f"📝 <b>Preguntas respondidas en esta sesión:</b> {total}\n"
                f"✅ <b>Respuestas correctas:</b> {correctas}\n"
                f"❌ <b>Respuestas incorrectas:</b> {total - correctas}\n"
                f"🎯 <b>Efectividad:</b> {porcentaje:.1f}%\n\n"
            )
            if porcentaje >= 85:
                reporte += "🏆 ¡Nivel Senior de Auditoría! Estás listo para el examen."
            elif porcentaje >= 70:
                reporte += "📈 Buen avance. Refuerza los dominios más débiles."
            else:
                reporte += "💡 Revisa los conceptos COSO y el Estatuto de Auditoría. Vamos a seguir practicando."

            preguntas_bank = cargar_preguntas()
            if preguntas_bank:
                totales = contar_totales_por_origen(preguntas_bank)
                vistos_usuario = obtener_vistos_usuario(chat_id)
                reporte += "\n\n📚 <b>Progreso de cobertura del banco</b> (preguntas distintas que ya te tocaron, al menos una vez):"
                for origen_key, label in [("clase", "🎓 Clase"), ("ia", "🤖 IA")]:
                    vistos_n = len(vistos_usuario.get(origen_key, []))
                    total_n = totales.get(origen_key, 0)
                    pct = (vistos_n / total_n * 100) if total_n else 0
                    reporte += f"\n{label}: {vistos_n}/{total_n} ({pct:.0f}%)"

            enviar_mensaje(chat_id, reporte, get_keyboard())

        elif text in ["/reiniciar_progreso"]:
            reiniciar_progreso_cobertura(chat_id)
            enviar_mensaje(
                chat_id,
                "🔄 <b>Progreso de cobertura reiniciado.</b> A partir de ahora, el bot vuelve a priorizar todas las preguntas como si no hubieras visto ninguna.",
                get_keyboard()
            )

        elif text in ["❓ Ayuda", "/ayuda"]:
            ayuda = (
                "ℹ️ <b>¿Cómo usar tu Bot Entrenador?</b>\n\n"
                "1. Usa el botón <b>🎯 Práctica Aleatoria (10)</b> para simulacros rápidos.\n"
                "2. Usa el botón <b>📚 Por Dominios</b> para estudiar tus puntos débiles.\n"
                "3. En ambos casos podrás elegir la fuente: <b>🤖 IA</b>, <b>🎓 Clase</b> (transcritas de tus exámenes reales) o <b>🔀 Mezcladas</b>.\n"
                "4. Cada pregunta muestra su <b>Dominio</b> y su <b>Fuente</b> (nombre del examen y fecha, o banco IA).\n"
                f"5. Si fallas una pregunta, se guarda en tu <b>🔁 Repasar mis Errores</b>. Necesitas acertarla {RACHA_PARA_GRADUAR} veces seguidas para que se dé por dominada; este registro es personal y persiste aunque el bot se reinicie.\n"
                "6. Para responder a una pregunta, simplemente escribe la letra de la opción (<b>A, B, C o D</b>).\n"
                "7. Usa <b>📊 Mi Resumen</b> para ver tu efectividad acumulada en la sesión actual y tu progreso de cobertura del banco (cuántas preguntas distintas ya te tocaron, del total disponible en Clase e IA).\n"
                "8. El bot prioriza automáticamente preguntas que todavía no te han tocado; cuando completes el 100% de una fuente te avisa. Si querés reiniciar ese conteo y volver a recorrer todo desde cero, escribe <b>/reiniciar_progreso</b>."
            )
            enviar_mensaje(chat_id, ayuda, get_keyboard())

        # --- RESPUESTAS (A, B, C, D) ---
        elif state.get("pregunta_actual") and text.upper() in ["A", "B", "C", "D"]:
            pregunta = state["pregunta_actual"]
            respuesta_usr = text.upper()
            correcta = pregunta["correcta"].upper()
            fue_correcta = (respuesta_usr == correcta)

            state["total_respondidas"] += 1
            graduada = registrar_respuesta_error(chat_id, pregunta, fue_correcta)

            if fue_correcta:
                state["score_correctas"] += 1
                msg = f"✅ <b>¡CORRECTO!</b>\n\n<b>Explicación:</b>\n{pregunta.get('explicacion') or '¡Bien razonado!'}"
                if graduada:
                    msg += f"\n\n🎓 <b>¡Graduaste esta pregunta!</b> La acertaste {RACHA_PARA_GRADUAR} veces seguidas y salió de tu lista de repaso."
            else:
                msg = (
                    f"❌ <b>INCORRECTO.</b> Tu respuesta: {respuesta_usr} | Respuesta correcta: <b>{correcta}</b>\n\n"
                    f"💡 <b>Explicación:</b>\n{pregunta.get('explicacion') or 'Repasa el concepto clave de esta pregunta.'}"
                    f"\n\n🔁 Esta pregunta quedó guardada en tu repaso de errores (necesitas {RACHA_PARA_GRADUAR} aciertos seguidos para graduarla)."
                )

            state["pregunta_actual"] = None
            enviar_mensaje(chat_id, msg)

            state["indice_lista"] += 1
            lanzar_siguiente_pregunta(chat_id)

        else:
            enviar_mensaje(chat_id, "💡 Utiliza el menú de botones interactivos para navegar o responde con <b>A, B, C o D</b>.", get_keyboard())

    return "ok", 200


def lanzar_siguiente_pregunta(chat_id):
    state = user_states[chat_id]
    lista = state.get("preguntas_lista", [])
    idx = state.get("indice_lista", 0)

    if idx < len(lista):
        pregunta = lista[idx]
        state["pregunta_actual"] = pregunta
        guardar_sesion_activa(chat_id, state)

        texto_preg = f"<b>{pregunta['pregunta']}</b>\n\n"
        for opc, txt in pregunta["opciones"].items():
            texto_preg += f"<b>{opc})</b> {txt}\n"

        fuente = pregunta.get("fuente", "")
        texto_preg += f"\n<i>📌 Dominio: {pregunta.get('dominio', 'General')}</i>"
        if fuente:
            texto_preg += f"\n<i>📅 Fuente: {fuente}</i>"
        texto_preg += "\n👉 <i>Responde enviando únicamente la letra (A, B, C o D).</i>"

        enviar_mensaje(chat_id, texto_preg, get_keyboard())

        completo = registrar_pregunta_vista(chat_id, pregunta, state.get("totales_origen"))
        if completo:
            origen_txt = "🎓 Clase" if pregunta.get("origen") == "clase" else "🤖 IA"
            enviar_mensaje(
                chat_id,
                f"🎉 <b>¡Completaste el 100% de las preguntas del banco {origen_txt}!</b>\n"
                f"Ya pasaste al menos una vez por todas las preguntas disponibles de esa fuente.\n\n"
                f"Si querés reiniciar el conteo y volver a recorrerlas todas desde cero, escribí "
                f"<b>/reiniciar_progreso</b>. Si preferís seguir practicando en modo libre, no hace falta que hagas nada más."
            )
    else:
        enviar_mensaje(chat_id, "🏁 <b>¡Has completado la tanda de preguntas!</b> Revisa tu resultado en el botón <b>📊 Mi Resumen</b>.", get_keyboard())
        state["modo"] = None
        borrar_sesion_activa(chat_id)


def registrar_webhook():
    """
    Registra el webhook en Telegram. Se ejecuta al importar el módulo
    (no solo dentro de __main__), para que funcione también cuando el
    servidor se levanta con gunicorn/uwsgi en Railway.
    """
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("URL_APP")
    if not railway_url:
        logger.warning("No se encontró RAILWAY_PUBLIC_DOMAIN ni URL_APP. No se registró el webhook automáticamente.")
        return

    if not railway_url.startswith("http"):
        railway_url = f"https://{railway_url}"

    # IMPORTANTE: esta URL debe coincidir exactamente con WEBHOOK_PATH definido arriba.
    full_webhook_url = f"{railway_url}{WEBHOOK_PATH}"

    try:
        res = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            params={"url": full_webhook_url},
            timeout=10
        )
        logger.info(f"Resultado registro Webhook ({full_webhook_url}): {res.text}")
    except requests.RequestException as e:
        logger.error(f"Error al registrar Webhook: {e}")


# Se ejecuta siempre que el módulo se importa (python app.py O gunicorn app:app)
registrar_webhook()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
