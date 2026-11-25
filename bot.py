"""Telegram-бот: поиск музыки по названию, строчке из текста и по аудио/голосовому.

Источники:
  * AudD  — распознавание звука и поиск по тексту песни (https://audd.io)
  * Jamendo — легальная музыка под лицензиями Creative Commons, из неё можно скачивать
"""
import asyncio
import html
import io
import logging
import os

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
AUDD_TOKEN = os.environ["AUDD_TOKEN"]
JAMENDO_CLIENT_ID = os.environ["JAMENDO_CLIENT_ID"]

MAX_UPLOAD = 50 * 1024 * 1024  # лимит Telegram на отправку файла ботом
MAX_DOWNLOAD = 20 * 1024 * 1024  # лимит Telegram на скачивание файла ботом

dp = Dispatcher()
TRACKS: dict[str, dict] = {}  # кэш найденных треков для кнопок (в памяти)
TIMEOUT = aiohttp.ClientTimeout(total=60)


# ───────────────────────── Источники ─────────────────────────
async def jamendo_search(query: str, limit: int = 5) -> list[dict]:
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": limit,
        "search": query,
        "audiodownload_allowed": "true",
    }
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        async with s.get("https://api.jamendo.com/v3.0/tracks/", params=params) as r:
            data = await r.json(content_type=None)
    out = []
    for t in data.get("results", []):
        url = t.get("audiodownload") or t.get("audio")
        if url:
            out.append(
                {
                    "id": f"j{t['id']}",
                    "title": t["name"],
                    "artist": t["artist_name"],
                    "url": url,
                    "duration": int(t.get("duration") or 0),
                }
            )
    return out


async def audd_recognize(data: bytes) -> dict | None:
    form = aiohttp.FormData()
    form.add_field("api_token", AUDD_TOKEN)
    form.add_field("return", "apple_music,spotify")
    form.add_field("file", data, filename="audio.ogg")
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        async with s.post("https://api.audd.io/", data=form) as r:
            js = await r.json(content_type=None)
    return js.get("result")


async def audd_lyrics(query: str, limit: int = 5) -> list[dict]:
    params = {"q": query, "api_token": AUDD_TOKEN}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        async with s.get("https://api.audd.io/findLyrics/", params=params) as r:
            js = await r.json(content_type=None)
    return (js.get("result") or [])[:limit]


# ───────────────────────── Хелперы ─────────────────────────
def fmt_dur(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


def tracks_kb(tracks: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for t in tracks:
        TRACKS[t["id"]] = t
        label = f"⬇️ {t['artist']} — {t['title']} {fmt_dur(t['duration'])}".strip()
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"dl:{t['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def links_text(r: dict) -> str:
    parts = []
    if r.get("song_link"):
        parts.append(f'<a href="{html.escape(r["song_link"])}">Все сервисы</a>')
    sp = (r.get("spotify") or {}).get("external_urls", {}).get("spotify")
    if sp:
        parts.append(f'<a href="{html.escape(sp)}">Spotify</a>')
    am = (r.get("apple_music") or {}).get("url")
    if am:
        parts.append(f'<a href="{html.escape(am)}">Apple Music</a>')
    return " · ".join(parts)


async def send_jamendo_for(message: Message, query: str):
    tracks = await jamendo_search(query)
    if tracks:
        await message.answer(
            "Можно скачать (свободная лицензия Jamendo):", reply_markup=tracks_kb(tracks)
        )
    else:
        await message.answer("Свободной версии для скачивания не нашлось.")


# ───────────────────────── Хендлеры ─────────────────────────
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я ищу музыку:\n"
        "• напиши название или исполнителя\n"
        "• напиши строчку из песни — /lyrics строчка\n"
        "• пришли голосовое, аудио или видео — распознаю трек\n\n"
        "Скачать можно треки со свободной лицензией (Jamendo). "
        "Для остальных пришлю ссылки на легальные сервисы."
    )


@dp.message(Command("lyrics"))
async def lyrics_cmd(m: Message, command):
    q = (command.args or "").strip()
    if not q:
        return await m.answer("Напиши так: /lyrics строчка из песни")
    results = await audd_lyrics(q)
    if not results:
        return await m.answer("По тексту ничего не нашёл.")
    lines = [
        f"{i}. <b>{html.escape(r['artist'])}</b> — {html.escape(r['title'])}"
        for i, r in enumerate(results, 1)
    ]
    await m.answer("\n".join(lines), parse_mode="HTML")
    await send_jamendo_for(m, f"{results[0]['title']}")


@dp.message(F.text & ~F.text.startswith("/"))
async def text_search(m: Message):
    q = m.text.strip()
    tracks = await jamendo_search(q)
    if tracks:
        return await m.answer(
            "Нашёл (можно скачать):", reply_markup=tracks_kb(tracks)
        )
    # ничего по названию — пробуем как строчку из текста
    results = await audd_lyrics(q)
    if not results:
        return await m.answer("Ничего не нашёл. Попробуй другой запрос.")
    lines = [
        f"{i}. <b>{html.escape(r['artist'])}</b> — {html.escape(r['title'])}"
        for i, r in enumerate(results, 1)
    ]
    await m.answer("Похоже на:\n" + "\n".join(lines), parse_mode="HTML")
    await send_jamendo_for(m, results[0]["title"])


@dp.message(F.voice | F.audio | F.video | F.video_note)
async def recognize(m: Message, bot: Bot):
    media = m.voice or m.audio or m.video or m.video_note
    if media.file_size and media.file_size > MAX_DOWNLOAD:
        return await m.answer("Файл больше 20 МБ — пришли фрагмент покороче.")
    status = await m.answer("🎧 Слушаю…")
    buf = io.BytesIO()
    await bot.download(media.file_id, destination=buf)
    result = await audd_recognize(buf.getvalue())
    if not result:
        return await status.edit_text("Не удалось распознать. Попробуй фрагмент 10–20 секунд почище.")
    text = f"🎵 <b>{html.escape(result['artist'])}</b> — {html.escape(result['title'])}"
    if result.get("album"):
        text += f"\nАльбом: {html.escape(result['album'])}"
    links = links_text(result)
    if links:
        text += f"\n{links}"
    await status.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    await send_jamendo_for(m, result["title"])


@dp.callback_query(F.data.startswith("dl:"))
async def download(cb: CallbackQuery):
    track = TRACKS.get(cb.data[3:])
    if not track:
        return await cb.answer("Результат устарел, повтори поиск.", show_alert=True)
    await cb.answer("Скачиваю…")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.get(track["url"]) as r:
            if r.status != 200:
                return await cb.message.answer("Не удалось скачать файл.")
            data = await r.content.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return await cb.message.answer("Файл слишком большой для Telegram (>50 МБ).")
    await cb.message.answer_audio(
        BufferedInputFile(data, filename=f"{track['artist']} - {track['title']}.mp3"),
        title=track["title"],
        performer=track["artist"],
        duration=track["duration"] or None,
    )


async def start_health_server():
    """Мини-веб-сервер для хостингов (Koyeb и др.), которым нужен открытый порт."""
    from aiohttp import web

    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ["PORT"])).start()


async def main():
    logging.basicConfig(level=logging.INFO)
    if os.environ.get("PORT"):
        await start_health_server()
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
