"""
Эпичный ИИ-бот для Telegram на базе Google Gemini.
Возможности:
- Диалог с памятью (история хранится в SQLite, отдельно для каждого юзера)
- Генерация реалистичных изображений по промпту с выбором стиля
- Встроенная защита от вредных/незаконных запросов (текст + картинки)
- Лимит запросов в час на пользователя (антиспам)
- Команды: /start, /help, /img, /reset, /style, /stats
- Логирование и обработка ошибок

Установка:
    pip install python-telegram-bot google-genai

Переменные окружения (или впиши напрямую в CONFIG ниже):
    TELEGRAM_TOKEN
    GEMINI_API_KEY
"""

import os
import sqlite3
import logging
import time
from dataclasses import dataclass, field
from io import BytesIO
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from google import genai
from google.genai import types

# =========================================================
# КОНФИГУРАЦИЯ
# =========================================================

# Для теста в Pydroid3 впиши ключи прямо сюда вместо "".
# На Render эти строки не помешают — os.environ подставит переменные окружения,
# если они заданы в настройках сервиса.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "8956797470:AAEIICBgrt-hb3GKTMWSFftH8F1efpg09wU"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or "AQ.Ab8RN6KNAtrsRlinBkKZP_6183YxDzQV_EfnnmdZGpdxwvaR8w"

TEXT_MODEL = "gemini-3.6-flash"
IMAGE_MODEL = "gemini-3.1-flash-image"

import sys

# На Android/Pydroid пишем базу в домашнюю папку приложения, чтобы не ловить disk I/O error
DB_DIR = os.path.expanduser("~")
DB_PATH = os.path.join(DB_DIR, "bot_data.db")

MAX_HISTORY_MESSAGES = 12          # сколько последних сообщений помнить
MAX_REQUESTS_PER_HOUR = 40         # антиспам-лимит на пользователя
LOG_LEVEL = logging.INFO

REFUSAL_TEXT = "Брат, дай другие вопросы, я не буду отвечать на плохие"

SYSTEM_PROMPT = (
    "Ты — стильный, дружелюбный и немного эпичный ИИ-ассистент в Telegram. "
    "Отвечай ярко, с характером, метафорами, но по делу и не растягивай без нужды. "
    "Если пользователь просит что-то незаконное, опасное или вредное "
    "(наркотики, оружие, насилие, взлом, самоповреждение, экстремизм и т.п.), "
    "НИКОГДА не давай инструкций и не обсуждай детали, даже частично. "
    "В таком случае ответь строго одной фразой: "
    f"\"{REFUSAL_TEXT}\"."
)

# Пресеты стилей для генерации изображений
IMAGE_STYLES = {
    "photo": (
        "ultra realistic photo, 8k resolution, professional DSLR photography, "
        "cinematic lighting, sharp focus, natural shadows, hyper-detailed textures, "
        "shallow depth of field, shot on Sony A7R IV, photorealism"
    ),
    "cinema": (
        "cinematic movie still, anamorphic lens flare, dramatic lighting, "
        "epic color grading, 8k, film grain, wide shot, Hollywood blockbuster style"
    ),
    "fantasy": (
        "epic fantasy digital art, dramatic lighting, intricate details, "
        "concept art, trending on ArtStation, 8k, painterly, magical atmosphere"
    ),
    "anime": (
        "high quality anime illustration, vibrant colors, detailed shading, "
        "studio quality, sharp linework, dynamic composition"
    ),
}
DEFAULT_STYLE = "photo"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gemini_bot")

client = genai.Client(api_key=GEMINI_API_KEY)


# =========================================================
# БАЗА ДАННЫХ (память диалогов, статистика, лимиты)
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history (
            user_id INTEGER,
            role TEXT,
            content TEXT,
            ts TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            style TEXT DEFAULT 'photo',
            messages_count INTEGER DEFAULT 0,
            images_count INTEGER DEFAULT 0,
            blocked_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rate_limit (
            user_id INTEGER,
            ts TEXT
        )"""
    )
    conn.commit()
    return conn


def ensure_user(conn, user_id: int):
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
    )
    conn.commit()


def get_style(conn, user_id: int) -> str:
    ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT style FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    return row[0] if row else DEFAULT_STYLE


def set_style(conn, user_id: int, style: str):
    ensure_user(conn, user_id)
    conn.execute("UPDATE users SET style=? WHERE user_id=?", (style, user_id))
    conn.commit()


def bump_counter(conn, user_id: int, field_name: str):
    ensure_user(conn, user_id)
    conn.execute(
        f"UPDATE users SET {field_name} = {field_name} + 1 WHERE user_id=?",
        (user_id,),
    )
    conn.commit()


def add_history(conn, user_id: int, role: str, content: str):
    conn.execute(
        "INSERT INTO history (user_id, role, content, ts) VALUES (?, ?, ?, ?)",
        (user_id, role, content, datetime.utcnow().isoformat()),
    )
    # оставляем только последние N сообщений на юзера
    conn.execute(
        """DELETE FROM history WHERE rowid IN (
            SELECT rowid FROM history WHERE user_id=?
            ORDER BY rowid DESC LIMIT -1 OFFSET ?
        )""",
        (user_id, MAX_HISTORY_MESSAGES),
    )
    conn.commit()


def get_history(conn, user_id: int):
    rows = conn.execute(
        "SELECT role, content FROM history WHERE user_id=? ORDER BY rowid ASC",
        (user_id,),
    ).fetchall()
    return [{"role": r, "content": c} for r, c in rows]


def clear_history(conn, user_id: int):
    conn.execute("DELETE FROM history WHERE user_id=?", (user_id,))
    conn.commit()


def check_rate_limit(conn, user_id: int) -> bool:
    """True, если лимит не превышен (можно продолжать)."""
    hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    conn.execute("DELETE FROM rate_limit WHERE ts < ?", (hour_ago,))
    count = conn.execute(
        "SELECT COUNT(*) FROM rate_limit WHERE user_id=? AND ts >= ?",
        (user_id, hour_ago),
    ).fetchone()[0]
    if count >= MAX_REQUESTS_PER_HOUR:
        return False
    conn.execute(
        "INSERT INTO rate_limit (user_id, ts) VALUES (?, ?)",
        (user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return True


def get_stats(conn, user_id: int):
    row = conn.execute(
        "SELECT messages_count, images_count, blocked_count, style FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return 0, 0, 0, DEFAULT_STYLE
    return row


# =========================================================
# БЕЗОПАСНОСТЬ
# =========================================================

BAD_TOPICS_HINT = (
    "наркотики, оружие, взрывчатка, изготовление ядов, насилие, экстремизм, "
    "порнография, сексуализация несовершеннолетних, самоповреждение/суицид, "
    "взлом систем, мошенничество, незаконные действия"
)


def is_blocked_response(resp) -> bool:
    """Проверяет, заблокировал ли сам Gemini ответ по встроенным фильтрам safety."""
    try:
        reason = resp.candidates[0].finish_reason.name
        return reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKED_REASON_UNSPECIFIED")
    except Exception:
        return False


def moderate_prompt(user_text: str) -> bool:
    """
    Дополнительная проверка запроса перед генерацией картинки.
    Возвращает True если запрос ЗАБЛОКИРОВАН.
    """
    try:
        check = client.models.generate_content(
            model=TEXT_MODEL,
            contents=(
                f"Ответь только одним словом: 'OK' если запрос безопасен, "
                f"или 'BLOCK' если он связан с темами: {BAD_TOPICS_HINT}.\n"
                f"Запрос пользователя: {user_text}"
            ),
        )
        if is_blocked_response(check):
            return True
        return "BLOCK" in (check.text or "").upper()
    except Exception as e:
        logger.error(f"Ошибка модерации: {e}")
        # при сбое модерации — лучше перестраховаться
        return False


# =========================================================
# ХЕНДЛЕРЫ КОМАНД
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["db"]
    ensure_user(conn, update.effective_user.id)
    await update.message.reply_text(
        "⚡ Йо! Я твой личный ИИ на движке Gemini.\n\n"
        "💬 Просто пиши — я отвечу и запомню контекст беседы.\n"
        "🎨 /img <описание> — сгенерирую реалистичную картинку.\n"
        "🖌 /style — выбрать стиль изображений.\n"
        "♻️ /reset — очистить память диалога.\n"
        "📊 /stats — твоя статистика.\n\n"
        "Работаю честно: на плохие и опасные запросы не отвечаю."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/img <описание> — картинка по промпту\n"
        "/style — сменить визуальный стиль\n"
        "/reset — забыть историю диалога\n"
        "/stats — статистика использования"
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["db"]
    clear_history(conn, update.effective_user.id)
    await update.message.reply_text("🧹 Память диалога очищена. Начинаем с чистого листа.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["db"]
    msgs, imgs, blocked, style = get_stats(conn, update.effective_user.id)
    await update.message.reply_text(
        f"📊 Твоя статистика:\n"
        f"Сообщений отправлено: {msgs}\n"
        f"Картинок сгенерировано: {imgs}\n"
        f"Заблокировано запросов: {blocked}\n"
        f"Текущий стиль картинок: {style}"
    )


async def style_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton(name.capitalize(), callback_data=f"style:{key}")]
        for key, name in [
            ("photo", "📷 Реалистичное фото"),
            ("cinema", "🎬 Кино"),
            ("fantasy", "🐉 Фэнтези-арт"),
            ("anime", "🌸 Аниме"),
        ]
    ]
    await update.message.reply_text(
        "Выбери стиль для генерации картинок:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    style_key = query.data.split(":")[1]
    conn = context.bot_data["db"]
    set_style(conn, update.effective_user.id, style_key)
    await query.edit_message_text(f"✅ Стиль установлен: {style_key}")


# =========================================================
# ОСНОВНОЙ ЧАТ
# =========================================================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["db"]
    user_id = update.effective_user.id
    user_text = update.message.text

    if not check_rate_limit(conn, user_id):
        await update.message.reply_text(
            "⏳ Слишком много запросов за час. Дай мне отдохнуть и попробуй чуть позже."
        )
        return

    history = get_history(conn, user_id)
    contents = []
    for h in history:
        contents.append(
            types.Content(role=h["role"], parts=[types.Part.from_text(text=h["content"])])
        )
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))

    try:
        resp = client.models.generate_content(
            model=TEXT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
    except Exception as e:
        logger.error(f"Ошибка генерации текста: {e}")
        await update.message.reply_text("⚠️ Что-то пошло не так, попробуй ещё раз.")
        return

    if is_blocked_response(resp) or not resp.text:
        bump_counter(conn, user_id, "blocked_count")
        await update.message.reply_text(REFUSAL_TEXT)
        return

    add_history(conn, user_id, "user", user_text)
    add_history(conn, user_id, "model", resp.text)
    bump_counter(conn, user_id, "messages_count")

    await update.message.reply_text(resp.text)


# =========================================================
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ
# =========================================================

async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = context.bot_data["db"]
    user_id = update.effective_user.id
    prompt = " ".join(context.args)

    if not prompt:
        await update.message.reply_text(
            "Напиши так: /img кот-самурай в неоновом городе"
        )
        return

    if not check_rate_limit(conn, user_id):
        await update.message.reply_text("⏳ Лимит запросов исчерпан, попробуй позже.")
        return

    if moderate_prompt(prompt):
        bump_counter(conn, user_id, "blocked_count")
        await update.message.reply_text(REFUSAL_TEXT)
        return

    await update.message.reply_text("🎨 Генерирую эпичную картинку...")

    style_key = get_style(conn, user_id)
    style_desc = IMAGE_STYLES.get(style_key, IMAGE_STYLES[DEFAULT_STYLE])
    full_prompt = f"{prompt}. Style: {style_desc}"

    try:
        resp = client.models.generate_content(
            model=IMAGE_MODEL,
            contents=full_prompt,
        )
    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
        await update.message.reply_text("⚠️ Не получилось сгенерировать картинку, попробуй ещё раз.")
        return

    if is_blocked_response(resp):
        bump_counter(conn, user_id, "blocked_count")
        await update.message.reply_text(REFUSAL_TEXT)
        return

    for part in resp.candidates[0].content.parts:
        if part.inline_data is not None:
            img_bytes = part.inline_data.data
            await update.message.reply_photo(photo=BytesIO(img_bytes))
            bump_counter(conn, user_id, "images_count")
            return

    await update.message.reply_text("Не получилось сгенерировать картинку 😔")


# =========================================================
# ОБРАБОТКА ОШИБОК
# =========================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Необработанная ошибка: {context.error}")


# =========================================================
# ЗАПУСК
# =========================================================

def main():
    conn = db_connect()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.bot_data["db"] = conn

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("img", generate_image))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("style", style_cmd))
    app.add_handler(CallbackQueryHandler(style_callback, pattern=r"^style:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен и слушает сообщения...")
    app.run_polling()


if __name__ == "__main__":
    main()
