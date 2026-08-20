import os
import json
import logging
import random
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIGURACIÓN ---
TOKEN_SECRETO = os.environ.get("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TOKEN_SECRETO)


# Carga del Banco Completo desde el archivo JSON
try:
    with open("preguntas.json", "r", encoding="utf-8") as f:
        BANCO_COMPLETO = json.load(f)
    print(f"✅ Banco cargado exitosamente: {len(BANCO_COMPLETO)} preguntas en total.")
except Exception as e:
    print(f"❌ Error al cargar preguntas.json: {e}")
    BANCO_COMPLETO = []

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola {user}! 🎯 Tu Entrenador CIA Parte 1 está listo con {len(BANCO_COMPLETO)} preguntas.\n\n"
        "Comandos disponibles:\n"
        "/practica - Iniciar un bloque aleatorio de 10 preguntas\n"
        "/resumen - Generar reporte para enviar a tu Coach"
    )

async def practica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BANCO_COMPLETO:
        await update.message.reply_text("❌ No hay preguntas cargadas en la base de datos.")
        return

    # Selecciona 10 preguntas ALEATORIAS sin repetir
    cant = min(10, len(BANCO_COMPLETO))
    context.user_data['sesion_preguntas'] = random.sample(BANCO_COMPLETO, cant)
    context.user_data['indice'] = 0
    context.user_data['aciertos'] = 0
    context.user_data['fallos'] = []
    
    await enviar_pregunta(update, context)

async def enviar_pregunta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get('indice', 0)
    preguntas_sesion = context.user_data.get('sesion_preguntas', [])
    
    if idx < len(preguntas_sesion):
        q = preguntas_sesion[idx]
        keyboard = [
            [InlineKeyboardButton("A", callback_data=f"A_{q['id']}"), InlineKeyboardButton("B", callback_data=f"B_{q['id']}")],
            [InlineKeyboardButton("C", callback_data=f"C_{q['id']}"), InlineKeyboardButton("D", callback_data=f"D_{q['id']}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        texto_opciones = "\n".join([f"{k}) {v}" for k, v in q['opciones'].items()])
        mensaje = f"📌 Pregunta {idx+1}/{len(preguntas_sesion)} (ID: #{q['id']})\n\n{q['pregunta']}\n\n{texto_opciones}"
        
        if update.message:
            await update.message.reply_text(mensaje, reply_markup=reply_markup)
        else:
            await update.callback_query.message.reply_text(mensaje, reply_markup=reply_markup)
    else:
        aciertos = context.user_data.get('aciertos', 0)
        total = len(preguntas_sesion)
        pct = (aciertos / total) * 100 if total > 0 else 0
        msg_final = f"🏁 **¡Bloque Completado!**\n\n🎯 Puntaje: {aciertos}/{total} ({pct:.1f}%)\n\nEscribe /resumen para generar el informe o /practica para lanzar 10 preguntas nuevas."
        if update.callback_query:
            await update.callback_query.message.reply_text(msg_final, parse_mode="Markdown")

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    opcion, q_id = query.data.split("_")
    q_id = int(q_id)
    q = next(p for p in BANCO_COMPLETO if p['id'] == q_id)
    
    idx = context.user_data.get('indice', 0)
    
    if opcion == q['correcta']:
        context.user_data['aciertos'] = context.user_data.get('aciertos', 0) + 1
        respuesta_msg = f"✅ **¡Correcto!**\n\n💡 *Concha de Mango:* {q['concha_mango']}"
    else:
        context.user_data.setdefault('fallos', []).append(q_id)
        respuesta_msg = f"❌ **Incorrecto.** La respuesta correcta era **{q['correcta']}**.\n\n💡 *Concha de Mango:* {q['concha_mango']}"
    
    await query.edit_message_text(text=f"{query.message.text}\n\nTu respuesta: {opcion}\n{respuesta_msg}", parse_mode="Markdown")
    context.user_data['indice'] = idx + 1
    await enviar_pregunta(update, context)

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    aciertos = context.user_data.get('aciertos', 0)
    preguntas_sesion = context.user_data.get('sesion_preguntas', [])
    total = len(preguntas_sesion)
    fallos = context.user_data.get('fallos', [])
    
    reporte = (
        f"📊 **REPORTE DE COACHING CIA**\n"
        f"-----------------------------\n"
        f"Puntaje: {aciertos}/{total}\n"
        f"IDs Fallados: {fallos if fallos else '¡Ninguno! Perfecta la racha.'}\n"
        f"-----------------------------\n"
        f"Copia y pega este texto en nuestro chat de entrenamiento."
    )
    await update.message.reply_text(reporte, parse_mode="Markdown")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("practica", practica))
    app.add_handler(CommandHandler("resumen", resumen))
    app.add_handler(CallbackQueryHandler(responder))
    
    print("🤖 CIA Coach Bot iniciado con base de datos completa...")
    app.run_polling()

if __name__ == "__main__":
    main()
