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


def _github_contents_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{ERRORES_FILE_PATH}"


def cargar_errores_raw():
    """Descarga errores.json completo (todos los usuarios) vía GitHub Contents API.
    Devuelve (dict_errores, sha). Si el archivo no existe todavía, devuelve ({}, None)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning("Falta GITHUB_TOKEN o GITHUB_REPO: no se puede usar el repaso persistente de errores.")
        return {}, None
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(_github_contents_url(), headers=headers, timeout=10)
        if r.status_code == 404:
            return {}, None
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content), data["sha"]
    except requests.RequestException as e:
        logger.error(f"Error al descargar errores.json: {e}")
        return {}, None
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"errores.json no es válido: {e}")
        return {}, None


def guardar_errores_raw(errores_dict, sha):
    """Sube errores.json completo a GitHub. Reintenta una vez si el sha quedó desactualizado."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    content_str = json.dumps(errores_dict, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
    payload = {
        "message": "Actualizar registro de repaso de errores",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(_github_contents_url(), headers=headers, json=payload, timeout=10)
        if r.status_code == 409:
            # sha desactualizado (otro usuario escribió al mismo tiempo): reintenta una vez
            _, fresh_sha = cargar_errores_raw()
            payload["sha"] = fresh_sha
            r = requests.put(_github_contents_url(), headers=headers, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Error al guardar errores.json: {e}")
        return False


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
            user_states[chat_id] = {
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

            preguntas = filtrar_por_origen(preguntas, origen)

            if state["modo"] == "esperando_origen_aleatoria":
                if not preguntas:
                    enviar_mensaje(chat_id, "⚠️ No hay preguntas disponibles para esa fuente.", get_keyboard())
                    state["modo"] = None
                    return "ok", 200
                random.shuffle(preguntas)
                state["preguntas_lista"] = preguntas[:10]
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

            enviar_mensaje(chat_id, reporte, get_keyboard())

        elif text in ["❓ Ayuda", "/ayuda"]:
            ayuda = (
                "ℹ️ <b>¿Cómo usar tu Bot Entrenador?</b>\n\n"
                "1. Usa el botón <b>🎯 Práctica Aleatoria (10)</b> para simulacros rápidos.\n"
                "2. Usa el botón <b>📚 Por Dominios</b> para estudiar tus puntos débiles.\n"
                "3. En ambos casos podrás elegir la fuente: <b>🤖 IA</b>, <b>🎓 Clase</b> (transcritas de tus exámenes reales) o <b>🔀 Mezcladas</b>.\n"
                "4. Cada pregunta muestra su <b>Dominio</b> y su <b>Fuente</b> (nombre del examen y fecha, o banco IA).\n"
                f"5. Si fallas una pregunta, se guarda en tu <b>🔁 Repasar mis Errores</b>. Necesitas acertarla {RACHA_PARA_GRADUAR} veces seguidas para que se dé por dominada; este registro es personal y persiste aunque el bot se reinicie.\n"
                "6. Para responder a una pregunta, simplemente escribe la letra de la opción (<b>A, B, C o D</b>).\n"
                "7. Usa <b>📊 Mi Resumen</b> para ver tu efectividad acumulada en la sesión actual."
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

        texto_preg = f"<b>{pregunta['pregunta']}</b>\n\n"
        for opc, txt in pregunta["opciones"].items():
            texto_preg += f"<b>{opc})</b> {txt}\n"

        fuente = pregunta.get("fuente", "")
        texto_preg += f"\n<i>📌 Dominio: {pregunta.get('dominio', 'General')}</i>"
        if fuente:
            texto_preg += f"\n<i>📅 Fuente: {fuente}</i>"
        texto_preg += "\n👉 <i>Responde enviando únicamente la letra (A, B, C o D).</i>"

        enviar_mensaje(chat_id, texto_preg, get_keyboard())
    else:
        enviar_mensaje(chat_id, "🏁 <b>¡Has completado la tanda de preguntas!</b> Revisa tu resultado en el botón <b>📊 Mi Resumen</b>.", get_keyboard())
        state["modo"] = None


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
