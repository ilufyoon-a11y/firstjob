import os
import random
import logging
import psycopg2
import unicodedata
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
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

Thread(target=run_web).start()

# --- ZONA HORARIA (Ciudad de México, sin horario de verano desde 2022) ---
ADMIN_TZ = timezone(timedelta(hours=-6))
DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

# --- PALETA PASTEL PARA EL PDF ---
CREMA = colors.HexColor("#FFFBF5")
MAUVE = colors.HexColor("#B9808F")
MAUVE_OSCURO = colors.HexColor("#8C5C6B")
TEXTO_PDF = colors.HexColor("#5A4048")
PALETA_PERSONAS = [
    colors.HexColor("#F5D6BA"),  # durazno
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
    cur.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS username TEXT;")
    cur.execute("ALTER TABLE sesiones_activas ADD COLUMN IF NOT EXISTS username TEXT;")
    cur.execute("ALTER TABLE sesiones ADD COLUMN IF NOT EXISTS username TEXT;")
    conn.commit()
    cur.close()
    conn.close()

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
    conn.commit()
    cur.close()
    conn.close()

def _iniciar_sesion(user_id: str, nombre: str, username: str = None) -> bool:
    """Guarda la hora de inicio. Si ya hay una sesión activa, la deja tal cual (no la reinicia)."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sesiones_activas (user_id, nombre, username, inicio)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id) DO NOTHING
        RETURNING user_id;
    """, (user_id, nombre, username))
    fue_nueva = cur.fetchone() is not None
    conn.commit()
    cur.close()
    conn.close()
    return fue_nueva

def _cerrar_sesion(user_id: str, nombre: str, username: str = None):
    """Cierra la sesión activa (si existe) y guarda la duración en el historial. Devuelve segundos o None si no había sesión activa."""
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

def _obtener_historial_mes(anio: int, mes: int):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT fecha, nombre, MAX(username) AS username, SUM(duracion_segundos) AS total
        FROM sesiones
        WHERE EXTRACT(YEAR FROM fecha) = %s AND EXTRACT(MONTH FROM fecha) = %s
        GROUP BY fecha, nombre
        ORDER BY fecha ASC, total DESC;
    """, (anio, mes))
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

# --- CONFIGURACIÓN ---
ADMIN_IDS = (7740467368, 6905064136)
config = {"keyword": "compte", "keyword_salida": "salgo"}

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
    mensaje = f" <b>Historial de {nombre_mostrado}</b>\n\n"
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

def _generar_pdf_general(filas, anio: int, mes: int) -> str:
    """Genera el PDF del reporte del mes (diseño pastel) y devuelve la ruta del archivo temporal."""
    ruta = f"/tmp/reporte_{anio}_{mes:02d}_{int(datetime.now().timestamp())}.pdf"

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
        Spacer(1, 20),
    ]

    # --- RESUMEN DEL MES ---
    elementos.append(Paragraph("RESUMEN DEL MES", estilo_seccion))
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

    ahora = datetime.now(ADMIN_TZ)
    anio, mes = ahora.year, ahora.month

    if context.args:
        try:
            if len(context.args) >= 2:
                mes = int(context.args[0])
                anio = int(context.args[1])
            else:
                mes = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Uso: <code>/reporte</code> (mes actual)", parse_mode="HTML")
            return

    filas = _obtener_historial_mes(anio, mes)
    if not filas:
        await update.message.reply_text(f" No hay actividad registrada de este mes en la base de datos.")
        return

    await update.message.reply_text(f"Generando el reporte de {MESES_ES[mes]}, esto tardará unos segundos...")
    ruta_pdf = _generar_pdf_general(filas, anio, mes)

    try:
        with open(ruta_pdf, "rb") as archivo:
            await update.message.reply_document(
                document=archivo,
                filename=f"reporte_{MESES_ES[mes].lower()}.pdf",
                caption=f" Reporte de actividad — {MESES_ES[mes]}"
            )
    finally:
        if os.path.exists(ruta_pdf):
            os.remove(ruta_pdf)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede reiniciar el contador.")
        return
    _reset_stats()
    await update.message.reply_text(" Contador reiniciado a cero.")

async def set_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" Solo el admin puede cambiar la palabra clave.")
        return

    if not context.args:
        await update.message.reply_text("Uso: <code>/setkeyword &lt;nueva_palabra&gt;</code>", parse_mode="HTML")
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
    user_id = str(update.effective_user.id)
    nombre = update.effective_user.first_name
    username = update.effective_user.username

    if config["keyword"] in texto:
        _sumar_punto(user_id, nombre, username)
        _iniciar_sesion(user_id, nombre, username)
        print(f"Registro: {nombre} dijo {config['keyword']}")

    elif config["keyword_salida"] in texto:
        segundos = _cerrar_sesion(user_id, nombre, username)
        if segundos is not None:
            await update.message.reply_text(
                f"<b>Se ha registrado con éxito los {_formatear_duracion(segundos)} que estuviste activo</b>",
                parse_mode="HTML"
            )
            print(f"Salida: {nombre} estuvo activo {_formatear_duracion(segundos)}")

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
    application.add_handler(CommandHandler("bitacora", historial))
    application.add_handler(CommandHandler("reporte", general))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("setkeyword", set_keyword))
    application.add_handler(CommandHandler("trabaja", trabaja))

    # Monitor de texto
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, monitor))

    application.run_polling(drop_pending_updates=True)
