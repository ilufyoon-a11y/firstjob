import os
import re
import html
import time
import random
import asyncio
import logging
import psycopg2
import unicodedata
import json
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, Conflict
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

# --- SERVIDOR WEB - MANTENDRA DESPIERTO AL BOT ---

from flask import Flask
from threading import Thread

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Esta vivo, tu puedes amor"

def run_web():
    web_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

Thread(target=run_web, daemon=True).start()

# --- ZONA HORARIA (Ciudad de México, sin horario de verano desde 2022) ---
ADMIN_TZ = timezone(timedelta(hours=-6))
DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

# --- TIEMPOS DEL "¿AÚN ESTÁS AHÍ?" ---
AVISO_CADA = timedelta(minutes=5)        # antes: hours=2
ESPERA_RESPUESTA = timedelta(minutes=3)  # antes: hours=1
ESPERA_ADMIN = timedelta(minutes=5)      # antes: hours=6

# --- PALETA PASTEL PARA EL PDF ---
CREMA = colors.HexColor("#FAF6FC")
MAUVE = colors.HexColor("#B4A9D2")
MAUVE_OSCURO = colors.HexColor("#6C5B7B")
TEXTO_PDF = colors.HexColor("#4A3F5C")
PALETA_PERSONAS = [
    colors.HexColor("#F6EAF3"),  # rosa pastel
    colors.HexColor("#E4D6E9"),  # lila claro
    colors.HexColor("#D7CEDB"),  # gris lavanda
    colors.HexColor("#EFDAF6"),  # orquídea claro
]

# --- BASE DE DATOS (Supabase / Postgres) ---

DATABASE_URL = os.environ.get("DATABASE_URL")

def _get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

def _init_db():
    """Crea las tablas si no existen."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id TEXT PRIMARY KEY,
            nombre TEXT NOT NULL,
            username TEXT,
            puntos INTEGER NOT NULL DEFAULT 0
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sesiones_activas (
            user_id TEXT PRIMARY KEY,
            nombre TEXT NOT NULL,
            username TEXT,
            inicio TIMESTAMPTZ NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sesiones (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            nombre TEXT NOT NULL,
            username TEXT,
            fecha DATE NOT NULL,
            duracion_segundos INTEGER NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
    """)
    # Sesiones que el usuario no confirmó y esperan decisión del admin
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pendientes (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            nombre TEXT NOT NULL,
            username TEXT,
            fecha DATE NOT NULL,
            segundos_calculados INTEGER NOT NULL,
            creado TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    cur.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS username TEXT;")
    cur.execute("ALTER TABLE sesiones_activas ADD COLUMN IF NOT EXISTS username TEXT;")
    cur.execute("ALTER TABLE sesiones ADD COLUMN IF NOT EXISTS username TEXT;")
    cur.execute("ALTER TABLE sesiones_activas ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
    cur.execute("ALTER TABLE sesiones_activas ADD COLUMN IF NOT EXISTS chequeo_en TIMESTAMPTZ;")
    cur.execute("ALTER TABLE sesiones_activas ADD COLUMN IF NOT EXISTS pregunta_en TIMESTAMPTZ;")
    cur.execute("ALTER TABLE sesiones_activas ADD COLUMN IF NOT EXISTS pregunta_msg_id BIGINT;")
    conn.commit()
    cur.close()
    conn.close()

def _guardar_config(clave: str, valor: str):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO config (clave, valor) VALUES (%s, %s)
        ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor;
    """, (clave, valor))
    conn.commit()
    cur.close()
    conn.close()

def _cargar_config() -> dict:
    """Carga la config guardada en la base de datos (keyword, mes del último
    reset, etc). Si no hay nada (primera vez), usa los valores por defecto
    y los guarda para la próxima, así sobrevive a reinicios del bot."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT clave, valor FROM config;")
    filas = dict(cur.fetchall())
    cur.close()
    conn.close()

    ahora = datetime.now(ADMIN_TZ)
    resultado = {
        "keyword": filas.get("keyword", "compte"),
        "keyword_salida": filas.get("keyword_salida", "salgo"),
        "reset_mes": filas.get("reset_mes", str(ahora.month)),
        "reset_anio": filas.get("reset_anio", str(ahora.year)),
    }
    for clave in ("keyword", "keyword_salida", "reset_mes", "reset_anio"):
        if clave not in filas:
            _guardar_config(clave, resultado[clave])
    return resultado

def _sumar_punto(user_id: str, nombre: str, username: str = None):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO stats (user_id, nombre, username, puntos)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (user_id)
        DO UPDATE SET puntos = stats.puntos + 1, nombre = EXCLUDED.nombre, username = EXCLUDED.username;
    """, (user_id, nombre, username))
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
    cur.execute("DELETE FROM sesiones;")
    cur.execute("DELETE FROM sesiones_activas;")
    cur.execute("DELETE FROM pendientes;")
    conn.commit()
    cur.close()
    conn.close()

def _exportar_backup():
    """Saca una copia completa de 'sesiones' y 'stats' en un dict serializable
    a JSON, para poder restaurarlo después con _importar_backup si se resetea
    todo por accidente."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, nombre, username, fecha, duracion_segundos FROM sesiones ORDER BY id;")
    sesiones = [
        {
            "user_id": fila[0],
            "nombre": fila[1],
            "username": fila[2],
            "fecha": fila[3].isoformat(),
            "duracion_segundos": fila[4],
        }
        for fila in cur.fetchall()
    ]
    cur.execute("SELECT user_id, nombre, username, puntos FROM stats;")
    stats = [
        {"user_id": fila[0], "nombre": fila[1], "username": fila[2], "puntos": fila[3]}
        for fila in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return {"sesiones": sesiones, "stats": stats}

def _importar_backup(data: dict) -> tuple:
    """Restaura un backup generado por _exportar_backup. Es aditivo: no borra
    nada de lo que ya haya, solo agrega las sesiones y suma los puntos."""
    sesiones = data.get("sesiones", [])
    stats = data.get("stats", [])
    conn = _get_conn()
    cur = conn.cursor()
    for fila in sesiones:
        cur.execute("""
            INSERT INTO sesiones (user_id, nombre, username, fecha, duracion_segundos)
            VALUES (%s, %s, %s, %s, %s);
        """, (fila["user_id"], fila["nombre"], fila.get("username"), fila["fecha"], fila["duracion_segundos"]))
    for fila in stats:
        cur.execute("""
            INSERT INTO stats (user_id, nombre, username, puntos)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET puntos = stats.puntos + EXCLUDED.puntos, nombre = EXCLUDED.nombre, username = EXCLUDED.username;
        """, (fila["user_id"], fila["nombre"], fila.get("username"), fila["puntos"]))
    conn.commit()
    cur.close()
    conn.close()
    return len(sesiones), len(stats)

# --- SESIONES (una por usuario, identificadas por user_id) ---

def _iniciar_sesion(user_id: str, nombre: str, username: str, chat_id: int):
    """Abre la sesión de ESTE usuario. Cada persona tiene su propia fila
    (clave = user_id), así que varias personas pueden activar a la vez sin
    pisarse. Si el usuario ya tenía una sesión activa, NO se reinicia su
    cronómetro (no se pierde tiempo ni se duplica el punto).
    Devuelve (fue_nueva, inicio)."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sesiones_activas (user_id, nombre, username, inicio, chat_id, chequeo_en)
        VALUES (%s, %s, %s, NOW(), %s, NOW() + %s)
        ON CONFLICT (user_id) DO NOTHING
        RETURNING inicio;
    """, (user_id, nombre, username, chat_id, AVISO_CADA))
    fila = cur.fetchone()
    if fila:
        fue_nueva, inicio = True, fila[0]
    else:
        cur.execute("""
            UPDATE sesiones_activas
            SET nombre = %s, username = %s,
                chat_id = COALESCE(chat_id, %s),
                chequeo_en = COALESCE(chequeo_en, inicio + %s)
            WHERE user_id = %s
            RETURNING inicio;
        """, (nombre, username, chat_id, AVISO_CADA, user_id))
        fue_nueva, inicio = False, cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return fue_nueva, inicio

def _cerrar_sesion(user_id: str, nombre: str, username: str = None):
    """Cierra la sesión activa de este usuario (si existe) y guarda la duración
    en el historial. Devuelve segundos o None si no había sesión activa."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM sesiones_activas WHERE user_id = %s RETURNING inicio;", (user_id,))
    fila = cur.fetchone()
    if not fila:
        conn.commit()
        cur.close()
        conn.close()
        return None

    inicio = fila[0]
    ahora = datetime.now(timezone.utc)
    duracion_segundos = int((ahora - inicio).total_seconds())
    fecha_local = inicio.astimezone(ADMIN_TZ).date()

    cur.execute("""
        INSERT INTO sesiones (user_id, nombre, username, fecha, duracion_segundos)
        VALUES (%s, %s, %s, %s, %s);
    """, (user_id, nombre, username, fecha_local, duracion_segundos))
    conn.commit()
    cur.close()
    conn.close()
    return duracion_segundos

# --- CHEQUEO "¿AÚN ESTÁS AHÍ?" ---

def _sesiones_por_preguntar():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, nombre, username, chat_id FROM sesiones_activas
        WHERE chat_id IS NOT NULL AND pregunta_en IS NULL AND chequeo_en <= NOW();
    """)
    filas = cur.fetchall()
    cur.close()
    conn.close()
    return filas

def _marcar_pregunta(user_id: str, msg_id):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE sesiones_activas SET pregunta_en = NOW(), pregunta_msg_id = %s
        WHERE user_id = %s;
    """, (msg_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def _confirmar_presencia(user_id: str):
    """El usuario dijo 'sí sigo aquí': el cronómetro sigue y se vuelve a
    preguntar en 2 horas. Devuelve el nombre, o None si ya no había pregunta activa."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE sesiones_activas
        SET chequeo_en = NOW() + %s, pregunta_en = NULL, pregunta_msg_id = NULL
        WHERE user_id = %s AND pregunta_en IS NOT NULL
        RETURNING nombre;
    """, (AVISO_CADA, user_id))
    fila = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return fila[0] if fila else None

def _pasar_vencidas_a_pendiente():
    """Las sesiones cuya pregunta lleva más de 1 hora sin respuesta se sacan de
    'activas' y pasan a 'pendientes'. El tiempo calculado es el que hubo hasta
    que se envió la pregunta (o sea, ya sin la hora de espera)."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id FROM sesiones_activas
        WHERE pregunta_en IS NOT NULL AND pregunta_en + %s <= NOW();
    """, (ESPERA_RESPUESTA,))
    ids = [f[0] for f in cur.fetchall()]
    vencidas = []
    for uid in ids:
        cur.execute("""
            DELETE FROM sesiones_activas WHERE user_id = %s
            RETURNING nombre, username, inicio, pregunta_en, chat_id, pregunta_msg_id;
        """, (uid,))
        fila = cur.fetchone()
        if not fila:
            continue
        nombre, username, inicio, pregunta_en, chat_id, msg_id = fila
        segundos = max(0, int((pregunta_en - inicio).total_seconds()))
        fecha = inicio.astimezone(ADMIN_TZ).date()
        cur.execute("""
            INSERT INTO pendientes (user_id, nombre, username, fecha, segundos_calculados)
            VALUES (%s, %s, %s, %s, %s) RETURNING id;
        """, (uid, nombre, username, fecha, segundos))
        pid = cur.fetchone()[0]
        vencidas.append({
            "pid": pid, "user_id": uid, "nombre": nombre, "username": username,
            "segundos": segundos, "chat_id": chat_id, "msg_id": msg_id,
        })
    conn.commit()
    cur.close()
    conn.close()
    return vencidas

def _finalizar_pendiente(pid: int, segundos: int = None):
    """Guarda una sesión pendiente en el historial. Si segundos es None se usa
    el tiempo calculado. Devuelve (nombre, segundos) o None si ya estaba resuelta."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM pendientes WHERE id = %s
        RETURNING user_id, nombre, username, fecha, segundos_calculados;
    """, (pid,))
    fila = cur.fetchone()
    if not fila:
        conn.commit()
        cur.close()
        conn.close()
        return None
    user_id, nombre, username, fecha, calculados = fila
    final = calculados if segundos is None else max(0, int(segundos))
    cur.execute("""
        INSERT INTO sesiones (user_id, nombre, username, fecha, duracion_segundos)
        VALUES (%s, %s, %s, %s, %s);
    """, (user_id, nombre, username, fecha, final))
    conn.commit()
    cur.close()
    conn.close()
    return _etiqueta_texto(nombre, username), final

def _existe_pendiente(pid: int) -> bool:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pendientes WHERE id = %s;", (pid,))
    existe = cur.fetchone() is not None
    cur.close()
    conn.close()
    return existe

def _autoguardar_pendientes_viejos():
    """Si ningún admin decidió a tiempo, se guarda el tiempo calculado."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM pendientes WHERE creado + %s <= NOW();", (ESPERA_ADMIN,))
    ids = [f[0] for f in cur.fetchall()]
    cur.close()
    conn.close()
    guardados = []
    for pid in ids:
        res = _finalizar_pendiente(pid, None)
        if res:
            guardados.append(res)
    return guardados

# --- OTRAS CONSULTAS ---

def _resolver_user_id_por_username(username: str):
    """Busca el user_id más reciente asociado a un @username (sin la @)."""
    username = username.lstrip("@").lower()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id FROM sesiones WHERE LOWER(username) = %s
        UNION
        SELECT user_id FROM stats WHERE LOWER(username) = %s
        LIMIT 1;
    """, (username, username))
    fila = cur.fetchone()
    cur.close()
    conn.close()
    return fila[0] if fila else None

def _obtener_historial_periodo_actual():
    """Trae todo el historial acumulado desde el último /reset (como /reset
    borra la tabla 'sesiones', todo lo que queda aquí pertenece al periodo
    actual: desde ese reset hasta ahora)."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT fecha, nombre, MAX(username) AS username, SUM(duracion_segundos) AS total
        FROM sesiones
        GROUP BY fecha, nombre
        ORDER BY fecha ASC, total DESC;
    """)
    resultados = cur.fetchall()
    cur.close()
    conn.close()
    return resultados


def _obtener_historial(user_id: str, dias: int = 14):
    """Historial de los últimos N días para un usuario específico."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT fecha, SUM(duracion_segundos) AS total, MAX(nombre) AS nombre
        FROM sesiones
        WHERE user_id = %s AND fecha >= (CURRENT_DATE - %s * INTERVAL '1 day')
        GROUP BY fecha
        ORDER BY fecha DESC;
    """, (user_id, dias))
    resultados = cur.fetchall()
    cur.close()
    conn.close()
    return resultados

def _sanitizar_texto_pdf(texto: str) -> str:
    """Convierte caracteres unicode 'decorados' (como los estilos matemáticos
    tipo 𝖬𝗎𝖾𝗌𝗍𝗋𝖺) a su letra normal, y descarta lo que no se pueda dibujar
    con las fuentes base de reportlab (emojis, símbolos raros), para
    evitar los cuadraditos."""
    if not texto:
        return texto
    normalizado = unicodedata.normalize('NFKC', texto)
    return normalizado.encode('latin-1', 'ignore').decode('latin-1')

def _formatear_duracion(segundos: int) -> str:
    horas = segundos // 3600
    minutos = (segundos % 3600) // 60
    if horas and minutos:
        return f"{horas}h {minutos}min"
    if horas:
        return f"{horas}h"
    return f"{minutos}min"

def _es_palabra_sola(palabra: str, texto: str) -> bool:
    """True solo si el mensaje ES la palabra. Se ignoran espacios, signos y
    emojis alrededor ('compte', 'Compte!', '¡compte! 🫡' valen), pero
    'no salgo hoy' o 'compte gracias' NO valen."""
    return re.fullmatch(rf"\W*{re.escape(palabra)}\W*", texto.strip(), re.IGNORECASE) is not None

def _registrar_entrada(user_id: str, nombre: str, username: str, chat_id: int):
    """Abre la sesión y suma el punto (solo si la sesión es nueva). Se agrupa
    en una función para ejecutarla fuera del hilo principal del bot."""
    fue_nueva, inicio = _iniciar_sesion(user_id, nombre, username, chat_id)
    if fue_nueva:
        _sumar_punto(user_id, nombre, username)
    return fue_nueva, inicio

def _esc(texto) -> str:
    return html.escape(str(texto or ""))

def _etiqueta_texto(nombre, username) -> str:
    """Cómo nombrar a alguien en texto plano: @usuario si tiene, si no su nombre."""
    return f"@{username}" if username else (nombre or "alguien")

def _mencion_html(user_id, nombre, username) -> str:
    """Cómo mencionar a alguien en un mensaje HTML. Con @usuario le llega la
    notificación; si no tiene, se usa un enlace con su nombre (también notifica)."""
    if username:
        return f"@{_esc(username)}"
    return f'<a href="tg://user?id={user_id}">{_esc(nombre or "alguien")}</a>'

# --- CONFIGURACIÓN ---
ADMIN_IDS = (6905064136,)
config = {"keyword": "compte", "keyword_salida": "salgo", "reset_mes": None, "reset_anio": None}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
# httpx imprime cada URL de Telegram, y esa URL lleva el token del bot. Lo silenciamos.
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- COMANDOS ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu = (
        "<b>Manual de Operaciones (Comandos)</b>\n\n"
        "<code>/top</code> → Muestra el top de actividad.\n"
        "<code>/bitacora [@usuario]</code> → Tu historial de los últimos 14 días (el admin puede ver el de otra persona).\n"
        "<code>/reporte</code> → PDF del periodo actual (admin).\n"
        "<code>/reset</code> → Reinicia el contador; antes te manda el backup por DM (admin).\n"
        "<code>/export</code> → Te manda un backup por DM sin resetear (admin).\n"
        "<code>/restore</code> → Respondiendo a un backup, lo restaura (admin).\n"
        "<code>/setkeyword &lt;palabra&gt;</code> → Cambia la palabra de entrada (admin).\n"
        "<code>/definir &lt;id&gt; &lt;segundos&gt;</code> → Define a mano el tiempo de una sesión pendiente (admin).\n"
        "<code>/trabaja [@usuario]</code> → Mensaje de sobreexplotación creativa.\n"
        "<code>/help</code> → Muestra este mensaje."
    )
    await update.message.reply_text(menu, parse_mode="HTML")

async def show_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ranking = _obtener_top(10)
    if not ranking:
        await update.message.reply_text(" No hay datos aún. ¡A trabajar!")
        return

    mensaje = "<b>TOP DE COMPTES</b>\n\n"
    for i, (nombre, puntos) in enumerate(ranking, 1):
        medalla = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        mensaje += f"{medalla} <b>{_esc(nombre)}</b>: {puntos} veces\n"

    await update.message.reply_text(mensaje, parse_mode="HTML")

async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    solicitante_id = update.effective_user.id

    if context.args:
        if solicitante_id not in ADMIN_IDS:
            await update.message.reply_text(" Solo el admin puede ver el historial de otra persona.")
            return
        objetivo_id = _resolver_user_id_por_username(context.args[0])
        if not objetivo_id:
            await update.message.reply_text(
                " Aun no hay actividad registrada por parte de esta(e) admin..."
            )
            return
    else:
        objetivo_id = str(solicitante_id)

    filas = _obtener_historial(objetivo_id, 14)
    if not filas:
        await update.message.reply_text(" Aun no hay actividad registrada por parte de esta(e) admin...")
        return

    nombre_mostrado = filas[0][2]
    mensaje = f" <b>Historial de {_esc(nombre_mostrado)}</b>\n\n"
    for fecha, total_segundos, _nombre in filas:
        dia_semana = DIAS_ES[fecha.weekday()]
        mensaje += f"─ {dia_semana} {fecha.strftime('%d/%m')} → {_formatear_duracion(int(total_segundos))}\n"

    await update.message.reply_text(mensaje, parse_mode="HTML")

# --- GENERACIÓN DEL PDF (diseño pastel) ---

def _color_persona(nombre, nombres_ordenados):
    idx = nombres_ordenados.index(nombre) % len(PALETA_PERSONAS)
    return PALETA_PERSONAS[idx]

def _pill(col1, col2, color_fondo, color_texto, negrita=False, ancho1=3.3, ancho2=2.2):
    fuente = "Helvetica-Bold" if negrita else "Helvetica"
    t = Table([[col1, col2]], colWidths=[ancho1 * inch, ancho2 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color_fondo),
        ("ROUNDEDCORNERS", [10, 10, 10, 10]),
        ("TEXTCOLOR", (0, 0), (-1, -1), color_texto),
        ("FONTNAME", (0, 0), (-1, -1), fuente),
        ("FONTSIZE", (0, 0), (-1, -1), 10.5),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("RIGHTPADDING", (1, 0), (1, 0), 14),
    ]))
    return t

def _pie_de_pagina(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(CREMA)
    canvas.rect(0, 0, letter[0], letter[1], fill=1, stroke=0)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MAUVE)
    generado = datetime.now(ADMIN_TZ).strftime("%d/%m/%Y %H:%M")
    canvas.drawString(0.75 * inch, 0.45 * inch, f"Generado el {generado}")
    canvas.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"Página {doc.page}")
    canvas.restoreState()

def _generar_pdf_general(filas, mes_titulo: str = None) -> str:
    """Genera el PDF del reporte del periodo actual (desde el último /reset)
    con diseño pastel, y devuelve la ruta del archivo temporal."""
    ruta = f"/tmp/reporte_{int(datetime.now().timestamp())}.pdf"

    por_dia = {}
    orden_dias = []
    totales_persona = {}
    etiquetas_persona = {}
    for fecha, nombre, username, total_segundos in filas:
        nombre = _sanitizar_texto_pdf(nombre)
        etiqueta = f"@{_sanitizar_texto_pdf(username)}" if username else nombre
        # Agrupamos por username (estable) y no por nombre (cambia seguido),
        # para que la misma persona no salga repetida en el resumen.
        clave = username.lower() if username else nombre
        if fecha not in por_dia:
            por_dia[fecha] = []
            orden_dias.append(fecha)
        por_dia[fecha].append((clave, etiqueta, int(total_segundos)))
        totales_persona[clave] = totales_persona.get(clave, 0) + int(total_segundos)
        etiquetas_persona[clave] = etiqueta

    nombres_ordenados = sorted(totales_persona.keys())

    doc = SimpleDocTemplate(
        ruta, pagesize=letter,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch
    )
    estilos = getSampleStyleSheet()

    estilo_titulo = ParagraphStyle("Titulo", parent=estilos["Title"],
                                    fontName="Times-Bold", textColor=MAUVE_OSCURO,
                                    fontSize=28, spaceAfter=0, alignment=TA_LEFT)
    estilo_subtitulo = ParagraphStyle("Subtitulo", parent=estilos["Normal"],
                                       fontName="Times-Italic", textColor=MAUVE,
                                       fontSize=14, spaceAfter=0)
    estilo_seccion = ParagraphStyle("Seccion", parent=estilos["Normal"],
                                     fontName="Helvetica-Bold", textColor=MAUVE_OSCURO,
                                     fontSize=12, spaceBefore=6, spaceAfter=10)

    elementos = [
        Paragraph("Reporte de Actividad", estilo_titulo),
    ]
    if mes_titulo:
        elementos.append(Paragraph(mes_titulo, estilo_subtitulo))
    elementos.append(Spacer(1, 20))

    # --- RESUMEN DEL MES ---
    elementos.append(Paragraph("RESUMEN DEL PERIODO", estilo_seccion))
    resumen_ordenado = sorted(totales_persona.items(), key=lambda x: x[1], reverse=True)
    for clave, seg in resumen_ordenado:
        color_fondo = _color_persona(clave, nombres_ordenados)
        elementos.append(_pill(etiquetas_persona[clave], _formatear_duracion(seg), color_fondo, TEXTO_PDF, negrita=True))
        elementos.append(Spacer(1, 6))

    total_general = sum(totales_persona.values())
    elementos.append(Spacer(1, 6))
    elementos.append(_pill("Total general", _formatear_duracion(total_general), MAUVE, colors.white, negrita=True))
    elementos.append(Spacer(1, 28))

    # --- DETALLE POR DÍA ---
    elementos.append(Paragraph("DETALLE POR DÍA", estilo_seccion))
    for fecha in orden_dias:
        dia_semana = DIAS_ES[fecha.weekday()]
        encabezado = Table([[f"{dia_semana} {fecha.strftime('%d/%m/%Y')}"]], colWidths=[5.5 * inch])
        encabezado.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), MAUVE_OSCURO),
            ("ROUNDEDCORNERS", [10, 10, 10, 10]),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10.5),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ]))
        elementos.append(encabezado)
        elementos.append(Spacer(1, 5))

        total_dia = 0
        for clave, etiqueta, seg in por_dia[fecha]:
            color_fondo = _color_persona(clave, nombres_ordenados)
            elementos.append(_pill(etiqueta, _formatear_duracion(seg), color_fondo, TEXTO_PDF))
            elementos.append(Spacer(1, 4))
            total_dia += seg

        elementos.append(_pill("Total del día", _formatear_duracion(total_dia), CREMA, MAUVE_OSCURO, negrita=True))
        elementos.append(Spacer(1, 18))

    doc.build(elementos, onFirstPage=_pie_de_pagina, onLaterPages=_pie_de_pagina)
    return ruta

async def general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede ver el reporte general.")
        return

    filas = _obtener_historial_periodo_actual()
    if not filas:
        await update.message.reply_text(" No hay actividad registrada desde el último reset.")
        return

    await update.message.reply_text("Generando el reporte del periodo actual, esto tardará unos segundos...")

    mes_periodo = MESES_ES[int(config["reset_mes"])]
    ruta_pdf = _generar_pdf_general(filas, mes_periodo)

    try:
        with open(ruta_pdf, "rb") as archivo:
            await update.message.reply_document(
                document=archivo,
                filename=f"reporte_{mes_periodo.lower()}.pdf",
                caption=f" Reporte de actividad — {mes_periodo}"
            )
    finally:
        if os.path.exists(ruta_pdf):
            os.remove(ruta_pdf)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede reiniciar el contador.")
        return

    data = _exportar_backup()
    if data["sesiones"] or data["stats"]:
        ruta_json = f"/tmp/backup_{int(datetime.now().timestamp())}.json"
        with open(ruta_json, "w", encoding="utf-8") as archivo:
            json.dump(data, archivo, ensure_ascii=False, indent=2)
        try:
            with open(ruta_json, "rb") as archivo:
                await context.bot.send_document(
                    chat_id=update.effective_user.id,
                    document=archivo,
                    filename="respaldo.json",
                    caption=(
                        "Si reseteaste los datos sin querer, responde a este archivo con /restore para recuperarlos."
                    )
                )
        except Exception:
            await update.message.reply_text(
                " No pude mandarte el archivo de respaldo por privado. Inicia primero al bot y vuelve a intentar /reset."
            )
            if os.path.exists(ruta_json):
                os.remove(ruta_json)
            return
        finally:
            if os.path.exists(ruta_json):
                os.remove(ruta_json)

    _reset_stats()

    ahora = datetime.now(ADMIN_TZ)
    config["reset_mes"] = str(ahora.month)
    config["reset_anio"] = str(ahora.year)
    _guardar_config("reset_mes", config["reset_mes"])
    _guardar_config("reset_anio", config["reset_anio"])

    await update.message.reply_text(" Contador y registros reiniciados a cero.")

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede generar un archivo de respaldo.")
        return

    data = _exportar_backup()
    if not data["sesiones"] and not data["stats"]:
        await update.message.reply_text(" No hay datos guardados todavía para respaldar.")
        return

    ruta_json = f"/tmp/backup_{int(datetime.now().timestamp())}.json"
    with open(ruta_json, "w", encoding="utf-8") as archivo:
        json.dump(data, archivo, ensure_ascii=False, indent=2)
    try:
        with open(ruta_json, "rb") as archivo:
            await context.bot.send_document(
                chat_id=update.effective_user.id,
                document=archivo,
                filename="respaldo.json",
                caption=" Respaldo generado con exito. Responde a este archivo con /restore para restaurarlo cuando quieras."
            )
    except Exception:
        await update.message.reply_text(
            " No se pudo enviar el backup por privado. Inicia primero al bot y vuelve a intentar /export."
        )
        return
    finally:
        if os.path.exists(ruta_json):
            os.remove(ruta_json)

async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede restaurar la base de datos.")
        return

    documento = update.message.document
    if not documento and update.message.reply_to_message:
        documento = update.message.reply_to_message.document

    if not documento:
        await update.message.reply_text(
            " Adjunta el archivo backup.json (o responde a el) usando /restore."
        )
        return

    try:
        archivo_tg = await context.bot.get_file(documento.file_id)
        contenido = await archivo_tg.download_as_bytearray()
        data = json.loads(bytes(contenido).decode("utf-8"))
    except Exception:
        await update.message.reply_text(
            " Este archivo debe ser un .json."
        )
        return

    total_sesiones, total_stats = _importar_backup(data)
    await update.message.reply_text(
        f"Base de datos restaurada: {total_sesiones} sesiones y {total_stats} registros de puntos recuperados."
    )

async def set_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede cambiar la palabra clave.")
        return

    if not context.args:
        await update.message.reply_text("Uso: <code>/setkeyword &lt;nueva_palabra&gt;</code>", parse_mode="HTML")
        return

    nueva_palabra = context.args[0].lower()
    config["keyword"] = nueva_palabra
    _guardar_config("keyword", nueva_palabra)
    await update.message.reply_text(f" Palabra de clave cambiada a: <b>{_esc(nueva_palabra)}</b>", parse_mode="HTML")

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

async def definir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/definir <id> <segundos>: fija a mano el tiempo de una sesión pendiente."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede definir el tiempo.")
        return
    if len(context.args) != 2 or not all(a.isdigit() for a in context.args):
        await update.message.reply_text("Uso: <code>/definir &lt;id&gt; &lt;segundos&gt;</code>", parse_mode="HTML")
        return
    res = await asyncio.to_thread(_finalizar_pendiente, int(context.args[0]), int(context.args[1]))
    if not res:
        await update.message.reply_text(" Ese pendiente no existe o ya fue resuelto.")
        return
    nombre, seg = res
    await update.message.reply_text(f" Guardé {_formatear_duracion(seg)} ({seg} s) para {nombre}.")

# --- MONITOR ---

async def _avisar_admins(context, mensaje: str, html_mode: bool = False, reply_markup=None):
    """Manda un aviso por DM a todos los admins. Si alguno nunca le escribió
    al bot en privado, ese envío falla en silencio (no hay forma de evitarlo)."""
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=mensaje,
                parse_mode="HTML" if html_mode else None,
                reply_markup=reply_markup
            )
        except Exception:
            pass

async def monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    texto = update.message.text.lower()
    user_id = str(update.effective_user.id)
    nombre = update.effective_user.first_name
    username = update.effective_user.username
    chat_id = update.effective_chat.id

    # Un admin que pulsó "Definir segundos" responde aquí con el número
    pid = context.user_data.get("definiendo")
    if pid and update.effective_chat.type == "private" and texto.strip().isdigit():
        context.user_data.pop("definiendo", None)
        res = await asyncio.to_thread(_finalizar_pendiente, pid, int(texto.strip()))
        if res:
            n, seg = res
            await update.message.reply_text(f" Guardé {_formatear_duracion(seg)} ({seg} s) para {n}.")
        else:
            await update.message.reply_text(" Ese pendiente ya fue resuelto.")
        return

    if _es_palabra_sola(config["keyword"], texto):
        try:
            fue_nueva, inicio = await asyncio.to_thread(
                _registrar_entrada, user_id, nombre, username, chat_id
            )
        except Exception as error:
            logging.exception("Fallo guardando el registro de entrada")
            await _avisar_admins(
                context,
                f"No se pudo registrar la entrada de {_etiqueta_texto(nombre, username)} (falló la base de datos): {error}"
            )
            return
        if fue_nueva:
            print(f"Registro: {nombre} dijo {config['keyword']}")
        else:
            llevas = int((datetime.now(timezone.utc) - inicio).total_seconds())
            await update.message.reply_text(
                f"<b>{_esc(nombre)}</b>, ya tienes una sesión activa (llevas {_formatear_duracion(llevas)}). Sigo contando 😉",
                parse_mode="HTML"
            )

    elif _es_palabra_sola(config["keyword_salida"], texto):
        try:
            segundos = await asyncio.to_thread(_cerrar_sesion, user_id, nombre, username)
        except Exception as error:
            logging.exception("Fallo guardando el registro de salida")
            await _avisar_admins(
                context,
                f"No se pudo registrar la salida de {_etiqueta_texto(nombre, username)} (falló la base de datos): {error}"
            )
            return
        if segundos is not None:
            await update.message.reply_text(
                f"<b>{_esc(nombre)}</b>, se ha registrado con éxito los {_formatear_duracion(segundos)} que estuviste activa(o)",
                parse_mode="HTML"
            )
            print(f"Salida: {nombre} estuvo activo {_formatear_duracion(segundos)}")

# --- BOTONES: "¿AÚN ESTÁS AHÍ?" ---

async def confirmar_presencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.data.split(":", 1)[1]
    if str(q.from_user.id) != uid:
        await q.answer("Este botón es solo para quien activó el compte 🙂", show_alert=True)
        return
    await q.answer()
    nombre = await asyncio.to_thread(_confirmar_presencia, uid)
    if nombre:
        await q.edit_message_text(
            f"✅ Perfecto, <b>{_esc(nombre)}</b>. Sigo contando tu tiempo y te vuelvo a preguntar en 2 horas.",
            parse_mode="HTML"
        )
    else:
        await q.edit_message_text(
            "Esa sesión ya no está activa (se agotó el tiempo de espera o ya registraste tu salida)."
        )

async def decision_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Solo los admins pueden decidir esto.", show_alert=True)
        return
    await q.answer()
    accion, pid = q.data.split(":")
    pid = int(pid)

    if accion == "pok":
        res = await asyncio.to_thread(_finalizar_pendiente, pid, None)
        if not res:
            await q.edit_message_text("Este pendiente ya fue resuelto.")
            return
        nombre, seg = res
        await q.edit_message_text(
            f"✅ Guardé {_formatear_duracion(seg)} para <b>{_esc(nombre)}</b>.", parse_mode="HTML"
        )
    else:  # pdef
        if not await asyncio.to_thread(_existe_pendiente, pid):
            await q.edit_message_text("Este pendiente ya fue resuelto.")
            return
        context.user_data["definiendo"] = pid
        await q.edit_message_text(
            f"✍️ Respóndeme aquí con los <b>segundos</b> que estuvo activa(o) (solo el número, ej. 5400 = 1h 30min).\n"
            f"También puedes usar <code>/definir {pid} &lt;segundos&gt;</code>.",
            parse_mode="HTML"
        )

# --- VIGILANTE (corre cada minuto) ---

async def _revisar_sesiones(bot):
    # 1) Preguntar "¿Aún estás ahí?" a quien ya cumplió 2 horas
    for user_id, nombre, username, chat_id in await asyncio.to_thread(_sesiones_por_preguntar):
        msg_id = None
        try:
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("Sí, sigo aquí", callback_data=f"aqui:{user_id}")
            ]])
            m = await bot.send_message(
                chat_id=chat_id,
                text=(
                    f'{_mencion_html(user_id, nombre, username)}, ¿aún estás ahí? 👀\n'
                    f"Pulsa el botón en la próxima hora o descontaré ese tiempo de espera."
                ),
                parse_mode="HTML",
                reply_markup=teclado
            )
            msg_id = m.message_id
        except Exception:
            logging.exception("No pude enviar el '¿Aún estás ahí?'")
        await asyncio.to_thread(_marcar_pregunta, user_id, msg_id)

    # 2) Pasar a "pendiente" las que llevan 1 hora sin respuesta y avisar a los admins
    for v in await asyncio.to_thread(_pasar_vencidas_a_pendiente):
        etiqueta = _etiqueta_texto(v["nombre"], v["username"])
        if v["chat_id"] and v["msg_id"]:
            try:
                await bot.edit_message_text(
                    chat_id=v["chat_id"], message_id=v["msg_id"],
                    text=f"⏰ {etiqueta} no respondió. Se descontó la hora de espera."
                )
            except Exception:
                pass
        teclado = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Guardar {_formatear_duracion(v['segundos'])}", callback_data=f"pok:{v['pid']}"),
            InlineKeyboardButton("✍️ Definir segundos", callback_data=f"pdef:{v['pid']}"),
        ]])
        sin_user = "" if v["username"] else f" (sin @usuario, ID <code>{v['user_id']}</code>)"
        aviso = (
            f"⚠️ <b>{_esc(etiqueta)}</b>{sin_user} no respondió al «¿Aún estás ahí?» en 1 hora.\n"
            f"Descontando la hora de espera, estuvo activa(o) <b>{_formatear_duracion(v['segundos'])}</b>.\n\n"
            f"¿Qué hago? Si nadie decide en 6 horas, se guarda ese tiempo automáticamente.\n"
            f"ID del pendiente: <code>{v['pid']}</code>"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=aviso, parse_mode="HTML", reply_markup=teclado)
            except Exception:
                pass

    # 3) Si ningún admin decidió a tiempo, se guarda el tiempo calculado
    for nombre, seg in await asyncio.to_thread(_autoguardar_pendientes_viejos):
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"Nadie decidió a tiempo: guardé automáticamente {_formatear_duracion(seg)} para {nombre}."
                )
            except Exception:
                pass

async def _vigilante(app):
    while True:
        try:
            await _revisar_sesiones(app.bot)
        except Exception:
            logging.exception("Fallo en el vigilante de sesiones")
        await asyncio.sleep(60)

async def post_init(app):
    app.bot_data["vigilante"] = asyncio.create_task(_vigilante(app))

# --- MANEJADOR GLOBAL DE ERRORES ---

_ultimo_aviso_error = 0.0

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Registra cualquier error que ocurra dentro de un handler y avisa a los
    admins por DM (máximo un aviso cada 5 minutos para no llenarte de mensajes)."""
    global _ultimo_aviso_error
    error = context.error
    logging.error("Error en un handler", exc_info=error)

    # Los fallos de red pasajeros se reintentan solos; no vale la pena alarmar.
    # (Además, su texto puede traer la URL con el token del bot.)
    if isinstance(error, NetworkError):
        return

    ahora = time.monotonic()
    if ahora - _ultimo_aviso_error < 300:
        return
    _ultimo_aviso_error = ahora

    detalle = f"{type(error).__name__}: {error}"
    detalle = re.sub(r"bot\d+:[\w-]+", "bot***", detalle)[:500]
    await _avisar_admins(context, f"⚠️ Error en el bot de asistencia:\n{detalle}")

# --- MAIN ---
if __name__ == '__main__':
    token_bot = os.environ.get('TOKEN')
    if not token_bot:
        raise ValueError("No configuraste bien el TOKEN hijita, porfavor")
    if not DATABASE_URL:
        raise ValueError("No configuraste bien el DATABASE_URL hijita, porfavor")

    print("🗄️ Verificando base de datos...")
    _init_db()
    config.update(_cargar_config())

    print("🤖 Iniciando bot de Telegram con run_polling...")
    application = ApplicationBuilder().token(token_bot).post_init(post_init).build()

    # Registro de Handlers
    application.add_handler(CommandHandler("inicio", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("top", show_top))
    application.add_handler(CommandHandler("bitacora", historial))
    application.add_handler(CommandHandler("reporte", general))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("restore", restore))
    application.add_handler(CommandHandler("setkeyword", set_keyword))
    application.add_handler(CommandHandler("trabaja", trabaja))
    application.add_handler(CommandHandler("definir", definir))

    # /restore escrito como pie de foto al adjuntar el archivo
    application.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/restore"), restore))

    # Botones
    application.add_handler(CallbackQueryHandler(confirmar_presencia, pattern=r"^aqui:"))
    application.add_handler(CallbackQueryHandler(decision_admin, pattern=r"^(pok|pdef):\d+$"))

    # Monitor de texto
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, monitor))

    # Errores
    application.add_error_handler(error_handler)

    application.run_polling(drop_pending_updates=True)
