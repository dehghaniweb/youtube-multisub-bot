import os
import re
import asyncio
import shutil
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import yt_dlp


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

BASE_DIR = Path("/tmp/youtube_multisub")
BASE_DIR.mkdir(parents=True, exist_ok=True)

LANGUAGES = {
    "en": "🇬🇧 English",
    "fa": "🇮🇷 فارسی",
    "nl": "🇳🇱 Nederlands",
    "de": "🇩🇪 Deutsch",
    "ar": "🇸🇦 العربية",
    "tr": "🇹🇷 Türkçe",
}


# =========================================================
# HELPERS
# =========================================================

def clean_url(text):
    text = text.strip()

    pattern = r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/[^\s]+"

    match = re.search(pattern, text)

    if not match:
        return None

    return match.group(0)


def new_job_dir(chat_id):
    job_dir = BASE_DIR / str(chat_id)

    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)

    job_dir.mkdir(parents=True, exist_ok=True)

    return job_dir


def find_video_file(folder):
    extensions = [".mp4", ".mkv", ".webm", ".mov"]

    files = []

    for file in folder.iterdir():
        if file.is_file() and file.suffix.lower() in extensions:
            files.append(file)

    if not files:
        return None

    return max(files, key=lambda x: x.stat().st_size)


def find_subtitle(folder, lang):
    candidates = []

    for file in folder.iterdir():
        if not file.is_file():
            continue

        name = file.name.lower()

        if not name.endswith((".vtt", ".srt", ".ass", ".ttml")):
            continue

        if f".{lang}." in name:
            candidates.append(file)

    if candidates:
        return candidates[0]

    return None


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data.clear()

    text = (
        "🎬 YouTube MultiSub Bot\n\n"
        "سلام 👋\n"
        "لینک ویدئوی YouTube را برای من بفرست.\n\n"
        "بعد از دریافت لینک، زبان زیرنویس را انتخاب می‌کنیم."
    )

    await update.message.reply_text(text)


# =========================================================
# RECEIVE URL
# =========================================================

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = clean_url(update.message.text)

    if not url:
        await update.message.reply_text(
            "❌ لینک YouTube معتبر پیدا نشد.\n\n"
            "لطفاً لینک ویدئو را ارسال کن."
        )
        return

    context.user_data["url"] = url

    keyboard = [
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
            InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang_fa"),
        ],
        [
            InlineKeyboardButton("🇳🇱 Nederlands", callback_data="lang_nl"),
            InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de"),
        ],
        [
            InlineKeyboardButton("🇸🇦 العربية", callback_data="lang_ar"),
            InlineKeyboardButton("🇹🇷 Türkçe", callback_data="lang_tr"),
        ],
    ]

    await update.message.reply_text(
        "🌍 زبان زیرنویس را انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# FIRST LANGUAGE
# =========================================================

async def language_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    lang = query.data.replace("lang_", "")

    context.user_data["lang1"] = lang

    keyboard = [
        [
            InlineKeyboardButton(
                "1️⃣ فقط همین زبان",
                callback_data="mode_one"
            )
        ],
        [
            InlineKeyboardButton(
                "2️⃣ دو زبان روی یک ویدئو",
                callback_data="mode_two"
            )
        ],
        [
            InlineKeyboardButton(
                "3️⃣ دو زبان در دو فایل جدا",
                callback_data="mode_separate"
            )
        ],
    ]

    await query.edit_message_text(
        f"زبان اول: {LANGUAGES[lang]}\n\n"
        "حالا انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# MODE
# =========================================================

async def mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    mode = query.data.replace("mode_", "")

    context.user_data["mode"] = mode

    if mode == "one":

        await start_download(update, context, one_language=True)

        return

    keyboard = []

    for code, name in LANGUAGES.items():

        if code == context.user_data["lang1"]:
            continue

        keyboard.append(
            [
                InlineKeyboardButton(
                    name,
                    callback_data=f"lang2_{code}"
                )
            ]
        )

    await query.edit_message_text(
        "🌍 زبان دوم را انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# SECOND LANGUAGE
# =========================================================

async def second_language(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    lang2 = query.data.replace("lang2_", "")

    context.user_data["lang2"] = lang2

    await query.edit_message_text(
        f"زبان اول: {LANGUAGES[context.user_data['lang1']]}\n"
        f"زبان دوم: {LANGUAGES[lang2]}\n\n"
        "⏳ آماده دریافت ویدئو..."
    )

    await start_download(update, context, one_language=False)


# =========================================================
# DOWNLOAD
# =========================================================

async def start_download(update, context, one_language):

    chat_id = update.effective_chat.id

    url = context.user_data["url"]

    lang1 = context.user_data["lang1"]

    lang2 = context.user_data.get("lang2")

    job_dir = new_job_dir(chat_id)

    status_message = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "⏳ در حال بررسی ویدئو...\n"
            "ممکن است چند دقیقه طول بکشد."
        ),
    )

    try:

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.TYPING
        )

        # -------------------------------------------------
        # VIDEO + SUBTITLES
        # -------------------------------------------------

        subtitle_languages = [lang1]

        if lang2:
            subtitle_languages.append(lang2)

        output_template = str(
            job_dir / "%(title).180B.%(ext)s"
        )

        ydl_opts = {
            "outtmpl": output_template,

            # Prefer MP4 when available.
            "format": (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "best[ext=mp4]/"
                "best"
            ),

            "merge_output_format": "mp4",

            # Subtitles
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": subtitle_languages,

            # Do not download playlists.
            "noplaylist": True,

            # Network / extractor settings
            "quiet": True,
            "no_warnings": False,

            # IPv4 can sometimes be more reliable on CI.
            "source_address": "0.0.0.0",

            # Retry transient network failures.
            "retries": 3,
            "fragment_retries": 3,

            # Do not download thumbnails.
            "writethumbnail": False,

            # Do not stop merely because one subtitle is unavailable.
            "ignoreerrors": False,
        }

        await status_message.edit_text(
            "⬇️ در حال دریافت ویدئو و زیرنویس..."
        )

        loop = asyncio.get_running_loop()

        def download():

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await loop.run_in_executor(None, download)

        video = find_video_file(job_dir)

        if not video:

            await status_message.edit_text(
                "❌ دریافت ویدئو موفق نبود."
            )

            return

        # -------------------------------------------------
        # CHECK SUBTITLES
        # -------------------------------------------------

        sub1 = find_subtitle(job_dir, lang1)

        sub2 = None

        if lang2:
            sub2 = find_subtitle(job_dir, lang2)

        if not sub1:

            await status_message.edit_text(
                f"⚠️ برای {LANGUAGES[lang1]} "
                "زیرنویس قابل دریافت پیدا نشد.\n\n"
                "YouTube برای این ویدئو زیرنویس موردنظر "
                "را در اختیار yt-dlp قرار نداده است."
            )

            return

        await status_message.edit_text(
            "✅ ویدئو دریافت شد.\n"
            "🔎 زیرنویس پیدا شد.\n\n"
            "📤 در حال آماده‌سازی ارسال..."
        )

        # -------------------------------------------------
        # COPY SUBTITLE
        # -------------------------------------------------

        subtitle1_name = f"subtitle_{lang1}{sub1.suffix}"

        subtitle1 = job_dir / subtitle1_name

        shutil.copy2(sub1, subtitle1)

        # -------------------------------------------------
        # TELEGRAM SIZE CHECK
        # -------------------------------------------------

        video_size_mb = video.stat().st_size / (1024 * 1024)

        if video_size_mb > 49:

            await status_message.edit_text(
                f"⚠️ حجم ویدئو حدود {video_size_mb:.1f} MB است.\n\n"
                "این نسخه فعلاً برای ارسال مستقیم "
                "ویدئوهای بزرگ مناسب نیست."
            )

            return

        # -------------------------------------------------
        # SEND VIDEO
        # -------------------------------------------------

        await status_message.edit_text(
            "📤 در حال ارسال ویدئو..."
        )

        with video.open("rb") as video_file:

            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=(
                    "🎬 YouTube MultiSub\n"
                    f"🌍 {LANGUAGES[lang1]}"
                ),
                supports_streaming=True,
            )

        # -------------------------------------------------
        # SEND FIRST SUBTITLE
        # -------------------------------------------------

        with subtitle1.open("rb") as subtitle_file:

            await context.bot.send_document(
                chat_id=chat_id,
                document=subtitle_file,
                caption=f"📝 Subtitle — {LANGUAGES[lang1]}",
            )

        # -------------------------------------------------
        # SECOND SUBTITLE
        # -------------------------------------------------

        if lang2 and sub2:

            subtitle2_name = f"subtitle_{lang2}{sub2.suffix}"

            subtitle2 = job_dir / subtitle2_name

            shutil.copy2(sub2, subtitle2)

            with subtitle2.open("rb") as subtitle_file:

                await context.bot.send_document(
                    chat_id=chat_id,
                    document=subtitle_file,
                    caption=f"📝 Subtitle — {LANGUAGES[lang2]}",
                )

        elif lang2 and not sub2:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ برای {LANGUAGES[lang2]} "
                    "زیرنویس مستقیمی پیدا نشد."
                ),
            )

        await status_message.edit_text(
            "✅ کار تمام شد."
        )

    except Exception as e:

        print("ERROR:", repr(e))

        error_text = str(e)

        if "Sign in to confirm" in error_text:

            await status_message.edit_text(
                "❌ YouTube درخواست GitHub را مسدود کرد.\n\n"
                "پیغام YouTube:\n"
                "Sign in to confirm you’re not a bot\n\n"
                "در مرحله بعد روش دریافت YouTube را تغییر می‌دهیم."
            )

        else:

            await status_message.edit_text(
                "❌ خطایی هنگام پردازش رخ داد.\n\n"
                "لطفاً دوباره امتحان کن."
            )

    finally:

        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CallbackQueryHandler(
            language_selected,
            pattern=r"^lang_(en|fa|nl|de|ar|tr)$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            mode_selected,
            pattern=r"^mode_(one|two|separate)$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            second_language,
            pattern=r"^lang2_(en|fa|nl|de|ar|tr)$"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_url
        )
    )

    print("YouTube MultiSub Bot started...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
