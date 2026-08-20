import os
import json
import logging
from threading import Thread  # <-- Agrega esta línea al inicio
from flask import Flask
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO") # Formato: "usuario/repositorio"
GITHUB_FILE_PATH = "preguntas.json"

app = Flask(__name__)

# Diccionario en memoria para gestionar el estado del usuario
user_states = {}

def get_keyboard():
    """Genera el teclado interactivo con botones fijos."""
    keyboard = [
        [KeyboardButton("🎯 Práctica Aleatoria (10)"), KeyboardButton("📚 Por Dominios")],
        [KeyboardButton("📊 Mi Resumen / Reporte"), KeyboardButton("❓ Ayuda")]
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

def cargar_preguntas():
    """Descarga el preguntas.json actualizado desde GitHub."""
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    return []

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
    requests.post(url, json=payload)

@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    update = request.get_json()
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
                "modo": None
            }

        state = user_states[chat_id]

        # --- MENU PRINCIPAL ---
        if text in ["/start", "/menu", "🔙 Volver al Menú Principal"]:
            state["modo"] = None
            msg = "👋 <b>¡Hola, Aldemar! Bienvenido a tu Bot Entrenador CIA Parte 1.</b>\n\nElige una opción en el menú inferior para empezar:"
            enviar_mensaje(chat_id, msg, get_keyboard())

        elif text in ["🎯 Práctica Aleatoria (10)", "/practica"]:
            preguntas = cargar_preguntas()
            if not preguntas:
                enviar_mensaje(chat_id, "⚠️ No se pudieron cargar las preguntas desde GitHub. Revisa el archivo preguntas.json.", get_keyboard())
                return "ok", 200
            
            random.shuffle(preguntas)
            state["preguntas_lista"] = preguntas[:10]
            state["indice_lista"] = 0
            state["modo"] = "aleatorio"
            
            enviar_mensaje(chat_id, "🚀 <b>Iniciando ronda de 10 preguntas aleatorias.</b> ¡Mucho éxito!", get_keyboard())
            lanzar_siguiente_pregunta(chat_id)

        elif text in ["📚 Por Dominios", "/dominios"]:
            state["modo"] = "esperando_dominio"
            msg = "📚 <b>Selecciona el Dominio que deseas practicar:</b>"
            enviar_mensaje(chat_id, msg, get_domain_keyboard())

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
                preguntas = cargar_preguntas()
                filtradas = [p for p in preguntas if p.get("dominio", "").startswith(dominio_codigo)]
                if not filtradas:
                    enviar_mensaje(chat_id, f"⚠️ No se encontraron preguntas registradas para el <b>{text}</b>.", get_domain_keyboard())
                else:
                    random.shuffle(filtradas)
                    state["preguntas_lista"] = filtradas
                    state["indice_lista"] = 0
                    state["modo"] = "dominio"
                    enviar_mensaje(chat_id, f"🎯 <b>Practicando: {text}</b> ({len(filtradas)} preguntas encontradas).", get_keyboard())
                    lanzar_siguiente_pregunta(chat_id)
            else:
                enviar_mensaje(chat_id, "Por favor selecciona un dominio válido del menú.", get_domain_keyboard())

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
                reporte += "📈 Buen avance. Refuerza las 'conchas de mango' en los dominios más débiles."
            else:
                reporte += "💡 Revisa los conceptos COSO y el Estatuto de Auditoría. Vamos a seguir practicando."
                
            enviar_mensaje(chat_id, reporte, get_keyboard())

        elif text in ["❓ Ayuda", "/ayuda"]:
            ayuda = (
                "ℹ️ <b>¿Cómo usar tu Bot Entrenador?</b>\n\n"
                "1. Usa el botón <b>🎯 Práctica Aleatoria (10)</b> para simulacros rápidos.\n"
                "2. Usa el botón <b>📚 Por Dominios</b> para estudiar tus puntos débiles.\n"
                "3. Para responder a una pregunta, simplemente escribe la letra de la opción (<b>A, B, C o D</b>).\n"
                "4. Usa <b>📊 Mi Resumen</b> para ver tu efectividad acumulada."
            )
            enviar_mensaje(chat_id, ayuda, get_keyboard())

        # --- RESPUESTAS (A, B, C, D) ---
        elif state.get("pregunta_actual") and text.upper() in ["A", "B", "C", "D"]:
            pregunta = state["pregunta_actual"]
            respuesta_usr = text.upper()
            correcta = pregunta["correcta"].upper()
            
            state["total_respondidas"] += 1
            
            if respuesta_usr == correcta:
                state["score_correctas"] += 1
                msg = f"✅ <b>¡CORRECTO!</b>\n\n<b>Explicación / Concha de mango:</b>\n{pregunta.get('concha_mango', '¡Bien razonado!')}"
            else:
                msg = (
                    f"❌ <b>INCORRECTO.</b> Tu respuesta: {respuesta_usr} | Respuesta correcta: <b>{correcta}</b>\n\n"
                    f"💡 <b>Análisis Auditor:</b>\n{pregunta.get('concha_mango', 'Repasa el concepto clave de esta pregunta.')}"
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
        
        texto_preg += f"\n<i>📌 Dominio: {pregunta.get('dominio', 'General')}</i>\n"
        texto_preg += "👉 <i>Responde enviando únicamente la letra (A, B, C o D).</i>"
        
        enviar_mensaje(chat_id, texto_preg, get_keyboard())
    else:
        enviar_mensaje(chat_id, "🏁 <b>¡Has completado la tanda de preguntas!</b> Revisa tu resultado en el botón <b>📊 Mi Resumen</b>.", get_keyboard())
        state["modo"] = None


if __name__ == "__main__":
    # 1. Arranca Flask en segundo plano sin necesitar una función run_flask definida
    t = Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False))
    t.daemon = True
    t.start()

    # 2. Arranca el bot de Telegram en el proceso principal
    start_telegram_bot()
