import os
import random
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- SERVIDOR WEB - MANTENDRA DESPIERTO AL BOT ---

from flask import Flask
from threading import Thread

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Esta vivo, tu puedes amor"

def run_web():
    web_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

Thread(target=run_web).start()

# --- CONFIGURACIÓN ---
ADMIN_IDS = (7740467368, 6905064136)  # tus IDs de administrador

# Variables globales
stats = {}
config = {"keyword": "compte"}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- COMANDOS ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu = (
        " **Manual de Operaciones (Comandos)**\n\n"
        " `/top` -> 𝖬𝗎𝖾𝗌𝗍𝗋𝖺 𝖾𝗅 𝗍𝗈𝗉 𝖽𝖾 𝖺𝖼𝗍𝗂𝗏𝗂𝖽𝖺𝖽.\n"
        " `/reset` -> 𝖱𝖾𝗂𝗇𝗂𝖼𝗂𝖺 𝖾𝗅 𝖼𝗈𝗇𝗍𝖺𝖽𝗈𝗋 𝖽𝖾 𝗆𝖾𝗇𝗌𝖺𝗃𝖾𝗌.\n"
        " `/setkeyword <palabra>` -> 𝖢𝖺𝗆𝖻𝗂𝖺 𝗅𝖺 𝗉𝖺𝗅𝖺𝖻𝗋𝖺 𝖽𝖾 𝗏𝗂𝗀𝗂𝗅𝖺𝗇𝖼𝗂𝖺.\n"
        " `/trabaja [@usuario]` -> 𝖬𝖾𝗇𝗌𝖺𝗃𝖾 𝖽𝖾 𝗌𝗈𝖻𝗋𝖾𝖾𝗑𝗉𝗅𝗈𝗍𝖺𝖼𝗂ó𝗇 𝖼𝗋𝖾𝖺𝗍𝗂𝗏𝖺.\n"
        " `/help` -> 𝖬𝗎𝖾𝗌𝗍𝗋𝖺 𝖾𝗌𝗍𝖾 𝗆𝖾𝗇𝗌𝖺𝗃𝖾."
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

async def show_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not stats:
        await update.message.reply_text(" No hay datos aún. ¡A trabajar!")
        return

    ranking = sorted(stats.items(), key=lambda x: x[1]["puntos"], reverse=True)
    mensaje = " **TOP DE COMPTES** \n\n"
    for i, (user_id, datos) in enumerate(ranking[:10], 1):
        medalla = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        mensaje += f"{medalla} **{datos['nombre']}**: {datos['puntos']} veces\n"

    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede reiniciar el contador.")
        return
    global stats
    stats = {}
    await update.message.reply_text(" Contador reiniciado a cero.")

async def set_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede cambiar la palabra clave.")
        return

    if not context.args:
        await update.message.reply_text("Uso: `/setkeyword <nueva_palabra>`", parse_mode="Markdown")
        return

    nueva_palabra = context.args[0].lower()
    config["keyword"] = nueva_palabra
    await update.message.reply_text(f" Palabra de clave cambiada a: **{nueva_palabra}**", parse_mode="Markdown")

async def trabaja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = " ".join(context.args) if context.args else "alguien"
    frases = [
        f"Menos drama y más pala, {target}. El puesto de arriba no se gana siendo un flojo.",
        f"A ver si así como chismeas, chambearas, {target}.",
        f"Oh, nena… menos carita bonita y más cartera llena, {target}.",
        f"Oh, nena {target}… muy icónica, pero poco productiva.",
        f"Admin y fantasma no es el mismo puesto, actívate {target}.",
        f"¿Qué tal si en vez de estar aquí chismeando, {target}, te pones a chambear?",
        f"Menos ghosteo y más movimiento, {target}",
        f"Amorcito {target}, tú muy presente… espiritualmente, porque en el cc no."
    ]
    await update.message.reply_text(random.choice(frases))

# --- MONITOR ---

async def monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    texto = update.message.text.lower()

    if config["keyword"] in texto:
        user_id = str(update.effective_user.id)
        nombre = update.effective_user.first_name

        if user_id not in stats:
            stats[user_id] = {"nombre": nombre, "puntos": 0}

        stats[user_id]["puntos"] += 1
        print(f"Registro: {nombre} dijo {config['keyword']}")

# --- MAIN ---
if __name__ == '__main__':
    token_bot = os.environ.get('TOKEN')
    if not token_bot:
        raise ValueError("No configuraste bien el TOKEN hijita, porfavor")

    print("🤖 Iniciando bot de Telegram con run_polling...")
    application = ApplicationBuilder().token(token_bot).build()

    # Registro de Handlers
    application.add_handler(CommandHandler("inicio", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("top", show_top))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("setkeyword", set_keyword))
    application.add_handler(CommandHandler("trabaja", trabaja))

    # Monitor de texto
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, monitor))

    application.run_polling(drop_pending_updates=True)
