"""
Телеграм-бот для канала о моде.

Собирает материалы из RSS модных изданий, пишет пост в одном из трёх
форматов (новость / разбор тренда / мнение), присылает владельцу
на подтверждение и публикует в канал после нажатия кнопки.
"""

import os
import json
import asyncio
import logging
import secrets
import re
from datetime import time as dtime

import pytz
import feedparser
from anthropic import Anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------- настройки

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]          # @mychannel  или  -1001234567890
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

FEEDS = [f.strip() for f in os.environ["FEEDS"].split(",") if f.strip()]
CHANNEL_TONE = os.environ.get(
    "CHANNEL_TONE",
    "живой, экспертный, без пафоса и без канцелярита; ты разбираешься в моде "
    "и говоришь как человек, а не как пресс-релиз",
)
TIMEZONE = pytz.timezone(os.environ.get("TZ", "Europe/Moscow"))
POST_TIMES = [t.strip() for t in os.environ.get("POST_TIMES", "09:00,13:00,17:00,20:00").split(",")]

MODEL = os.environ.get("MODEL", "claude-sonnet-5")
SEEN_FILE = os.environ.get("SEEN_FILE", "seen.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fashionbot")

client = Anthropic(api_key=ANTHROPIC_API_KEY)
drafts: dict[str, dict] = {}
rotation = {"i": 0}

# ---------------------------------------------------------------- форматы

SLOT_ORDER = ["news", "trend", "style", "opinion"]

SLOT_STYLES = {
    "news": """Формат: НОВОСТЬ ИНДУСТРИИ.
Что произошло — в первых двух строках. Дальше: почему это важно и что это
значит для моды в целом. Смена креативного директора, покупка бренда,
скандальная кампания, цифры продаж — подай так, чтобы было понятно даже
тому, кто не следит за индустрией каждый день.""",

    "trend": """Формат: РАЗБОР ТРЕНДА ИЛИ ВЕЩИ.
Что за тренд, откуда он пришёл, кто его двигает — конкретные бренды и
показы. Обязательно: как это носится в реальной жизни и с чем сочетать.
Читатель должен закрыть пост с ощущением, что теперь понимает вещь,
а не просто увидел красивую картинку.""",

    "style": """Формат: ОБРАЗ И ВИЗУАЛ.
Конкретный лук: показ, красная дорожка, стритстайл, кампания. Разбери,
что на человеке и почему это работает — силуэт, цвет, детали, отсылки.
Обязательно дай practical-часть: как повторить логику образа в обычном
гардеробе, без бюджета в тысячу евро. Такие посты сохраняют и пересылают.""",

    "opinion": """Формат: МНЕНИЕ И ВОВЛЕЧЕНИЕ.
Займи позицию — согласись или поспорь с тем, что происходит. Можно быть
острой, но аргументированной. В конце — прямой вопрос читателям, на который
хочется ответить в комментариях. Никаких «а что думаете вы?» — вопрос
должен быть конкретным.""",
}

# ---------------------------------------------------------------- источники

def load_seen() -> set:
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen)[-500:], f, ensure_ascii=False)
    except Exception as e:
        log.warning("не смог сохранить seen.json: %s", e)


def extract_image(entry) -> str | None:
    """Достаёт картинку из RSS-записи: медиа-блоки, вложения или тег img."""
    for key in ("media_content", "media_thumbnail"):
        items = entry.get(key) or []
        if items and items[0].get("url"):
            return items[0]["url"]
    for enc in entry.get("enclosures") or []:
        if "image" in (enc.get("type") or ""):
            return enc.get("href")
    html = (entry.get("summary", "") or "") + str(entry.get("content", ""))
    m = re.search(r'<img[^>]+src="([^"]+)"', html)
    return m.group(1) if m else None


def clean(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


def pick_entry():
    """Берёт самый свежий материал, который ещё не использовали."""
    seen = load_seen()
    candidates = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log.warning("не читается фид %s: %s", url, e)
            continue
        for e in feed.entries[:15]:
            uid = e.get("id") or e.get("link")
            if not uid or uid in seen:
                continue
            candidates.append({
                "uid": uid,
                "title": clean(e.get("title", "")),
                "summary": clean(e.get("summary", ""))[:2000],
                "link": e.get("link", ""),
                "image": extract_image(e),
                "source": feed.feed.get("title", ""),
                "published": e.get("published_parsed") or e.get("updated_parsed"),
            })
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["published"] or (), reverse=True)
    best = candidates[0]
    seen.add(best["uid"])
    save_seen(seen)
    return best

# ---------------------------------------------------------------- генерация

def build_prompt(entry: dict, slot: str, extra: str = "") -> str:
    return f"""Ты ведёшь телеграм-канал о моде: высокая мода, показы, тренды,
новости индустрии. Аудитория — люди, которые любят моду и хотят понимать её
глубже, чем по картинкам в ленте.

Тон: {CHANNEL_TONE}

{SLOT_STYLES[slot]}

Исходный материал ({entry['source']}):
Заголовок: {entry['title']}
Текст: {entry['summary']}
Ссылка: {entry['link']}

Напиши один пост.

Жёсткие требования:
- 500-850 знаков, не больше (пост идёт с картинкой)
- первая строка — крючок, который цепляет прямо в уведомлении
- пересказывай своими словами, не копируй фразы из исходника
- НИЧЕГО не выдумывай: имена дизайнеров, бренды, даты, цифры — только те,
  что есть в исходном тексте. Если детали нет — не додумывай её
- имена и бренды пиши корректно, бренды латиницей (Saint Laurent, Miu Miu)
- абзацы по 1-2 строки, между ними пустая строка
- 2-3 эмодзи максимум, не в каждой строке
- НЕ используй символы markdown (** ## __ и подобные), только чистый текст
- в конце отдельной строкой: Источник: {entry['link']}

{extra}

Верни только текст поста, без пояснений и кавычек."""


def generate(entry: dict, slot: str, extra: str = "") -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": build_prompt(entry, slot, extra)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать", callback_data=f"pub:{draft_id}")],
        [InlineKeyboardButton("🔄 Другой вариант", callback_data=f"redo:{draft_id}"),
         InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{draft_id}")],
    ])

# ---------------------------------------------------------------- отправка

async def show_draft(bot, draft_id: str, chat_id: int):
    d = drafts[draft_id]
    text = f"Черновик ({d['slot']}):\n\n{d['text']}"
    if d["entry"].get("image"):
        try:
            await bot.send_photo(chat_id, d["entry"]["image"], caption=text[:1024],
                                 reply_markup=keyboard(draft_id))
            return
        except BadRequest as e:
            log.warning("картинка не отправилась: %s", e)
            d["entry"]["image"] = None
    await bot.send_message(chat_id, text, reply_markup=keyboard(draft_id),
                           disable_web_page_preview=True)


async def close_draft(q, text: str):
    """Помечает черновик обработанным — работает и для фото, и для текста."""
    try:
        if q.message.photo:
            await q.edit_message_caption(caption=text[:1024])
        else:
            await q.edit_message_text(text, disable_web_page_preview=True)
    except BadRequest:
        pass


async def send_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    slot = (context.job.data if context.job and context.job.data
            else SLOT_ORDER[rotation["i"] % len(SLOT_ORDER)])
    rotation["i"] += 1

    entry = await asyncio.to_thread(pick_entry)
    if entry is None:
        await context.bot.send_message(OWNER_ID, "Новых материалов в источниках нет 🤷")
        return
    try:
        text = await asyncio.to_thread(generate, entry, slot)
    except Exception as e:
        await context.bot.send_message(OWNER_ID, f"Ошибка генерации: {e}")
        return

    draft_id = secrets.token_hex(4)
    drafts[draft_id] = {"text": text, "entry": entry, "slot": slot}
    await show_draft(context.bot, draft_id, OWNER_ID)

# ---------------------------------------------------------------- кнопки

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action, draft_id = q.data.split(":", 1)
    d = drafts.get(draft_id)
    if not d:
        await close_draft(q, "Черновик устарел — жми /now для нового.")
        return

    if action == "pub":
        img = d["entry"].get("image")
        if img:
            try:
                await context.bot.send_photo(CHANNEL_ID, img, caption=d["text"][:1024])
            except BadRequest:
                await context.bot.send_message(CHANNEL_ID, d["text"])
        else:
            await context.bot.send_message(CHANNEL_ID, d["text"])
        await close_draft(q, f"✅ Опубликовано\n\n{d['text']}")
        drafts.pop(draft_id, None)

    elif action == "skip":
        await close_draft(q, "❌ Пропущено")
        drafts.pop(draft_id, None)

    elif action == "redo":
        await close_draft(q, "🔄 Переписываю…")
        text = await asyncio.to_thread(
            generate, d["entry"], d["slot"],
            "Предыдущий вариант не подошёл. Зайди с совершенно другого угла.",
        )
        d["text"] = text
        await show_draft(context.bot, draft_id, OWNER_ID)


async def on_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на черновик = правка. Пишешь что изменить — бот переписывает."""
    if update.effective_user.id != OWNER_ID or not update.message.reply_to_message:
        return
    src = update.message.reply_to_message
    original = src.text or src.caption or ""
    draft_id = next((k for k, v in drafts.items() if v["text"][:40] in original), None)
    if not draft_id:
        return
    await update.message.reply_text("Правлю…")
    d = drafts[draft_id]
    d["text"] = await asyncio.to_thread(
        generate, d["entry"], d["slot"], f"Правки от автора канала: {update.message.text}"
    )
    await show_draft(context.bot, draft_id, OWNER_ID)

# ---------------------------------------------------------------- команды

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "Бот на связи 👗\n\n"
        f"Черновики приходят в {', '.join(POST_TIMES)}:\n"
        "новость индустрии → разбор тренда → образ дня → мнение с вопросом.\n\n"
        "/now — сделать черновик прямо сейчас\n"
        "Ответь на черновик текстом — перепишу с твоими правками."
    )


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text("Готовлю черновик…")
    await send_draft(context)

# ---------------------------------------------------------------- запуск

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reply))

    for i, t in enumerate(POST_TIMES):
        h, m = (int(x) for x in t.split(":"))
        slot = SLOT_ORDER[i % len(SLOT_ORDER)]
        app.job_queue.run_daily(send_draft, time=dtime(hour=h, minute=m, tzinfo=TIMEZONE),
                                data=slot, name=f"slot-{t}")
        log.info("расписание: %s → %s", t, slot)

    log.info("бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
