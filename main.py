import os
import random
import logging
import psycopg2
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

# --- BASE DE DATOS (Supabase / Postgres) ---

DATABASE_URL = os.environ.get("DATABASE_URL")

def _get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

def _init_db():
    """Crea la tabla si no existe."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id TEXT PRIMARY KEY,
            nombre TEXT NOT NULL,
            puntos INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

def _sumar_punto(user_id: str, nombre: str):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO stats (user_id, nombre, puntos)
        VALUES (%s, %s, 1)
        ON CONFLICT (user_id)
        DO UPDATE SET puntos = stats.puntos + 1, nombre = EXCLUDED.nombre;
    """, (user_id, nombre))
    conn.commit()
    cur.close()
    conn.close()

def _obtener_top(limite: int = 10):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT nombre, puntos FROM stats ORDER BY puntos DESC LIMIT %s;", (limite,))
    resultados = cur.fetchall()
    cur.close()
    conn.close()
    return resultados

def _reset_stats():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM stats;")
    conn.commit()
    cur.close()
    conn.close()

# --- CONFIGURACIÓN ---
ADMIN_ID = 7740467368  # tu ID de administrador

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
    ranking = _obtener_top(10)
    if not ranking:
        await update.message.reply_text(" No hay datos aún. ¡A trabajar!")
        return

    mensaje = "<b>TOP DE COMPTES</b>\n\n"
    for i, (nombre, puntos) in enumerate(ranking, 1):
        medalla = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        mensaje += f"{medalla} <b>{nombre}</b>: {puntos} veces\n"

    await update.message.reply_text(mensaje, parse_mode="HTML")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(" Solo el admin puede reiniciar el contador.")
        return
    _reset_stats()
    await update.message.reply_text(" Contador reiniciado a cero.")

async def set_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(" Solo el admin puede cambiar la palabra clave.")
        return

    if not context.args:
        await update.message.reply_text("Uso: `/setkeyword <nueva_palabra>`", parse_mode="Markdown")
        return

    nueva_palabra = context.args[0].lower()
    config["keyword"] = nueva_palabra
    await update.message.reply_text(f" Palabra de clave cambiada a: <b>{nueva_palabra}</b>", parse_mode="HTML")

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
        _sumar_punto(user_id, nombre)
        print(f"Registro: {nombre} dijo {config['keyword']}")

# --- MAIN ---
if __name__ == '__main__':
    token_bot = os.environ.get('TOKEN')
    if not token_bot:
        raise ValueError("No configuraste bien el TOKEN hijita, porfavor")
    if not DATABASE_URL:
        raise ValueError("No configuraste bien el DATABASE_URL hijita, porfavor")

    print("🗄️ Verificando base de datos...")
    _init_db()

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
